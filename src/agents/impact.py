"""ImpactSliceAgent（Phase 9J / 11B）：围绕目标的有界波及面。

11B 起委托给 BuildTaskViewSkill + ImpactAnalysisSkill：视图选择与
caller/route 聚合都在 skill 里，本 agent 保留 ImpactSlice 数据形态与
scope 记账。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.errors import DataAgentError
from src.semgraph.task_view import TaskGraphView
from src.skills.runtime import SkillRuntime


@dataclass
class ImpactSlice:
    target_ids: list[str] = field(default_factory=list)
    route_ids: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)      # 路由路径
    caller_ids: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)     # 短名
    view: TaskGraphView | None = None
    finding_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def stats(self) -> dict:
        return self.view.stats() if self.view else {}


class ImpactSliceAgent:
    ROLE = "ImpactSliceAgent"
    READS = ["resolve_target", "get_target_context", "create_task_view",
             "expand_task_view", "add_finding"]
    SKILLS = ["build_task_view", "impact_analysis"]

    def __init__(self, broker):
        self.broker = broker
        self._runtime = SkillRuntime(broker)

    def slice_impact(self, target_ids: list[str], task_id: str,
                     scope=None) -> ImpactSlice:
        self.broker.rec.tool(f"agent:{self.ROLE}:slice")
        # 1. 有界视图（taskview 层未激活时 skill 返回 partial + view=None，
        #    与旧行为一致：不建视图，其余照跑）
        v = self._runtime.run("build_task_view", {
            "task_id": task_id, "target_ids": list(target_ids),
            "actor": self.ROLE})
        if v.status == "failed":
            raise DataAgentError(v.error or "task view failed")
        # 2. 确定性波及面聚合（读视图扩展也在 skill 内）
        r = self._runtime.run("impact_analysis", {
            "target_ids": list(target_ids), "task_id": task_id,
            "actor": self.ROLE})
        if r.status == "failed":
            raise DataAgentError(r.error or "impact analysis failed")
        if scope:
            if r.evidence_ids:
                scope.produced(evidence=r.evidence_ids)
            if r.data.get("finding_id"):
                scope.produced(finding=r.data["finding_id"])
        return ImpactSlice(
            target_ids=list(r.data["target_ids"]),
            route_ids=list(r.data["route_ids"]),
            routes=list(r.data["routes"]),
            caller_ids=list(r.data["caller_ids"]),
            files=list(r.data["files"]),
            symbols=list(r.data["symbols"]),
            view=v.data.get("view"),
            finding_id=r.data.get("finding_id", ""),
            evidence_ids=list(r.data.get("evidence_ids", [])))
