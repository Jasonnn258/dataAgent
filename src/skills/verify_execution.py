"""VerifyExecutionSkill —— 执行结果终审（Phase 13G）。

唯一动作：broker.verify_execution(attempt)。八项检查五个裁决面全部在
VerificationService 里确定性完成；本 skill 只翻译裁决：

- VERIFIED → success（attempt 状态 VERIFIED，可进 policy gate）
- PARTIAL   → partial（硬面过、软面缺：停在 VALIDATED，补齐后重验）
- FAILED    → failed（attempt 已落 VERIFICATION_FAILED 终态）
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_FAILED, SKILL_PARTIAL, SKILL_SUCCESS,
                             SkillResult, SkillSpec)


class VerifyExecutionSkill(BaseSkill):
    spec = SkillSpec(
        name="verify_execution",
        description=("final verdict on a validated execution: expected "
                     "changes happened, keep side preserved, scope clean, "
                     "tests and evidence complete"),
        required_inputs=["execution"],
        produced_outputs=["execution_id", "status", "verification",
                          "execution"],
        allowed_capabilities=["execution.verify"],
        evidence_requirements="ExecutionVerification 五面裁决 + 逐项明细"
                              "落 attempt.verification_results",
        preconditions=["execution 状态 VALIDATED（proposed/actual patch "
                       "与 validation_results 已在案）"],
        success_conditions=["overall VERIFIED 且 attempt 状态 VERIFIED"],
        failure_conditions=["FAILED：forbidden 被动 / 越界 / 回退缺失 → "
                            "VERIFICATION_FAILED 终态",
                            "PARTIAL：没跑测试或证据链不全（不推进状态）",
                            "attempt 未 VALIDATED → fail fast"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        attempt = context["execution"]
        out = SkillResult(skill=self.spec.name)
        try:
            attempt, verdict = broker.verify_execution(attempt)
        except Exception as e:   # 前置缺失：fail fast
            out.status = SKILL_FAILED
            out.error = f"verification refused: {e}"
            out.data = {"execution_id": getattr(attempt, "execution_id", ""),
                        "status": getattr(attempt, "status", "")}
            return out
        out.data = {"execution_id": attempt.execution_id,
                    "status": attempt.status,
                    "verification": verdict.to_dict(),
                    "execution": attempt}
        if verdict.overall == "VERIFIED":
            out.status = SKILL_SUCCESS
        elif verdict.overall == "PARTIAL":
            out.status = SKILL_PARTIAL
            out.error = "verification PARTIAL: " + "; ".join(
                c["detail"] for c in verdict.checks if not c["pass"])
        else:
            out.status = SKILL_FAILED
            out.error = "verification FAILED: " + "; ".join(
                c["detail"] for c in verdict.checks if not c["pass"])
        return out


register(VerifyExecutionSkill())
