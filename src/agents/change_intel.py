"""ChangeIntelligenceAgent (Phase 9J): change history -> ChangeUnits.

Matches unit labels/symbols/UI strings against the query vocabulary —
deterministic scoring over the change layer, evidence attached per match.
Never assumes commit == change unit: the output is always unit-level.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.objects import Evidence, EvidenceType, Finding
from src.semgraph.schema_v2 import NodeType


@dataclass
class UnitMatch:
    unit_id: str                 # graph node id (cu:...)
    commit: str
    label: str
    date: str = ""               # commit date (recency ordering)
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    score: float = 0.0
    finding_id: str = ""
    evidence_id: str = ""        # the unit's own CHANGE_UNIT evidence


class ChangeIntelligenceAgent:
    ROLE = "ChangeIntelligenceAgent"
    READS = ["find_change_units", "get_change_context", "add_evidence",
             "add_finding", "node"]

    # deterministic weights: label is the strongest signal, UI copy next
    W_LABEL, W_UI, W_SYMBOL, W_FILE = 3.0, 2.0, 2.0, 1.0

    def __init__(self, broker):
        self.broker = broker

    def find_units(self, terms: list[str], commits: list[str] | None = None,
                   scope=None) -> list[UnitMatch]:
        """Units matching the term vocabulary, best first. `commits`
        restricts the search (the orchestrator pins the commit via the
        keep hint when the user anchors it)."""
        self.broker.rec.tool(f"agent:{self.ROLE}:find_units")
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
        # best first: score, then recency (commit date, not sha order)
        matches.sort(key=lambda m: (-m.score, m.date, m.commit))
        return matches

    def _commit_date(self, sha: str) -> str:
        node = self.broker.node(f"commit:{sha}")
        return node.props.get("date", "") if node else ""
