"""GraphQueryService（Phase 11D）：图节点读取与多跳路径。

确定性服务：节点读取（repository.node 能力的真源）与经 v1 PathFinder
的关联路径查询（broker 负责保持 v2→v1 同步）。逻辑自 ContextBroker
原样迁入；11E 起路径查询统一 ToolResult 形态。
"""
from __future__ import annotations

from src.schema import ToolResult
from src.semgraph.schema_v2 import Node


class GraphQueryService:
    def __init__(self, broker):
        self.broker = broker
        self._own_v1 = None       # 首次 path 时懒克隆（见 _v1_isolated）

    def node(self, node_id: str) -> Node | None:
        return self.broker.graph.node(node_id)

    def _v1_isolated(self):
        """本 broker 专属的 v1 薄克隆。

        get_context_graph 对同一 repo 是进程级缓存、跨 broker 共享；
        而 sync_back_to_v1 会把 v2 独有实体灌进目标 v1。若直接同步到
        共享对象，一个 broker 的路径查询会污染同进程所有图使用者
        （test_graph_build_counts 曾因此多出一个 File）。deepcopy 会撞
        上 semantica 内部的 thread-local，所以这里只浅拷贝 kg 的两个
        列表（sync 只 append、不改既有元素）并重建自有索引；path 仅
        用 nx 投影与 PathFinder，语义不变。
        """
        if self._own_v1 is None:
            from src.semgraph.graph import ContextGraph
            shared = self.broker._v1
            own = ContextGraph(self.broker.repo, self.broker.rec)
            own._KG, own._GB, own._PF, own._PT = \
                shared._KG, shared._GB, shared._PF, shared._PT
            own.kg = shared._KG(
                entities=list(shared.kg.entities),
                relationships=list(shared.kg.relationships),
                metadata={"repo": str(self.broker.repo),
                          "builder": "dataAgent-graph-query-isolated"})
            own._build_adj()
            own._build_nx()
            self._own_v1 = own
        return self._own_v1

    def path_result(self, a: str, b: str) -> ToolResult:
        """多跳关联路径，统一 ToolResult 形态（11E）。value=None 表示
        图中无路径 —— 这是答案，不是失败。"""
        from src.services.tooling import call_tool

        def _find():
            cg = self._v1_isolated()
            self.broker.graph.sync_back_to_v1(cg)
            return cg.path(a, b)

        return call_tool("graph.find_path", _find)

    def path(self, a: str, b: str) -> list[str] | None:
        """经 v1 PathFinder 查多跳关联路径（broker 负责保持同步）。"""
        return self.path_result(a, b).unwrap()
