"""Configuration for the code-agent experiment framework.

Everything is environment-driven so the same CLI runs in ablation mode
without code changes. LLM settings are optional: when unset, all tasks
fall back to deterministic heuristics and structural/Git analysis still
works (工程要求 #8).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_MODES = ("lexical", "structural", "structural_git", "semantica")
VALID_TASKS = ("locate", "impact", "rollback")

# Context budget guards (工程要求 #5: never dump a whole repo into a prompt)
MAX_SNIPPET_CHARS = 1200      # per retrieved item
MAX_CONTEXT_CHARS = 24_000    # total context handed to the LLM in one call
MAX_CANDIDATES = 20           # max candidates returned per query


@dataclass
class LLMConfig:
    base_url: str | None = field(default_factory=lambda: os.environ.get("LLM_BASE_URL"))
    api_key: str | None = field(default_factory=lambda: os.environ.get("LLM_API_KEY"))
    model: str | None = field(default_factory=lambda: os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    timeout_s: float = field(default_factory=lambda: float(os.environ.get("LLM_TIMEOUT_S", "60")))

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass
class Settings:
    repo: Path
    mode: str = "lexical"
    task: str = "locate"
    top_k: int = 10
    # cache dir for structural index / semantica graph; default inside repo parent
    index_dir: Path | None = None
    llm: LLMConfig = field(default_factory=LLMConfig)
    no_llm: bool = False  # explicit --no-llm overrides env config

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        if self.task not in VALID_TASKS:
            raise ValueError(f"task must be one of {VALID_TASKS}, got {self.task!r}")
        if not self.repo.is_dir():
            raise FileNotFoundError(f"--repo path is not a directory: {self.repo}")
        if self.mode in ("structural_git", "semantica") or self.task == "rollback":
            git_dir = self.repo / ".git"
            if not git_dir.exists():
                raise FileNotFoundError(
                    f"mode={self.mode} / task={self.task} needs a git repo, "
                    f"but {self.repo} has no .git — see experiments/fixtures/seed_fixture.py"
                )

    def llm_enabled(self) -> bool:
        return (not self.no_llm) and self.llm.available


# ================================================================ 维护策略参数
# Phase 11I：W_* 打分权重 / keep-problem tie-break / action→risk 映射
# 属于"策略"而不是"算法"，外置到 config/maintenance_policy.yaml。
# 默认值与 11I 之前的硬编码逐项一致 —— 没有配置文件时行为不变。

REPO_ROOT = Path(__file__).resolve().parent.parent
MAINTENANCE_POLICY_PATH = REPO_ROOT / "config" / "maintenance_policy.yaml"

DEFAULT_MAINTENANCE_POLICY: dict = {
    "version": "11I.1",
    "rollback": {
        "scoring": {"label": 3.0, "ui": 2.0, "symbol": 2.0, "file": 1.0,
                    "label_partial": 0.7},
        "tie_break": {"prefer_problem_side": True},
    },
    "policy": {
        "action_risk": {"PASS": "low", "HUMAN_REVIEW": "medium",
                        "BLOCK": "high"},
    },
    "execution_policy": {
        "pre_gate": {
            "public_api_change": "HUMAN_REVIEW",
            "auth_change": "HUMAN_REVIEW",
            "same_symbol_rollback_keep": "HUMAN_REVIEW",
        },
        "post_gate": {
            "unexpected_file_change": "BLOCK",
            "forbidden_keep_change": "BLOCK",
            "repository_state_changed": "BLOCK",
            "apply_check_failed": "BLOCK",
            "test_failed": "BLOCK",
            "public_api_change": "HUMAN_REVIEW",
            "auth_change": "HUMAN_REVIEW",
            "same_symbol_rollback_keep": "HUMAN_REVIEW",
        },
        "auto_push": False,
        # 13J：晋升总开关，默认关闭
        "promote_enabled": False,
    },
}

_maintenance_policy_cache: dict | None = None


def _parse_simple_yaml(text: str) -> dict:
    """极简 YAML 解析（PyYAML 缺席时的退路）。

    只支持本仓库策略文件用到的子集：缩进层级 + `key: value` 标量
    （带引号字符串 / bool / 数值）+ 注释。超出子集的写法不要进
    maintenance_policy.yaml。
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        key = key.strip()
        val = val.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":                       # key: → 新层级
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(val)
    return root


def _scalar(val: str):
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    for cast in (int, float):
        try:
            return cast(val)
        except ValueError:
            pass
    return val


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_maintenance_policy(path: Path | None = None) -> dict:
    """从磁盘读策略配置（每次都真读，不走缓存；测试/消融用）。"""
    p = path or MAINTENANCE_POLICY_PATH
    try:
        text = p.read_text()
    except OSError:
        return dict(DEFAULT_MAINTENANCE_POLICY)
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
    except ImportError:
        data = _parse_simple_yaml(text)
    if not isinstance(data, dict):
        return dict(DEFAULT_MAINTENANCE_POLICY)
    return _deep_merge(DEFAULT_MAINTENANCE_POLICY, data)


def maintenance_policy() -> dict:
    """当前生效的策略配置（进程内缓存；覆盖见 set_maintenance_policy）。"""
    global _maintenance_policy_cache
    if _maintenance_policy_cache is None:
        _maintenance_policy_cache = load_maintenance_policy()
    return _maintenance_policy_cache


def set_maintenance_policy(cfg: dict | None) -> None:
    """策略 A/B 消融钩子：cfg=None 恢复为文件加载。

    注意：scoring 权重在 skill import 时固化成模块常量，改权重做
    消融需 reload 对应模块；tie_break / action_risk 每次调用现读。
    """
    global _maintenance_policy_cache
    if cfg is None:
        _maintenance_policy_cache = None   # 下次访问回到文件
    else:
        _maintenance_policy_cache = _deep_merge(
            DEFAULT_MAINTENANCE_POLICY, cfg)


def policy_version() -> str:
    """当前策略版本（记入 Decision.policy_version，供消融对账）。"""
    return str(maintenance_policy().get("version", ""))
