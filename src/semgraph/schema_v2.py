"""Graph Schema v2 (Phase 9A).

Upgrades the Phase 5 structural graph into six logical layers:

    Code Graph        Repository/File/Function/Class/Component/...
    Semantic Graph    Feature/Capability/Concept/Requirement
    Change Graph      Commit/Diff/Hunk/ChangedSymbol/ChangeUnit/...
    Evidence Graph    Task/Hypothesis/Finding/Evidence
    Task Graph        Task views over the above (see task_view.py)
    Decision Graph    Decision/Policy

Design rules (spec §9A + engineering principles):
- every node has a stable id (v1 prefixes kept: file:/sym:/commit:/... so the
  Phase 5 ContextGraph converts losslessly in both directions)
- temporal edges carry OPTIONAL valid_from_commit / valid_to_commit /
  observed_at in props — no full historical reconstruction is attempted
  (v1 commitment), but HEAD relations link to the ChangeUnit/Commit that
  last touched them
- conflicting props are never silently overwritten (principle 6):
  add_node with a different type on an existing id raises GraphError
- this schema is our OWN dataclass layer; semantica.kg objects stay behind
  the ContextBroker (principle: Semantica is infrastructure, not framework)
"""
from __future__ import annotations

import time
import zlib
from dataclasses import dataclass, field
from enum import Enum

from src.errors import GraphError


class NodeType(str, Enum):
    # ---- Code Graph ------------------------------------------------------
    REPOSITORY = "Repository"
    FILE = "File"
    FUNCTION = "Function"
    METHOD = "Method"
    CLASS = "Class"
    COMPONENT = "Component"
    API_ENDPOINT = "APIEndpoint"
    VARIABLE = "Variable"
    TEST = "Test"
    UI_STRING = "UIString"
    CONFIG = "Config"
    PROMPT = "Prompt"
    # internal helper kinds carried over from v1 (not in the spec list but
    # needed for name resolution paths; they stay reachable via adapter)
    SCOPE = "Scope"
    CALL_NAME = "CallName"
    # ---- Semantic Graph --------------------------------------------------
    FEATURE = "Feature"
    CAPABILITY = "Capability"
    CONCEPT = "Concept"
    REQUIREMENT = "Requirement"
    # ---- Change Graph ----------------------------------------------------
    COMMIT = "Commit"
    DIFF = "Diff"
    HUNK = "Hunk"
    CHANGED_SYMBOL = "ChangedSymbol"
    CHANGE_UNIT = "ChangeUnit"
    ISSUE = "Issue"
    PULL_REQUEST = "PullRequest"
    TEST_FAILURE = "TestFailure"
    # ---- Evidence / Task / Decision Graphs -------------------------------
    TASK = "Task"
    HYPOTHESIS = "Hypothesis"
    FINDING = "Finding"
    EVIDENCE = "Evidence"
    DECISION = "Decision"
    POLICY = "Policy"


class EdgeType(str, Enum):
    # structural
    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    REFERENCES = "REFERENCES"
    READS = "READS"
    WRITES = "WRITES"
    ROUTES_TO = "ROUTES_TO"
    TESTS = "TESTS"
    # semantic
    IMPLEMENTS = "IMPLEMENTS"
    IMPLEMENTED_BY = "IMPLEMENTED_BY"
    REPRESENTS = "REPRESENTS"
    RELATED_TO = "RELATED_TO"
    # change / temporal
    CONTAINS_CHANGE = "CONTAINS_CHANGE"
    MODIFIES = "MODIFIES"
    INTRODUCED_BY = "INTRODUCED_BY"
    CHANGED_BY = "CHANGED_BY"
    CO_CHANGED_WITH = "CO_CHANGED_WITH"
    # evidence / decision
    SUPPORTED_BY = "SUPPORTED_BY"
    CONTRADICTS = "CONTRADICTS"
    DERIVED_FROM = "DERIVED_FROM"
    TARGETS = "TARGETS"
    AFFECTS = "AFFECTS"
    BASED_ON = "BASED_ON"
    REQUIRES_POLICY = "REQUIRES_POLICY"
    APPROVED_BY = "APPROVED_BY"


# layer membership for stats / selective building (G0..G4 ablation)
LAYER_OF_NODE: dict[NodeType, str] = {}
for _n, _layer in [
    *[(t, "code") for t in (NodeType.REPOSITORY, NodeType.FILE, NodeType.FUNCTION,
                            NodeType.METHOD, NodeType.CLASS, NodeType.COMPONENT,
                            NodeType.API_ENDPOINT, NodeType.VARIABLE, NodeType.TEST,
                            NodeType.UI_STRING, NodeType.CONFIG, NodeType.PROMPT,
                            NodeType.SCOPE, NodeType.CALL_NAME)],
    *[(t, "semantic") for t in (NodeType.FEATURE, NodeType.CAPABILITY,
                                NodeType.CONCEPT, NodeType.REQUIREMENT)],
    *[(t, "change") for t in (NodeType.COMMIT, NodeType.DIFF, NodeType.HUNK,
                              NodeType.CHANGED_SYMBOL, NodeType.CHANGE_UNIT,
                              NodeType.ISSUE, NodeType.PULL_REQUEST,
                              NodeType.TEST_FAILURE)],
    *[(t, "evidence") for t in (NodeType.TASK, NodeType.HYPOTHESIS,
                                NodeType.FINDING, NodeType.EVIDENCE)],
    *[(t, "decision") for t in (NodeType.DECISION, NodeType.POLICY)],
]:
    LAYER_OF_NODE[_n] = _layer

