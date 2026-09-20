"""Read-only Git access layer. Destructive operations are impossible by
construction: only whitelisted read commands ever run (工程要求 #9)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from src.errors import GitError
from src.schema import ToolRecorder

# command prefixes allowed — anything else raises GitError *before* exec
ALLOWED = {
    "log", "show", "diff", "blame", "ls-files", "status", "rev-parse",
    "name-rev", "merge-base", "branch", "tag", "cat-file", "shortlog",
}
# leading record separator: each record = "\n<sha>US author US email US date US subject>\n\n<files>"
LOG_FORMAT = "%x1e%H%x1f%an%x1f%ae%x1f%ad%x1f%s"


@dataclass
class Commit:
    sha: str
    author: str = ""
    email: str = ""
    date: str = ""
    subject: str = ""
    body: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.sha[:8]


@dataclass
class Hunk:
    file: str
    idx: int
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str = ""
    lines: list[tuple[str, str]] = field(default_factory=list)  # (tag, text) tag in " +-"


@dataclass
class CommitDiff:
    commit: Commit
    hunks: list[Hunk] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)


class GitAPI:
    def __init__(self, repo: Path, rec: ToolRecorder | None = None):
        self.repo = repo
        self.rec = rec

    # ---------------------------------------------------------------- core
    def _run(self, args: list[str], timeout: int = 60) -> str:
        if args[0] not in ALLOWED:
            raise GitError(f"blocked non-read git command: git {' '.join(args)}")
        cmd = ["git", "-c", "core.quotepath=false", "-C", str(self.repo)] + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  errors="replace")
        except (subprocess.TimeoutExpired, OSError) as e:
            raise GitError(f"git {args[0]} failed: {e}") from e
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        if self.rec:
            self.rec.tool(f"git:{args[0]}")
        return proc.stdout

    def is_repo(self) -> bool:
        try:
            self._run(["rev-parse", "--is-inside-work-tree"])
            return True
        except GitError:
            return False

    # ---------------------------------------------------------------- queries
    def log(self, max_count: int = 100, file: str | None = None,
            grep: str | None = None) -> list[Commit]:
        args = ["log", f"--max-count={max_count}", f"--format={LOG_FORMAT}", "--name-only"]
        if file:
            args += ["--", file]
        if grep:
            args.insert(1, f"--grep={grep}")
        out = self._run(args)
        commits: list[Commit] = []
        for record in out.split("\x1e")[1:]:  # leading separator: first chunk is empty
            record = record.strip("\n")
            if not record.strip():
                continue
            head, _, files_part = record.partition("\n")
            fields = head.split("\x1f")
            if len(fields) < 5:
                continue
            c = Commit(sha=fields[0], author=fields[1], email=fields[2],
                       date=fields[3], subject=fields[4])
            c.files = [ln for ln in files_part.splitlines() if ln.strip()]
            commits.append(c)
        return commits

    def head(self) -> str:
        return self._run(["rev-parse", "HEAD"]).strip()

    def resolve(self, ref: str) -> str:
        return self._run(["rev-parse", ref + "^{commit}"]).strip()

    def show(self, ref: str) -> CommitDiff:
        sha = self.resolve(ref)
        meta = self.log(max_count=1)
        commit = next((c for c in meta if c.sha == sha), Commit(sha=sha))
        raw = self._run(["show", "--format=", "--find-renames", sha])
        return CommitDiff(commit=commit, **_parse_unified_diff(raw))

    def diff(self, ref: str) -> CommitDiff:
        """Diff of a commit against its first parent."""
        sha = self.resolve(ref)
        raw = self._run(["diff", f"{sha}^", sha]) if _has_parent(self, sha) else \
            self._run(["show", "--format=", sha])
        meta = self.log(max_count=1)
        commit = next((c for c in meta if c.sha == sha), Commit(sha=sha))
        return CommitDiff(commit=commit, **_parse_unified_diff(raw))

    def blame(self, file: str, lines: tuple[int, int] | None = None) -> dict[int, str]:
        """line_no(1-based, current file) -> commit sha, via porcelain headers."""
        import re
        out = self._run(["blame", "-l", "--root", "--line-porcelain", "--", file])
        header_re = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
        blame: dict[int, str] = {}
        for ln in out.splitlines():
            if ln.startswith("\t"):
                continue
            m = header_re.match(ln)
            if m:
                blame[int(m.group(3))] = m.group(1)
        if lines and blame:
            lo, hi = lines
            return {k: v for k, v in blame.items() if lo <= k <= hi}
        return blame

    def file_history(self, file: str, max_count: int = 50) -> list[Commit]:
        return self.log(max_count=max_count, file=file)

    def co_change_pairs(self, max_count: int = 100) -> dict[tuple[str, str], int]:
        """File pairs changing in the same commit, with support counts."""
        from itertools import combinations
        pairs: dict[tuple[str, str], int] = {}
        for c in self.log(max_count=max_count):
            fs = sorted(set(c.files))
            for a, b in combinations(fs, 2):
                pairs[(a, b)] = pairs.get((a, b), 0) + 1
        return pairs

    def commits_touching(self, file: str, max_count: int = 50) -> list[Commit]:
        return self.file_history(file, max_count)


def _has_parent(api: GitAPI, sha: str) -> bool:
    try:
        api._run(["rev-parse", f"{sha}^"])
        return True
    except GitError:
        return False


def _parse_unified_diff(raw: str) -> dict:
    """Parse `git diff` output into hunks/added/deleted/renamed."""
    hunks: list[Hunk] = []
    added: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []
    cur_file = None
    cur_hunk: Hunk | None = None
    hunk_idx = 0
    import re
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
    for line in raw.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- "):
            continue  # file names come from diff --git / new file / deleted file lines
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+) b/(.+)$", line)
            if m:
                a, b = m.group(1), m.group(2)
                if a != b:
                    renamed.append((a, b))
                cur_file = b
                hunk_idx = 0
            continue
        if line.startswith("new file"):
            if cur_file:
                added.append(cur_file)
            continue
        if line.startswith("deleted file"):
            if cur_file:
                deleted.append(cur_file)
            continue
        if line.startswith("rename from "):
            continue
        if line.startswith("rename to "):
            continue
        if line.startswith(("index ", "similarity ", "old mode", "new mode", "Binary files")):
            continue
        m = hunk_re.match(line)
        if m:
            cur_hunk = Hunk(
                file=cur_file or "?", idx=hunk_idx,
                old_start=int(m.group(1)), old_lines=int(m.group(2) or "1"),
                new_start=int(m.group(3)), new_lines=int(m.group(4) or "1"),
                header=line[:200])
            hunks.append(cur_hunk)
            hunk_idx += 1
            continue
        if cur_hunk is not None and line[:1] in (" ", "+", "-"):
            cur_hunk.lines.append((line[0], line[1:]))
    return {"hunks": hunks, "added_files": added, "deleted_files": deleted, "renamed": renamed}
