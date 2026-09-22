"""Phase 13E 测试：沙箱 apply + actual vs proposed 对比（fixture 复制 tmp）。"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.errors import DataAgentError                      # noqa: E402
from src.schema import ToolRecorder                        # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    tmp = tmp_path_factory.mktemp("p13e") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


def _run_chain(repo, task_id="t13e"):
    """完整链到 APPLIED_SANDBOX，返回 (broker, rt, plan, attempt, results)。"""
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
    bp = rt.run("build_execution_plan",
                {"rollback_plan": sr.data, "task_id": task_id})
    pe = rt.run("prepare_execution", {"plan": bp.data["plan"]})
    br = rt.run("build_rollback_patch", {"execution": pe.data["execution"]})
    ap = rt.run("apply_patch", {"execution": br.data["execution"]})
    return broker, rt, bp.data["plan"], ap


@pytest.fixture(scope="module")
def applied(repo):
    broker, rt, plan, ap = _run_chain(repo)
    assert ap.ok, ap.error
    return {"broker": broker, "rt": rt, "plan": plan,
            "result": ap, "attempt": ap.data["execution"]}


# ================================================================ 应用结果
class TestApply:
    def test_states_and_artifacts(self, applied):
        attempt = applied["attempt"]
        assert attempt.status == "APPLIED_SANDBOX"
        assert Path(attempt.patch_path).name == "proposed.patch"
        assert Path(attempt.actual_patch_path).name == "actual.patch"
        assert Path(attempt.actual_patch_path).is_file()

    def test_sandbox_content_reverted(self, applied):
        """行为面：沙箱里 auth 回到 4，keep 侧字节不动。"""
        attempt = applied["attempt"]
        wt = Path(attempt.workspace)
        auth = (wt / "src/lib/auth.ts").read_text(encoding="utf-8")
        assert "account.length >= 4" in auth
        assert "account.length >= 8" not in auth
        layout_ws = (wt / "src/app/layout.tsx").read_bytes()
        layout_src = (Path(applied["plan"].repo) /
                      "src/app/layout.tsx").read_bytes()
        assert layout_ws == layout_src

    def test_source_repo_still_untouched(self, applied, repo):
        from src.git_history.api import GitAPI
        snap = applied["plan"].repository_snapshot
        assert GitAPI(repo).head() == snap.head
        assert GitAPI(repo).status_porcelain() == []
        for f, h in snap.target_file_hashes.items():
            import hashlib
            assert hashlib.sha256(
                (repo / f).read_bytes()).hexdigest() == h


# ================================================================ 对比器
class TestCompare:
    def test_identical_patch_passes(self, applied):
        from src.services.patch import compare_patches
        attempt = applied["attempt"]
        assert compare_patches(Path(attempt.patch_path),
                               Path(attempt.actual_patch_path)) == []

    def test_offset_tolerated(self, tmp_path):
        """同样的内容、不同的 hunk 起始行 → 不算计划外修改。"""
        from src.services.patch import compare_patches
        base = ("diff --git a/f b/f\n--- a/f\n+++ b/f\n"
                "@@ -10,3 +10,3 @@\n ctx\n-old\n+new\n")
        shifted = ("diff --git a/f b/f\n--- a/f\n+++ b/f\n"
                   "@@ -40,3 +40,3 @@\n ctx\n-old\n+new\n")
        p, a = tmp_path / "p.patch", tmp_path / "a.patch"
        p.write_text(base); a.write_text(shifted)
        assert compare_patches(p, a) == []

    def test_extra_file_detected(self, tmp_path):
        from src.services.patch import compare_patches
        p = tmp_path / "p.patch"; a = tmp_path / "a.patch"
        p.write_text("diff --git a/f b/f\n--- a/f\n+++ b/f\n"
                     "@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n")
        a.write_text(p.read_text() +
                     "diff --git a/extra b/extra\n--- a/extra\n+++ b/extra\n"
                     "@@ -1,1 +1,2 @@\n+rogue\n")
        problems = compare_patches(p, a)
        assert any("outside plan" in x and "extra" in x for x in problems)

    def test_extra_line_detected(self, tmp_path):
        from src.services.patch import compare_patches
        p = tmp_path / "p.patch"; a = tmp_path / "a.patch"
        p.write_text("diff --git a/f b/f\n--- a/f\n+++ b/f\n"
                     "@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n")
        a.write_text("diff --git a/f b/f\n--- a/f\n+++ b/f\n"
                     "@@ -1,3 +1,4 @@\n ctx\n-old\n+new\n+sneaky\n")
        assert any("differs" in x for x in compare_patches(p, a))

    def test_missing_revert_detected(self, tmp_path):
        from src.services.patch import compare_patches
        p = tmp_path / "p.patch"; a = tmp_path / "a.patch"
        p.write_text("diff --git a/f b/f\n--- a/f\n+++ b/f\n"
                     "@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n")
        a.write_text("")
        assert any("missing" in x for x in compare_patches(p, a))


# ================================================================ 失败路径
class TestApplyFailures:
    def test_conflict_when_state_changed_between_build_and_apply(self, repo):
        """失败用例：build 与 apply 之间沙箱文件被弄坏 → CONFLICT。"""
        broker, rt, plan, _ = _run_chain(
            repo, task_id="t13e-conflict-prep")
        # 重新走一遍到 PATCH_BUILT，然后破坏 base
        pe = rt.run("prepare_execution", {"plan": plan})
        br = rt.run("build_rollback_patch", {"execution": pe.data["execution"]})
        assert br.ok
        wt = Path(br.data["execution"].workspace)
        (wt / "src/lib/auth.ts").write_text("garbage\n", encoding="utf-8")
        ap = rt.run("apply_patch", {"execution": br.data["execution"]})
        assert ap.status == "failed"
        assert ap.data["status"] == "CONFLICT"

    def test_out_of_plan_edit_detected(self, applied):
        """失败用例：apply 后再有人动沙箱文件 → 对比器必须抓到
        （13G 也要复用这个对比器做复核）。"""
        from src.services.patch import compare_patches
        attempt = applied["attempt"]
        wt = Path(attempt.workspace)
        # 计划外修改：改一个不在计划里的文件 + 再动一行 auth
        (wt / "README.md").write_text(
            (wt / "README.md").read_text(encoding="utf-8") + "\nrogue\n",
            encoding="utf-8")
        r = subprocess.run(["git", "diff", "--no-color", "HEAD"], cwd=wt,
                           capture_output=True, text=True)
        tampered = Path(attempt.actual_patch_path).parent / "tampered.patch"
        tampered.write_text(r.stdout, encoding="utf-8")
        problems = compare_patches(Path(attempt.patch_path), tampered)
        assert any("outside plan" in x and "README" in x for x in problems)

    def test_refuses_unbuilt_attempt(self, repo):
        """没 build 过 patch（PREPARED）→ fail fast，不偷偷走流程。"""
        from src.maintenance.models import ExecutionPlan

        broker, rt, plan, _ = _run_chain(repo, task_id="t13e-refuse-prep")
        pe = rt.run("prepare_execution", {"plan": plan})
        ap = rt.run("apply_patch", {"execution": pe.data["execution"]})
        assert ap.status == "failed"
        assert "PATCH_BUILT" in ap.error

    def test_apply_twice_refused(self, applied):
        """重复 apply 必须拒绝（状态机语义：重试 = 新 attempt）。"""
        broker = applied["broker"]
        with pytest.raises(DataAgentError):
            broker.apply_execution_patch(applied["attempt"])
