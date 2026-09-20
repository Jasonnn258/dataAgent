"""Portable lexical search backend (the *only* context source in mode=lexical).

Uses ripgrep when installed (`rg`), otherwise an equivalent pure-Python walker.
Both paths return the same Match objects; behaviour differences are recorded
as warnings, never silently ignored.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.errors import SearchError
from src.schema import ToolRecorder

IGNORE_DIRS = {
    ".git", "node_modules", ".next", "dist", "build", "out", "coverage",
    ".cache", ".turbo", "venv", "__pycache__", ".pytest_cache", ".claude",
    "skills",  # vendored agent-skill packs (tooling, not app code under analysis)
}
SKIP_FILENAMES = {
    "package-lock.json", "bun.lock", "yarn.lock", "pnpm-lock.yaml",
    "next-env.d.ts", ".DS_Store",
}
TEXT_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css", ".scss",
    ".html", ".md", ".prisma", ".sql", ".sh", ".py", ".yaml", ".yml", ".env",
}
MAX_FILE_BYTES = 512 * 1024

# hit-type weights (documented scoring contract for the lexical baseline)
W_FILENAME = 6.0
W_STRING = 4.0
W_CJK_VERBATIM = 4.0
W_IDENTIFIER = 3.0
W_PLAIN = 2.0
W_COMMENT = 1.2

# First-stage scope is TS/TSX/JS repos: code files rank at full weight,
# other artifacts (docs, scripts, styles) get a demotion prior.
EXTENSION_WEIGHT = {
    ".ts": 1.0, ".tsx": 1.0, ".js": 1.0, ".jsx": 1.0, ".mjs": 1.0, ".cjs": 1.0,
    ".prisma": 0.9, ".sql": 0.9, ".json": 0.8, ".css": 0.7, ".scss": 0.7,
    ".html": 0.6, ".md": 0.6, ".py": 0.6, ".sh": 0.6, ".yaml": 0.6, ".yml": 0.6,
}
MAX_GROUPS_PER_FILE = 3  # candidate diversity for file-level recall@k


def extension_weight(path: str) -> float:
    return EXTENSION_WEIGHT.get(Path(path).suffix.lower(), 0.6)


@dataclass
class Match:
    file: str          # repo-relative posix path
    line_no: int       # 1-based
    col: int           # 0-based
    term: str
    origin: str        # cjk:seg | expand:seg | en:term
    line_text: str


class LexicalSearcher:
    def __init__(self, repo: Path, rec: ToolRecorder):
        self.repo = repo
        self.rec = rec
        self.rg = shutil.which("rg")
        if not self.rg:
            self.rec.warn("ripgrep not found; using pure-python fallback searcher")

    # -------------------------------------------------------------- files
    def iter_files(self) -> list[Path]:
        import os
        self.rec.tool("walk_files")
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.repo):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if fn in SKIP_FILENAMES:
                    continue
                if p.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                try:
                    if p.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                files.append(p)
        return files

    # -------------------------------------------------------------- search
    def search(self, terms: list[tuple[str, str]]) -> list[Match]:
        """Search all (term, origin) pairs. CJK terms matched verbatim;
        ASCII terms matched on word boundaries, case-insensitive."""
        if not terms:
            return []
        matches: list[Match] = []
        if self.rg:
            matches = self._search_rg(terms)
        else:
            matches = self._search_python(terms)
        matches.sort(key=lambda m: (m.file, m.line_no, m.col))
        return matches

    def _search_rg(self, terms: list[tuple[str, str]]) -> list[Match]:
        regex = "|".join(
            (re.escape(t) if _has_cjk(t) else r"\b(?:" + re.escape(t) + r")\b")
            for t, _ in terms
        )
        self.rec.tool("rg")
        try:
            proc = subprocess.run(
                [self.rg, "--no-heading", "--line-number", "--column", "--smart-case",
                 "-e", regex, "."],
                cwd=self.repo, capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise SearchError(f"rg backend failed: {e}") from e
        if proc.returncode not in (0, 1):
            raise SearchError(f"rg exited {proc.returncode}: {proc.stderr[:300]}")
        term_list = [t for t, _ in terms]
        matches = []
        for line in proc.stdout.splitlines():
            m = re.match(r"^(.+?):(\d+):(\d+):(.*)$", line)
            if not m:
                continue
            f, ln, col, text = m.group(1), int(m.group(2)), int(m.group(3)) - 1, m.group(4)
            hit = _first_matching_term(text, term_list)
            if hit is None:
                continue
            matches.append(Match(file=f, line_no=ln, col=col, term=hit,
                                 origin=_origin_of(hit, terms), line_text=text))
        return matches

    def _search_python(self, terms: list[tuple[str, str]]) -> list[Match]:
        cjk_terms = [(t, o) for t, o in terms if _has_cjk(t)]
        ascii_terms = [(t, o) for t, o in terms if not _has_cjk(t)]
        compiled = [(re.compile(r"\b" + re.escape(t) + r"\b", re.I), t, o) for t, o in ascii_terms]
        matches: list[Match] = []
        for p in self.iter_files():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self.rec.warn(f"unreadable file skipped: {p}: {e}")
                continue
            rel = p.relative_to(self.repo).as_posix()
            for i, line in enumerate(text.splitlines(), start=1):
                for t, o in cjk_terms:
                    col = line.find(t)
                    if col >= 0:
                        matches.append(Match(rel, i, col, t, o, line))
                        break
                for rx, t, o in compiled:
                    m = rx.search(line)
                    if m:
                        matches.append(Match(rel, i, m.start(), t, o, line))
                        break
        self.rec.tool("py_search")
        return matches


def _has_cjk(s: str) -> bool:
    return bool(re.search(r"[一-鿿]", s))


def _first_matching_term(line: str, terms: list[str]) -> str | None:
    for t in terms:
        if _has_cjk(t):
            if t in line:
                return t
        else:
            if re.search(r"\b" + re.escape(t) + r"\b", line, re.I):
                return t
    return None


def _origin_of(term: str, terms: list[tuple[str, str]]) -> str:
    for t, o in terms:
        if t == term:
            return o
    return "?"


def classify_hit(line_text: str, col: int, term: str) -> tuple[str, float]:
    """Classify a line hit into (kind, weight). Used by scoring in locate."""
    stripped = line_text.lstrip()
    if stripped.startswith(("//", "*", "/*", "#")):
        return "comment", W_COMMENT
    # inside a quoted segment that closes after the hit -> string literal / UI copy
    opens = re.search(r"""['"`][^'"`]*$""", line_text[:col])
    closes = re.match(r"""^[^'"`]*['"`]""", line_text[col + len(term):])
    if opens and closes:
        return "string", W_STRING
    if _has_cjk(term):
        return "ui_text", W_CJK_VERBATIM
    after = line_text[col + len(term):col + len(term) + 1]
    before = line_text[col - 1] if col > 0 else ""
    if after and (after.isalnum() or after in "_$") or (before and (before.isalnum() or before in "_$")):
        return "identifier", W_IDENTIFIER
    return "plain", W_PLAIN


def filename_bonus(rel_path: str, terms: list[tuple[str, str]]) -> float:
    stem = Path(rel_path).stem.lower()
    total = 0.0
    for t, o in terms:
        t_low = t.lower()
        if _has_cjk(t):
            continue
        if t_low in stem:
            total += W_FILENAME
        elif t_low in rel_path.lower():
            total += W_FILENAME / 2
    return total
