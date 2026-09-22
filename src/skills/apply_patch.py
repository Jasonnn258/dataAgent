"""ApplyPatchSkill —— 沙箱内应用 patch（Phase 13E）。

唯一动作：broker.apply_execution_patch(attempt)。apply 只发生在沙箱
worktree（SandboxGit 的 cwd 钉死保证）；本 skill 不碰 git、不写文件。

- APPLIED_SANDBOX      → success（actual.patch 已存档且与 proposed
                          内容级一致）
- CONFLICT             → failed（apply --check 复核没过）
- VERIFICATION_FAILED  → failed（actual 与 proposed 对不上：git 干了
                          计划之外的事，绝不放行）
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import SKILL_FAILED, SKILL_SUCCESS, SkillResult, SkillSpec


class ApplyPatchSkill(BaseSkill):
    spec = SkillSpec(
        name="apply_patch",
        description=("apply the proposed patch inside the sandbox "
                     "worktree and diff-check it against the plan"),
        required_inputs=["execution"],
        produced_outputs=["execution_id", "status", "actual_patch_path",
                          "execution"],
        allowed_capabilities=["execution.apply_patch"],
        evidence_requirements="actual.patch 存档（沙箱内 diff，非源仓库）",
        preconditions=["execution 状态 PATCH_BUILT（proposed.patch 在案）"],
        success_conditions=["APPLIED_SANDBOX：actual 与 proposed 内容级一致"],
        failure_conditions=["CONFLICT：apply --check 复核失败",
                            "VERIFICATION_FAILED：出现计划外修改",
                            "attempt 未 PATCH_BUILT → fail fast"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        attempt = context["execution"]
        out = SkillResult(skill=self.spec.name)
        try:
            attempt = broker.apply_execution_patch(attempt)
        except Exception as e:   # 前置缺失（状态不对/无 patch）fail fast
            out.status = SKILL_FAILED
            out.error = f"apply refused: {e}"
            out.data = {"execution_id": getattr(attempt, "execution_id", ""),
                        "status": getattr(attempt, "status", "")}
            return out
        out.data = {"execution_id": attempt.execution_id,
                    "status": attempt.status,
                    "actual_patch_path": attempt.actual_patch_path,
                    "execution": attempt}
        if attempt.status == "APPLIED_SANDBOX":
            out.status = SKILL_SUCCESS
        else:
            out.status = SKILL_FAILED
            out.error = (f"execution {attempt.execution_id} ended in "
                         f"{attempt.status}: " +
                         (attempt.notes[-1] if attempt.notes else ""))
        return out


register(ApplyPatchSkill())
