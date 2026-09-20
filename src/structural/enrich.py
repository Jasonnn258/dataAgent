"""Structural enrichment of locate candidates (mode>=structural).

Three contributions:
1. attach symbol identity (name/kind/exact range) to line-level candidates
2. inject *definition candidates* for exact symbol names in the query
3. convention knowledge: Next.js root layout is the canonical home of the app
   title/metadata — a decisive signal lexical search cannot have.
"""
from __future__ import annotations

from pathlib import Path

from src.schema import Evidence, LocatedCandidate, ToolRecorder
from src.search.keywords import QueryTerms
from src.structural.index import CodeIndex

TITLE_TERMS = {"标题", "title", "系统名", "sitename", "app_name"}
STRUCT_BOOST_SYMBOL_EXACT = 6.0
STRUCT_BOOST_ROOT_LAYOUT_TITLE = 5.0
STRUCT_SAME_SYMBOL = 1.5


_idx_cache: dict[str, CodeIndex] = {}


def build_index(repo: Path, rec: ToolRecorder) -> CodeIndex:
    """Build (or reuse in-process) the structural index for a repo.

    Analysis is read-only, so caching by repo path is safe; it makes the
    4-mode x N-task evaluation matrix build the index once per repo.
    """
    key = str(repo.resolve())
    idx = _idx_cache.get(key)
    if idx is None:
        idx = CodeIndex(repo, rec).build()
        _idx_cache[key] = idx
    return idx


def enrich_locate_candidates(repo: Path, cands: list[LocatedCandidate],
                             rec: ToolRecorder, qt: QueryTerms | None = None) -> list[LocatedCandidate]:
    idx = build_index(repo, rec)
    root_layout_files = {s.file for s in idx.root_layouts}
    wants_title = bool(qt and ({*qt.cjk_segments, *qt.cjk_subterms} & {"标题", "系统标题", "标题栏"}
                               or any(t.lower() in TITLE_TERMS for t, _ in (qt.all_search_terms() if qt else []))))

    out: list[LocatedCandidate] = []
    for c in cands:
        mid = (c.line_start or 1) if c.line_end is None else (c.line_start + c.line_end) // 2
        sym = idx.symbol_at(c.file, mid)
        if sym is not None:
            c.symbol = sym.name
            c.kind = sym.kind if sym.kind != "arrow_const" else "function"
            c.line_start, c.line_end = sym.line_start, sym.line_end
            c.score = round(c.score + STRUCT_SAME_SYMBOL, 3)
            c.reason += f" | sym: {sym.kind} {sym.name}"
            c.evidence.append(Evidence(kind="symbol_def", source=f"{sym.file}:{sym.line_start}-{sym.line_end}",
                                       detail=f"{sym.kind} {sym.name}{'' if sym.exported else ' (not exported)'}",
                                       snippet=sym.snippet))
        if wants_title and c.file in root_layout_files:
            c.score = round(c.score + STRUCT_BOOST_ROOT_LAYOUT_TITLE, 3)
            c.reason += " | nextjs root layout (app title/metadata home)"
            c.evidence.append(Evidence(kind="convention", source=c.file,
                                       detail="root layout.tsx owns the app <title>/metadata"))
        out.append(c)

    # exact symbol-name candidates from the query itself
    if qt:
        for name in qt.en_terms:
            if len(name) < 3:
                continue
            for sym in idx.find_symbols(name):
                out.append(LocatedCandidate(
                    file=sym.file, symbol=sym.name,
                    kind=sym.kind if sym.kind != "arrow_const" else "function",
                    line_start=sym.line_start, line_end=sym.line_end,
                    score=round(sym.line_end - sym.line_start + STRUCT_BOOST_SYMBOL_EXACT, 3),
                    reason=f"structural: exact definition of '{name}'",
                    evidence=[Evidence(kind="symbol_def", source=f"{sym.file}:{sym.line_start}",
                                       detail=f"{sym.kind} {sym.name}", snippet=sym.snippet)]))
    return out
