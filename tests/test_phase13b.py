"""Phase 13B 测试：RollbackPlan → ExecutionPlan 翻译（只读，不建沙箱）。"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.errors import DataAgentError                       # noqa: E402
from src.git_history.api import GitAPI                      # noqa: E402
from src.maintenance.models import ExecutionPlan           # noqa: E402
from src.schema import ToolRecorder                        # noqa: E402
from src.semgraph.objects import EvidenceType              # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(scope="module")
def plan_result(seeded):
    """完整链：双侧导航 → 双侧单元匹配 → 仲裁 → build_execution_plan。"""
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    from src.skills import SkillRuntime

    broker = ContextBroker(FIXTURE, ToolRecorder())
    build_change_graph(broker)
    rt = SkillRuntime(broker)
    nav = rt.run("resolve_target", {"query": DEMO_QUERY, "task_id": "t13b"})
    nav_keep = rt.run("resolve_target", {"query": DEMO_KEEP, "task_id": "t13b"})
    cu = rt.run("change_unit_analysis",
                {"terms": nav.data["terms"], "task_id": "t13b"})
    cu_keep = rt.run("change_unit_analysis",
                     {"terms": nav_keep.data["terms"], "task_id": "t13b"})
    sr = rt.run("safe_rollback", {
        "problem_matches": cu.data["matches"],
        "keep_matches": cu_keep.data["matches"],
        "affected_routes": ["/api/auth/login"], "task_id": "t13b"})
    assert sr.ok, sr.error
    bp = rt.run("build_execution_plan",
                {"rollback_plan": sr.data, "task_id": "t13b"})
    assert bp.ok, bp.error
    return bp, broker


# ================================================================ 主链
class TestPlanTranslation:
    def test_plan_is_execution_plan_with_current_head(self, seeded,
                                                      plan_result):
        bp, _ = plan_result
        plan = bp.data["plan"]
        assert isinstance(plan, ExecutionPlan)
        head = GitAPI(FIXTURE).head()
        assert plan.base_commit == head          # 计划锚定在当前 HEAD
        assert plan.task_id == "t13b"
        assert plan.repo == str(FIXTURE)

    def test_expected_contract_from_rollback_units(self, plan_result):
        bp, _ = plan_result
        plan = bp.data["plan"]
        # U2（auth）→ 回退合同：login 页 + auth 库都要 REVERT
        auth_expected = [c for c in plan.expected_changes
                         if c.file == "src/lib/auth.ts"]
        assert auth_expected, "auth.ts 必须出现在 expected_changes"
        assert auth_expected[0].change == "REVERT"
        assert "validateAccount" in auth_expected[0].symbols
        assert "bbdc659f-U2" in auth_expected[0].source_unit
        assert any(c.file == "src/app/login/page.tsx"
                   for c in plan.expected_changes)

    def test_forbidden_contract_from_keep_units(self, plan_result):
        bp, _ = plan_result
        plan = bp.data["plan"]
        title = [c for c in plan.forbidden_changes
                 if c.file == "src/app/layout.tsx"]
        assert title, "layout.tsx 必须出现在 forbidden_changes"
        assert title[0].must == "UNCHANGED"
        assert "bbdc659f-U1" in title[0].source_unit
        # keep 侧绝不能出现在 expected；rollback 侧绝不能在 forbidden
        assert not any(c.file == "src/app/layout.tsx"
                       for c in plan.expected_changes)
        assert not any(c.file == "src/lib/auth.ts"
                       for c in plan.forbidden_changes)

    def test_snapshot_is_real_fingerprint(self, seeded, plan_result):
        bp, _ = plan_result
        snap = bp.data["plan"].repository_snapshot
        assert snap is not None
        assert snap.head == GitAPI(FIXTURE).head()
        assert snap.working_tree_clean, "fixture 应该是干净工作树"
        assert "src/lib/auth.ts" in snap.target_file_hashes
        assert "src/app/layout.tsx" in snap.target_file_hashes
        for h in snap.target_file_hashes.values():
            assert len(h) == 64                     # sha256 hex

    def test_policy_result_carried_not_redecided(self, plan_result):
        bp, _ = plan_result
        pol = bp.data["plan"].policy_result
        # fixture 走 public auth route → HUMAN_REVIEW（与 skill_eval GT 一致）
        assert pol["action"] == "HUMAN_REVIEW"

    def test_execution_plan_evidence_registered(self, plan_result):
        bp, broker = plan_result
        eid = bp.data["evidence_id"]
        assert eid in bp.data["plan"].evidence_ids
        ev = broker.get_evidence([eid])[0]
        assert ev.type == EvidenceType.EXECUTION_PLAN
        assert ev.source == "execution:plan"
        assert "REVERT" in ev.payload and "UNCHANGED" in ev.payload

    def test_summary_is_bounded_dict(self, plan_result):
        bp, _ = plan_result
        s = bp.data["execution_plan"]
        assert s["task_id"] == "t13b"
        assert any("auth.ts" in e for e in s["expected"])
        assert any("layout.tsx" in f for f in s["forbidden"])


# ================================================================ 输入鲁棒性
class TestPlanInputRobustness:
    def test_empty_rollback_side_fails_loud(self, seeded):
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(FIXTURE, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        r = rt.run("build_execution_plan",
                   {"rollback_plan": {"rollback_units": [],
                                      "keep_units": []}})
        assert r.status == "failed"
        assert "rollback side is empty" in r.error

    def test_bad_payload_type_fails_loud(self, seeded):
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(FIXTURE, ToolRecorder())
        rt = SkillRuntime(broker)
        r = rt.run("build_execution_plan", {"rollback_plan": 42})
        assert r.status == "failed"

    def test_shell_string_commands_dropped(self, seeded):
        """validation_commands 只收 argv list；shell 字符串必须被丢弃。"""
        from src.skills.build_execution_plan import _sanitize_commands
        assert _sanitize_commands([["pytest", "-q"]]) == [["pytest", "-q"]]
        assert _sanitize_commands(["pytest -q"]) == []
        assert _sanitize_commands([[]]) == []
        assert _sanitize_commands(None) == []

    def test_stale_detection_via_assert_unchanged(self, seeded,
                                                  plan_result):
        """快照对比：文件内容一变，assert_unchanged 必须大声报错。"""
        from src.services.workspace import WorkspaceService

        bp, broker = plan_result
        snap = bp.data["plan"].repository_snapshot
        svc = WorkspaceService(broker)
        svc.assert_unchanged(snap)            # 现状一致 → 静默通过
        forged = type(snap)(head=snap.head,
                            working_tree_clean=snap.working_tree_clean,
                            target_file_hashes={**snap.target_file_hashes,
                                                "src/lib/auth.ts": "dead"},
                            tracked_state=snap.tracked_state)
        with pytest.raises(DataAgentError):
            svc.assert_unchanged(forged)
