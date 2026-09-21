"""ChangeService（Phase 11D）：变更层事实的组合与查询。

确定性服务：目标的时间性上下文（最近触达的 ChangeUnit/Commit）、
单元筛选、两组文件集合之间的直接 IMPORTS 耦合。读取时铸造
CHANGE_UNIT evidence（原则 5）。逻辑自 ContextBroker 原样迁入。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.objects import Evidence, EvidenceType
from src.semgraph.schema_v2 import EdgeType, Node, NodeType


@dataclass
class ChangeContext:
    """时间性事实：最近触达目标的 change unit 有哪些。"""
    target: str = ""
    last_commits: list[Node] = field(default_factory=list)
    change_units: list[Node] = field(default_factory=list)   # 由 9E 填充
    evidence_ids: list[str] = field(default_factory=list)


class ChangeService:
    def __init__(self, broker):
        self.broker = broker

    def get_change_context(self, target_id: str) -> ChangeContext:
        """时间性上下文：最近触达目标的 ChangeUnit 与 Commit，新的在前，
        HEAD 关联显式可查。回退规划从这里拿单元起步 —— 绝不从"那个
        commit"起步（spec 9E：commit != change unit）。"""
        g = self.broker.graph
        ctx = ChangeContext(target=target_id)
        fid = self._file_of(target_id)
        if not fid:
            return ctx
        units = [cu for cu in g.neighbors(fid, rel_types={EdgeType.MODIFIES},
                                          direction="in")
                 if cu.type == NodeType.CHANGE_UNIT]
        units.sort(key=self._commit_date_of, reverse=True)
        ctx.change_units = units[:10]
        commits = [c for c in g.neighbors(fid, rel_types={EdgeType.CHANGED_BY},
                                          direction="out")
                   if c.type == NodeType.COMMIT]
        commits += [c for c in g.neighbors(fid, rel_types={EdgeType.MODIFIES},
                                           direction="in")
                    if c.type == NodeType.COMMIT]
        dedup: dict[str, Node] = {c.id: c for c in commits}
        ctx.last_commits = sorted(dedup.values(),
                                  key=self._commit_date_of, reverse=True)[:10]
        # 读取时铸造 provenance（原则 5）
        ev = Evidence.make(
            EvidenceType.CHANGE_UNIT, source="broker:get_change_context",
            target=target_id, location=fid,
            payload=f"{len(ctx.change_units)} change unit(s), "
                    f"{len(ctx.last_commits)} commit(s) touch {fid}")
        self.broker.add_evidence(ev)
        ctx.evidence_ids.append(ev.id)
        return ctx

    def _commit_date_of(self, node: Node) -> str:
        if node.type == NodeType.CHANGE_UNIT:
            cn = self.broker.graph.node(
                f"commit:{node.props.get('commit', '')}")
            return cn.props.get("date", "") if cn else ""
        return node.props.get("date", "")

    def _file_of(self, node_id: str) -> str | None:
        n = self.broker.graph.node(node_id)
        if n is None:
            return None
        if n.type == NodeType.FILE:
            return node_id
        f = n.props.get("file")
        return f"file:{f}" if f else None

    def find_change_units(self, label: str = "", file: str = "",
                          commit: str = "") -> list[Node]:
        """给 change-intelligence agent 的 ChangeUnit 查询（file 是对
        单元文件的子串匹配）。"""
        out = []
        for cu in self.broker.graph.nodes_of_type(NodeType.CHANGE_UNIT):
            p = cu.props
            if label and p.get("semantic_label", "") != label:
                continue
            if commit and p.get("commit", "") != commit:
                continue
            if file and not any(file in f for f in p.get("files", [])):
                continue
            out.append(cu)
        return out

    def import_couplings(self, files_a: list[str],
                         files_b: list[str]) -> list[str]:
        """两个文件集合之间的直接 IMPORTS 边，双向都查 —— 回退规划的
        附带损伤信号。"""
        g = self.broker.graph
        a = {f"file:{f}" for f in files_a}
        b = {f"file:{f}" for f in files_b}
        out = []
        for fa in a:
            for e in g.edges_from(fa):
                if e.type == EdgeType.IMPORTS and e.dst in b and e.dst != fa:
                    out.append(f"{fa[len('file:'):]} imports {e.dst[len('file:'):]}")
        for fb in b:
            for e in g.edges_from(fb):
                if e.type == EdgeType.IMPORTS and e.dst in a and e.dst != fb:
                    out.append(f"{fb[len('file:'):]} imports {e.dst[len('file:'):]}")
        return sorted(set(out))
