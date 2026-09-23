"""Split a commit into semantic ChangeUnits.

Pipeline:  commit → hunks → (symbol mapping) → (domain signals) → union-find
clustering → ChangeUnits.

Signal sources (accumulating by mode, which is the ablation):
- lexical:          changed-line tokens + UI strings only
- structural:       + touched symbol names, call-graph binding (a hunk in a
                    caller of a symbol another hunk modifies binds them)
- structural_git:   + commit message tokens, co-change file history
- semantica:        + graph-neighborhood evidence (Phase 5)

Domain catalog is intentionally small and documented — it encodes "what kinds
of changes get mixed in one commit", not repo-specific knowledge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.errors import DataAgentError
from src.git_history.api import CommitDiff, GitAPI, Hunk
from src.schema import ChangeUnit, Evidence, HunkRef, ToolRecorder
from src.structural.index import CodeIndex

HAS_CJK = re.compile(r"[一-鿿]")

# domain -> trigger tokens (lowercase; CJK matched by substring)
DOMAIN_SIGNALS: dict[str, list[str]] = {
    "auth": ["login", "signin", "sign_in", "logout", "password", "token", "session",
             "auth", "register", "captcha", "登录", "登出", "注册", "验证", "鉴权", "密码", "用户"],
    "title": ["title", "metadata", "logo", "branding", "app_name", "site_name",
              "标题", "系统名", "名称"],
    "ui": ["css", "classname", "style", "color", "theme", "样式", "颜色", "布局"],
    "deps": ["package.json", "dependenc", "lockfile", "依赖"],
    "docs": ["readme", "docs", "changelog", "注释", "文档"],
    "api": ["route", "endpoint", "controller", "接口"],
    "data": ["schema", "prisma", "migration", "sql", "数据库", "表结构"],
}
# how many signal hits a source must have to *bind* hunks across files
BIND_MIN_HITS = 2
SAME_SYMBOL_BIND = True


@dataclass
class HunkInfo:
    hunk: Hunk
    symbols: list[str] = field(default_factory=list)      # touched symbol qualified names
    ui_strings_added: list[str] = field(default_factory=list)
    ui_strings_removed: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)          # lowercase identifiers
    cjk: set[str] = field(default_factory=set)             # CJK substrings found
    file: str = ""
    deleted_file: bool = False
    added_file: bool = False

    def domain_strengths(self) -> dict[str, int]:
        """Concrete trigger-hit counts per domain (deterministic, ordered by catalog)."""
        blob_parts = [" ".join(self.symbols), self.file, " ".join(sorted(self.tokens)),
                      " ".join(sorted(self.cjk))]
        blob = " ".join(blob_parts).lower()
        hits: dict[str, int] = {}
        for dom, triggers in DOMAIN_SIGNALS.items():
            n = sum(1 for t in triggers if t in blob)
            # count extra evidence: token-level exact hits (e.g. 'title' token)
            n += sum(1 for t in self.tokens if t in triggers)
            if n:
                hits[dom] = n
        return hits

    def domains(self) -> set[str]:
        return set(self.domain_strengths())


_STOP = {"the", "a", "an", "and", "or", "if", "for", "let", "const", "return", "await",
         "async", "function", "import", "from", "export", "default", "new", "this",
         "true", "false", "null", "undefined", "string", "number", "boolean"}


def characterize_hunks(diff: CommitDiff, idx: CodeIndex | None, rec: ToolRecorder,
                       use_git_signals: bool = False, git_api: GitAPI | None = None
                       ) -> list[HunkInfo]:
    infos: list[HunkInfo] = []
    added = set(diff.added_files)
    deleted = set(diff.deleted_files)
    for h in diff.hunks:
        info = HunkInfo(hunk=h, file=h.file,
                        added_file=h.file in added, deleted_file=h.file in deleted)
        for tag, text in h.lines:
            if tag == "\\":
                continue   # "\ No newline" 修饰行：不参与 token/串提取
            if tag == "+":
                info.ui_strings_added += _cjk_strings(text)
            elif tag == "-":
                info.ui_strings_removed += _cjk_strings(text)
            for tok in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]{2,}", text):
                t = tok.lower()
                if t not in _STOP:
                    info.tokens.add(t)
            for run in re.findall(r"[一-鿿]{2,}", text):
                info.cjk.add(run)
        if idx is not None and not info.deleted_file:
            lo = h.new_start
            hi = h.new_start + max(h.new_lines - 1, 0)
            seen: set[str] = set()
            for ln in range(lo, hi + 1):
                sym = idx.symbol_at(h.file, ln)
                if sym and sym.qualified not in seen:
                    seen.add(sym.qualified)
            info.symbols = sorted(seen)
        infos.append(info)
    rec.tool("change_units:characterize")
    return infos


def _cjk_strings(text: str) -> list[str]:
    return [m for m in re.findall(r"[\"'`][^\"'`]*[一-鿿][^\"'`]*[\"'`]", text)][:5]


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def cluster_into_units(commit_sha: str, infos: list[HunkInfo], idx: CodeIndex | None,
                       rec: ToolRecorder) -> list[ChangeUnit]:
    """Union hunks by: same symbol, same file+domain, cross-file domain bind,
    and (structural) call-graph binding between touched symbols."""
    n = len(infos)
    uf = _UnionFind(n)
    domain_sets = [i.domains() for i in infos]
    symbol_owner: dict[str, int] = {}
    call_syms: set[str] = set()
    if idx is not None:
        for info in infos:
            for q in info.symbols:
                call_syms.add(q)

    for a in range(n):
        for b in range(a + 1, n):
            ia, ib = infos[a], infos[b]
            bind = False
            # 1. same touched symbol
            if SAME_SYMBOL_BIND and set(ia.symbols) & set(ib.symbols):
                bind = True
            # 2. same file + shared domain
            elif ia.file == ib.file and domain_sets[a] & domain_sets[b]:
                bind = True
            # 3. cross-file domain bind: strong shared domain signal
            elif (domain_sets[a] & domain_sets[b]) and _domain_strength(domain_sets[a] & domain_sets[b],
                                                                        ia, ib) >= BIND_MIN_HITS:
                bind = True
            # 4. structural: hunk B's file is caller/callee of hunk A's symbols
            elif idx is not None and _callgraph_bind(ia, ib, idx, call_syms):
                bind = True
            if bind:
                uf.union(a, b)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    units: list[ChangeUnit] = []
    for k, (root, members) in enumerate(sorted(groups.items()), start=1):
        files = sorted({infos[m].file for m in members})
        # label by summed trigger strength; ties break by catalog order (stable)
        strength: dict[str, int] = {}
        for m in members:
            for d, n in infos[m].domain_strengths().items():
                strength[d] = strength.get(d, 0) + n
        label = max(DOMAIN_SIGNALS.keys(), key=lambda d: (strength.get(d, 0),
                                                          -list(DOMAIN_SIGNALS).index(d))) \
            if strength else "mixed"
        syms = sorted({q for m in members for q in infos[m].symbols})
        ui_add = [s for m in members for s in infos[m].ui_strings_added]
        ui_rm = [s for m in members for s in infos[m].ui_strings_removed]
        hunks = [HunkRef(file=infos[m].file, hunk_idx=infos[m].hunk.idx,
                         old_start=infos[m].hunk.old_start, old_lines=infos[m].hunk.old_lines,
                         new_start=infos[m].hunk.new_start, new_lines=infos[m].hunk.new_lines,
                         header=infos[m].hunk.header)
                 for m in members]
        summary = _unit_summary(label, files, ui_add, ui_rm, syms)
        ev = [Evidence(kind="diff_hunk", source=f"{infos[m].file}@{infos[m].hunk.new_start}",
                       detail=f"hunk {''.join(t for t, _ in infos[m].hunk.lines if t in '+-')[:40]!r}",
                       snippet="".join(f"{t}{x}\n" for t, x in infos[m].hunk.lines if t in "+-")[:400])
              for m in members]
        units.append(ChangeUnit(
            unit_id=f"{commit_sha[:8]}-U{k}", commit=commit_sha, summary=summary,
            semantic_label=label, files=files, hunks=hunks, symbols=syms,
            ui_strings=(ui_add + ui_rm)[:10], evidence=ev,
            cohesion=round(len(members) / max(len({infos[m].file for m in members}), 1), 2)))
    rec.tool("change_units:cluster")
    return units


def _domain_strength(shared: set[str], ia: HunkInfo, ib: HunkInfo) -> int:
    """Count concrete trigger evidence behind the shared domains."""
    strength = 0
    for dom in shared:
        triggers = DOMAIN_SIGNALS[dom]
        for info in (ia, ib):
            blob = " ".join([info.file, " ".join(info.symbols), " ".join(info.tokens)])
            strength += sum(1 for t in triggers if t in blob.lower())
    return strength


def _callgraph_bind(ia: HunkInfo, ib: HunkInfo, idx: CodeIndex, call_syms: set[str]) -> bool:
    """A's touched symbols are called within B's file (or vice versa)."""
    for qa in ia.symbols:
        sym = _resolve(idx, qa)
        if sym is None:
            continue
        for e in idx.callers_of(sym):
            if e.caller_file == ib.file:
                return True
    for qb in ib.symbols:
        sym = _resolve(idx, qb)
        if sym is None:
            continue
        for e in idx.callers_of(sym):
            if e.caller_file == ia.file:
                return True
    return False


def _resolve(idx: CodeIndex, qualified: str) -> object | None:
    file, _, name = qualified.partition("::")
    name = name.split(".")[0]
    for s in idx.symbols_by_name.get(name, []):
        if s.file == file:
            return s
    return None


def _unit_summary(label: str, files: list[str], ui_add: list[str], ui_rm: list[str],
                  syms: list[str]) -> str:
    bits = [f"{label} change"]
    if ui_add or ui_rm:
        shown = (ui_add + ui_rm)[:2]
        bits.append("UI copy: " + " / ".join(s.strip("\"'`")[:40] for s in shown))
    if syms:
        bits.append("symbols: " + ", ".join(s.split("::")[-1] for s in syms[:4]))
    bits.append(f"files: {', '.join(files[:4])}")
    return " | ".join(bits)
