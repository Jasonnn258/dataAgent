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
from pathlib import Path

from src.errors import DataAgentError
from src.git_history.api import GitAPI
from src.maintenance.models import RepositorySnapshot


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
        """全部执行尝试的根目录：<repo>/.dataagent/runs。"""
        return self.repo / self.RUNS_DIRNAME
