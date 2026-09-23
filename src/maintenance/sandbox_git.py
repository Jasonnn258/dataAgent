"""沙箱 git 运行器（Phase 13C/13J）：唯一允许碰"写"的 git 出口。

与 GitAPI 的分工：
- GitAPI（src/git_history/api.py）：只读白名单，跑在源仓库上。
- SandboxGit（本模块）：两条写路径，都在白名单内 ——
  * run()（13C）：worktree 生命周期 + patch 应用/检查 + 沙箱内
    diff/status，apply/diff/status 只能跑在**已注册的沙箱 worktree** 里；
  * run_source()（13J）：晋升专用，apply 沙箱里验证过的 patch 到**源
    仓库根本身** —— 全系统唯一修改真实 workspace 的物理出口，argv 形态
    钉死成三种（apply --check / apply / diff HEAD）， PromotionService
    之外无人可用。
  **绝不执行 reset / revert / commit / push / checkout --** —— 那些命令
  不在白名单里，构造上不可能跑起来。

纪律（spec 13C/13F/13J）：
- shell=False，只收 argv list，绝不收 shell 字符串
- timeout 有界、输出截断，防止刷屏爆内存
- 每次调用记 rec.tool 审计轨迹

本模块是物理工具（import subprocess），只允许 src/services/ 与
src/maintenance/ 引用 —— tests/test_architecture.py 守着这条线。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from src.errors import GitError

# 每个子命令允许的旗标集合（不在集合里的旗标一律拒绝）
_FLAGS: dict[str, set[str]] = {
    "worktree": {"add", "remove", "prune", "list", "--detach", "--force"},
    "apply":    {"--check", "-R", "--cached"},
    "diff":     {"--stat", "--name-only", "--no-color", "HEAD"},
    "status":   {"--porcelain"},
    "rev-parse": {"HEAD"},
    # 13E 内容级核对：物化期望内容（只读源仓库对象 + 写临时 index）
    "read-tree": set(),
    "ls-files":  {"-s"},
    "hash-object": set(),
}
# 这些子命令的 cwd 必须落在已注册的沙箱 worktree 里（防逃逸）
_SANDBOX_ONLY = {"apply", "diff", "status", "read-tree", "ls-files",
                 "hash-object"}
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
    def _validate(self, args: list[str], cwd: Path,
                  env: dict[str, str] | None = None) -> None:
        if not args or not all(isinstance(a, str) and a for a in args):
            raise GitError(f"sandbox git needs non-empty argv list, "
                           f"got {args!r}")
        head = args[0]
        if head not in _FLAGS:
            raise GitError(f"blocked non-sandbox git command: git "
                           f"{' '.join(args)}")
        # 会写 index 的命令只允许写在隔离的临时 index 上（GIT_INDEX_FILE
        # 未指向临时文件就拒绝）：read-tree/apply --cached 直接覆写
        # cwd 所在仓库的 index，绝不许碰 worktree/源仓库的真 index
        if head == "read-tree" or (head == "apply" and "--cached" in args):
            if not (env or {}).get("GIT_INDEX_FILE"):
                raise GitError(
                    f"git {head} writes the index — requires isolated "
                    f"GIT_INDEX_FILE (temp file), refusing to touch the "
                    f"real one")
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
            timeout: int = _DEFAULT_TIMEOUT,
            env: dict[str, str] | None = None) -> str:
        """校验后执行；失败/超时大声报错，输出截断到 _OUTPUT_CAP。

        env 叠加在 os.environ 之上，用于隔离 GIT_INDEX_FILE（13E 期望
        内容物化）。_validate 会拒绝写 index 命令不带隔离 env 的调用。
        """
        self._validate(args, cwd, env)
        cmd = ["git", "-c", "core.quotepath=false"] + args
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                                  text=True, timeout=timeout,
                                  errors="replace",
                                  env={**os.environ, **env} if env else None)
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

    # ------------------------------------------------------------ 晋升（13J）
    def run_source(self, args: list[str], timeout: int = _DEFAULT_TIMEOUT
                   ) -> str:
        """晋升专用的源仓库出口：全系统唯一能改真实 workspace 的方法。

        argv 钉死成三种形态，其余一律拒绝：
          ["apply", "--check", <patch>]   预检（必须先过）
          ["apply", <patch>]              落地（未提交：commit/push 不存在）
          ["diff", "--no-color", "HEAD"]  落地后的实际改动（reverse.patch 素材）
        patch 必须是已存在的文件；cwd 永远是源仓库根（调用方指定不了）。
        """
        if not (isinstance(args, list) and len(args) >= 2
                and all(isinstance(a, str) and a for a in args)):
            raise GitError(f"source git needs argv list, got {args!r}")
        head = args[0]
        shapes = (
            head == "apply" and (
                (len(args) == 3 and args[1] == "--check")
                or len(args) == 2),
            head == "diff" and args[1:] == ["--no-color", "HEAD"],
        )
        if not any(shapes):
            raise GitError(f"blocked source git command: git "
                           f"{' '.join(args)} (promote allows only "
                           f"apply [--check] <patch> / diff --no-color HEAD)")
        if head == "apply":
            patch = Path(args[-1])
            if not patch.is_file():
                raise GitError(f"source apply: patch not found: {patch}")
        cwd = self.repo
        cmd = ["git", "-c", "core.quotepath=false"] + args
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                                  text=True, timeout=timeout,
                                  errors="replace")
        except subprocess.TimeoutExpired as e:
            raise GitError(f"source git {head} timed out "
                           f"({timeout}s)") from e
        except OSError as e:
            raise GitError(f"source git {head} failed: {e}") from e
        if self.rec:
            self.rec.tool(f"sandbox-git:source:{head}"
                          + (" --check" if "--check" in args else ""))
        if proc.returncode != 0:
            raise GitError(f"source git {head} exited "
                           f"{proc.returncode}: {proc.stderr.strip()[:300]}")
        out = proc.stdout
        if len(out) > _OUTPUT_CAP:
            out = out[:_OUTPUT_CAP] + f"\n... [truncated {len(out)} chars]"
        return out
