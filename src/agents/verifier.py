"""Verifier v2（Phase 9L / 11B）：三档定性裁决，拒绝假精确。

DeterministicVerifier —— 裁决由机器可查证据（AST / CALL_PATH /
GIT_* / CHANGE_UNIT / GRAPH_PATH / TEST）支撑的 finding。引用证据全部
已注册且至少一条确定性 => SUPPORTED（finding 升为 `verified`，冲突
守卫生效）。一条都没有 => UNSUPPORTED（finding 降级）。

SemanticVerifier —— 裁决依赖 semantic-mapping / lexical 证据的
finding。LLM 产出的映射只能佐证、永远不能单独verify：有确定性证据佐
证才 SUPPORTED，否则 PARTIALLY_SUPPORTED。它绝不自行把 finding 升为
verified。

11B 起两者都委托 EvidenceVerificationSkill（mode=deterministic /
semantic），verifier 归属名沿用各 agent，报告输出不变。
"""
from __future__ import annotations

from src.errors import DataAgentError
from src.semgraph.objects import Finding, Verdict
from src.skills.runtime import SkillRuntime


class DeterministicVerifier:
    ROLE = "DeterministicVerifier"
    READS = ["get_evidence", "set_finding_status", "conflicts_involving"]
    SKILLS = ["evidence_verification"]

    def __init__(self, broker):
        self.broker = broker
        self._runtime = SkillRuntime(broker)

    def verify(self, finding: Finding) -> Verdict:
        self.broker.rec.tool(f"agent:{self.ROLE}:verify")
        r = self._runtime.run("evidence_verification", {
            "finding_ids": [finding.id], "mode": "deterministic",
            "actor": self.ROLE})
        if r.status != "success" or not r.data["verdicts"]:
            raise DataAgentError(r.error or "deterministic verify failed")
        return r.data["verdicts"][0]


class SemanticVerifier:
    ROLE = "SemanticVerifier"
    READS = ["get_evidence"]
    SKILLS = ["evidence_verification"]

    def __init__(self, broker):
        self.broker = broker
        self._runtime = SkillRuntime(broker)

    def verify(self, finding: Finding) -> Verdict | None:
        """None = 不归它管（没有语义证据可裁决）。"""
        self.broker.rec.tool(f"agent:{self.ROLE}:verify")
        r = self._runtime.run("evidence_verification", {
            "finding_ids": [finding.id], "mode": "semantic",
            "actor": self.ROLE})
        if r.status == "failed":
            raise DataAgentError(r.error or "semantic verify failed")
        return r.data["verdicts"][0] if r.data["verdicts"] else None
