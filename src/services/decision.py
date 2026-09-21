"""DecisionService（Phase 11D）：决策记忆（记录 + 先例查询）。

确定性服务：decision 注册表与按 category/target/自由文本的先例检索。
审计文本硬上限在这里执行 —— decision 只存 reason 摘要，绝不存模型
隐藏思维链（spec 9H）。逻辑自 ContextBroker 原样迁入。
"""
from __future__ import annotations

from src.semgraph.objects import Decision
from src.semgraph.schema_v2 import Edge, EdgeType, Node, NodeType
from src.services.evidence import _topic_symbols


class DecisionService:
    # 审计文本的硬上限：decision 只存 reason 摘要，绝不存模型隐藏思维链
    #（spec 9H）
    REASON_SUMMARY_CAP = 500

    def __init__(self, broker):
        self.broker = broker
        self.decisions: dict[str, Decision] = {}

    def record_decision(self, d: Decision) -> Decision:
        if len(d.reason_summary) > self.REASON_SUMMARY_CAP:
            self.broker.rec.warn(
                f"decision {d.id}: reason_summary {len(d.reason_summary)} chars "
                f"capped to {self.REASON_SUMMARY_CAP} — hidden CoT is not stored")
            d.reason_summary = d.reason_summary[:self.REASON_SUMMARY_CAP - 1] + "…"
        self.decisions[d.id] = d
        self.broker.graph.add_node(Node(d.id, NodeType.DECISION, props={
            "category": d.category, "outcome": d.outcome, "task_id": d.task_id,
            "risk": d.risk, "decision_maker": d.decision_maker,
            "reason_summary": d.reason_summary, "policy": d.policy}))
        for eid in d.evidence_ids:
            if self.broker._evidence_svc.evidence.get(eid):
                self.broker.graph.add_edge(Edge(d.id, eid, EdgeType.SUPPORTED_BY))
        for fid in d.related_findings:
            if self.broker._evidence_svc.findings.get(fid):
                self.broker.graph.add_edge(Edge(d.id, fid, EdgeType.DERIVED_FROM))
        return d

    def get_precedents(self, category: str = "", target: str = "",
                       query: str = "") -> list[Decision]:
        """决策记忆查询：按 category、按目标节点、和/或按自由文本 query
        （query 的符号必须出现在 decision 的文本块里）。"""
        qsyms = _topic_symbols(query) if query else frozenset()
        out = []
        for d in self.decisions.values():
            if category and d.category != category:
                continue
            if target and target not in (d.target or ""):
                continue
            if qsyms:
                blob = f"{d.outcome} {d.reason_summary} {d.target}"
                if not (qsyms & _topic_symbols(blob)):
                    continue
            out.append(d)
        return sorted(out, key=lambda d: d.timestamp)
