"""Verifier v2（Phase 9L）：三档定性裁决，拒绝假精确。

DeterministicVerifier —— 裁决由机器可查证据（AST / CALL_PATH /
GIT_* / CHANGE_UNIT / GRAPH_PATH / TEST）支撑的 finding。引用证据全部
已注册且至少一条确定性 => SUPPORTED（finding 升为 `verified`，冲突
守卫生效）。一条都没有 => UNSUPPORTED（finding 降级）。

SemanticVerifier —— 裁决依赖 semantic-mapping / lexical 证据的
finding。LLM 产出的映射只能佐证、永远不能单独verify：有确定性证据佐
证才 SUPPORTED，否则 PARTIALLY_SUPPORTED。它绝不自行把 finding 升为
verified。
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
        # 经带守卫的迁移升/降级；被挡下的升级（存在未解决冲突）要报告，
        # 绝不硬闯
        if status == VerdictStatus.SUPPORTED:
            try:
                self.broker.set_finding_status(finding.id, "verified",
                                               verifier=self.ROLE)
                checks.append("finding promoted to verified")
            except Exception as e:  # DataAgentError：守卫拒绝
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
        """None = 不归它管（没有语义证据可裁决）。"""
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
