"""沙箱 git 运行器（Phase 13C）：唯一允许碰"写"的 git 出口，且只写沙箱。

与 GitAPI 的分工：
- GitAPI（src/git_history/api.py）：只读白名单，跑在源仓库上。
- SandboxGit（本模块）：worktree 生命周期 + patch 应用/检查 + 沙箱内
  diff/status。**绝不执行 reset / revert / commit / push / checkout --
  ** —— 那些命令不在白名单里，构造上不可能跑起来。

纪律（spec 13C/13F）：
- shell=False，只收 argv list，绝不收 shell 字符串
- cwd 钉死：apply/diff/status 只能跑在**已注册的沙箱 worktree** 里；
  worktree add/remove/prune 只能跑在源仓库根
- timeout 有界、输出截断，防止刷屏爆内存
- 每次调用记 rec.tool 审计轨迹

本模块是物理工具（import subprocess），只允许 src/services/workspace.py
与 src/maintenance/ 引用 —— tests/test_architecture.py 守着这条线。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from src.errors import GitError

# 每个子命令允许的旗标集合（不在集合里的旗标一律拒绝）
_FLAGS: dict[str, set[str]] = {
    "worktree": {"add", "remove", "prune", "list", "--detach", "--force"},
    "apply":    {"--check", "-R"},
    "diff":     {"--stat", "--name-only", "--no-color", "HEAD"},
    "status":   {"--porcelain"},
    "rev-parse": {"HEAD"},
}
# 这些子命令的 cwd 必须落在已注册的沙箱 worktree 里（防逃逸）
_SANDBOX_ONLY = {"apply", "diff", "status"}
# 这些子命令的 cwd 必须是源仓库根本身
_REPO_ONLY = {"worktree"}

_OUTPUT_CAP = 200_000        # 输出字符硬上限
_DEFAULT_TIMEOUT = 120       # 秒


class SandboxGit:
    """argv 白名单 + cwd 钉死的沙箱 git 运行器。"""

    def __init__(self, repo: Path, rec=None) -> None:
        self.repo = Path(repo)
        self.rec = rec
        # 已注册的沙箱 worktree 根目录（prepare 时登记，cleanup 时注销）
        self._worktrees: set[Path] = set()

    # ------------------------------------------------------------ 注册
    def register_worktree(self, path: Path) -> None:
        self._worktrees.add(Path(path).resolve())

    def unregister_worktree(self, path: Path) -> None:
        self._worktrees.discard(Path(path).resolve())

    # ------------------------------------------------------------ 校验
    def _validate(self, args: list[str], cwd: Path) -> None:
        if not args or not all(isinstance(a, str) and a for a in args):
            raise GitError(f"sandbox git needs non-empty argv list, "
                           f"got {args!r}")
        head = args[0]
        if head not in _FLAGS:
            raise GitError(f"blocked non-sandbox git command: git "
                           f"{' '.join(args)}")
        if head == "worktree":
            if len(args) < 2 or args[1] not in {"add", "remove", "prune",
                                                "list"}:
                raise GitError(f"blocked worktree subcommand: {args!r}")
            if args[1] == "add" and "--detach" not in args:
                raise GitError("worktree add must be --detach "
                               "(no branch may be created/checked out)")
        allowed = _FLAGS[head]
        for a in args[1:]:
            if a.startswith("-") and a not in allowed:
                raise GitError(f"blocked flag {a!r} for git {head}")
        # cwd 规则
        here = Path(cwd).resolve()
        if head in _SANDBOX_ONLY:
            if not any(here == wt or here.is_relative_to(wt)
                       for wt in self._worktrees):
                raise GitError(
                    f"git {head} may only run inside a registered sandbox "
                    f"worktree, cwd={cwd}")
        elif head in _REPO_ONLY:
            if here != self.repo.resolve():
                raise GitError(f"git worktree may only run from the source "
                               f"repo root, cwd={cwd}")

    # ------------------------------------------------------------ 执行
    def run(self, args: list[str], cwd: Path,
            timeout: int = _DEFAULT_TIMEOUT) -> str:
        """校验后执行；失败/超时大声报错，输出截断到 _OUTPUT_CAP。"""
        self._validate(args, cwd)
        cmd = ["git", "-c", "core.quotepath=false"] + args
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                                  text=True, timeout=timeout,
                                  errors="replace")
        except subprocess.TimeoutExpired as e:
            raise GitError(f"sandbox git {args[0]} timed out "
                           f"({timeout}s)") from e
        except OSError as e:
            raise GitError(f"sandbox git {args[0]} failed: {e}") from e
        if self.rec:
            self.rec.tool(f"sandbox-git:{args[0]}")
        if proc.returncode != 0:
            raise GitError(f"sandbox git {args[0]} exited "
                           f"{proc.returncode}: {proc.stderr.strip()[:300]}")
        out = proc.stdout
        if len(out) > _OUTPUT_CAP:
            out = out[:_OUTPUT_CAP] + f"\n... [truncated {len(out)} chars]"
        return out