LAYER_OF_EDGE: dict[EdgeType, str] = {}
for _e, _layer in [
    *[(e, "code") for e in (EdgeType.CONTAINS, EdgeType.DEFINES, EdgeType.IMPORTS,
                            EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.READS,
                            EdgeType.WRITES, EdgeType.ROUTES_TO, EdgeType.TESTS)],
    *[(e, "semantic") for e in (EdgeType.IMPLEMENTS, EdgeType.IMPLEMENTED_BY,
                                EdgeType.REPRESENTS, EdgeType.RELATED_TO)],
    *[(e, "change") for e in (EdgeType.CONTAINS_CHANGE, EdgeType.MODIFIES,
                              EdgeType.INTRODUCED_BY, EdgeType.CHANGED_BY,
                              EdgeType.CO_CHANGED_WITH)],
    *[(e, "evidence") for e in (EdgeType.SUPPORTED_BY, EdgeType.CONTRADICTS,
                                EdgeType.DERIVED_FROM, EdgeType.TARGETS,
                                EdgeType.AFFECTS, EdgeType.BASED_ON)],
    *[(e, "decision") for e in (EdgeType.REQUIRES_POLICY, EdgeType.APPROVED_BY)],
]:
    LAYER_OF_EDGE[_e] = _layer


def stable_id(*parts: str, salt: str = "") -> str:
    """Deterministic id for unstructured content (evidence payloads etc.)."""
    basis = "\x1f".join(parts) + (f"\x1e{salt}" if salt else "")
    return f"{zlib.crc32(basis.encode('utf-8')):08x}"


@dataclass
class Node:
    id: str
    type: NodeType
    props: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.type, str):  # tolerate string input from v1 dicts
            self.type = NodeType(self.type)

    def layer(self) -> str:
        return LAYER_OF_NODE[self.type]


@dataclass
class Edge:
    src: str
    dst: str
    type: EdgeType
    props: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.type, str):
            self.type = EdgeType(self.type)

    # temporal helpers (optional fields live in props by design)
    def set_validity(self, from_commit: str | None, to_commit: str | None = None,
                     observed_at: str | None = None) -> "Edge":
        if from_commit:
            self.props["valid_from_commit"] = from_commit
        if to_commit:
            self.props["valid_to_commit"] = to_commit
        if observed_at:
            self.props["observed_at"] = observed_at
        return self

    def layer(self) -> str:
        return LAYER_OF_EDGE[self.type]


