"""WorkspaceService（Phase 13B/13C）：源仓库状态指纹与沙箱工作区。

两个职责，边界严格：
1. **只读快照**（13B）：HEAD / 工作树是否干净 / 目标文件哈希 ——
   ExecutionPlan 的 stale 检测基准。本类绝不写源仓库。
2. **沙箱工作区**（13C 起）：git worktree 检出、运行目录、清理。
   所有写动作只发生在 <repo>/.dataagent/runs/<execution_id>/ 下的
   独立 worktree，绝不 reset/revert/commit/push 源仓库。

broker 只暴露 snapshot / assert_unchanged / prepare_execution 等门面，
agent/skill 永远拿不到裸 git。
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from src.errors import DataAgentError
from src.git_history.api import GitAPI
from src.maintenance.models import (ExecutionAttempt, ExecutionPlan,
                                    RepositorySnapshot)
from src.maintenance.sandbox_git import SandboxGit
from src.maintenance.state import AttemptRegistry


def _file_hash(path: Path) -> str:
    """单文件 sha256（二进制安全）；不存在/不是文件返回空串。"""
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def diff_snapshots(expected: RepositorySnapshot,
                   current: RepositorySnapshot) -> list[str]:
    """两份快照的差异清单（人类可读，空列表 = 完全一致）。

    stale 判定的真源：head 变了 / 工作树干净态变了 / tracked 状态变了 /
    目标文件哈希变了，任何一条都是"计划已经过期"。
    """
    problems: list[str] = []
    if expected.head != current.head:
        problems.append(f"HEAD moved: {expected.head[:12]} -> "
                        f"{current.head[:12]}")
    if expected.working_tree_clean != current.working_tree_clean:
        problems.append("working tree clean: "
                        f"{expected.working_tree_clean} -> "
                        f"{current.working_tree_clean}")
    if sorted(expected.tracked_state) != sorted(current.tracked_state):
        problems.append(f"tracked state changed: "
                        f"{current.tracked_state[:5]}")
    for f, h in sorted(expected.target_file_hashes.items()):
        cur = current.target_file_hashes.get(f)
        if cur is None:
            problems.append(f"target file vanished from snapshot: {f}")
        elif cur != h:
            problems.append(f"target file changed: {f} "
                            f"{h[:8]} -> {cur[:8]}")
    return problems


class WorkspaceService:
    """源仓库快照 + 沙箱运行目录管理（broker 内部委托至此）。"""

    # runs 目录固定在源仓库内（持久卷纪律：不写 /tmp）
    RUNS_DIRNAME = ".dataagent/runs"

    def __init__(self, broker) -> None:
        self.broker = broker
        self.repo = broker.repo
        self.rec = broker.rec
        self._git: GitAPI | None = None
        # 13C：执行尝试注册表 + 沙箱 git（写路径只经它）
        self.registry = AttemptRegistry()
        self.sandbox = SandboxGit(self.repo, self.rec)

    # ------------------------------------------------------------ 只读快照
    @property
    def git(self) -> GitAPI:
        """懒构造只读 GitAPI（不在 import 期碰物理工具）。"""
        if self._git is None:
            self._git = GitAPI(self.repo, self.rec)
        return self._git

    def snapshot(self, files: list[str] | None = None) -> RepositorySnapshot:
        """拍当前仓库状态指纹（只读）。files 是计划关心的目标文件集。"""
        porcelain = self.git.status_porcelain()
        hashes = {f: _file_hash(self.repo / f) for f in (files or [])}
        self.rec.tool("workspace:snapshot")
        return RepositorySnapshot(
            head=self.git.head(),
            working_tree_clean=not porcelain,
            target_file_hashes=hashes,
            tracked_state=porcelain)

    def assert_unchanged(self, expected: RepositorySnapshot,
                         files: list[str] | None = None) -> None:
        """重拍快照与 expected 对比；不一致就地大声报错（调用方转
        STALE_PLAN）。一致则静默返回。"""
        current = self.snapshot(files or list(expected.target_file_hashes))
        problems = diff_snapshots(expected, current)
        if problems:
            raise DataAgentError(
                "repository changed since plan was built: " +
                "; ".join(problems))

    # ------------------------------------------------------------ 沙箱布局
    @property
    def runs_root(self) -> Path:
        """全部执行尝试的根目录：<repo>/.dataagent/runs。

        环境变量 DATAAGENT_RUNS_DIR 可整体搬到别处（测试/特殊布局）。
        """
        env = os.environ.get("DATAAGENT_RUNS_DIR")
        return Path(env) if env else self.repo / self.RUNS_DIRNAME

    def _ensure_excluded(self) -> None:
        """把 .dataagent/ 幂等地加进 .git/info/exclude（防沙箱产物
        污染 status）。这是源仓库 .git 内唯一允许的元数据写入。"""
        info = self.repo / ".git" / "info"
        if not info.parent.is_dir():
            return   # 非 .git 目录布局（如 linked worktree）—— 跳过
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        text = exclude.read_text() if exclude.exists() else ""
        if ".dataagent/" not in text:
            exclude.write_text(text.rstrip("\n") + "\n.dataagent/\n")

    def _dump_attempt(self, attempt: ExecutionAttempt) -> None:
        """attempt.json 落盘（运行目录；每一步后都重写，崩溃也有现场）。"""
        run_dir = self.runs_root / attempt.execution_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "attempt.json").write_text(
            attempt.to_json(), encoding="utf-8")

    # ------------------------------------------------------------ prepare
    def prepare(self, plan: ExecutionPlan) -> ExecutionAttempt:
        """为 plan 建沙箱 worktree（检出在 plan.base_commit，--detach）。

        任何一步不对都停在一个明确的终态上，绝不带病前进：
        - plan 没带快照 → 拒绝（无法做 stale 检测就是无法安全执行）
        - 快照对比不一致 → STALE_PLAN（不建 worktree）
        - worktree 建完后源仓库指纹变了 → 清掉 worktree + STALE_PLAN
        成功 → PREPARED，workspace/attempt.json 落档。
        """
        if plan.repository_snapshot is None:
            raise DataAgentError(
                "plan lacks repository_snapshot — refuse to prepare "
                "(no stale-detection baseline)")
        attempt = self.registry.create(plan)
        run_dir = self.runs_root / attempt.execution_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_excluded()

        # ---- 1. stale 前检：写任何东西之前先确认计划没过期 ----
        try:
            self.assert_unchanged(plan.repository_snapshot,
                                  plan.target_files)
        except DataAgentError as e:
            self.registry.update_status(attempt.execution_id,
                                        "STALE_PLAN", note=str(e)[:300])
            self._dump_attempt(self.registry.get(attempt.execution_id))
            return self.registry.get(attempt.execution_id)

        # ---- 2. worktree 检出（写动作只发生在 run_dir 下）----
        worktree = run_dir / "worktree"
        self.sandbox.run(["worktree", "add", "--detach",
                          str(worktree), plan.base_commit],
                         cwd=self.repo)
        self.sandbox.register_worktree(worktree)

        # ---- 3. 后检：源仓库必须和计划时刻一模一样 ----
        problems = diff_snapshots(
            plan.repository_snapshot,
            self.snapshot(plan.target_files))
        if problems:
            self._teardown_worktree(worktree)
            self.registry.update_status(
                attempt.execution_id, "STALE_PLAN",
                note="repo changed during prepare: " +
                     "; ".join(problems)[:300])
            self._dump_attempt(self.registry.get(attempt.execution_id))
            return self.registry.get(attempt.execution_id)

        self.registry.update_status(attempt.execution_id, "PREPARED",
                                    note=f"worktree at {plan.base_commit[:12]}")
        attempt = self.registry.attach(attempt.execution_id,
                                       workspace=str(worktree))
        self._dump_attempt(attempt)
        return attempt

    # ------------------------------------------------------------ cleanup
    def _teardown_worktree(self, worktree: Path) -> None:
        """移除一个沙箱 worktree（容错：remove 失败就 rmtree + prune）。"""
        self.sandbox.unregister_worktree(worktree)
        try:
            self.sandbox.run(["worktree", "remove", "--force",
                              str(worktree)], cwd=self.repo)
        except Exception:
            shutil.rmtree(worktree, ignore_errors=True)
        try:
            self.sandbox.run(["worktree", "prune"], cwd=self.repo)
        except Exception:
            pass   # prune 只是元数据回收，失败不影响正确性

    def cleanup(self, execution_id: str) -> ExecutionAttempt:
        """清掉该尝试的 worktree；proposed/actual patch 与 attempt.json
        留档（审计要求：执行过的东西必须可回看）。状态不回退。"""
        attempt = self.registry.get(execution_id)
        if attempt.workspace:
            self._teardown_worktree(Path(attempt.workspace))
            attempt = self.registry.get(execution_id)
            attempt.notes.append(f"worktree cleaned: {attempt.workspace}")
            attempt.workspace = ""
            self._dump_attempt(attempt)
        return attempt

    # ------------------------------------------------------------ 查询
    def get_execution(self, execution_id: str) -> ExecutionAttempt:
        return self.registry.get(execution_id)

    def executions(self) -> list[ExecutionAttempt]:
        return self.registry.all_attempts()

    def load_attempt_from_disk(self, execution_id: str) -> ExecutionAttempt:
        """从 attempt.json 恢复（跨进程/审计读取用）。"""
        path = self.runs_root / execution_id / "attempt.json"
        if not path.is_file():
            raise DataAgentError(f"no attempt.json for {execution_id}")
        return ExecutionAttempt.from_json(path.read_text(encoding="utf-8"))
