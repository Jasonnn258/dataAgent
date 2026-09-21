"""ImpactSliceAgent（Phase 9J）：围绕目标的有界波及面。

构建 task view（select + 带审计的 expand），从 broker 确定性收集
caller/callee/importer/route，并注册一条 impact Finding —— 它的证据是
上下文读取时铸造的 AST evidence，不是 agent 的意见。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.objects import Finding
from src.semgraph.schema_v2 import EdgeType
from src.semgraph.task_view import TaskGraphView


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

    def __init__(self, broker):
        self.broker = broker

    def slice_impact(self, target_ids: list[str], task_id: str,
                     scope=None) -> ImpactSlice:
        self.broker.rec.tool(f"agent:{self.ROLE}:slice")
        view = None
        if self.broker.layer_active("taskview"):
            view = self.broker.create_task_view(task_id, target_ids)
        slice_ = ImpactSlice(target_ids=list(target_ids), view=view)
        ev_ids: list[str] = []
        callers: list[str] = []
        for tid in target_ids:
            ctx = self.broker.get_target_context(tid)
            ev_ids.extend(ctx.evidence_ids)
            callers += [c.id for c in ctx.direct_callers]
            for r in ctx.related_routes:
                if r.id not in slice_.route_ids:
                    slice_.route_ids.append(r.id)
                    slice_.routes.append(r.props.get("route", r.id))
            node = ctx.target
            f = node.props.get("file")
            if f and f not in slice_.files:
                slice_.files.append(f)
            name = node.props.get("name")
            if name and name not in slice_.symbols:
                slice_.symbols.append(name)
        slice_.caller_ids = sorted(set(callers))
        # 带审计的生长：caller/import 闭包到这一步才进视图，trigger 写明
        # 是本 agent 发起的
        if slice_.caller_ids and view is not None:
            self.broker.expand_task_view(
                task_id, slice_.caller_ids,
                relations={EdgeType.CALLS, EdgeType.REFERENCES,
                           EdgeType.IMPORTS},
                depth=1, trigger=f"{self.ROLE}: caller closure")
        if slice_.routes:
            f = self.broker.add_finding(Finding.make(
                f"modifying {slice_.symbols or slice_.target_ids} affects "
                f"routes {slice_.routes} via {len(slice_.caller_ids)} caller(s)",
                self.ROLE, ev_ids))
            slice_.finding_id = f.id
            if scope:
                scope.produced(finding=f.id)
        slice_.evidence_ids = ev_ids
        return slice_
