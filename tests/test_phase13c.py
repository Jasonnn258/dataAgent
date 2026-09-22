"""Phase 13C 测试：沙箱工作区（fixture 复制到 tmp，绝不动原仓库）。"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.errors import GitError                               # noqa: E402
from src.git_history.api import GitAPI                         # noqa: E402
from src.schema import ToolRecorder                           # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """把 fixture 整棵复制到 tmp（worktree 会写 .git/worktrees +
    .dataagent/，不能落在 dataAgent 自己的 repo 里）。"""
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    tmp = tmp_path_factory.mktemp("p13c") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


@pytest.fixture(scope="module")
def prepared(repo):
    """完整链到 PREPARED：导航→单元→仲裁→plan→prepare。"""
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    from src.skills import SkillRuntime

    broker = ContextBroker(repo, ToolRecorder())
    build_change_graph(broker)
    rt = SkillRuntime(broker)
    nav = rt.run("resolve_target", {"query": DEMO_QUERY, "task_id": "t13c"})
    navk = rt.run("resolve_target", {"query": DEMO_KEEP, "task_id": "t13c"})
    cu = rt.run("change_unit_analysis",
                {"terms": nav.data["terms"], "task_id": "t13c"})
    cuk = rt.run("change_unit_analysis",
                 {"terms": navk.data["terms"], "task_id": "t13c"})
    sr = rt.run("safe_rollback", {
        "problem_matches": cu.data["matches"],
        "keep_matches": cuk.data["matches"],
        "affected_routes": ["/api/auth/login"], "task_id": "t13c"})
    bp = rt.run("build_execution_plan",
                {"rollback_plan": sr.data, "task_id": "t13c"})
    pe = rt.run("prepare_execution", {"plan": bp.data["plan"]})
    assert pe.ok, pe.error
    return {"broker": broker, "rt": rt, "plan": bp.data["plan"],
            "skill": pe, "attempt": pe.data["execution"]}


# ================================================================ prepare
class TestPrepare:
    def test_worktree_detached_at_base_commit(self, prepared):
        attempt = prepared["attempt"]
        assert attempt.status == "PREPARED"
        wt = Path(attempt.workspace)
        assert wt.is_dir()
        # worktree 检出在 plan.base_commit（--detach，无分支）
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt,
                              capture_output=True, text=True,
                              check=True).stdout.strip()
        assert head == prepared["plan"].base_commit
        branches = subprocess.run(["git", "branch", "--show-current"],
                                  cwd=wt, capture_output=True, text=True,
                                  check=True).stdout.strip()
        assert branches == "", "--detach 不应有当前分支"

    def test_run_dir_layout_and_attempt_json(self, prepared, repo):
        attempt = prepared["attempt"]
        run_dir = repo / ".dataagent" / "runs" / attempt.execution_id
        assert run_dir.is_dir()
        assert (run_dir / "attempt.json").is_file()
        from src.maintenance.models import ExecutionAttempt
        back = ExecutionAttempt.from_json(
            (run_dir / "attempt.json").read_text(encoding="utf-8"))
        assert back.execution_id == attempt.execution_id
        assert back.status == "PREPARED"
        assert back.plan.base_commit == prepared["plan"].base_commit

    def test_dataagent_excluded_from_status(self, prepared, repo):
        exclude = repo / ".git" / "info" / "exclude"
        assert ".dataagent/" in exclude.read_text()

    def test_sandbox_never_mutates_source_repo(self, prepared, repo):
        """spec 13C 核心失败用例：全程跑完，源仓库内容零修改。"""
        plan = prepared["plan"]
        snap = plan.repository_snapshot
        # HEAD 没动
        assert GitAPI(repo).head() == snap.head
        # 工作树 tracked 状态没变（.dataagent 被 exclude 挡住；
        # .git/worktrees 是 git 自己的元数据区，不在工作树内）
        assert GitAPI(repo).status_porcelain() == []
        # 目标文件哈希一个都没变
        for f, h in snap.target_file_hashes.items():
            assert (repo / f).is_file(), f
            got = hashlib.sha256((repo / f).read_bytes()).hexdigest()
            assert got == h, f"source file changed during sandbox: {f}"

    def test_prepare_twice_gives_new_execution_ids(self, prepared):
        broker, plan = prepared["broker"], prepared["plan"]
        from src.skills import SkillRuntime
        pe2 = SkillRuntime(broker).run("prepare_execution", {"plan": plan})
        assert pe2.ok
        assert pe2.data["execution_id"] != prepared["attempt"].execution_id
        # 两次都登记在案
        ids = {a.execution_id for a in broker.executions()}
        assert {prepared["attempt"].execution_id,
                pe2.data["execution_id"]} <= ids


# ================================================================ stale
class TestStalePlan:
    def test_target_file_modified_after_plan(self, repo):
        """失败用例：plan 之后目标文件被改 → STALE_PLAN，不建 worktree。"""
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        plan = _plan_on(repo, broker, rt)
        # 改一个目标文件（计划外的人类/其他工具改动）
        target = repo / "src/lib/auth.ts"
        original = target.read_text(encoding="utf-8")
        target.write_text(original + "\n// out-of-band edit\n",
                          encoding="utf-8")
        try:
            pe = rt.run("prepare_execution", {"plan": plan})
            assert pe.status == "failed"
            assert pe.data["status"] == "STALE_PLAN"
            attempt = pe.data["execution"]
            assert not attempt.workspace
            run_dir = repo / ".dataagent" / "runs" / attempt.execution_id
            assert not (run_dir / "worktree").exists()
            assert "auth.ts" in pe.error or "changed" in pe.error
        finally:
            target.write_text(original, encoding="utf-8")

    def test_head_moved_after_plan(self, repo):
        """失败用例：plan 之后 HEAD 前进 → STALE_PLAN。"""
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        plan = _plan_on(repo, broker, rt)
        forged = plan.repository_snapshot
        plan.repository_snapshot = type(forged)(
            head="0" * 40, working_tree_clean=forged.working_tree_clean,
            target_file_hashes=forged.target_file_hashes,
            tracked_state=forged.tracked_state)
        pe = rt.run("prepare_execution", {"plan": plan})
        assert pe.status == "failed"
        assert pe.data["status"] == "STALE_PLAN"

    def test_plan_without_snapshot_refused(self, repo):
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        plan = _plan_on(repo, broker, rt)
        plan.repository_snapshot = None
        pe = rt.run("prepare_execution", {"plan": plan})
        assert pe.status == "failed"
        assert "refuse to prepare" in pe.error


def _plan_on(repo, broker, rt):
    """在给定 broker 上现建一份合法 plan（快照取当前真状态）。"""
    from src.maintenance.models import ExecutionPlan
    snap = broker.execution_snapshot(["src/lib/auth.ts",
                                      "src/app/layout.tsx"])
    return ExecutionPlan(
        task_id="stale-check", repo=str(repo), base_commit=snap.head,
        rollback_units=[{"id": "cu:bbdc659f-U2", "label": "auth",
                         "commit": "bbdc659f",
                         "files": ["src/lib/auth.ts"]}],
        keep_units=[{"id": "cu:bbdc659f-U1", "label": "title",
                     "commit": "bbdc659f",
                     "files": ["src/app/layout.tsx"]}],
        target_files=["src/lib/auth.ts", "src/app/layout.tsx"],
        repository_snapshot=snap)


# ================================================================ 沙箱 runner
class TestSandboxRunner:
    def test_destructive_commands_blocked(self, repo):
        from src.maintenance.sandbox_git import SandboxGit

        sg = SandboxGit(repo)
        for bad in (["reset", "--hard"], ["commit", "-m", "x"],
                    ["push"], ["revert", "HEAD"],
                    ["checkout", "main"], ["clean", "-fd"]):
            with pytest.raises(GitError):
                sg.run(bad, cwd=repo)

    def test_shell_strings_rejected(self, repo):
        from src.maintenance.sandbox_git import SandboxGit

        sg = SandboxGit(repo)
        with pytest.raises(GitError):
            sg.run("git status --porcelain", cwd=repo)   # type: ignore[arg-type]

    def test_apply_requires_registered_worktree_cwd(self, repo, prepared):
        from src.maintenance.sandbox_git import SandboxGit

        sg = SandboxGit(repo)
        # 未注册任何 worktree：apply 即便在 repo 根也拒绝
        with pytest.raises(GitError):
            sg.run(["apply", "--check", "/nonexistent.patch"], cwd=repo)
        # 注册后才放行；patch 不存在 → git 自己报错（合法失败路径）
        attempt = prepared["attempt"]
        sg.register_worktree(Path(attempt.workspace))
        with pytest.raises(GitError, match="exited|No such file"):
            sg.run(["apply", "--check", "/nonexistent.patch"],
                   cwd=Path(attempt.workspace))

    def test_worktree_add_requires_detach(self, repo):
        from src.maintenance.sandbox_git import SandboxGit

        sg = SandboxGit(repo)
        with pytest.raises(GitError, match="detach"):
            sg.run(["worktree", "add", "/tmp/x", "HEAD"], cwd=repo)


# ================================================================ cleanup
class TestCleanup:
    def test_cleanup_removes_worktree_keeps_artifacts(self, prepared):
        broker = prepared["broker"]
        attempt = prepared["attempt"]
        run_dir = Path(attempt.workspace).parent
        workspace = Path(attempt.workspace)   # cleanup 会原地清空该字段
        cleaned = broker._workspace_svc.cleanup(attempt.execution_id)
        assert not workspace.exists()
        assert cleaned.workspace == ""
        # attempt.json 必须留档（执行过的东西必须可回看）
        assert (run_dir / "attempt.json").is_file()
        # 源仓库仍然零修改
        assert GitAPI(Path(prepared["plan"].repo)).status_porcelain() == []

    def test_cleanup_is_idempotent(self, prepared):
        broker = prepared["broker"]
        again = broker._workspace_svc.cleanup(prepared["attempt"].execution_id)
        assert again.workspace == ""
