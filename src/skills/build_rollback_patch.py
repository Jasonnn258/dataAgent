"""BuildRollbackPatchSkill —— 确定性反向 patch 构建（Phase 13D）。

唯一动作：broker.build_rollback_patch(attempt)。构建本身是纯数据运算
（图上 HUNK 节点 + git diff 反转），本 skill 不做判断、不碰 git、不写
文件；keep 侧排除在构建算法里是构造性的，这里没有"挑 hunk"的余地。

- PATCH_BUILT → success，带 execution_id / patch_path / artifact / status
- CONFLICT    → failed（apply --check 在 base 状态上就过不了；
                不偷改 patch、不试 --3way，重试 = 新 attempt）
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import SKILL_FAILED, SKILL_SUCCESS, SkillResult, SkillSpec


class BuildRollbackPatchSkill(BaseSkill):
    spec = SkillSpec(
        name="build_rollback_patch",
        description=("build the deterministic inverse patch for a PREPARED "
                     "execution (keep-side hunks never enter the patch)"),
        required_inputs=["execution"],
        produced_outputs=["execution_id", "patch_path", "artifact", "status"],
        allowed_capabilities=["execution.build_patch"],
        evidence_requirements="EXECUTION_PATCH evidence（sha256 身份证）",
        preconditions=["execution 状态 PREPARED（沙箱 worktree 已就位）",
                       "回退单元的 HUNK 节点在图上"],
        success_conditions=["proposed.patch 落盘，apply --check 预检通过，"
                            "attempt 状态 PATCH_BUILT"],
        failure_conditions=["CONFLICT：apply --check 失败（不消解、不 --3way）",
                            "attempt 未 PREPARED / 图上无 hunk → fail fast"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        attempt = context["execution"]
        out = SkillResult(skill=self.spec.name)
        try:
            attempt, artifact = broker.build_rollback_patch(attempt)
        except Exception as e:   # 前置缺失（未 PREPARED/图缺 hunk）fail fast
            out.status = SKILL_FAILED
            out.error = f"patch build refused: {e}"
            out.data = {"execution_id": getattr(attempt, "execution_id", ""),
                        "status": getattr(attempt, "status", "")}
            return out
        if artifact is None:    # CONFLICT 终态
            out.status = SKILL_FAILED
            out.error = (f"execution {attempt.execution_id} ended in "
                         f"CONFLICT: " +
                         (attempt.notes[-1] if attempt.notes
                          else "apply --check failed"))
        else:
            out.status = SKILL_SUCCESS
        out.data = {"execution_id": attempt.execution_id,
                    "patch_path": attempt.patch_path,
                    "artifact": artifact,
                    "status": attempt.status}
        return out


register(BuildRollbackPatchSkill())