class GraphV2:
    """In-memory typed graph over the v2 schema.

    Not a replacement for the v1 ContextGraph/semantica.kg bridge — v1 stays
    the query engine for PathFinder; GraphV2 is the canonical *schema* the
    broker exposes. Both views are kept in sync by the broker.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[int, Edge] = {}      # keyed by (src,dst,type,salt)
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}

    # ------------------------------------------------------------- mutation
    @staticmethod
    def _ekey(src: str, dst: str, etype: EdgeType, props: dict) -> int:
        salt = props.get("salt", "")
        return hash((src, dst, etype, salt))

    def add_node(self, node: Node, merge_props: bool = True) -> Node:
        existing = self._nodes.get(node.id)
        if existing is None:
            self._nodes[node.id] = node
            return node
        if existing.type != node.type:
            raise GraphError(
                f"node id collision with different type: {node.id} "
                f"is {existing.type.value}, tried {node.type.value} "
                f"(conflicts must be resolved explicitly, never overwritten)")
        if merge_props:
            for k, v in node.props.items():
                if k in existing.props and existing.props[k] != v:
                    # keep first writer, record the disagreement
                    conflicts = existing.props.setdefault("_prop_conflicts", {})
                    conflicts[k] = v
                else:
                    existing.props[k] = v
        return existing

    def add_edge(self, edge: Edge) -> Edge:
        key = self._ekey(edge.src, edge.dst, edge.type, edge.props)
        existing = self._edges.get(key)
        if existing is not None:
            # v1 emits one edge per occurrence (e.g. every call site); v2
            # keeps one typed edge and counts occurrences — no info lost
            existing.props["count"] = int(existing.props.get("count", 1)) + 1
            existing.props.update({k: v for k, v in edge.props.items()
                                   if k not in existing.props and k != "count"})
            return existing
        if edge.src not in self._nodes or edge.dst not in self._nodes:
            missing = [x for x in (edge.src, edge.dst) if x not in self._nodes]
            raise GraphError(f"edge references unknown node(s): {missing}")
        self._edges[key] = edge
        self._out.setdefault(edge.src, []).append(edge)
        self._in.setdefault(edge.dst, []).append(edge)
        return edge

    # ------------------------------------------------------------- queries
    def node(self, nid: str) -> Node | None:
        return self._nodes.get(nid)

    def nodes_of_type(self, *types: NodeType) -> list[Node]:
        want = set(types)
        return [n for n in self._nodes.values() if n.type in want]

    def edges_of_type(self, *types: EdgeType) -> list[Edge]:
        want = set(types)
        return [e for e in self._edges.values() if e.type in want]

    def neighbors(self, nid: str, rel_types: set[EdgeType] | None = None,
                  direction: str = "both", depth: int = 1) -> list[Node]:
        seen: dict[str, None] = {}
        frontier = [nid]
        for _ in range(depth):
            nxt = []
            for cur in frontier:
                candidates: list[Edge] = []
                if direction in ("both", "out"):
                    candidates += self._out.get(cur, [])
                if direction in ("both", "in"):
                    candidates += self._in.get(cur, [])
                for e in candidates:
                    if rel_types and e.type not in rel_types:
                        continue
                    other = e.dst if e.src == cur else e.src
                    if other != nid and other not in seen:
                        seen[other] = None
                        nxt.append(other)
            frontier = nxt
        return [self._nodes[i] for i in seen if i in self._nodes]

    def edge_between(self, src: str, dst: str, *types: EdgeType) -> list[Edge]:
        want = set(types) if types else None
        return [e for e in self._out.get(src, [])
                if e.dst == dst and (want is None or e.type in want)]

    def layer_stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for n in self._nodes.values():
            bucket = out.setdefault(n.layer(), {"nodes": 0, "edges": 0})
            bucket["nodes"] += 1
        for e in self._edges.values():
            out.setdefault(e.layer(), {"nodes": 0, "edges": 0})["edges"] += 1
        return out

    def stats(self) -> dict:
        return {"nodes": len(self._nodes), "edges": len(self._edges),
                "by_layer": self.layer_stats()}

    def all_nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def all_edges(self) -> list[Edge]:
        return list(self._edges.values())

    # ------------------------------------------------------------- v1 bridge
    def to_v1_dicts(self) -> tuple[list[dict], list[dict]]:
        """Render as the v1 (entities/relationships) dict shapes so the
        Phase 5 semantica.kg pipeline keeps working unchanged."""
        entities = [{"id": n.id, "type": n.type.value, **{
            k: v for k, v in n.props.items() if not k.startswith("_")}}
            for n in self._nodes.values()]
        rels = [{"source": e.src, "target": e.dst, "type": e.type.value, **{
            k: v for k, v in e.props.items() if not k.startswith("_")}}
            for e in self._edges.values()]
        return entities, rels

    @classmethod
    def from_v1(cls, cg) -> "GraphV2":
        """Import of the Phase 5 ContextGraph (adapter, spec 9A).

        v1 tolerates dangling name references (e.g. JSX REFERENCES edges to
        callnames that never got a node); v2 materialises them as
        implicit CallName nodes so no edge is silently dropped.
        """
        g = cls()
        for e in cg.kg.entities:
            g.add_node(Node(id=e["id"], type=_coerce_type(e["type"]),
                            props={k: v for k, v in e.items()
                                   if k not in ("id", "type")}))
        for r in cg.kg.relationships:
            for end in (r["source"], r["target"]):
                if end not in g._nodes:
                    g.add_node(Node(id=end, type=NodeType.CALL_NAME,
                                    props={"implicit": True}))
            g.add_edge(Edge(src=r["source"], dst=r["target"],
                            type=EdgeType(r["type"]),
                            props={k: v for k, v in r.items()
                                   if k not in ("source", "target", "type")}))
        return g

    def sync_back_to_v1(self, cg) -> None:
        """Push v2-only content (semantic/evidence/decision layers) into the
        v1 semantica KnowledgeGraph so PathFinder keeps a full view."""
        entities, rels = self.to_v1_dicts()
        seen = {e["id"] for e in cg.kg.entities}
        known = {r["source"] + r["target"] + r["type"]
                 for r in cg.kg.relationships}
        for e in entities:
            if e["id"] not in seen:
                cg.kg.entities.append(e)
        for r in rels:
            if r["source"] + r["target"] + r["type"] not in known:
                cg.kg.relationships.append(r)
        cg._build_adj()
        cg._build_nx()


def _coerce_type(raw: str) -> NodeType:
    try:
        return NodeType(raw)
    except ValueError:
        # unknown v1 kinds degrade to generic semantic carriers rather than
        # crashing the import (the id keeps them distinguishable)
        return NodeType.CONCEPT


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
