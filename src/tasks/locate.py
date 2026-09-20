"""Task 1: natural-language code location.

Mode ablation semantics (identical across all three tasks):
    lexical        -> lexical layer only
    structural     -> + symbol/type enrichment from the AST index
    structural_git -> + files recently/repeatedly touching the query terms
    semantica      -> + context-graph neighborhood evidence

Candidate *generation* is always lexical-seeded (every mode keeps the text
searcher as a fallback); what grows with mode is *evidence and enrichment*,
which is exactly the ablation hypothesis under test.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from src.config import Settings, MAX_SNIPPET_CHARS
from src.errors import DataAgentError
from src.schema import (Evidence, LocatedCandidate, LocateResult, TaskOutput)
from src.search.keywords import QueryTerms, extract_terms
from src.search.lexical import (LexicalSearcher, Match, MAX_GROUPS_PER_FILE,
                                classify_hit, extension_weight, filename_bonus)
from src.tasks import register
from src.tasks.base import ContextItem, TaskRunnerBase

MERGE_WINDOW = 3  # matches within N lines merge into one candidate


@register("locate")
def run_locate(settings: Settings, query: str, commit: str | None = None) -> TaskOutput:
    if commit:
        raise DataAgentError("locate task does not use --commit")
    return LocateRunner(settings).run(query)


class LocateRunner(TaskRunnerBase):
    task = "locate"

    def run(self, query: str) -> TaskOutput:
        self.qt: QueryTerms = extract_terms(query)
        self.searcher = LexicalSearcher(self.repo, self.rec)

        candidates = self._lexical_layer()

        if self.structural_enabled():
            candidates = self._structural_layer(candidates)   # Phase 2 enrichment
        if self.git_enabled():
            candidates = self._git_layer(candidates)          # Phase 3 recency/co-change boost
        if self.graph_enabled():
            candidates = self._graph_layer(candidates)        # Phase 5 graph evidence

        candidates = self._optional_llm_rerank(query, candidates)
        candidates.sort(key=lambda c: c.score, reverse=True)
        k = self.s.top_k
        result = LocateResult(
            query_interpretation=f"terms[{self.qt.summary()}]",
            candidates=candidates[:k],
        )
        return TaskOutput(
            task="locate", mode=self.s.mode, query=query,
            repo=str(self.repo), result=result,
            system=self.rec.metrics(self.s.mode, llm_used=self.llm.available and self._llm_used),
        )

    # ------------------------------------------------------------ layer 1
    def _lexical_layer(self) -> list[LocatedCandidate]:
        terms = self.qt.all_search_terms()
        matches = self.searcher.search(terms)
        for m in matches:
            self.bundle.add(
                ContextItem(kind="lexical_match", source=f"{m.file}:{m.line_no}", text=m.line_text),
                self.rec, MAX_SNIPPET_CHARS)

        # group matches into line-window candidates per file
        by_file: dict[str, list[Match]] = defaultdict(list)
        for m in matches:
            by_file[m.file].append(m)

        out: list[LocatedCandidate] = []
        for file, ms in by_file.items():
            ms.sort(key=lambda m: m.line_no)
            groups: list[list[Match]] = []
            for m in ms:
                if groups and m.line_no - groups[-1][-1].line_no <= MERGE_WINDOW:
                    groups[-1].append(m)
                else:
                    groups.append([m])
            bonus = filename_bonus(file, terms)
            groups.sort(key=lambda g: -len(g))  # denser groups first
            for g in groups[:MAX_GROUPS_PER_FILE]:
                # distinct-term coverage scoring: each distinct term contributes its
                # best hit weight; repetitions add only a small bonus (anti-farming)
                best: dict[str, tuple[float, Match, str]] = {}
                for m in g:
                    kind, w = classify_hit(m.line_text, m.col, m.term)
                    if m.term not in best or w > best[m.term][0]:
                        best[m.term] = (w, m, kind)
                # same-line co-occurrence: ≥2 distinct terms on one line is a strong
                # signal the line *is* the sought thing (e.g. title + 系统)
                line_terms: dict[int, set[str]] = {}
                for m in g:
                    line_terms.setdefault(m.line_no, set()).add(m.term)
                co_lines = {ln for ln, ts in line_terms.items() if len(ts) >= 2}
                score = 0.0
                for w, m, kind in best.values():
                    score += w * (1.3 if m.line_no in co_lines else 1.0)
                for w, _, _ in best.values():
                    score += min(0.25, w / 16.0)  # tiny repetition credit
                reasons, evs = [], []
                for w, m, kind in sorted(best.values(), key=lambda x: -x[0]):
                    reasons.append(f"{m.term}({kind}@L{m.line_no})")
                    evs.append(Evidence(kind="lexical_match", source=f"{m.file}:{m.line_no}",
                                        detail=f"term '{m.term}' [{m.origin}] hit as {kind}",
                                        snippet=m.line_text.strip()[:300]))
                score += bonus
                if bonus:
                    reasons.append(f"filename~{Path(file).stem}")
                score = round(score * extension_weight(file), 3)
                out.append(LocatedCandidate(
                    file=file, line_start=g[0].line_no, line_end=g[-1].line_no,
                    score=round(score, 3), reason="lexical: " + ", ".join(reasons[:6]),
                    evidence=evs[:6],
                ))
        return out

    # ------------------------------------------------- layers 2..4 (later phases)
    def _structural_layer(self, cands: list[LocatedCandidate]) -> list[LocatedCandidate]:
        try:
            from src.structural.enrich import enrich_locate_candidates
            return enrich_locate_candidates(self.repo, cands, self.rec)
        except ImportError:
            self.rec.warn("structural layer not implemented yet (Phase 2)")
            return cands

    def _git_layer(self, cands: list[LocatedCandidate]) -> list[LocatedCandidate]:
        try:
            from src.git_history.enrich import enrich_locate_with_git
            return enrich_locate_with_git(self.repo, cands, self.qt, self.rec)
        except ImportError:
            self.rec.warn("git layer not implemented yet (Phase 3)")
            return cands

    def _graph_layer(self, cands: list[LocatedCandidate]) -> list[LocatedCandidate]:
        try:
            from src.semgraph.enrich import enrich_locate_with_graph
            return enrich_locate_with_graph(self.repo, cands, self.qt, self.rec)
        except ImportError:
            self.rec.warn("graph layer not implemented yet (Phase 5)")
            return cands

    # ------------------------------------------------------------ LLM (optional)
    _llm_used = False

    def _optional_llm_rerank(self, query: str, cands: list[LocatedCandidate]) -> list[LocatedCandidate]:
        if not self.llm.available or not cands:
            return cands
        lines = [f"{i}. {c.file}:{c.line_start}-{c.line_end} score={c.score} :: "
                 + (c.evidence[0].snippet if c.evidence else "")
                 for i, c in enumerate(cands[:20])]
        user = (f"Query: {query}\nCandidates (file:lines, snippet):\n"
                + "\n".join(lines)
                + '\nReturn JSON {"picks": [{"idx": int, "score": 0..1, "reason": str}]}'
                  " for the candidates that actually answer the query.")
        try:
            data = self.llm.chat_json(
                "You are a precise code-location ranker. Judge only from given evidence.",
                user)
        except Exception as e:  # LLM failure must not sink the run, but must be visible
            self.rec.warn(f"LLM rerank failed, keeping heuristic order: {e}")
            return cands
        self._llm_used = True
        picked = {}
        for p in data.get("picks", []):
            if isinstance(p, dict) and isinstance(p.get("idx"), int):
                picked[p["idx"]] = (float(p.get("score", 0.5)), str(p.get("reason", "")))
        for i, c in enumerate(cands):
            if i in picked:
                sc, reason = picked[i]
                c.score = round(c.score * 0.5 + sc * 10.0, 3)  # blend, LLM opinion weighted
                c.reason += f" | llm: {reason}"
        return cands
