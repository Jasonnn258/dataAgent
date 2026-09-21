"""ImpactAnalysisSkill —— 目标集合的确定性波及面（Phase 11A）。

逻辑自 ImpactSliceAgent.slice_impact 抽出：逐目标读上下文、聚合
callers/routes/files/symbols、经审计地扩展 task view、注册 impact
finding（证据 = 上下文读取时铸造的 AST evidence，不是 agent 意见）。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_SUCCESS, SkillResult, SkillSpec)
from src.semgraph.objects import Finding
from src.semgraph.schema_v2 import EdgeType


class ImpactAnalysisSkill(BaseSkill):
    spec = SkillSpec(
        name="impact_analysis",
        description="deterministic blast radius: callers/routes/files around targets",
        required_inputs=["target_ids", "task_id"],
        produced_outputs=["route_ids", "routes", "caller_ids", "files",
                          "symbols", "finding_id", "evidence_ids"],
        allowed_capabilities=["repository.get_context",
                              "graph.get_task_view", "graph.expand_task_view",
                              "evidence.finding.add"],
        evidence_requirements="每目标一条 AST evidence（读取时铸造）",
        preconditions=["target_ids 是图中存在的符号/文件节点"],
        success_conditions=["波及面聚合完成；有路由时注册带证据的 finding"],
        failure_conditions=["target 节点不存在（broker 显式报错）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        actor = context.get("actor") or "ImpactAnalysisSkill"
        target_ids = list(context["target_ids"])
        task_id = context["task_id"]
        slice_: dict = {"target_ids": target_ids, "route_ids": [],
                        "routes": [], "caller_ids": [], "files": [],
                        "symbols": [], "finding_id": "",
                        "evidence_ids": []}
        ev_ids: list[str] = []
        callers: list[str] = []
        for tid in target_ids:
            ctx = broker.get_target_context(tid)
            ev_ids.extend(ctx.evidence_ids)
            callers += [c.id for c in ctx.direct_callers]
            for r in ctx.related_routes:
                if r.id not in slice_["route_ids"]:
                    slice_["route_ids"].append(r.id)
                    slice_["routes"].append(r.props.get("route", r.id))
            node = ctx.target
            f = node.props.get("file")
            if f and f not in slice_["files"]:
                slice_["files"].append(f)
            name = node.props.get("name")
            if name and name not in slice_["symbols"]:
                slice_["symbols"].append(name)
        slice_["caller_ids"] = sorted(set(callers))
        # 带审计的生长：caller/import 闭包进视图（视图存在时）
        view = None
        try:
            view = broker.get_task_view(task_id)
        except KeyError:
            view = None
        if slice_["caller_ids"] and view is not None:
            broker.expand_task_view(
                task_id, slice_["caller_ids"],
                relations={EdgeType.CALLS, EdgeType.REFERENCES,
                           EdgeType.IMPORTS},
                depth=1, trigger=f"{actor}: caller closure")
        if slice_["routes"]:
            f = broker.add_finding(Finding.make(
                f"modifying {slice_['symbols'] or target_ids} affects "
                f"routes {slice_['routes']} via {len(slice_['caller_ids'])} caller(s)",
                actor, ev_ids))
            slice_["finding_id"] = f.id
        slice_["evidence_ids"] = ev_ids
        out = SkillResult(skill=self.spec.name, status=SKILL_SUCCESS,
                          data=slice_, evidence_ids=ev_ids)
        return out


register(ImpactAnalysisSkill())
