"""Task 2: change impact analysis.

"修改 X 会影响什么?" — resolve target, then report:
target definition / direct callers / callees / importing files /
indirectly affected (2-hop) / related API routes & tests.

Mode ablation:
- lexical: text search only. Callers are raw `name(` occurrences — no scoping,
  no import awareness, no callees, no routes. (This weakness is the point.)
- structural+: full call/import graph + Next.js route conventions.
- structural_git / semantica: Phase 3/5 add co-change evidence.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.config import Settings, MAX_SNIPPET_CHARS
from src.errors import DataAgentError
from src.schema import (Evidence, ImpactResult, ImpactSide, Target, TaskOutput)
from src.search.keywords import extract_terms
from src.search.lexical import LexicalSearcher
from src.tasks import register
from src.tasks.base import ContextItem, TaskRunnerBase


@register("impact")
def run_impact(settings: Settings, query: str, commit: str | None = None) -> TaskOutput:
    if commit:
        raise DataAgentError("impact task does not use --commit")
    return ImpactRunner(settings).run(query)


class ImpactRunner(TaskRunnerBase):
    task = "impact"

    def run(self, query: str) -> TaskOutput:
        # the query for impact is normally a symbol name (possibly in a sentence)
        qt = extract_terms(query)
        name = self._pick_symbol_name(query, qt)

        if self.structural_enabled():
            result = self._structural_impact(name, qt)
        else:
            result = self._lexical_impact(name, qt)

        if self.git_enabled():
            self._git_layer(name, result)
        if self.graph_enabled():
            self._graph_layer(name, result)

        result.task = "impact"
        return TaskOutput(
            task="impact", mode=self.s.mode, query=query, repo=str(self.repo),
            result=result,
            system=self.rec.metrics(self.s.mode, llm_used=False),
        )

    # ---------------------------------------------------------------- target
    @staticmethod
    def _pick_symbol_name(query: str, qt) -> str:
        raw = query.strip().strip('"`')
        # prefer a token that looks like an identifier (camelCase / PascalCase)
        idents = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]{2,}", raw)
        for t in idents:
            if any(ch.isupper() for ch in t[1:]):
                return t
        for t in qt.en_terms:
            if len(t) >= 3:
                return t
        if idents:
            return max(idents, key=len)
        raise DataAgentError(
            f"impact: cannot extract a symbol name from query {query!r} — "
            f"pass e.g. --query generateWithRetry")

    # ---------------------------------------------------------------- git layer
    def _git_layer(self, name: str, result: ImpactResult) -> None:
        try:
            from src.git_history.enrich import enrich_impact_with_git
            tf = result.target.file if result.target else None
            tl = (result.target.line_start, result.target.line_end) if result.target and result.target.line_start else None
            enrich_impact_with_git(self.repo, result, self.rec, tf, tl)
        except ImportError:
            self.rec.warn("git layer not implemented yet (Phase 3)")

    def _graph_layer(self, name: str, result: ImpactResult) -> None:
        from src.semgraph.enrich import enrich_impact_with_graph
        enrich_impact_with_graph(self.repo, name, result, self.rec)

    # ---------------------------------------------------------------- structural
    def _structural_impact(self, name: str, qt) -> ImpactResult:
        from src.structural.enrich import build_index
        idx = build_index(self.repo, self.rec)
        defs = idx.find_symbols(name)
        res = ImpactResult()
        if not defs:
            res.notes.append(f"no definition of '{name}' found in structural index")
            # fall back to lexical guessing for the target file
            return self._lexical_impact(name, qt)

        for sym in defs:
            tgt = Target(type=sym.kind, name=sym.name, file=sym.file,
                         line_start=sym.line_start, line_end=sym.line_end,
                         confidence=1.0 if sym.exported else 0.8,
                         evidence=[Evidence(kind="symbol_def",
                                            source=f"{sym.file}:{sym.line_start}-{sym.line_end}",
                                            detail=f"{sym.kind} {sym.name}", snippet=sym.snippet)])
            if res.target is None:
                res.target = tgt
            else:
                res.notes.append(f"additional definition: {sym.qualified}")

            for e in idx.callers_of(sym):
                res.direct_callers.append(ImpactSide(
                    file=e.caller_file, symbol=e.caller.split("::")[-1], kind="function",
                    relation="caller", via=f"{e.caller} -> {e.callee}",
                    evidence=[Evidence(kind="call_edge", source=f"{e.file}:{e.line}",
                                       detail=f"calls {e.callee}")]))
            for f, ln in idx.jsx_callers_of(sym):
                res.direct_callers.append(ImpactSide(
                    file=f, symbol=sym.name, kind="component", relation="jsx-render",
                    evidence=[Evidence(kind="jsx_ref", source=f"{f}:{ln}",
                                       detail=f"renders <{sym.name}>")]))
            for e in idx.callees_of(sym):
                res.callees.append(ImpactSide(
                    file=e.file, symbol=e.callee_short, kind="function",
                    relation="callee",
                    evidence=[Evidence(kind="call_edge", source=f"{e.file}:{e.line}",
                                       detail=f"calls {e.callee}")]))
            for ri in idx.importers_of_file(sym.file):
                res.importing_files.append(ImpactSide(
                    file=ri.importer, kind="file", relation="imports",
                    evidence=[Evidence(kind="import_edge", source=ri.importer,
                                       detail=f"imports {ri.specifier}")]))
            for caller_q, chain in idx.indirect_callers(sym, depth=2).items():
                res.indirectly_affected.append(ImpactSide(
                    file=caller_q.split("::")[0], symbol=caller_q.split("::")[-1],
                    kind="function", relation="indirect",
                    via=f"2-hop: {caller_q} (via {chain[0] if chain else '?'})",
                    evidence=[Evidence(kind="call_edge_2hop", source=chain[0] if chain else caller_q,
                                       detail=f"reaches {sym.name} transitively")]))
            for ep in idx.api_routes_reaching(sym):
                res.related.append(ImpactSide(
                    file=ep.file, symbol=ep.route_path, kind="api_route",
                    relation="api-route",
                    evidence=[Evidence(kind="api_route", source=ep.file,
                                       detail=f"{ep.route_path} {','.join(ep.methods)}")]))
            for ri in idx.tests_referencing(sym):
                res.related.append(ImpactSide(
                    file=ri.importer, kind="test", relation="test",
                    evidence=[Evidence(kind="import_edge", source=ri.importer,
                                       detail="test file imports target module")]))
        return res

    # ---------------------------------------------------------------- lexical
    def _lexical_impact(self, name: str, qt) -> ImpactResult:
        """No AST: text search for occurrences; classify by line shape."""
        searcher = LexicalSearcher(self.repo, self.rec)
        res = ImpactResult()
        res.notes.append("lexical mode: caller/callee relations are text-pattern guesses, "
                         "no scoping, imports or routes")
        matches = searcher.search([(name, f"en:{name}")])
        def_re = re.compile(
            rf"^\s*(export\s+)?(async\s+)?(function|const|class)\s+{re.escape(name)}\b"
            rf"|^\s*(export\s+)?default\s+function\s+{re.escape(name)}\b", re.M)
        call_re = re.compile(rf"\b{re.escape(name)}\s*\(")
        for m in matches:
            self.rec.context(len(m.line_text), 1)
            self.bundle.add(ContextItem(kind="lexical_match", source=f"{m.file}:{m.line_no}",
                                        text=m.line_text), self.rec, MAX_SNIPPET_CHARS)
            ev = Evidence(kind="lexical_match", source=f"{m.file}:{m.line_no}",
                          detail=f"text hit for '{name}'", snippet=m.line_text.strip()[:300])
            if def_re.search(m.line_text):
                tgt = Target(type="function", name=name, file=m.file,
                             line_start=m.line_no, line_end=m.line_no, confidence=0.6,
                             evidence=[ev])
                if res.target is None:
                    res.target = tgt
                else:
                    res.notes.append(f"possible extra definition: {m.file}:{m.line_no}")
            elif call_re.search(m.line_text) and f"function {name}" not in m.line_text:
                res.direct_callers.append(ImpactSide(
                    file=m.file, kind="function", relation="possible-caller", evidence=[ev]))
            else:
                res.indirectly_affected.append(ImpactSide(
                    file=m.file, kind="file", relation="mentions", evidence=[ev]))
        return res
