"""First-class data objects (Phase 9F/9G/9H/9I groundwork).

Evidence / Finding / Decision / Policy are data, not log lines (principle 4):
- provenance exists from the moment a fact enters the system, not appended
  afterwards (principle 5)
- findings never overwrite each other; contradictions are explicit edges in
  a conflict registry (principle 6)
- no hidden CoT is stored; Decision.reason_summary is an audit-facing
  explanation, not model-private reasoning (spec 9H)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from src.semgraph.schema_v2 import stable_id


class EvidenceType(str, Enum):
    LEXICAL = "LEXICAL"
    AST = "AST"
    CALL_PATH = "CALL_PATH"
    GIT_DIFF = "GIT_DIFF"
    GIT_BLAME = "GIT_BLAME"
    CHANGE_UNIT = "CHANGE_UNIT"
    GRAPH_PATH = "GRAPH_PATH"
    TEST = "TEST"
    SEMANTIC_MAPPING = "SEMANTIC_MAPPING"


@dataclass
class Evidence:
    id: str
    type: EvidenceType
    source: str                       # producer tool id, e.g. "git:diff"
    target: str                       # graph node id it is about
    location: str = ""                # file:line / sha / node id
    payload: str = ""                 # bounded human-readable fact
    producer: str = ""                # agent/tool that created it
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    provenance: dict = field(default_factory=dict)  # upstream ids (evidence chains)

    @classmethod
    def make(cls, type: EvidenceType, source: str, target: str, **kw) -> "Evidence":
        loc = kw.get("location", "")
        eid = kw.pop("id", None) or f"evid:{stable_id(type.value, source, target, loc)}"
        return cls(id=eid, type=type, source=source, target=target, **kw)


@dataclass
class Finding:
    id: str
    statement: str                    # one factual claim, verifiable
    evidence_ids: list[str] = field(default_factory=list)
    producer: str = ""                # agent name
    status: str = "proposed"          # proposed | verified | unsupported | contradicted
    contradicts: list[str] = field(default_factory=list)  # finding ids
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    @property
    def supported(self) -> bool:
        return bool(self.evidence_ids)

    @classmethod
    def make(cls, statement: str, producer: str, evidence_ids: list[str] | None = None,
             fid: str | None = None) -> "Finding":
        return cls(id=fid or f"finding:{stable_id(statement, producer)}",
                   statement=statement, producer=producer,
                   evidence_ids=list(evidence_ids or []))


@dataclass
class Decision:
    id: str
    category: str                     # rollback | keep | expand_context | ...
    task_id: str = ""
    target: str = ""
    outcome: str = ""                 # what was decided
    evidence_ids: list[str] = field(default_factory=list)
    related_findings: list[str] = field(default_factory=list)
    risk: str = "low"                 # low | medium | high
    decision_maker: str = ""          # agent or "human:<name>"
    reason_summary: str = ""          # audit-facing, NOT model-private CoT
    policy: str = ""                  # "rule v_version -> ACTION" when gated
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    @classmethod
    def make(cls, category: str, outcome: str, **kw) -> "Decision":
        did = kw.pop("id", None) or f"dec:{stable_id(category, outcome, kw.get('task_id', ''))}"
        return cls(id=did, category=category, outcome=outcome, **kw)


class PolicyAction(str, Enum):
    PASS = "PASS"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BLOCK = "BLOCK"


@dataclass
class PolicyRule:
    id: str
    name: str
    version: str
    description: str
    action: PolicyAction
    trigger: str = ""                 # human-readable trigger condition


@dataclass
class PolicyResult:
    rule: PolicyRule | None
    action: PolicyAction
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.action == PolicyAction.PASS


# ---------------------------------------------------------------- conflicts
@dataclass
class Conflict:
    """Two findings that disagree. Both survive; the verifier owns resolution."""
    finding_a: str
    finding_b: str
    topic: str = ""
    resolved: bool = False
    resolution: str = ""
