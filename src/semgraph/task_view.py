"""TaskGraphView（Phase 9C）：任务实际看到的有界图切片。

核心思想：任务永远不拿整张图当上下文（原则：禁止默认整图检索）。视图
按三步构建：

    select    —— 围绕目标做初始 1 跳投影
    project   —— 决定哪些节点/边类型被带进视图
    expand    —— agent 沿指定关系定向生长（全程审计）

每次扩展都带 trigger 记进 expansion_history，最终报告因此能说清每个
节点是"为什么"进入上下文的。stats（task_graph_nodes / task_graph_edges /
expansion_count）供 Phase 10 做上下文规模对比。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.schema_v2 import Edge, EdgeType, GraphV2, Node, NodeType


@dataclass
class Expansion:
    seeds: list[str]
    relations: tuple[EdgeType, ...]
    depth: int
    added_nodes: list[str] = field(default_factory=list)
    added_edges: list[tuple[str, str, str]] = field(default_factory=list)
    trigger: str = ""                 # 哪个 agent 发起的、为什么


@dataclass
class TaskGraphView:
    task_id: str
    target_nodes: list[str] = field(default_factory=list)
    selected_nodes: dict[str, Node] = field(default_factory=dict)
    selected_edges: dict[int, Edge] = field(default_factory=dict)
    expansion_history: list[Expansion] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    _next_edge_key: int = 1           # 视图内局部编号，id 在视图内稳定

    # ------------------------------------------------------------- 构建
    @classmethod
    def select(cls, graph: GraphV2, task_id: str, targets: list[str],
               rel_types: set[EdgeType] | None = None,
               depth: int = 1, trigger: str = "init") -> "TaskGraphView":
        """初始投影：目标节点 + 它们的邻域。"""
        view = cls(task_id=task_id, target_nodes=list(targets))
        exp = Expansion(seeds=list(targets),
                        relations=tuple(rel_types or ()), depth=depth,
                        trigger=trigger)
        for t in targets:
            view._admit(graph.node(t), exp)
        frontier = list(targets)
        for _ in range(depth):
            nxt: list[str] = []
            for cur in frontier:
                for e in graph.edges_from(cur) + graph.edges_to(cur):
                    if rel_types and e.type not in rel_types:
                        continue
                    other = e.dst if e.src == cur else e.src
                    new_node = other not in view.selected_nodes
                    if new_node:
                        view._admit(graph.node(other), exp)
                    key = (e.src, e.dst, e.type.value)
                    if key not in {(x.src, x.dst, x.type.value) for x in view.selected_edges.values()}:
                        view.selected_edges[view._next_edge_key] = e
                        view._next_edge_key += 1
                        exp.added_edges.append(key)
                    if new_node and other not in nxt:
                        nxt.append(other)
            frontier = nxt
        view.expansion_history.append(exp)
        return view

    def _admit(self, node: Node | None, exp: Expansion) -> None:
        if node is None or node.id in self.selected_nodes:
            return
        self.selected_nodes[node.id] = node
        exp.added_nodes.append(node.id)

    # ------------------------------------------------------------- 扩展
    def expand(self, graph: GraphV2, seeds: list[str],
               relations: set[EdgeType] | None = None, depth: int = 1,
               trigger: str = "") -> Expansion:
        """从 seed 节点沿指定关系生长视图（带审计）。"""
        exp = Expansion(seeds=list(seeds), relations=tuple(relations or ()),
                        depth=depth, trigger=trigger)
        seeds = [s for s in seeds if s in self.selected_nodes or graph.node(s)]
        for s in seeds:
            self._admit(graph.node(s), exp)
        frontier = list(seeds)
        have = {(e.src, e.dst, e.type.value) for e in self.selected_edges.values()}
        for _ in range(depth):
            nxt: list[str] = []
            for cur in frontier:
                for e in graph.edges_from(cur) + graph.edges_to(cur):
                    if relations and e.type not in relations:
                        continue
                    other = e.dst if e.src == cur else e.src
                    new_node = other not in self.selected_nodes
                    if new_node:
                        self._admit(graph.node(other), exp)
                    key = (e.src, e.dst, e.type.value)
                    if key not in have:
                        have.add(key)
                        self.selected_edges[self._next_edge_key] = e
                        self._next_edge_key += 1
                        exp.added_edges.append(key)
                    if new_node and other not in nxt:
                        nxt.append(other)
            frontier = nxt
        self.expansion_history.append(exp)
        return exp

    def refresh(self, graph: GraphV2) -> None:
        """图变更后的重投影：重读已入选节点之间的边。

        不长视图 —— 只刷新已选节点之间的边（已选节点连出的新端点不追：
        生长必须走 expand()）。
        """
        ids = set(self.selected_nodes)
        fresh: dict[int, Edge] = {}
        key = 1
        for e in graph.all_edges():
            if e.src in ids and e.dst in ids:
                fresh[key] = e
                key += 1
        self.selected_edges = fresh
        self._next_edge_key = key

    # ------------------------------------------------------------- 渲染
    def nodes_of_type(self, *types: NodeType) -> list[Node]:
        want = set(types)
        return [n for n in self.selected_nodes.values() if n.type in want]

    def dump(self, budget_chars: int = 4000) -> str:
        """有界文本投影 —— 唯一允许进 prompt 的形态。构造上就不可能
        dump 出整图。"""
        parts: list[str] = [f"# task view {self.task_id}",
                            f"targets: {', '.join(self.target_nodes)}"]
        for n in self.selected_nodes.values():
            label = n.props.get("name") or n.props.get("path") or n.id
            parts.append(f"[{n.type.value}] {label}  ({n.id})")
        parts.append("edges:")
        for e in self.selected_edges.values():
            parts.append(f"  {e.src} -{e.type.value}-> {e.dst}")
        text = "\n".join(parts)
        if len(text) > budget_chars:
            text = text[:budget_chars] + f"\n...[view budget hit: {self.stats()}]"
        return text

    def stats(self) -> dict:
        return {"task_graph_nodes": len(self.selected_nodes),
                "task_graph_edges": len(self.selected_edges),
                "expansion_count": len(self.expansion_history)}
