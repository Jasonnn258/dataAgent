"""ValidateExecutionSkill —— 沙箱里跑验证命令（Phase 13F）。

唯一动作：broker.validate_execution(attempt)。命令清单在 plan/repo
配置里定死，本 skill 不生成、不改写、不补命令；跑不过就是 TEST_FAILED
终态 —— 绝不"看起来差不多了就当过了"。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import SKILL_FAILED, SKILL_SUCCESS, SkillResult, SkillSpec


class ValidateExecutionSkill(BaseSkill):
    spec = SkillSpec(
        name="validate_execution",
        description=("run the configured validation commands inside the "
                     "sandbox worktree (argv whitelist, shell-free)"),
        required_inputs=["execution"],
        produced_outputs=["execution_id", "status", "validation_results",
                          "execution"],
        allowed_capabilities=["execution.validate"],
        evidence_requirements="ValidationResult 逐条落 attempt（含 exit_code/"
                              "时长/输出摘要，不是 bool）",
        preconditions=["execution 状态 APPLIED_SANDBOX",
                       "命令来自 plan.validation_commands 或 repo 配置"],
        success_conditions=["VALIDATED：全部命令 exit 0"],
        failure_conditions=["TEST_FAILED：任一命令 FAILED/TIMEOUT/ERROR/"
                            "BLOCKED",
                            "attempt 未 APPLIED_SANDBOX → fail fast"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        attempt = context["execution"]
        out = SkillResult(skill=self.spec.name)
        try:
            attempt, results = broker.validate_execution(attempt)
        except Exception as e:   # 状态不对等前置缺失：fail fast
            out.status = SKILL_FAILED
            out.error = f"validation refused: {e}"
            out.data = {"execution_id": getattr(attempt, "execution_id", ""),
                        "status": getattr(attempt, "status", "")}
            return out
        out.data = {"execution_id": attempt.execution_id,
                    "status": attempt.status,
                    "validation_results": attempt.validation_results,
                    "execution": attempt}
        if attempt.status == "VALIDATED":
            out.status = SKILL_SUCCESS
        else:
            out.status = SKILL_FAILED
            out.error = (f"execution {attempt.execution_id} ended in "
                         f"{attempt.status}: " +
                         (attempt.notes[-1] if attempt.notes else ""))
        return out


register(ValidateExecutionSkill())
