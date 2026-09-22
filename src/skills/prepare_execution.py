"""PrepareExecutionSkill —— 为 ExecutionPlan 建沙箱工作区（Phase 13C）。

唯一动作：broker.prepare_execution(plan)。它做 stale 前检 / 后检、
worktree 检出、attempt 落档 —— 本 skill 不碰任何 git、不写任何文件，
只把结构化结果（ExecutionAttempt，不是 bool）翻译给上层：

- PREPARED   → success，带 execution_id / workspace / status
- STALE_PLAN → failed，error 说明仓库从计划生成后变过 —— 重做计划，
  绝不"照旧执行"

cleanup 不在本 skill：沙箱回收由调用方/编排器显式决定（成功 promote
或放弃后才清）。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import SKILL_FAILED, SKILL_SUCCESS, SkillResult, SkillSpec


class PrepareExecutionSkill(BaseSkill):
    spec = SkillSpec(
        name="prepare_execution",
        description=("create a sandbox worktree for an ExecutionPlan "
                     "(detached at base_commit; source repo untouched)"),
        required_inputs=["plan"],
        produced_outputs=["execution_id", "workspace", "status",
                          "execution"],
        allowed_capabilities=["execution.prepare"],
        evidence_requirements="attempt.json 落档（WorkspaceService 负责）",
        preconditions=["plan 来自 build_execution_plan（带仓库快照）",
                       "plan 未过期（快照对比在 prepare 内部做）"],
        success_conditions=["沙箱 worktree 检出在 base_commit，"
                            "attempt 状态 PREPARED"],
        failure_conditions=["STALE_PLAN：源仓库在计划之后变过",
                            "plan 缺 repository_snapshot（prepare 拒绝）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        plan = context["plan"]
        out = SkillResult(skill=self.spec.name)
        attempt = broker.prepare_execution(plan)
        if attempt.status == "PREPARED":
            out.status = SKILL_SUCCESS
            out.data = {"execution_id": attempt.execution_id,
                        "workspace": attempt.workspace,
                        "status": attempt.status,
                        "execution": attempt}
        else:
            # 唯一合法的失败路径是 STALE_PLAN（prepare 内部已落终态）
            out.status = SKILL_FAILED
            out.error = (
                f"execution {attempt.execution_id} ended in "
                f"{attempt.status}: " +
                (attempt.notes[-1] if attempt.notes else "no detail"))
            out.data = {"execution_id": attempt.execution_id,
                        "workspace": attempt.workspace,
                        "status": attempt.status,
                        "execution": attempt}
        return out


register(PrepareExecutionSkill())
