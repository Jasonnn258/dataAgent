"""架构静态约束（Phase 11G）：用 AST 保证分层不被悄悄破坏。

原则：Trace 是运行期审计，不能代替架构约束 —— 这里在**源码层**守住：

  1. Agent 层与 Skill 层永不直连物理工具（subprocess/git_history/
     search/structural/change_units/semantica），一切经 ContextBroker
  2. Skill 不绕过 broker 摸 Service；Service 不反向依赖 Agent/Skill
  3. 物理工具层（git_history/search/structural/change_units）保持
     底层纯净，不 import 上层
  4. 声明了 ROLE 的 Agent 必须声明 SKILLS，且清单里的 skill 真实存在
  5. SkillSpec.allowed_capabilities 声明的必须是 broker 真实提供的能力
  6. LLM 只能出现在 semantic_reasoning=allowed 的 skill 源码里
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"

# 物理工具层的绝对模块名（Agent/Skill 一律禁止）
FORBIDDEN_MODULES = {"subprocess"}
FORBIDDEN_PREFIXES = ("src.git_history", "src.search", "src.structural",
                      "src.change_units", "semantica")


def iter_py(package: str):
    return sorted((SRC / package).glob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """一个源文件 import 的全部绝对模块名（含 from X import Y 的 X）。"""
    tree = ast.parse(path.read_text())
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module)
    return mods


def class_attrs(path: Path) -> dict[str, dict[str, ast.expr]]:
    """class 名 → {类属性名: 值表达式}（只看直接赋值）。"""
    tree = ast.parse(path.read_text())
    out: dict[str, dict[str, ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            attrs = {t.targets[0].id: t.value for t in node.body
                     if isinstance(t, ast.Assign)
                     and isinstance(t.targets[0], ast.Name)}
            out[node.name] = attrs
    return out


# ================================================================ 1. 禁直连物理工具
def test_agents_never_import_physical_tools():
    offenders = []
    for path in iter_py("agents"):
        for m in imported_modules(path):
            if m in FORBIDDEN_MODULES or m.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{path.name}: {m}")
    assert not offenders, f"agent 层禁直连物理工具: {offenders}"


def test_skills_never_import_physical_tools():
    offenders = []
    for path in iter_py("skills"):
        for m in imported_modules(path):
            if m in FORBIDDEN_MODULES or m.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{path.name}: {m}")
    assert not offenders, f"skill 层禁直连物理工具: {offenders}"


def test_broker_facade_never_imports_physical_tools():
    mods = imported_modules(SRC / "semgraph" / "context_broker.py")
    direct = [m for m in mods
              if m in FORBIDDEN_MODULES or m.startswith(FORBIDDEN_PREFIXES)]
    assert not direct, f"broker 只委托 services，不直连工具: {direct}"


# ================================================================ 2. 依赖方向
def test_skills_do_not_bypass_broker_into_services():
    offenders = []
    for path in iter_py("skills"):
        if path.name in ("capability.py",):
            continue   # CAPABILITIES 契约表在 broker 模块，允许引用
        for m in imported_modules(path):
            if m.startswith(("src.services", "src.agents")):
                offenders.append(f"{path.name}: {m}")
    assert not offenders, "skill 必须经 broker 取能力，不得直连 service/agent"


def test_agents_do_not_import_services():
    offenders = [f"{p.name}: {m}" for p in iter_py("agents")
                 for m in imported_modules(p)
                 if m.startswith("src.services")]
    assert not offenders, "agent 经 skill/broker 取能力，不得直连 service"


def test_services_never_reach_upward():
    offenders = [f"{p.name}: {m}" for p in iter_py("services")
                 for m in imported_modules(p)
                 if m.startswith(("src.agents", "src.skills"))]
    assert not offenders, "service 是底层确定性组合，不得反向依赖上层"


def test_physical_tools_stay_pure():
    offenders = []
    for pkg in ("git_history", "search", "structural", "change_units"):
        for path in iter_py(pkg):
            for m in imported_modules(path):
                if m.startswith(("src.agents", "src.skills", "src.services")):
                    offenders.append(f"{pkg}/{path.name}: {m}")
    assert not offenders, "物理工具层不得 import 上层"


# ================================================================ 3. Agent 声明 SKILLS
def test_every_role_agent_declares_real_skills():
    from src.skills import default_registry
    known = set(default_registry())
    declared: dict[str, list[str]] = {}
    for path in iter_py("agents"):
        for cls, attrs in class_attrs(path).items():
            if "ROLE" not in attrs:
                continue
            assert "SKILLS" in attrs, f"{cls} 声明了 ROLE 却没有 SKILLS 清单"
            lit = attrs["SKILLS"]
            assert isinstance(lit, ast.List), f"{cls}.SKILLS 必须是列表字面量"
            names = [e.value for e in lit.elts
                     if isinstance(e, ast.Constant)]
            assert names, f"{cls}.SKILLS 不能为空"
            unknown = [n for n in names if n not in known]
            assert not unknown, f"{cls}.SKILLS 引用未注册 skill: {unknown}"
            declared[cls] = names
    # 五个执行 agent + 两个 verifier 都在委托（Orchestrator 无 ROLE，
    # 它是组合根，不自带 skill）
    assert len(declared) == 6
    assert declared["RepositoryNavigator"] == ["resolve_target"]
    assert declared["RollbackPlanner"] == ["safe_rollback"]


# ================================================================ 4. capability 声明真实存在
def test_skill_capabilities_resolve_to_broker_methods():
    from src.semgraph.context_broker import CAPABILITIES
    from src.skills import default_registry
    provided = set(CAPABILITIES.values())
    for name, skill in default_registry().items():
        for cap in skill.spec.allowed_capabilities:
            if cap.endswith(".*"):
                prefix = cap[:-1]
                assert any(c.startswith(prefix) for c in provided), \
                    f"{name} 的通配 {cap} 不匹配任何真实能力"
            else:
                assert cap in provided, \
                    f"{name} 声明了 broker 不提供的能力 {cap}"


# ================================================================ 5. LLM 白名单（11H 前置）
def test_llm_mentions_only_in_semantic_skills():
    from src.skills import default_registry
    for name, skill in default_registry().items():
        path = Path(sys.modules[skill.__module__].__file__)
        text = path.read_text()
        mentions = '"llm"' in text or "llm=" in text
        if skill.spec.semantic_reasoning == "allowed":
            assert mentions, f"{name} 声明 allowed 却不消费 llm"
        else:
            assert not mentions, \
                f"{name} 未声明 semantic_reasoning=allowed，源码不得触碰 llm"
