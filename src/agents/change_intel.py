"""ChangeIntelligenceAgent（Phase 9J）：变更历史 → ChangeUnit。

把单元的 label/符号/UI 文案与查询词表匹配 —— 对变更层做确定性打分，
每次匹配挂上 evidence。绝不假设 commit == change unit：输出永远是单元
级的。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.objects import Evidence, EvidenceType, Finding
from src.semgraph.schema_v2 import NodeType


@dataclass
class UnitMatch:
    unit_id: str                 # 图节点 id（cu:...）
    commit: str
    label: str
    date: str = ""               # commit 日期（时近排序）
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    score: float = 0.0
    finding_id: str = ""
    evidence_id: str = ""        # 单元自带的 CHANGE_UNIT evidence


class ChangeIntelligenceAgent:
    ROLE = "ChangeIntelligenceAgent"
    READS = ["find_change_units", "get_change_context", "add_evidence",
             "add_finding", "node"]

    # 确定性权重：label 是最强信号，UI 文案次之
    W_LABEL, W_UI, W_SYMBOL, W_FILE = 3.0, 2.0, 2.0, 1.0

    def __init__(self, broker):
        self.broker = broker

    def find_units(self, terms: list[str], commits: list[str] | None = None,
                   scope=None) -> list[UnitMatch]:
        """匹配词表的单元，优者在前。`commits` 限定搜索范围
        （用户用 keep hint 锚定"同一个 commit"时，orchestrator 就用它
        把 commit 钉住）。"""
        self.broker.rec.tool(f"agent:{self.ROLE}:find_units")
        if not self.broker.layer_active("change"):
            return []          # 变更层未激活：无可匹配（G<2）
        tl = [t.lower() for t in terms if t]
        matches: list[UnitMatch] = []
        for cu in self.broker.graph.nodes_of_type(NodeType.CHANGE_UNIT):
            p = cu.props
            if commits and p.get("commit", "") not in commits:
                continue
            label = p.get("semantic_label", "")
            syms = p.get("symbols", [])
            short_syms = [s.rsplit("::", 1)[-1].lower() for s in syms]
            ui = " ".join(p.get("ui_strings", [])).lower()
            files = " ".join(p.get("files", [])).lower()
            score = 0.0
            for t in tl:
                if t == label.lower():
                    score += self.W_LABEL
                elif t in label.lower():
                    score += self.W_LABEL * 0.7
                if t in ui:
                    score += self.W_UI
                if any(t == s or t in s for s in short_syms):
                    score += self.W_SYMBOL
                if t in files:
                    score += self.W_FILE
            if score <= 0:
                continue
            unit_ev = p.get("evidence_id", "")
            ev = Evidence.make(
                EvidenceType.CHANGE_UNIT, source="change-intel:match",
                target=cu.id, location=p.get("commit", "")[:12],
                payload=f"unit {p.get('unit_id')} [{label}] matches terms "
                        f"{[t for t in tl if t][:6]} (score {score})",
                provenance={"derived_from": [unit_ev]} if unit_ev else {})
            self.broker.add_evidence(ev)
            f = self.broker.add_finding(Finding.make(
                f"unit {p.get('unit_id')} [{label}] in {p.get('commit', '')[:8]} "
                f"matches the change description", self.ROLE, [ev.id]))
            if scope:
                scope.produced(evidence=[ev.id], finding=f.id)
            matches.append(UnitMatch(
                unit_id=cu.id, commit=p.get("commit", ""), label=label,
                date=self._commit_date(p.get("commit", "")),
                files=list(p.get("files", [])), symbols=list(syms),
                score=score, finding_id=f.id, evidence_id=unit_ev))
        # 优者在前：先分数，再时近（按 commit 日期，不按 sha 序）
        matches.sort(key=lambda m: (-m.score, m.date, m.commit))
        return matches

    def _commit_date(self, sha: str) -> str:
        node = self.broker.node(f"commit:{sha}")
        return node.props.get("date", "") if node else ""
