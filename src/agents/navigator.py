"""RepositoryNavigator（Phase 9J）：模糊自然语言 → feature 目标。

唯一允许 LLM 参与的 agent —— 即便在这里，LLM 也只能在已有 feature 名
字里挑（SemanticMapper 契约）；每一次挑选都停在 candidate 级，直到确
定性工具给出佐证。导航失败要大声报错：DataAgentError，绝不静默返回空
结果。
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
    terms: list[str] = field(default_factory=list)     # 给 CI 的查询词表
    finding_id: str = ""
    candidates: list[SemanticTargetCandidate] = field(default_factory=list)


class RepositoryNavigator:
    ROLE = "RepositoryNavigator"
    READS = ["map_query", "resolve_target", "add_evidence", "add_finding"]

    def __init__(self, broker, llm=None):
        self.broker = broker
        self.llm = llm
        self.mapper = None
        if broker.layer_active("semantic"):
            self.mapper = SemanticMapper(broker.graph, broker.rec)
            self.mapper.seed_deterministic()

    def find_target(self, query: str, scope=None) -> NavigationResult:
        self.broker.rec.tool(f"agent:{self.ROLE}:navigate")
        cands = self.mapper.map_query(query, llm=self.llm) \
            if self.mapper is not None else []
        if not cands:
            # 没有 feature 命中 —— 确定性符号/文件名退路
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
    """change-intelligence agent 要在单元里找的词表：query 自身词元 +
    命中 feature 的别名词汇。"""
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
