"""SandboxGit index 隔离护栏（12A 引入的写 index 命令）。

content_drift（13E 内容级核对）需要 read-tree / apply --cached 物化
期望内容 —— 这两个命令会写 index。护栏：不带隔离 GIT_INDEX_FILE 的
调用直接拒绝（否则会覆写 worktree/源仓库的真 index）。
"""
import subprocess

import pytest

from src.errors import GitError
from src.maintenance.sandbox_git import SandboxGit


def _mini_repo(tmp_path) -> object:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "f.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(r), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"],
                   check=True)
    return r


def test_read_tree_requires_isolated_index(tmp_path):
    sg = SandboxGit(_mini_repo(tmp_path))
    with pytest.raises(GitError, match="isolated"):
        sg.run(["read-tree", "HEAD"], cwd=sg.repo)


def test_apply_cached_requires_isolated_index(tmp_path):
    sg = SandboxGit(_mini_repo(tmp_path))
    with pytest.raises(GitError, match="isolated"):
        sg.run(["apply", "--cached", "x.patch"], cwd=sg.repo)


def test_isolated_index_read_tree_ls_files_roundtrip(tmp_path):
    """正路：隔离 index 上 read-tree → ls-files -s 列出 base 树。"""
    repo = _mini_repo(tmp_path)
    sg = SandboxGit(repo)
    sg.register_worktree(repo)      # sandbox-only 命令要求注册过的 cwd
    idx = {"GIT_INDEX_FILE": str(tmp_path / "expected-index")}
    sg.run(["read-tree", "HEAD"], cwd=repo, env=idx)
    out = sg.run(["ls-files", "-s"], cwd=repo, env=idx)
    assert "f.txt" in out
    # 临时 index 不影响仓库本体：真 index 里 f.txt 仍在（status 干净）
    st = sg.run(["status", "--porcelain"], cwd=repo)
    assert st.strip() == ""
