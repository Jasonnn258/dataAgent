"""Phase 12B：通用 feature 播种 + resolve_target 消融基建的测试。

覆盖三件事：
1. seed_public_symbols 的源码目录过滤 / 幂等 / IMPLEMENTS+evidence 落图
2. 播种后 map_query 的词面命中（播种 → 解析 全链）
3. 图验证不变量：_llm_map 只放行图里真实存在的 feature id（幻觉丢弃）
   + LLMClient.usage 用量累计
"""
from __future__ import annotations

import pytest

from src.schema import ToolRecorder
from src.semgraph.schema_v2 import (Edge, EdgeType, GraphV2, Node,
                                    NodeType)


def _mini_graph() -> GraphV2:
    """三个目录各放一个符号的最小图（source ✓ src ✓ lib ✓ test ✗ 根 ✗）。"""
    g = GraphV2()
    g.add_node(Node("repo:r", NodeType.REPOSITORY, props={"name": "r"}))
    syms = [
        ("sym:source/index.js::buildColor", NodeType.FUNCTION,
         {"name": "buildColor", "file": "source/index.js", "exported": True}),
        ("sym:src/shallow.ts::shallow", NodeType.FUNCTION,
         {"name": "shallow", "file": "src/shallow.ts"}),
        ("sym:lib/response.js::stringify", NodeType.METHOD,
         {"name": "stringify", "file": "lib/response.js"}),
        ("sym:test/x.js::helper", NodeType.FUNCTION,
         {"name": "helper", "file": "test/x.js"}),
        ("sym:examples/d.js::demo", NodeType.FUNCTION,
         {"name": "demo", "file": "examples/d.js"}),
        # 无名/无文件符号：播种必须跳过而不是崩
        ("sym:source/weird.js::", NodeType.FUNCTION,
         {"name": "", "file": "source/weird.js"}),
        ("sym::noname", NodeType.FUNCTION, {"name": "orphan"}),
    ]
    for sid, nt, props in syms:
        g.add_node(Node(sid, nt, props=props))
    return g


class TestSeedPublicSymbols:
    def test_seeds_only_source_dirs(self):
        from src.semgraph.semantic_mapper import SemanticMapper
        g = _mini_graph()
        created = SemanticMapper(g, ToolRecorder()).seed_public_symbols()
        names = {n.props["name"] for n in created}
        # source/src/lib 全播种；test/examples/根目录 不播
        assert names == {"buildColor", "shallow", "stringify"}

    def test_implements_edge_and_evidence(self):
        from src.semgraph.semantic_mapper import SemanticMapper
        g = _mini_graph()
        SemanticMapper(g, ToolRecorder()).seed_public_symbols()
        assert g.node("feature:buildColor") is not None
        edges = g.edge_between("feature:buildColor",
                               "sym:source/index.js::buildColor",
                               EdgeType.IMPLEMENTS)
        assert edges, "IMPLEMENTS 边必须存在"
        assert edges[0].props.get("seeded_by", "").startswith(
            "seed:source-symbol")
        assert g.node("feature:buildColor").props.get("evidence_id")

    def test_idempotent(self):
        from src.semgraph.semantic_mapper import SemanticMapper
        g = _mini_graph()
        m = SemanticMapper(g, ToolRecorder())
        first = m.seed_public_symbols()
        assert m.seed_public_symbols() == []   # 第二次全部跳过
        assert len(first) == 3

    def test_query_hits_seeded_feature(self):
        """播种 → map_query 全链：词面 query 拿到 buildColor 候选。"""
        from src.semgraph.semantic_mapper import SemanticMapper
        g = _mini_graph()
        m = SemanticMapper(g, ToolRecorder())
        m.seed_public_symbols()
        cands = m.map_query("color build utility")
        assert cands and cands[0].feature_id == "feature:buildColor"
        assert cands[0].related_symbols == ["sym:source/index.js::buildColor"]


class _StubLLM:
    """duck-typed 客户端：返回真实+幻觉混合的候选。"""

    available = True

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def chat_json(self, system: str, user: str) -> dict:
        self.calls += 1
        return self.payload


class TestLLMGraphVerification:
    def test_hallucinated_ids_dropped(self):
        """图验证不变量：LLM 只能挑已有 feature 名（12B spec 硬要求）。"""
        from src.semgraph.semantic_mapper import SemanticMapper
        g = _mini_graph()
        m = SemanticMapper(g, ToolRecorder())
        m.seed_public_symbols()
        llm = _StubLLM({"candidates": [
            {"feature_id": "feature:buildColor", "reason": "real"},
            {"feature_id": "feature:NotInGraph", "reason": "hallucination"},
        ]})
        cands = m._llm_map("color build utility", llm)
        assert [c.feature_id for c in cands] == ["feature:buildColor"]
        assert cands[0].mapping_method == "llm"
        assert llm.calls == 1

    def test_no_features_no_call(self):
        from src.semgraph.semantic_mapper import SemanticMapper
        g = GraphV2()   # 空 feature 集：不发 LLM 调用
        m = SemanticMapper(g, ToolRecorder())
        llm = _StubLLM({"candidates": [{"feature_id": "feature:x"}]})
        assert m._llm_map("anything", llm) == []
        assert llm.calls == 0


class TestLLMClientUsage:
    def _client(self, resp) -> "LLMClient":
        from src.config import LLMConfig
        from src.llm.client import LLMClient
        cfg = LLMConfig(base_url="http://x", api_key="k", model="m")
        client = LLMClient(cfg)
        client._client = resp   # 替身 SDK 客户端
        return client

    @staticmethod
    def _resp(content: str, usage=None):
        """替身 SDK：chat.completions.create(model=..., messages=...) → 响应。"""
        msg = type("M", (), {"content": content})()
        choice = type("C", (), {"message": msg})()
        resp = type("R", (), {"choices": [choice], "usage": usage})()

        class _Create:
            def __call__(self, **kw):
                return resp
        sdk = type("Sdk", (), {})()
        sdk.chat = type("Chat", (), {})()
        sdk.chat.completions = type("Compl", (), {"create": _Create()})()
        return sdk

    def test_usage_accumulates(self):
        from src.llm.client import LLMClient
        sdk = self._resp('{"ok": 1}',
                         usage=type("U", (), {"prompt_tokens": 11,
                                              "completion_tokens": 7})())
        client = self._client(sdk)
        assert client.chat_json("s", "u") == {"ok": 1}
        assert client.usage == {"calls": 1, "prompt_tokens": 11,
                                "completion_tokens": 7}
        client.chat_json("s", "u")
        assert client.usage["calls"] == 2
        assert client.usage["prompt_tokens"] == 22

    def test_missing_usage_not_fabricated(self):
        from src.llm.client import LLMClient
        sdk = self._resp('{"ok": 1}', usage=None)
        client = self._client(sdk)
        client.chat_json("s", "u")
        assert client.usage == {"calls": 0, "prompt_tokens": 0,
                                "completion_tokens": 0}
