"""GraphQueryService（Phase 11D）：图节点读取与多跳路径。

确定性服务：节点读取（repository.node 能力的真源）与经 v1 PathFinder
的关联路径查询（broker 负责保持 v2→v1 同步）。逻辑自 ContextBroker
原样迁入。
"""
from __future__ import annotations

from src.semgraph.schema_v2 import Node


class GraphQueryService:
    def __init__(self, broker):
        self.broker = broker

    def node(self, node_id: str) -> Node | None:
        return self.broker.graph.node(node_id)

    def path(self, a: str, b: str) -> list[str] | None:
        """经 v1 PathFinder 查多跳关联路径（broker 负责保持同步）。"""
        self.broker.graph.sync_back_to_v1(self.broker._v1)
        return self.broker._v1.path(a, b)
