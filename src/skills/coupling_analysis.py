"""CouplingAnalysisSkill —— 回退侧/保留侧的导入耦合（Phase 11A）。

逻辑自 RollbackPlanner.plan 的耦合段抽出：两组文件集合之间的直接
IMPORTS 边（双向），附带损伤的核心信号。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import SKILL_SUCCESS, SkillResult, SkillSpec


class CouplingAnalysisSkill(BaseSkill):
    spec = SkillSpec(
        name="coupling_analysis",
        description="direct import couplings between two file sets (both directions)",
        required_inputs=["files_a", "files_b"],
        produced_outputs=["couplings"],
        allowed_capabilities=["change.get_couplings"],
        evidence_requirements="结果直接来自图上的 IMPORTS 边（确定性事实）",
        preconditions=["两组文件集合非空"],
        success_conditions=["返回排序去重的耦合描述列表（可为空=无耦合）"],
        failure_conditions=["输入文件不在图中（此时耦合为空，不算失败）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        couplings = broker.import_couplings(list(context["files_a"]),
                                            list(context["files_b"]))
        return SkillResult(skill=self.spec.name, status=SKILL_SUCCESS,
                           data={"couplings": couplings})


register(CouplingAnalysisSkill())
