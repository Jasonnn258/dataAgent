"""SemanticService（Phase 11D）：语义门面的确定性播种与词表。

确定性服务：SemanticMapper 的懒构造 + 确定性播种（幂等）+ 查询词表。
LLM 候选只在这个门面里被允许进入，且永远停在 candidate 级（由 skill
spec 的 semantic_reasoning 白名单与 runtime 注入控制）。逻辑自
ContextBroker 原样迁入；11E 起工具调用统一 ToolResult 形态。
"""
from __future__ import annotations

from src.schema import ToolResult


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

    def seed_public_symbols(self) -> int:
        """通用 feature 播种（12B 真实 repo 实验路径专用）。

        幂等：先照常懒构造 + Next.js 形状播种，再补源码目录符号。
        fixture 路径不调用 —— 保基线零漂移。返回新建 feature 数。
        """
        mapper = self.mapper()
        if mapper is None:
            return 0
        return len(mapper.seed_public_symbols())

    def map_candidates_result(self, query: str, llm=None) -> "ToolResult":
        """模糊 query → feature 候选，统一 ToolResult 形态（11E）。

        降级语义：请求了 LLM 精化但不可用 => degraded=True（词面/别名
        匹配仍然有效）；语义层未激活 => 降级空结果（调用方走确定性
        退路）。
        """
        from src.services.tooling import call_tool
        mapper = self.mapper()
        if mapper is None:
            return ToolResult.degraded_ok(
                "semantic.map_candidates", [],
                note="semantic layer inactive — no candidates "
                     "(deterministic fallback expected)")
        r = call_tool("semantic.map_candidates", mapper.map_query,
                      query, llm=llm)
        if r.ok:
            llm_usable = llm is not None and getattr(llm, "available", False)
            r.meta["llm_used"] = llm_usable
            r.meta["candidates"] = len(r.value or [])
            if llm is not None and not llm_usable:
                r.degraded = True
                r.meta["note"] = ("llm requested but unavailable — "
                                  "deterministic lexical/alias match only")
        return r

    def map_candidates(self, query: str, llm=None) -> list:
        """模糊 query → feature 候选（agent/skill 不再自己构造 mapper）。"""
        return self.map_candidates_result(query, llm).value or []

    def query_terms(self, query: str, feature_name: str) -> list[str]:
        """变更分析词表（query 词元 + feature 别名）。"""
        from src.semgraph.semantic_mapper import query_vocabulary
        return query_vocabulary(query, feature_name)
