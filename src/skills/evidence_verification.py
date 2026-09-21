"""EvidenceVerificationSkill —— 三档裁决（Phase 11A）。

逻辑自 DeterministicVerifier + SemanticVerifier 原样抽出：
- 确定性裁决：引用证据全部已注册且含确定性项 => SUPPORTED（经守卫升
  verified，被冲突挡下要报告不硬闯）；否则 UNSUPPORTED（降级）
- 语义裁决：LLM 映射只能佐证 —— 有确定性佐证才 SUPPORTED，否则
  PARTIALLY_SUPPORTED，永不单独升 verified
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_PARTIAL, SKILL_SUCCESS, SkillResult,
                             SkillSpec)
from src.semgraph.objects import (EvidenceType, Verdict, VerdictStatus)

DETERMINISTIC_TYPES = {EvidenceType.AST, EvidenceType.CALL_PATH,
                       EvidenceType.GIT_DIFF, EvidenceType.GIT_BLAME,
                       EvidenceType.CHANGE_UNIT, EvidenceType.GRAPH_PATH,
                       EvidenceType.TEST}
SEMANTIC_TYPES = {EvidenceType.SEMANTIC_MAPPING, EvidenceType.LEXICAL}


class EvidenceVerificationSkill(BaseSkill):
    spec = SkillSpec(
        name="evidence_verification",
        description="categorical verdicts on findings (deterministic + semantic)",
        required_inputs=["finding_ids"],
        produced_outputs=["verdicts"],
        allowed_capabilities=["evidence.get", "evidence.query",
                              "evidence.finding.set_status"],
        evidence_requirements="被裁决 finding 引用的全部 evidence",
        preconditions=["finding 已注册于 broker"],
        success_conditions=["每条 finding 得到确定性裁决；语义类另附语义裁决"],
        failure_conditions=["finding 不在注册表（跳过并告警）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        out = SkillResult(skill=self.spec.name)
        verdicts: list[Verdict] = []
        for fid in list(context["finding_ids"]):
            f = next((x for x in broker.all_findings() if x.id == fid), None)
            if f is None:
                out.warn(f"unknown finding {fid} — skipped")
                continue
            verdicts.append(self._deterministic(broker, f))
            sem = self._semantic(broker, f)
            if sem is not None:
                verdicts.append(sem)
        out.status = SKILL_SUCCESS if verdicts else SKILL_PARTIAL
        if not verdicts:
            out.warn("no verifiable findings given")
        out.data = {"verdicts": verdicts}
        return out

    # ---------------------------------------------------------- 确定性
    def _deterministic(self, broker, finding) -> Verdict:
        checks: list[str] = []
        evidence = broker.get_evidence(finding.evidence_ids)
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
                broker.set_finding_status(finding.id, "verified",
                                          verifier="EvidenceVerificationSkill")
                checks.append("finding promoted to verified")
            except Exception as e:  # DataAgentError：守卫拒绝
                checks.append(f"verified blocked: {e}")
        elif status == VerdictStatus.UNSUPPORTED:
            broker.set_finding_status(finding.id, "unsupported",
                                      verifier="EvidenceVerificationSkill")
        return Verdict(finding_id=finding.id, status=status,
                       verifier="EvidenceVerificationSkill", checks=checks)

    # ---------------------------------------------------------- 语义
    def _semantic(self, broker, finding) -> Verdict | None:
        """None = 不归语义裁决（没有语义证据）。"""
        evidence = broker.get_evidence(finding.evidence_ids)
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
                       verifier="EvidenceVerificationSkill:semantic",
                       checks=checks)


register(EvidenceVerificationSkill())
