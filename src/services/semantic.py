"""SemanticService（Phase 11D）：语义门面的确定性播种与词表。

确定性服务：SemanticMapper 的懒构造 + 确定性播种（幂等）+ 查询词表。
LLM 候选只在这个门面里被允许进入，且永远停在 candidate 级（由 skill
spec 的 semantic_reasoning 白名单与 runtime 注入控制）。逻辑自
ContextBroker 原样迁入。
"""
from __future__ import annotations


class SemanticService:
    def __init__(self, broker):
        self.broker = broker
        self._mapper = None

    def mapper(self):
        """SemanticMapper 懒加载 + 确定性播种（幂等）。语义层未激活时
        返回 None —— 调用方据此走确定性退路。"""
        if self._mapper is None and self.broker.layer_active("semantic"):
            from src.semgraph.semantic_mapper import SemanticMapper
            self._mapper = SemanticMapper(self.broker.graph, self.broker.rec)
            self._mapper.seed_deterministic()
        return self._mapper

    def map_candidates(self, query: str, llm=None) -> list:
        """模糊 query → feature 候选（agent/skill 不再自己构造 mapper）。"""
        mapper = self.mapper()
        return mapper.map_query(query, llm=llm) if mapper is not None else []

    def query_terms(self, query: str, feature_name: str) -> list[str]:
        """变更分析词表（query 词元 + feature 别名）。"""
        from src.semgraph.semantic_mapper import query_vocabulary
        return query_vocabulary(query, feature_name)
