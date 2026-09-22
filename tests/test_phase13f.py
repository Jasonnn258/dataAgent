"""Phase 13F 测试：沙箱验证命令执行（fixture 复制 tmp）。"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.maintenance.models import ValidationResult    # noqa: E402
from src.schema import ToolRecorder                    # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"

# 验证命令（plan 显式给，走 13B 的 argv 消毒进 plan）
PASS_CMDS = [["python", "-c", "import sys; print('auth ok'); "
                              "sys.exit(0)"]]
FAIL_CMDS = [["python", "-c", "raise SystemExit(3)"]]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    tmp = tmp_path_factory.mktemp("p13f") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


def _chain_to_applied(repo, validation_commands, task_id="t13f"):
    """完整链到 APPLIED_SANDBOX（验证命令按需注入 plan）。"""
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
    bp = rt.run("build_execution_plan", {
        "rollback_plan": sr.data, "task_id": task_id,
        "validation_commands": validation_commands})
    pe = rt.run("prepare_execution", {"plan": bp.data["plan"]})
    br = rt.run("build_rollback_patch", {"execution": pe.data["execution"]})
    ap = rt.run("apply_patch", {"execution": br.data["execution"]})
    assert ap.ok, ap.error
    return broker, rt, bp.data["plan"], ap.data["execution"]


# ================================================================ runner
class TestSafeCommandRunner:
    def test_pass_and_fail_shapes(self, repo):
        from src.services.validation import SafeCommandRunner

        r = SafeCommandRunner()
        ok = r.run(["python", "-c", "print('hi')"], cwd=repo)
        assert isinstance(ok, ValidationResult)
        assert ok.status == "PASSED" and ok.exit_code == 0
        assert ok.duration >= 0
        assert "hi" in ok.stdout_summary

        bad = r.run(["python", "-c", "raise SystemExit(2)"], cwd=repo)
        assert bad.status == "FAILED" and bad.exit_code == 2

    def test_shell_strings_and_paths_blocked(self, repo):
        from src.services.validation import SafeCommandRunner

        r = SafeCommandRunner()
        for argv in ("pytest -q", ["/bin/sh", "-c", "x"], ["./run.sh"],
                     [], ["pytest", ""], "python"):
            res = r.run(argv, cwd=repo)   # type: ignore[arg-type]
            assert res.status == "BLOCKED", argv
            assert res.exit_code is None

    def test_unknown_executable_blocked(self, repo):
        from src.services.validation import SafeCommandRunner
        r = SafeCommandRunner()
        res = r.run(["curl", "http://evil"], cwd=repo)
        assert res.status == "BLOCKED"
        assert "whitelist" in res.stderr_summary

    def test_timeout_bounded(self, repo):
        from src.services.validation import SafeCommandRunner
        r = SafeCommandRunner(timeout=2)
        res = r.run(["python", "-c", "import time; time.sleep(30)"],
                    cwd=repo)
        assert res.status == "TIMEOUT" and res.exit_code is None

    def test_long_output_summarized(self, repo):
        from src.services.validation import SafeCommandRunner
        r = SafeCommandRunner()
        res = r.run(["python", "-c", "print('x' * 100000)"], cwd=repo)
        assert len(res.stdout_summary) < 5_000   # 头尾有界
        assert "truncated" in res.stdout_summary


# ================================================================ service
class TestValidationService:
    def test_pass_chain_to_validated(self, repo):
        broker, rt, plan, attempt = _chain_to_applied(repo, PASS_CMDS)
        vr = rt.run("validate_execution", {"execution": attempt})
        assert vr.ok, vr.error
        attempt = vr.data["execution"]
        assert attempt.status == "VALIDATED"
        results = attempt.validation_results
        assert len(results) == 1
        assert results[0]["status"] == "PASSED"
        assert results[0]["exit_code"] == 0
        assert results[0]["command"] == PASS_CMDS[0]

    def test_fail_chain_to_test_failed(self, repo):
        broker, rt, plan, attempt = _chain_to_applied(repo, FAIL_CMDS,
                                                      task_id="t13f-fail")
        vr = rt.run("validate_execution", {"execution": attempt})
        assert vr.status == "failed"
        assert vr.data["status"] == "TEST_FAILED"
        assert vr.data["validation_results"][0]["exit_code"] == 3

    def test_repo_config_used_when_plan_empty(self, repo):
        """命令来源 2：<repo>/.dataagent/validation_commands.json。"""
        cfg = repo / ".dataagent" / "validation_commands.json"
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text('[["python", "-c", "print(\'cfg ok\')"]]\n',
                       encoding="utf-8")
        try:
            broker, rt, plan, attempt = _chain_to_applied(repo, None)
            assert plan.validation_commands == []
            vr = rt.run("validate_execution", {"execution": attempt})
            assert vr.ok, vr.error
            assert vr.data["execution"].status == "VALIDATED"
            assert vr.data["validation_results"][0]["command"] == \
                ["python", "-c", "print('cfg ok')"]
        finally:
            cfg.unlink()

    def test_no_commands_validated_with_note(self, repo):
        broker, rt, plan, attempt = _chain_to_applied(repo, None,
                                                      task_id="t13f-none")
        vr = rt.run("validate_execution", {"execution": attempt})
        assert vr.ok
        attempt = vr.data["execution"]
        assert attempt.status == "VALIDATED"
        assert attempt.validation_results == []
        assert any("no validation commands" in n for n in attempt.notes)

    def test_blocked_executable_is_test_failed(self, repo):
        """白名单外命令：BLOCKED → TEST_FAILED（配置本身有问题）。"""
        broker, rt, plan, attempt = _chain_to_applied(
            repo, [["git", "push", "origin", "main"]], task_id="t13f-blk")
        vr = rt.run("validate_execution", {"execution": attempt})
        assert vr.status == "failed"
        assert vr.data["status"] == "TEST_FAILED"
        assert vr.data["validation_results"][0]["status"] == "BLOCKED"

    def test_refuses_unapplied_attempt(self, repo):
        from src.errors import DataAgentError
        from src.maintenance.models import ExecutionPlan

        broker, rt, plan, attempt = _chain_to_applied(repo, PASS_CMDS,
                                                      task_id="t13f-ref")
        fresh = broker._workspace_svc.registry.create(plan)   # PLANNED
        vr = rt.run("validate_execution", {"execution": fresh})
        assert vr.status == "failed"
        assert "APPLIED_SANDBOX" in vr.error
