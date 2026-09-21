"""ResolutionService（Phase 11D）：目标解析与确定性邻域上下文。

确定性服务：模糊 query → 图节点（符号精确名 → 文件名退路），以及围绕
目标的 callers/callees/importers/routes 组合（v1 api_routes_reaching 语义，
深度封顶）。读取时铸造 AST evidence（原则 5）。逻辑自 ContextBroker
原样迁入。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.errors import DataAgentError, GraphError
from src.semgraph.objects import Evidence, EvidenceType
from src.semgraph.schema_v2 import EdgeType, Node, NodeType


@dataclass
class TargetContext:
    """解析到目标之后，围绕它的确定性邻域事实。"""
    target: Node
    definition: str = ""                # file:行号区间
    direct_callers: list[Node] = field(default_factory=list)
    direct_callees: list[Node] = field(default_factory=list)
    importers: list[Node] = field(default_factory=list)
    related_routes: list[Node] = field(default_factory=list)
    related_tests: list[Node] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


class ResolutionService:
    def __init__(self, broker):
        self.broker = broker

    def resolve_target(self, query: str) -> Node:
        g = self.broker.graph
        # 1) query 里出现精确符号名（camelCase/snake_case 形态）
        import re
        tokens = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
                  if any(c.isupper() for c in t[1:]) or "_" in t]
        for tok in tokens:
            for n in g.nodes_of_type(NodeType.FUNCTION, NodeType.METHOD,
                                     NodeType.CLASS, NodeType.COMPONENT):
                if n.props.get("name") == tok:
                    return n
        # 文件名退路
        for n in g.nodes_of_type(NodeType.FILE):
            name = n.id.split("/")[-1]
            if name in query or name.rsplit(".", 1)[0] in query:
                return n
        raise DataAgentError(f"cannot resolve target from query: {query!r}")

    def get_target_context(self, target_id: str) -> TargetContext:
        g = self.broker.graph
        node = g.node(target_id)
        if node is None:
            raise GraphError(f"unknown target node {target_id}")
        ctx = TargetContext(target=node)

        def is_def(n: Node) -> bool:
            return n.type in (NodeType.FUNCTION, NodeType.METHOD, NodeType.CLASS,
                              NodeType.COMPONENT)

        # caller：scope -CALLS-> callname -REFERENCES-> sym
        for callname in g.neighbors(target_id, rel_types={EdgeType.REFERENCES},
                                    direction="in"):
            for scope in g.neighbors(callname.id, rel_types={EdgeType.CALLS},
                                     direction="in"):
                ctx.direct_callers.append(scope)
        # callee：sym -REFERENCES-> callname <-CALLS- scope（要的是该符号
        # scope 可达的定义）
        scope_id = f"scope:{target_id[4:]}" if target_id.startswith("sym:") else None
        if scope_id and g.node(scope_id):
            for cn in g.neighbors(scope_id, rel_types={EdgeType.CALLS}, direction="out"):
                for d in g.neighbors(cn.id, rel_types={EdgeType.REFERENCES},
                                     direction="out"):
                    if d.id != target_id and is_def(d):
                        ctx.direct_callees.append(d)
        fid = f"file:{node.props.get('file', '')}" if node.props.get("file") else None
        if fid and g.node(fid):
            ctx.importers = g.neighbors(fid, rel_types={EdgeType.IMPORTS},
                                        direction="in")
            for api in g.neighbors(fid, rel_types={EdgeType.DEFINES}, direction="out"):
                if api.type == NodeType.API_ENDPOINT:
                    ctx.related_routes.append(api)
            ctx.definition = node.props.get("file", "")
        # 触达目标的路由：从每个 caller 沿调用链向上走（v1
        # api_routes_reaching 语义，确定性）。逻辑上一步 = callname:{caller
        # 短名} <-CALLS- 各 scope。深度封顶：这是路由发现，不是完整传递
        # 闭包。
        seen_routes: set[str] = {r.id for r in ctx.related_routes}
        seen_scopes: set[str] = set()
        frontier = [c.id for c in ctx.direct_callers]
        for _ in range(3):
            nxt: list[str] = []
            for sid in frontier:
                if sid in seen_scopes:
                    continue
                seen_scopes.add(sid)
                snode = g.node(sid)
                sfile = snode.props.get("file") if snode else None
                if sfile:
                    for api in g.neighbors(f"file:{sfile}",
                                           rel_types={EdgeType.DEFINES},
                                           direction="out"):
                        if api.type == NodeType.API_ENDPOINT and api.id not in seen_routes:
                            seen_routes.add(api.id)
                            ctx.related_routes.append(api)
                short = sid.split("::")[-1]
                for upper in g.neighbors(f"callname:{short}",
                                         rel_types={EdgeType.CALLS},
                                         direction="in"):
                    if upper.id not in seen_scopes:
                        nxt.append(upper.id)
            frontier = nxt
            if not frontier:
                break
        # 读取时铸造 evidence（原则 5）
        ev = Evidence.make(type=EvidenceType.AST,
                           source="broker:get_target_context", target=target_id,
                           location=ctx.definition,
                           payload=f"{len(ctx.direct_callers)} callers, "
                                   f"{len(ctx.direct_callees)} callees, "
                                   f"{len(ctx.importers)} importers")
        self.broker.add_evidence(ev)
        ctx.evidence_ids.append(ev.id)
        return ctx
