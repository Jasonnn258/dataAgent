"""Role-scoped context (Phase 9K).

Agents share *structured facts* — evidence, findings, decisions, policy
results — through the broker's registries (principle 3). They do NOT share
each other's intermediate thinking. Each agent runs against a
ScopedContext that names:

  - what it may read from the broker (capability list, by method name)
  - the task view it is allowed to grow (bounded, audited)
  - the evidence/findings/decisions it produced (its own output trail)

The capability list is a documented contract, not a sandbox: the hard
boundaries (no Semantica internals, no whole-graph retrieval, no git
execution) are enforced by the broker API itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.task_view import TaskGraphView


@dataclass
class ScopedContext:
    role: str
    task_id: str
    reads: list[str] = field(default_factory=list)   # broker method names
    task_view: TaskGraphView | None = None
    evidence_ids: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)

    def produced(self, evidence: list[str] = None, finding: str = "",
                 decision: str = "") -> None:
        if evidence:
            self.evidence_ids.extend(evidence)
        if finding:
            self.finding_ids.append(finding)
        if decision:
            self.decision_ids.append(decision)
