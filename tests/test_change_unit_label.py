"""Phase 12C：ChangeUnitLabelSkill 契约测试。

三条硬约束逐条验证：
1. candidate 级输出：LLM 标签铸 SEMANTIC_MAPPING evidence，单元的
   semantic_label/props 一个字节都不改
2. 图验证：candidate_features 只保留图里真实存在的 feature 名；
   幻觉 unit_id 直接丢弃；label/intent 白名单外归 other
3. LLM 缺位/失败 => degraded（labels 空、警告留痕），确定性标签不受影响
"""
from __future__ import annotations

import pytest

from src.schema import ToolRecorder
from src.semgraph.schema_v2 import (Edge, EdgeType, GraphV2, Node,
                                    NodeType)


class _StubLLM:
    available = True

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, system, user):
        return self.payload


def _broker(units: list[dict], features: list[str]):
    """最小 broker 替身：node()/graph/rec.add_evidence 三件套。"""

    class _Rec(ToolRecorder):
        def __init__(self):
            super().__init__()
            self.warns = []

        def warn(self, msg):
            self.warns.append(msg)

    class _B:
        def __init__(self):
            self.rec = _Rec()
            self.graph = GraphV2()
            self._nodes = {u["uid"]: Node(u["uid"], NodeType.CHANGE_UNIT,
                                          props=dict(u["props"]))
                           for u in units}
            for n in self._nodes.values():
                self.graph.add_node(n)
            for name in features:
                fid = f"feature:{name}"
                self.graph.add_node(Node(fid, NodeType.FEATURE,
                                         props={"name": name}))
                # 随便连条边（feature 独立存在即可）

        def node(self, nid):
            if nid.startswith("commit:"):
                sha = nid[7:]
                hit = [u for u in units if u["props"].get("commit") == sha]
                if not hit:
                    return None
                return Node(nid, NodeType.COMMIT,
                            props={"subject": "subject " + sha[:6]})
            return self._nodes.get(nid)

        def add_evidence(self, ev):
            self.rec.tool("evidence:add")
            return ev

    return _B()


UNITS = [
    {"uid": "cu:a#1", "props": {"commit": "a" * 40, "semantic_label": "ui",
                                "summary": "ui change | files: x.js",
                                "files": ["x.js"], "symbols": ["x.js::f"]}},
    {"uid": "cu:a#2", "props": {"commit": "a" * 40, "semantic_label": "docs",
                                "summary": "docs change | files: r.md",
                                "files": ["r.md"], "symbols": []}},
]


