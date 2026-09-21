"""Graph Schema v2（Phase 9A）。

把 Phase 5 的结构图升级为六个逻辑层：

    Code Graph        代码层 Repository/File/Function/Class/Component/...
    Semantic Graph    语义层 Feature/Capability/Concept/Requirement
    Change Graph      变更层 Commit/Diff/Hunk/ChangedSymbol/ChangeUnit/...
    Evidence Graph    证据层 Task/Hypothesis/Finding/Evidence
    Task Graph        任务层 以上各层的有界切片（见 task_view.py）
    Decision Graph    决策层 Decision/Policy

设计约束（spec §9A + 工程原则）：
- 每个节点有稳定 id（保留 v1 前缀：file:/sym:/commit:/...，Phase 5 的
  ContextGraph 可无损双向转换）
- 时间边在 props 里带可选的 valid_from_commit / valid_to_commit /
  observed_at —— 不做完整历史重建（v1 的承诺），但 HEAD 关系能关联到
  最近触达它的 ChangeUnit/Commit
- 属性冲突绝不静默覆盖（原则 6）：对已有 id 用不同 type add_node 直接
  raise GraphError
- 这套 schema 是我们自己的 dataclass 层；semantica.kg 对象留在
  ContextBroker 后面（原则：Semantica 是基础设施，不是框架）
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
    # v1 沿用的内部辅助类型（不在 spec 清单里，但名字解析路径需要；
    # 经 adapter 始终可达）
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
    # 结构
    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    REFERENCES = "REFERENCES"
    READS = "READS"
    WRITES = "WRITES"
    ROUTES_TO = "ROUTES_TO"
    TESTS = "TESTS"
    # 语义
    IMPLEMENTS = "IMPLEMENTS"
    IMPLEMENTED_BY = "IMPLEMENTED_BY"
    REPRESENTS = "REPRESENTS"
    RELATED_TO = "RELATED_TO"
    # 变更 / 时间
    CONTAINS_CHANGE = "CONTAINS_CHANGE"
    MODIFIES = "MODIFIES"
    INTRODUCED_BY = "INTRODUCED_BY"
    CHANGED_BY = "CHANGED_BY"
    CO_CHANGED_WITH = "CO_CHANGED_WITH"
    # 证据 / 决策
    SUPPORTED_BY = "SUPPORTED_BY"
    CONTRADICTS = "CONTRADICTS"
    DERIVED_FROM = "DERIVED_FROM"
    TARGETS = "TARGETS"
    AFFECTS = "AFFECTS"
    BASED_ON = "BASED_ON"
    REQUIRES_POLICY = "REQUIRES_POLICY"
    APPROVED_BY = "APPROVED_BY"


# 层归属表：stats 统计与选择性建层用（Phase 10 G0..G4 消融）
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
    """非结构化内容（evidence payload 等）的确定性 id。"""
    basis = "\x1f".join(parts) + (f"\x1e{salt}" if salt else "")
    return f"{zlib.crc32(basis.encode('utf-8')):08x}"


@dataclass
class Node:
    id: str
    type: NodeType
    props: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.type, str):  # 容忍 v1 dict 直接传字符串 type
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

    # 时间字段辅助（可选字段按设计放在 props 里）
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
    """v2 schema 上的内存 typed graph。

    不替代 v1 ContextGraph/semantica.kg 桥 —— v1 仍是 PathFinder 的查询
    引擎；GraphV2 是 broker 对外暴露的规范 *schema*。两个视图由 broker
    负责保持同步。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[int, Edge] = {}      # key 为 (src,dst,type,salt)
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}

    # ------------------------------------------------------------- 写入
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
                    # 先写者胜，分歧记录在案
                    conflicts = existing.props.setdefault("_prop_conflicts", {})
                    conflicts[k] = v
                else:
                    existing.props[k] = v
        return existing

    def add_edge(self, edge: Edge) -> Edge:
        key = self._ekey(edge.src, edge.dst, edge.type, edge.props)
        existing = self._edges.get(key)
        if existing is not None:
            # v1 每次出现发一条边（如每个调用点）；v2 只留一条 typed edge
            # 并累计次数 —— 信息不丢
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

    # ------------------------------------------------------------- 查询
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

    def edges_from(self, nid: str) -> list[Edge]:
        return list(self._out.get(nid, []))

    def edges_to(self, nid: str) -> list[Edge]:
        return list(self._in.get(nid, []))

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

    def prune_to_layers(self, active: set[str]) -> "GraphV2":
        """按激活层裁剪出副本（Phase 10 G0-G4）。

        节点属于激活层才保留；边要自身层激活且两端点都存活才保留。
        """
        out = GraphV2()
        for n in self._nodes.values():
            if n.layer() in active:
                out.add_node(Node(n.id, n.type, props=dict(n.props)))
        for e in self._edges.values():
            if (e.layer() in active and e.src in out._nodes
                    and e.dst in out._nodes):
                out.add_edge(Edge(e.src, e.dst, e.type, props=dict(e.props)))
        return out

    def all_nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def all_edges(self) -> list[Edge]:
        return list(self._edges.values())

    # ------------------------------------------------------------- v1 桥接
    def to_v1_dicts(self) -> tuple[list[dict], list[dict]]:
        """渲染成 v1（entities/relationships）dict 形态，Phase 5 的
        semantica.kg 管线因此一行不用改。"""
        entities = [{"id": n.id, "type": n.type.value, **{
            k: v for k, v in n.props.items() if not k.startswith("_")}}
            for n in self._nodes.values()]
        rels = [{"source": e.src, "target": e.dst, "type": e.type.value, **{
            k: v for k, v in e.props.items() if not k.startswith("_")}}
            for e in self._edges.values()]
        return entities, rels

    @classmethod
    def from_v1(cls, cg) -> "GraphV2":
        """导入 Phase 5 ContextGraph（adapter，spec 9A）。

        v1 容忍悬空的名字引用（如 JSX REFERENCES 指向从未建节点的
        callname）；v2 把它们物化为隐式 CallName 节点，任何边都不会被
        静默丢弃。
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
        """把 v2 独有的内容（semantic/evidence/decision 层）推回 v1
        semantica KnowledgeGraph，PathFinder 始终看得到完整视图。"""
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
        # v1 未知类型降级为通用语义载体而不是让导入崩溃
        #（id 保证它们仍可区分）
        return NodeType.CONCEPT


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
