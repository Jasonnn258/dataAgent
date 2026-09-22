"""Phase 13H 测试：执行层策略门（pre/post gate + auto_push 恒关）。"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.config import maintenance_policy             # noqa: E402
from src.maintenance.models import ExecutionPlan      # noqa: E402
from src.schema import ToolRecorder                   # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"
PASS_CMDS = [["python", "-c", "print('ok')"]]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    import shutil
    tmp = tmp_path_factory.mktemp("p13h") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


def _chain(repo, task_id="t13h", validation_commands=PASS_CMDS,
           affected_routes=None):
    """完整链到 VERIFIED，返回 (broker, rt, plan, attempt)。"""
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
        "affected_routes": affected_routes or [], "task_id": task_id})
    bp = rt.run("build_execution_plan", {
        "rollback_plan": sr.data, "task_id": task_id,
        "validation_commands": validation_commands,
        "affected_routes": affected_routes or ["/api/auth/login"]})
    pe = rt.run("prepare_execution", {"plan": bp.data["plan"]})
    br = rt.run("build_rollback_patch", {"execution": pe.data["execution"]})
    ap = rt.run("apply_patch", {"execution": br.data["execution"]})
    vr = rt.run("validate_execution", {"execution": ap.data["execution"]})
    assert vr.ok, vr.error
    rv = rt.run("verify_execution", {"execution": vr.data["execution"]})
    assert rv.ok, rv.error
    return broker, rt, bp.data["plan"], rv.data["execution"]


def _pol(rt, attempt, task_id="t13h"):
    return rt.run("policy_check", {
        "gate": "execution_post", "execution": attempt,
        "rollback_symbols": [], "keep_symbols": [], "task_id": task_id})


# ================================================================ 配置
class TestPolicyConfig:
    def test_execution_policy_section_loaded(self):
        cfg = maintenance_policy()
        ep = cfg["execution_policy"]
        assert ep["post_gate"]["unexpected_file_change"] == "BLOCK"
        assert ep["post_gate"]["forbidden_keep_change"] == "BLOCK"
        assert ep["post_gate"]["repository_state_changed"] == "BLOCK"
        assert ep["post_gate"]["apply_check_failed"] == "BLOCK"
        assert ep["post_gate"]["test_failed"] == "BLOCK"
        assert ep["post_gate"]["auth_change"] == "HUMAN_REVIEW"
        assert ep["post_gate"]["public_api_change"] == "HUMAN_REVIEW"
        assert ep["pre_gate"]["same_symbol_rollback_keep"] == "HUMAN_REVIEW"
        # PASS 也不能自动 push
        assert ep["auto_push"] is False

    def test_policy_version_bumped(self):
        assert maintenance_policy()["version"] == "13H.1"


# ================================================================ pre gate
class TestPreGate:
    def test_canonical_auth_plan_human_review(self, repo):
        """canonical：auth 面 → pre gate HUMAN_REVIEW（不是 BLOCK）。"""
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        nav = rt.run("resolve_target", {"query": DEMO_QUERY, "task_id": "pg"})
        navk = rt.run("resolve_target", {"query": DEMO_KEEP, "task_id": "pg"})
        cu = rt.run("change_unit_analysis",
                    {"terms": nav.data["terms"], "task_id": "pg"})
        cuk = rt.run("change_unit_analysis",
                     {"terms": navk.data["terms"], "task_id": "pg"})
        sr = rt.run("safe_rollback", {
            "problem_matches": cu.data["matches"],
            "keep_matches": cuk.data["matches"],
            "affected_routes": ["/api/auth/login"], "task_id": "pg"})
        bp = rt.run("build_execution_plan", {
            "rollback_plan": sr.data, "task_id": "pg",
            "affected_routes": ["/api/auth/login"]})
        plan = bp.data["plan"]
        assert plan.affected_routes == ["/api/auth/login"]
        pol = rt.run("policy_check", {
            "gate": "execution_pre", "plan": plan,
            "rollback_symbols": [], "keep_symbols": [], "task_id": "pg"})
        assert pol.ok
        assert pol.data["action"] == "HUMAN_REVIEW"
        assert pol.data["rule_name"] in {"auth_change", "public_api_change"}
        # decision 已落档
        cats = {d.category for d in broker.get_precedents()}
        assert "execution_pre_gate" in cats

    def test_clean_plan_passes(self, repo):
        """无危险面的计划 → PASS。"""
        from src.maintenance.models import RepositorySnapshot
        snap = RepositorySnapshot(head="x" * 40)
        plan = ExecutionPlan(
            task_id="clean", rollback_units=[{"id": "cu:a",
                                             "label": "ui",
                                             "commit": "a" * 40,
                                             "files": ["src/ui/button.tsx"],
                                             "symbols": []}],
            target_files=["src/ui/button.tsx"],
            repository_snapshot=snap)
        from src.maintenance.execution_policy import pre_execution_gate
        result = pre_execution_gate(plan, maintenance_policy())
        assert result.action.value == "PASS"


# ================================================================ post gate
class TestPostGate:
    def test_canonical_verified_still_human_review(self, repo):
        """执行全绿，但 auth 面复检 → HUMAN_REVIEW（不许自动放行）。"""
        broker, rt, plan, attempt = _chain(repo)
        pol = _pol(rt, attempt)
        assert pol.ok
        assert pol.data["action"] == "HUMAN_REVIEW"
        assert pol.data["rule_name"] == "auth_change"
        cats = {d.category for d in broker.get_precedents()}
        assert "execution_post_gate" in cats

    def test_tampered_keep_blocked(self, repo):
        """keep 损伤 → forbidden_keep_change BLOCK。"""
        from src.services.verification import VerificationService

        broker, rt, plan, attempt = _chain(repo, task_id="t13h-tamper")
        # 伪造核验失败面（模拟 13G 判 keep 损伤）
        attempt.verification_results = [{
            "preservation_pass": False, "scope_pass": True,
            "expected_change_pass": True, "tests_pass": True,
            "evidence_pass": True, "overall": "FAILED",
            "checks": [{"check": "preserve.forbidden_file", "pass": False,
                        "detail": "keep-only file changed: layout.tsx"}]}]
        pol = _pol(rt, attempt, task_id="t13h-tamper")
        assert pol.data["action"] == "BLOCK"
        assert pol.data["rule_name"] == "forbidden_keep_change"

    def test_out_of_plan_blocked(self, repo):
        broker, rt, plan, attempt = _chain(repo, task_id="t13h-oop")
        attempt.verification_results = [{
            "preservation_pass": True, "scope_pass": False,
            "expected_change_pass": True, "tests_pass": True,
            "evidence_pass": True, "overall": "FAILED",
            "checks": [{"check": "scope.files", "pass": False,
                        "detail": "out-of-plan: ['README.md']"}]}]
        pol = _pol(rt, attempt, task_id="t13h-oop")
        assert pol.data["action"] == "BLOCK"
        assert pol.data["rule_name"] == "unexpected_file_change"

    def test_conflict_and_stale_and_testfail_blocked(self, repo):
        """CONFLICT / STALE_PLAN / TEST_FAILED 三种状态各触发各自红线。"""
        from src.maintenance.execution_policy import detect_post_triggers

        broker, rt, plan, attempt = _chain(repo, task_id="t13h-states")
        a = broker._workspace_svc.registry.get(attempt.execution_id)
        for status, rule in (("CONFLICT", "apply_check_failed"),
                             ("STALE_PLAN", "repository_state_changed"),
                             ("TEST_FAILED", "test_failed")):
            a.status = status
            if status == "TEST_FAILED":
                a.validation_results = [{"status": "FAILED"}]
            triggers = detect_post_triggers(a)
            assert rule in triggers, (status, triggers)
            from src.maintenance.execution_policy import post_execution_gate
            result = post_execution_gate(a, maintenance_policy())
            assert result.action.value == "BLOCK", status
            assert result.rule.name == rule

    def test_gate_mode_requires_payload(self, repo):
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        r = rt.run("policy_check", {
            "gate": "execution_pre",
            "rollback_symbols": [], "keep_symbols": []})
        assert r.status == "partial"
        assert "requires a plan" in r.error
