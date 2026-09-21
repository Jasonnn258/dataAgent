"""BuildTaskViewSkill —— 围绕目标建初始有界视图（Phase 11A）。

逻辑自 ImpactSliceAgent.slice_impact 的视图创建段抽出。
taskview 伪层未激活（G<3）时返回 partial + 警告，view=None ——
与原 agent 行为一致（不建视图，其余照跑）。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_PARTIAL, SKILL_SUCCESS, SkillResult,
                             SkillSpec)


class BuildTaskViewSkill(BaseSkill):
    spec = SkillSpec(
        name="build_task_view",
        description="select a bounded task graph view around targets",
        required_inputs=["task_id", "target_ids"],
        produced_outputs=["view", "view_stats"],
        allowed_capabilities=["graph.create_task_view"],
        evidence_requirements="视图节点本身即图事实（无需额外语义证据）",
        preconditions=["targets 是图中存在的节点 id"],
        success_conditions=["视图建立且 task_graph_nodes < 全图节点数"],
        failure_conditions=["target 节点不存在；taskview 层未激活（partial）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        out = SkillResult(skill=self.spec.name)
        if not broker.layer_active("taskview"):
            out.status = SKILL_PARTIAL
            out.data = {"view": None, "view_stats": {}}
            out.warn("taskview layer inactive — no bounded view (G<3)")
            return out
        view = broker.create_task_view(context["task_id"],
                                       list(context["target_ids"]))
        out.status = SKILL_SUCCESS
        out.data = {"view": view, "view_stats": view.stats()}
        return out


register(BuildTaskViewSkill())