class TestContract:
    def test_candidate_evidence_props_untouched(self):
        from src.skills.change_unit_label import ChangeUnitLabelSkill
        b = _broker([dict(u) for u in UNITS], ["buildColor"])
        llm = _StubLLM({"units": [
            {"unit_id": "cu:a#1", "label": "ui", "intent": "feature",
             "candidate_features": ["buildColor"], "reason": "ok"}]})
        out = ChangeUnitLabelSkill().run(
            {"unit_ids": ["cu:a#1", "cu:a#2"], "hint_terms": ["color"],
             "llm": llm}, b)
        assert out.status == "success"
        lab = out.data["labels"][0]
        assert (lab["label"], lab["intent"]) == ("ui", "feature")
        assert lab["status"] == "candidate"
        assert lab["evidence_id"]
        # 确定性标签不被覆盖 —— C0 事实原样保留
        assert b.node("cu:a#1").props["semantic_label"] == "ui"
        assert b.node("cu:a#2").props["semantic_label"] == "docs"

    def test_hallucinations_filtered(self):
        """幻觉 feature 名 / 幻觉 unit_id / 白名单外 label 全部被滤。"""
        from src.skills.change_unit_label import ChangeUnitLabelSkill
        b = _broker([dict(u) for u in UNITS], ["buildColor"])
        llm = _StubLLM({"units": [
            {"unit_id": "cu:a#1", "label": "finance", "intent": "hack",
             "candidate_features": ["buildColor", "NotInGraph"], "reason": "x"},
            {"unit_id": "cu:ghost#9", "label": "ui", "intent": "bugfix",
             "candidate_features": [], "reason": "ghost unit"}]})
        out = ChangeUnitLabelSkill().run(
            {"unit_ids": ["cu:a#1"], "hint_terms": [], "llm": llm}, b)
        labels = out.data["labels"]
        assert len(labels) == 1
        assert labels[0]["unit_id"] == "cu:a#1"
        assert labels[0]["label"] == "other"       # 白名单外归 other
        assert labels[0]["intent"] == "other"
        assert labels[0]["candidate_features"] == ["buildColor"]

    def test_llm_missing_degrades(self):
        from src.skills.change_unit_label import ChangeUnitLabelSkill
        b = _broker([dict(u) for u in UNITS], [])
        out = ChangeUnitLabelSkill().run(
            {"unit_ids": ["cu:a#1"], "hint_terms": []}, b)   # 无 llm 注入
        assert out.status == "partial"
        assert out.data["labels"] == [] and not out.data["llm_used"]
        assert out.warnings           # 降级留痕（result 级），不静默

    def test_llm_exception_degrades(self):
        from src.skills.change_unit_label import ChangeUnitLabelSkill

        class _Boom(_StubLLM):
            def chat_json(self, system, user):
                raise RuntimeError("endpoint down")

        b = _broker([dict(u) for u in UNITS], [])
        out = ChangeUnitLabelSkill().run(
            {"unit_ids": ["cu:a#1"], "llm": _Boom({})}, b)
        assert out.status == "partial"
        assert out.data["labels"] == []
        assert out.warnings

    def test_no_units_partial(self):
        from src.skills.change_unit_label import ChangeUnitLabelSkill
        b = _broker([], [])
        out = ChangeUnitLabelSkill().run(
            {"unit_ids": ["cu:none"], "llm": _StubLLM({"units": []})}, b)
        assert out.status == "partial"

    def test_unit_id_prefix_forms_both_work(self):
        """unit id 双形态：无前缀（props 形态）与 cu: 前缀（图节点 id）
        都必须能找到单元（12C 实跑翻车点：gold 传无前缀整批被丢）。"""
        from src.skills.change_unit_label import ChangeUnitLabelSkill
        b = _broker([dict(u) for u in UNITS], [])
        llm = _StubLLM({"units": [
            {"unit_id": "a#1", "label": "ui", "intent": "perf",
             "candidate_features": [], "reason": "r"}]})
        out = ChangeUnitLabelSkill().run(
            {"unit_ids": ["a#1"], "llm": llm}, b)   # 无前缀形态
        assert out.status == "success"
        # 返回 unit_id 原样透传（调用方用输入形态做 join）
        assert out.data["labels"][0]["unit_id"] == "a#1"
        assert out.data["labels"][0]["evidence_id"]  # evidence 也落上了


class TestAdapterBatch:
    def test_batch_parse_and_caps(self):
        from src.llm.semantic_adapter import SemanticReasoningAdapter
        llm = _StubLLM({"units": [
            {"unit_id": "u1", "label": "auth", "intent": "bugfix",
             "candidate_features": ["Real", "Fake"], "reason": "r"},
        ]})
        got = SemanticReasoningAdapter(llm).label_change_units(
            [{"unit_id": "u1", "summary": "s"}], ["登录"],
            ["Real"])
        assert got == [{"unit_id": "u1", "label": "auth",
                        "intent": "bugfix", "candidate_features": ["Real"],
                        "reason": "r", "status": "candidate"}]

    def test_unavailable_returns_empty(self):
        from src.llm.semantic_adapter import SemanticReasoningAdapter

        class _None:
            available = False
        assert SemanticReasoningAdapter(_None()).label_change_units(
            [{"unit_id": "u1"}], [], []) == []
