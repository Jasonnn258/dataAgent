"""Verifiers v2 (Phase 9L): categorical judgments, no fake confidence.

DeterministicVerifier — judges findings backed by machine-checkable
evidence (AST / CALL_PATH / GIT_* / CHANGE_UNIT / GRAPH_PATH / TEST).
All cited evidence registered and at least one deterministic item =>
SUPPORTED (and the finding is promoted to `verified`, conflict guards
apply). None => UNSUPPORTED (finding demoted).

SemanticVerifier — judges findings that lean on semantic-mapping or
lexical evidence. LLM-derived mappings can corroborate but never verify
alone: SUPPORTED only when deterministic evidence corroborates, else
PARTIALLY_SUPPORTED. It never promotes a finding to verified by itself.
"""
from __future__ import annotations

from src.semgraph.objects import (EvidenceType, Finding, Verdict,
                                  VerdictStatus)

DETERMINISTIC_TYPES = {EvidenceType.AST, EvidenceType.CALL_PATH,
                       EvidenceType.GIT_DIFF, EvidenceType.GIT_BLAME,
                       EvidenceType.CHANGE_UNIT, EvidenceType.GRAPH_PATH,
                       EvidenceType.TEST}
SEMANTIC_TYPES = {EvidenceType.SEMANTIC_MAPPING, EvidenceType.LEXICAL}


class DeterministicVerifier:
    ROLE = "DeterministicVerifier"
    READS = ["get_evidence", "set_finding_status", "conflicts_involving"]

    def __init__(self, broker):
        self.broker = broker

    def verify(self, finding: Finding) -> Verdict:
        self.broker.rec.tool(f"agent:{self.ROLE}:verify")
        checks: list[str] = []
        evidence = self.broker.get_evidence(finding.evidence_ids)
        missing = [e for e in finding.evidence_ids
                   if e not in {x.id for x in evidence}]
        if missing:
            checks.append(f"unregistered evidence: {missing}")
        det = [e for e in evidence if e.type in DETERMINISTIC_TYPES]
        sem = [e for e in evidence if e.type in SEMANTIC_TYPES]
        checks.append(f"{len(det)} deterministic + {len(sem)} semantic evidence")
        if not finding.evidence_ids:
            status = VerdictStatus.UNSUPPORTED
            checks.append("no evidence cited at all")
        elif missing:
            status = VerdictStatus.UNSUPPORTED
        elif not det:
            status = VerdictStatus.UNSUPPORTED
            checks.append("no deterministic evidence — not machine-checkable")
        else:
            status = VerdictStatus.SUPPORTED
        # promote/demote through the guarded transition; a blocked promote
        # (open conflict) is reported, never forced
        if status == VerdictStatus.SUPPORTED:
            try:
                self.broker.set_finding_status(finding.id, "verified",
                                               verifier=self.ROLE)
                checks.append("finding promoted to verified")
            except Exception as e:  # DataAgentError: guard refused
                checks.append(f"verified blocked: {e}")
        elif status == VerdictStatus.UNSUPPORTED:
            self.broker.set_finding_status(finding.id, "unsupported",
                                           verifier=self.ROLE)
        return Verdict(finding_id=finding.id, status=status,
                       verifier=self.ROLE, checks=checks)


class SemanticVerifier:
    ROLE = "SemanticVerifier"
    READS = ["get_evidence"]

    def __init__(self, broker):
        self.broker = broker

    def verify(self, finding: Finding) -> Verdict | None:
        """None = out of scope (no semantic evidence to judge)."""
        self.broker.rec.tool(f"agent:{self.ROLE}:verify")
        evidence = self.broker.get_evidence(finding.evidence_ids)
        sem = [e for e in evidence if e.type in SEMANTIC_TYPES]
        if not sem:
            return None
        det = [e for e in evidence if e.type in DETERMINISTIC_TYPES]
        checks = [f"{len(sem)} semantic evidence, {len(det)} deterministic "
                  "corroboration"]
        if det:
            status = VerdictStatus.SUPPORTED
            checks.append("semantic claim corroborated by deterministic evidence")
        else:
            status = VerdictStatus.PARTIALLY_SUPPORTED
            checks.append("semantic-only: usable as a lead, not as proof")
        return Verdict(finding_id=finding.id, status=status,
                       verifier=self.ROLE, checks=checks)
