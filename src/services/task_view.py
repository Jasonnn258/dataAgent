"""TaskViewService（Phase 11D）：有界 task view 的创建/生长/读取。

确定性服务：view 选择与带审计的扩展（trigger 必须写明发起者）。视图
注册表在这里；taskview 伪层的门控语义不变（未激活时大声报错）。
逻辑自 ContextBroker 原样迁入。
"""
from __future__ import annotations

from src.errors import DataAgentError
from src.semgraph.schema_v2 import EdgeType
from src.semgraph.task_view import TaskGraphView


class TaskViewService:
    def __init__(self, broker):
        self.broker = broker
        self.views: dict[str, TaskGraphView] = {}

    def create(self, task_id: str, target_ids: list[str],
               relations: set[EdgeType] | None = None) -> TaskGraphView:
        if not self.broker.layer_active("taskview"):
            raise DataAgentError(
                "task views are disabled (taskview layer inactive, G<3)")
        view = TaskGraphView.select(self.broker.graph, task_id, target_ids,
                                    rel_types=relations, trigger="init")
        self.views[task_id] = view
        return view

    def expand(self, task_id: str, seeds: list[str],
               relations: set[EdgeType] | None = None,
               depth: int = 1, trigger: str = "") -> TaskGraphView:
        view = self.views.get(task_id)
        if view is None:
            raise DataAgentError(f"no task view {task_id!r} — create it first")
        view.expand(self.broker.graph, seeds, relations=relations, depth=depth,
                    trigger=trigger)
        return view

    def get(self, task_id: str) -> TaskGraphView:
        return self.views[task_id]
