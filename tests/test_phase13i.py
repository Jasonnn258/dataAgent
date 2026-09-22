"""Phase 13I 测试：MaintenanceExecutorAgent 沙箱执行链。

重点测三件事：
1. 全链走通：canonical 回退 → VERIFIED + 双门 HUMAN_REVIEW（不是 PASS，
   auth 面不放行）+ 不 promote。
2. pre gate BLOCK → 拒绝建沙箱（没有 worktree、没有 execution_id）。
3. 中途失败就地停车（TEST_FAILED / STALE_PLAN），不重试不修计划。
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"
PASS_CMDS = [["python", "-c", "print('ok')"]]
FAIL_CMDS = [["python", "-c", "import sys; sys.exit(1)"]]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    import shutil
    tmp = tmp_path_factory.mktemp("p13i") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


def _rollback_plan(repo, task_id):
    """跑分析链到 safe_rollback，返回 (broker, rt, rollback_plan data)。"""
    from src.schema import ToolRecorder
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    from src.skills import SkillRuntime

    broker = ContextBroker(repo, ToolRecorder())
    build_change_graph(broker)
    rt = SkillRuntime(broker)
    nav = rt.run("resolve_target", {"query": DEMO_QUERY, "task_id": task_id})
    navk = rt.run("resolve_target", {"query": DEMO_KEEP, "task_id": task_id})
    cu = rt.run("change_unit_analysis",
                {"terms": nav.data["terms"], "task_id": task_id})
    cuk = rt.run("change_unit_analysis",
                 {"terms": navk.data["terms"], "task_id": task_id})
    sr = rt.run("safe_rollback", {
        "problem_matches": cu.data["matches"],
        "keep_matches": cuk.data["matches"],
        "affected_routes": ["/api/auth/login"], "task_id": task_id})
    assert sr.ok, sr.error
    return broker, rt, sr.data


# ================================================================ 全链
class TestHappyPath:
    def test_canonical_runs_to_verified(self, repo):
        from src.agents.executor import MaintenanceExecutorAgent
        from src.agents.scopes import ScopedContext
        from src.maintenance.models import ExecutionStatus

        broker, rt, plan_data = _rollback_plan(repo, "t13i")
        agent = MaintenanceExecutorAgent(broker)
        scope = ScopedContext(role=agent.ROLE, task_id="t13i",
                              reads=agent.READS, skills=agent.SKILLS)
        out = agent.execute(plan_data, task_id="t13i",
                            validation_commands=PASS_CMDS,
                            affected_routes=["/api/auth/login"],
                            scope=scope)
        assert out.status == ExecutionStatus.VERIFIED, out.dump()
        assert out.stopped_at == "post_gate"
        assert out.verification == "VERIFIED"
        # auth 面：两道门都是 HUMAN_REVIEW，绝不静默 PASS
        assert out.pre_gate["action"] == "HUMAN_REVIEW"
        assert out.post_gate["action"] == "HUMAN_REVIEW"
        assert out.ok
        # attempt 真在注册表里（可审计）
        assert broker.get_execution(out.execution_id) is out.attempt
        # scope 记了计划的 evidence
        assert scope.evidence_ids
        assert "promote" in out.dump()

    def test_worktree_is_sandbox_not_source(self, repo):
        """执行后源仓库必须干净（改动只在沙箱 worktree）。"""
        import subprocess as sp
        from src.agents.executor import MaintenanceExecutorAgent

        broker, rt, plan_data = _rollback_plan(repo, "t13i-src")
        agent = MaintenanceExecutorAgent(broker)
        out = agent.execute(plan_data, task_id="t13i-src",
                            validation_commands=PASS_CMDS,
                            affected_routes=["/api/auth/login"])
        assert out.status == "VERIFIED"
        status = sp.run(["git", "status", "--porcelain"], cwd=repo,
                        capture_output=True, text=True).stdout.strip()
        # 只允许 .git/worktrees 元数据；工作区文件零改动
        assert status == "", status
        # 回退内容确实存在于沙箱
        text = (Path(out.attempt.workspace) / "src" / "lib" / "auth.ts"
                ).read_text()
        assert ">= 4" in text and ">= 8" not in text


# ================================================================ pre gate BLOCK
class TestPreGateBlock:
    def test_blocked_plan_never_enters_sandbox(self, repo, monkeypatch):
        """pre gate BLOCK → 不建 worktree、不产 execution_id。"""
        from src.agents.executor import MaintenanceExecutorAgent
        from src.config import set_maintenance_policy

        broker, rt, plan_data = _rollback_plan(repo, "t13i-block")
        # 把 canonical 必踩的 auth 面拧到 BLOCK（策略旋钮，不改代码）
        cfg = {"execution_policy": {"pre_gate": {
            "auth_change": "BLOCK", "public_api_change": "BLOCK"}}}
        set_maintenance_policy(cfg)
        try:
            agent = MaintenanceExecutorAgent(broker)
            out = agent.execute(plan_data, task_id="t13i-block",
                                validation_commands=PASS_CMDS,
                                affected_routes=["/api/auth/login"])
        finally:
            set_maintenance_policy(None)
        assert out.stopped_at == "pre_gate"
        assert out.pre_gate["action"] == "BLOCK"
        assert out.execution_id == ""          # 连尝试都没建
        assert broker.executions() == []
        assert not out.ok

    def test_pre_gate_decision_recorded(self, repo):
        from src.agents.executor import MaintenanceExecutorAgent
        from src.config import set_maintenance_policy

        broker, rt, plan_data = _rollback_plan(repo, "t13i-dec")
        set_maintenance_policy({"execution_policy": {"pre_gate": {
            "auth_change": "BLOCK", "public_api_change": "BLOCK"}}})
        try:
            agent = MaintenanceExecutorAgent(broker)
            agent.execute(plan_data, task_id="t13i-dec",
                          validation_commands=PASS_CMDS,
                          affected_routes=["/api/auth/login"])
        finally:
            set_maintenance_policy(None)
        cats = [d.category for d in broker.get_precedents()]
        assert "execution_pre_gate" in cats


# ================================================================ 中途停车
class TestMidChainStops:
    def test_validation_failure_stops_at_validate(self, repo):
        """验证命令失败 → TEST_FAILED 终态，链停在 validate_execution。"""
        from src.agents.executor import MaintenanceExecutorAgent

        broker, rt, plan_data = _rollback_plan(repo, "t13i-fail")
        agent = MaintenanceExecutorAgent(broker)
        out = agent.execute(plan_data, task_id="t13i-fail",
                            validation_commands=FAIL_CMDS,
                            affected_routes=["/api/auth/login"])
        assert out.stopped_at == "validate_execution"
        assert out.status == "TEST_FAILED"
        assert not out.ok
        # post gate 没跑（车停了）；且失败事实仍触发红线
        assert out.post_gate == {}

    def test_stale_plan_stops_at_prepare(self, repo):
        """计划建好之后源仓库动了 → 重放该计划 STALE_PLAN 停车。"""
        import subprocess as sp
        from src.agents.executor import MaintenanceExecutorAgent

        broker, rt, plan_data = _rollback_plan(repo, "t13i-stale")
        # 先把计划构建成合同（快照锚定此刻 HEAD），再动源仓库
        bp = rt.run("build_execution_plan", {
            "rollback_plan": plan_data, "task_id": "t13i-stale",
            "validation_commands": PASS_CMDS,
            "affected_routes": ["/api/auth/login"]})
        assert bp.ok, bp.error
        plan_obj = bp.data["plan"]
        sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "STALE_MARKER.md").write_text("moved on\n")
        sp.run(["git", "add", "STALE_MARKER.md"], cwd=repo, check=True)
        sp.run(["git", "commit", "-qm", "move on"], cwd=repo, check=True)
        try:
            agent = MaintenanceExecutorAgent(broker)
            out = agent.execute(plan_obj, task_id="t13i-stale",
                                validation_commands=PASS_CMDS,
                                affected_routes=["/api/auth/login"])
        finally:
            sp.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo, check=True)
            sp.run(["git", "clean", "-fdq"], cwd=repo, check=True)
        assert out.stopped_at == "prepare_execution"
        assert out.status == "STALE_PLAN"
        assert not out.ok

    def test_no_retry_no_plan_mutation(self, repo):
        """失败的尝试保持终态；agent 不改计划、不重跑。"""
        from src.agents.executor import MaintenanceExecutorAgent

        broker, rt, plan_data = _rollback_plan(repo, "t13i-noretry")
        agent = MaintenanceExecutorAgent(broker)
        out = agent.execute(plan_data, task_id="t13i-noretry",
                            validation_commands=FAIL_CMDS,
                            affected_routes=["/api/auth/login"])
        assert out.status == "TEST_FAILED"
        n_execs = len(broker.executions())
        # 再执行一次 = 新尝试（新 execution_id），旧尝试仍是 TEST_FAILED
        out2 = agent.execute(plan_data, task_id="t13i-noretry",
                             validation_commands=PASS_CMDS,
                             affected_routes=["/api/auth/login"])
        assert out2.execution_id != out.execution_id
        assert len(broker.executions()) == n_execs + 1
        assert broker.get_execution(out.execution_id).status == "TEST_FAILED"


# ================================================================ 权限边界
class TestBoundaries:
    def test_agent_module_has_no_direct_execution(self):
        """agent 源码不 import 任何进程/文件原语（AST 检查，不看注释）。"""
        import ast
        tree = ast.parse((ROOT / "src" / "agents" / "executor.py")
                         .read_text())
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        bad = [m for m in imports
               if m.split(".")[0] in ("subprocess", "os", "shutil",
                                      "pathlib")]
        assert not bad, f"执行原语不得进 agent 层: {bad}"
        # 只经 skill runtime + broker
        assert "SkillRuntime" in (ROOT / "src" / "agents" / "executor.py"
                                  ).read_text()

    def test_architecture_declares_seven_agents(self):
        from src.agents import MaintenanceExecutorAgent
        assert MaintenanceExecutorAgent.SKILLS == [
            "build_execution_plan", "prepare_execution",
            "build_rollback_patch", "apply_patch", "validate_execution",
            "verify_execution", "policy_check"]
