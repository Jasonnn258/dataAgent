"""RepositoryNavigator (Phase 9J): fuzzy natural language -> feature target.

The only agent where an LLM may participate — and even here it may only
pick among existing feature names (SemanticMapper contract); every pick
stays a candidate until deterministic tools corroborate it. A failed
navigation is loud: DataAgentError, never a silent empty result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.objects import Evidence, EvidenceType, Finding
from src.semgraph.semantic_mapper import SemanticMapper, SemanticTargetCandidate


@dataclass
class NavigationResult:
    feature_id: str = ""
    feature_name: str = ""
    related_symbols: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)     # query terms for CI
    finding_id: str = ""
    candidates: list[SemanticTargetCandidate] = field(default_factory=list)


class RepositoryNavigator:
    ROLE = "RepositoryNavigator"
    READS = ["map_query", "resolve_target", "add_evidence", "add_finding"]

    def __init__(self, broker, llm=None):
        self.broker = broker
        self.mapper = SemanticMapper(broker.graph, broker.rec)
        self.mapper.seed_deterministic()
        self.llm = llm

    def find_target(self, query: str, scope=None) -> NavigationResult:
        self.broker.rec.tool(f"agent:{self.ROLE}:navigate")
        cands = self.mapper.map_query(query, llm=self.llm)
        if not cands:
            # no feature matched — deterministic symbol/filename fallback
            node = self.broker.resolve_target(query)
            ev = Evidence.make(EvidenceType.AST, source="navigator:resolve",
                               target=node.id,
                               payload=f"fallback resolution to {node.id}")
            self.broker.add_evidence(ev)
            f = self.broker.add_finding(Finding.make(
                f"query targets {node.id}", self.ROLE, [ev.id]))
            if scope:
                scope.produced(evidence=[ev.id], finding=f.id)
            return NavigationResult(feature_id=node.id,
                                    feature_name=node.props.get("name", ""),
                                    related_symbols=[node.id],
                                    terms=[query], finding_id=f.id,
                                    candidates=[])
        top = cands[0]
        for ev in top.evidence:
            self.broker.add_evidence(ev)
        f = self.broker.add_finding(Finding.make(
            f"query targets feature {top.name} ({top.mapping_method})",
            self.ROLE, [ev.id for ev in top.evidence]))
        if scope:
            scope.produced(evidence=[ev.id for ev in top.evidence],
                           finding=f.id)
        return NavigationResult(
            feature_id=top.feature_id, feature_name=top.name,
            related_symbols=top.related_symbols,
            terms=_terms_for(query, top.name), finding_id=f.id,
            candidates=cands)


def _terms_for(query: str, feature_name: str) -> list[str]:
    """Terms the change-intelligence agent will look for in units: the
    query's own tokens plus the alias vocabulary of the matched feature."""
    from src.search.keywords import extract_terms
    from src.semgraph.semantic_mapper import ZH_FEATURE_ALIASES
    terms = {query}
    qt = extract_terms(query)
    terms |= {t.lower() for t, _ in qt.all_search_terms()}
    terms |= set(qt.cjk_segments) | set(qt.cjk_subterms)
    terms.add(feature_name.lower())
    for name, alias_terms in ZH_FEATURE_ALIASES:
        if name == feature_name:
            terms |= {t.lower() for t in alias_terms}
    return sorted(t for t in terms if t)
