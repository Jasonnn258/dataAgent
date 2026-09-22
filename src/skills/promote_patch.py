"""PromotePatchSkill（Phase 13J）：把沙箱验证过的 patch 落进真实仓库。

全系统唯一允许修改真实 workspace 的技能，且默认关闭
（execution_policy.promote_enabled=false）。三把钥匙缺一不可：
READY_TO_PROMOTE（审批态，approve_promotion 落过 human decision）+
explicit_approval is True（调用方显式声明）+ promote_enabled。

它贴的是**冻结的 verified.patch**（审批时逐字节拷贝 + 哈希对账），
绝不重新生成 —— 重新生成 = 重新引入 LLM/计划漂移，等于把验证作废。
落地前重拍源仓库快照：计划过期连预检都不做（STALE_PLAN）。

默认不 commit 不 push：改动留在工作树，接管的是人。
"""
from __future__ import annotations

from src.errors import DataAgentError
from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import SKILL_FAILED, SKILL_SUCCESS, SkillResult, SkillSpec


class PromotePatchSkill(BaseSkill):
    spec = SkillSpec(
        name="promote_patch",
        description=("apply the frozen, sandbox-verified patch to the "
                     "real workspace (the ONLY such path; disabled by "
                     "default; never commits or pushes)"),
        required_inputs=["execution", "explicit_approval"],
        produced_outputs=["execution", "execution_id", "status",
                          "verified_patch", "reverse_patch"],
        allowed_capabilities=["execution.promote", "evidence.add"],
        evidence_requirements="PROMOTION evidence（verified/reverse sha256 + "
                              "未提交声明）由 PromotionService 落档",
        preconditions=["attempt 已 READY_TO_PROMOTE（approve_promotion 过）",
                       "execution_policy.promote_enabled=true（默认 false）",
                       "explicit_approval 严格为 True"],
        success_conditions=["verified.patch 字节对账通过 + 源仓库未漂移 + "
                            "apply --check 通过 → PROMOTED + reverse.patch"],
        failure_conditions=["promote 关闭 / 没批准 / 没有显式批准",
                            "verified.patch 缺失或哈希对不上",
                            "源仓库已漂移（STALE_PLAN）",
                            "apply 预检失败（PROMOTION_FAILED）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        out = SkillResult(skill=self.spec.name)
        attempt = context["execution"]
        execution_id = getattr(attempt, "execution_id", "") or \
            (attempt.get("execution_id", "") if isinstance(attempt, dict)
             else "")
        if not execution_id:
            out.status = SKILL_FAILED
            out.error = "execution payload lacks execution_id"
            return out

        # 显式批准必须是布尔 True（字符串 "true" 都不算 —— 钥匙就是钥匙）
        if context.get("explicit_approval") is not True:
            out.status = SKILL_FAILED
            out.error = ("promote requires explicit_approval=True; "
                         "nothing else unlocks the real workspace")
            return out

        actor = context.get("actor") or "PromotePatchSkill"
        try:
            attempt = broker.promote_execution(
                execution_id, explicit_approval=True, actor=actor)
        except DataAgentError as e:
            out.status = SKILL_FAILED
            out.error = str(e)
            out.warn(f"promote refused: {str(e)[:200]}")
            # 尝试把终态尝试带回去给调用方看现场
            current = broker.get_execution(execution_id)
            if current is not None:
                out.data = {"execution": current,
                            "execution_id": execution_id,
                            "status": current.status}
            return out

        out.status = SKILL_SUCCESS
        out.data = {"execution": attempt, "execution_id": execution_id,
                    "status": attempt.status,
                    "verified_patch": attempt.verified_patch_path,
                    "reverse_patch": attempt.reverse_patch_path}
        out.warn("promoted — changes are UNCOMMITTED in the real "
                 "workspace; reverse.patch saved for git apply -R")
        return out


register(PromotePatchSkill())
