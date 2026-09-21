"""TaskGraphView (Phase 9C): the bounded graph slice a task actually sees.

Core idea: a task NEVER gets the whole graph as context (principle: no
default whole-graph retrieval). A view is built by

    select    — initial 1-hop projection around the target
    project   — which node/edge types are carried into the view
    expand    — agent-driven growth along specific relations (audited)

Every expansion is recorded in expansion_history with its trigger, so the
final report can say exactly why a node entered the context. Stats
(task_graph_nodes / task_graph_edges / expansion_count) feed the Phase 10
context-size comparison.
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
    trigger: str = ""                 # which agent asked, and why


@dataclass
class TaskGraphView:
    task_id: str
    target_nodes: list[str] = field(default_factory=list)
    selected_nodes: dict[str, Node] = field(default_factory=dict)
    selected_edges: dict[int, Edge] = field(default_factory=dict)
    expansion_history: list[Expansion] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    _next_edge_key: int = 1           # local numbering, ids stable per view

    # ------------------------------------------------------------- build
    @classmethod
    def select(cls, graph: GraphV2, task_id: str, targets: list[str],
               rel_types: set[EdgeType] | None = None,
               depth: int = 1, trigger: str = "init") -> "TaskGraphView":
        """Initial projection: target nodes + their neighborhood."""
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

    # ------------------------------------------------------------- expand
    def expand(self, graph: GraphV2, seeds: list[str],
               relations: set[EdgeType] | None = None, depth: int = 1,
               trigger: str = "") -> Expansion:
        """Grow the view from seed nodes along given relations (audited)."""
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
        """Re-project after graph mutations: re-read admitted nodes' edges.

        Does not grow the view — only refreshes edges between nodes that are
        already selected (plus edges from selected nodes that gained new
        endpoints are ignored: growth must go through expand()).
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

    # ------------------------------------------------------------- render
    def nodes_of_type(self, *types: NodeType) -> list[Node]:
        want = set(types)
        return [n for n in self.selected_nodes.values() if n.type in want]

    def dump(self, budget_chars: int = 4000) -> str:
        """Bounded textual projection — the only shape that may reach a
        prompt. Whole-graph dumps are impossible by construction."""
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
