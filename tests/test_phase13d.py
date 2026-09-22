"""Phase 13D 测试：确定性反向 patch 构建（fixture 复制到 tmp）。"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.errors import DataAgentError, GitError            # noqa: E402
from src.git_history.api import GitAPI                      # noqa: E402
from src.schema import ToolRecorder                        # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    tmp = tmp_path_factory.mktemp("p13d") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


@pytest.fixture(scope="module")
def built(repo):
    """完整链到 PATCH_BUILT：导航→单元→仲裁→plan→prepare→build patch。"""
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    from src.skills import SkillRuntime

    broker = ContextBroker(repo, ToolRecorder())
    build_change_graph(broker)
    rt = SkillRuntime(broker)
    nav = rt.run("resolve_target", {"query": DEMO_QUERY, "task_id": "t13d"})
    navk = rt.run("resolve_target", {"query": DEMO_KEEP, "task_id": "t13d"})
    cu = rt.run("change_unit_analysis",
                {"terms": nav.data["terms"], "task_id": "t13d"})
    cuk = rt.run("change_unit_analysis",
                 {"terms": navk.data["terms"], "task_id": "t13d"})
    sr = rt.run("safe_rollback", {
        "problem_matches": cu.data["matches"],
        "keep_matches": cuk.data["matches"],
        "affected_routes": ["/api/auth/login"], "task_id": "t13d"})
    bp = rt.run("build_execution_plan",
                {"rollback_plan": sr.data, "task_id": "t13d"})
    pe = rt.run("prepare_execution", {"plan": bp.data["plan"]})
    assert pe.ok, pe.error
    br = rt.run("build_rollback_patch",
                {"execution": pe.data["execution"]})
    assert br.ok, br.error
    return {"broker": broker, "rt": rt, "plan": bp.data["plan"],
            "attempt": br.data and pe.data["execution"],
            "result": br, "artifact": br.data["artifact"]}


# ================================================================ 构建产物
class TestPatchArtifact:
    def test_status_and_files(self, built, repo):
        attempt, art = built["attempt"], built["artifact"]
        assert attempt.status == "PATCH_BUILT"
        patch = Path(attempt.patch_path)
        assert patch.is_file()
        assert patch.parent == repo / ".dataagent" / "runs" / attempt.execution_id
        assert art.affected_files == ["src/app/login/page.tsx",
                                      "src/lib/auth.ts"]
        assert art.source_change_units == ["bbdc659f-U2"]
        assert art.base_commit == built["plan"].base_commit
        assert art.hunks_total == 2
        assert set(art.affected_symbols) >= {"validateAccount",
                                             "verifyPassword"}

    def test_sha256_is_file_identity(self, built):
        art = built["artifact"]
        got = hashlib.sha256(Path(art.path).read_bytes()).hexdigest()
        assert got == art.sha256

    def test_patch_reverts_exactly_the_broken_lines(self, built):
        text = Path(built["attempt"].patch_path).read_text(encoding="utf-8")
        # auth.ts：阈值改回 4（bbdc659 把它从 4 改成 8）
        assert "+  return account.length >= 4;" in text
        assert "-  return account.length >= 8;" in text
        # 反向头：old 侧 = 前向 new 侧
        assert "@@ -12,5 +12,5 @@" in text
        assert "diff --git a/src/lib/auth.ts b/src/lib/auth.ts" in text
        # login 页提示文案也在回退范围
        assert "src/app/login/page.tsx" in text

    def test_keep_side_never_enters_patch(self, built):
        """构造性排除：keep 单元的 hunk 根本不进 patch（不是事后删）。"""
        text = Path(built["attempt"].patch_path).read_text(encoding="utf-8")
        assert "layout.tsx" not in text
        assert "系统" not in text    # 标题文案不出现

    def test_determinism_two_attempts_same_sha(self, built):
        broker, plan = built["broker"], built["plan"]
        from src.skills import SkillRuntime
        rt = SkillRuntime(broker)
        pe2 = rt.run("prepare_execution", {"plan": plan})
        br2 = rt.run("build_rollback_patch",
                     {"execution": pe2.data["execution"]})
        assert br2.ok
        assert br2.data["artifact"].sha256 == built["artifact"].sha256

    def test_execution_patch_evidence_registered(self, built):
        from src.semgraph.objects import EvidenceType
        broker = built["broker"]
        evs = [e for e in broker.all_evidence()
               if e.type == EvidenceType.EXECUTION_PATCH]
        assert evs, "EXECUTION_PATCH evidence 应落图"
        assert any(built["artifact"].sha256[:12] == e.location for e in evs)


# ================================================================ 真实 apply
class TestPatchApplies:
    def test_apply_in_sandbox_reverts_auth_only(self, built):
        """把 patch 真正 apply 进沙箱，确认行为面：auth 回退、title 保留。"""
        attempt, art = built["attempt"], built["artifact"]
        wt = Path(attempt.workspace)
        r = subprocess.run(["git", "apply", attempt.patch_path], cwd=wt,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        stat = subprocess.run(["git", "diff", "--name-only"], cwd=wt,
                              capture_output=True, text=True)
        changed = set(stat.stdout.split())
        assert changed == set(art.affected_files)
        # auth.ts 回到 >= 4
        auth = (wt / "src/lib/auth.ts").read_text(encoding="utf-8")
        assert "account.length >= 4" in auth
        assert "account.length >= 8" not in auth
        # layout.tsx（keep 侧）字节级不动
        layout_ws = (wt / "src/app/layout.tsx").read_bytes()
        layout_src = (Path(built["plan"].repo) /
                      "src/app/layout.tsx").read_bytes()
        assert layout_ws == layout_src


# ================================================================ 失败路径
class TestPatchFailures:
    def test_conflict_when_base_state_broken(self, repo):
        """失败用例：沙箱 base 状态被弄坏 → apply --check 不过 → CONFLICT
        终态，不偷改 patch、不 --3way。"""
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        from src.maintenance.models import ExecutionPlan

        snap = broker.execution_snapshot(["src/lib/auth.ts"])
        plan = ExecutionPlan(
            task_id="conflict", repo=str(repo), base_commit=snap.head,
            rollback_units=[{"id": "cu:bbdc659f-U2", "label": "auth",
                             "commit": "bbdc659f",
                             "files": ["src/lib/auth.ts"]}],
            keep_units=[], target_files=["src/lib/auth.ts"],
            repository_snapshot=snap)
        pe = rt.run("prepare_execution", {"plan": plan})
        assert pe.ok
        # 把沙箱里的 auth.ts 换成垃圾 → hunk 上下文对不上
        wt = Path(pe.data["workspace"])
        (wt / "src/lib/auth.ts").write_text("junk\n", encoding="utf-8")
        br = rt.run("build_rollback_patch",
                    {"execution": pe.data["execution"]})
        assert br.status == "failed"
        assert br.data["status"] == "CONFLICT"
        assert br.data["artifact"] is None
        attempt = broker.get_execution(br.data["execution_id"])
        # 终态：再想迁去 PATCH_BUILT 会被状态机拒绝
        with pytest.raises(DataAgentError):
            broker._workspace_svc.registry.update_status(
                attempt.execution_id, "PATCH_BUILT")
        # 但 proposed.patch 已落盘（审计现场）
        assert Path(attempt.patch_path).is_file()

    def test_refuses_non_prepared_attempt(self, built, repo):
        """没 prepare 过的 attempt（PLANNED）必须 fail fast。"""
        from src.maintenance.models import ExecutionPlan
        from src.skills import SkillRuntime

        broker = built["broker"]
        rt = SkillRuntime(broker)
        from src.maintenance.state import AttemptRegistry
        plan = built["plan"]
        reg = broker._workspace_svc.registry
        attempt = reg.create(plan)          # PLANNED，无 worktree
        br = rt.run("build_rollback_patch", {"execution": attempt})
        assert br.status == "failed"
        assert "PREPARED" in br.error

    def test_unit_without_hunks_refused(self, built):
        """图上没有 HUNK 的单元：无法确定性构建，直接拒绝。"""
        from src.maintenance.models import ExecutionPlan
        from src.skills import SkillRuntime

        broker = built["broker"]
        rt = SkillRuntime(broker)
        plan = built["plan"]
        plan.rollback_units = [{"id": "cu:zzzzzzzz-U9",
                                "label": "ghost", "commit": "0000000",
                                "files": ["src/lib/auth.ts"]}]
        pe = rt.run("prepare_execution", {"plan": plan})
        assert pe.ok
        br = rt.run("build_rollback_patch",
                    {"execution": pe.data["execution"]})
        assert br.status == "failed"
        assert "not on graph" in br.error
