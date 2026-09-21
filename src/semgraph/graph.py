"""Semantica Context Graph 适配层（mode=semantica）。

把代码 + git 知识建进 semantica 规范的内存 KnowledgeGraph
（entities/relationships dict），依赖：

- semantica.kg.KnowledgeGraph          —— 图容器
- semantica.kg.GraphBuilder            —— 构建/校验
- semantica.kg.PathFinder              —— 多跳路径查询
- semantica.provenance.ProvenanceManager —— 实体级 evidence/provenance
  （kg.ProvenanceTracker 在 0.6.8 已弃用）

节点类型（spec §3）：Repository、File、Function、Class、Component、
APIEndpoint、UIString、Commit、ChangeUnit。
边类型：DEFINES、IMPORTS、CALLS、REFERENCES、CONTAINS、CHANGED_BY、
MODIFIES、CO_CHANGED_WITH。

这是*结构+git 事实之上的可查询索引* —— Semantica 负责组织上下文/
provenance，绝不替代 AST parser（spec §3）。这里的所有查询都是只读图读取。
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

from src.errors import GraphError
from src.schema import ToolRecorder


def _import_semantica():
    try:
        from semantica.kg.knowledge_graph import KnowledgeGraph
        from semantica.kg.graph_builder import GraphBuilder
        from semantica.kg.path_finder import PathFinder
        from semantica.provenance import ProvenanceManager
        return KnowledgeGraph, GraphBuilder, PathFinder, ProvenanceManager
    except Exception as e:  # ImportError 或 ABI 问题 —— 显式抛出，绝不吞
        raise GraphError(
            f"semantica is required for mode=semantica: {e}. "
            f"Install with: pip install --no-deps semantica==0.6.8 "
            f"(or full: pip install semantica==0.6.8)") from e


class ContextGraph:
    """薄封装：从 CodeIndex+Git 事实建图，供任务上下文查询。"""

    def __init__(self, repo: Path, rec: ToolRecorder | None = None):
        self.repo = repo
        self.rec = rec
        KG, GB, PF, PT = _import_semantica()
        self._KG, self._GB, self._PF, self._PT = KG, GB, PF, PT
        self.kg = None            # semantica KnowledgeGraph
        self.graph_result = None  # GraphBuilder.build() 结果 dict
        self.prov = PT()
        self._by_id: dict[str, dict] = {}
        self._adj: dict[str, list[tuple[str, str, str]]] = {}  # id -> [(rel, 方向, 对端)]

    # ---------------------------------------------------------------- 构建
    def build(self, idx, git_api=None, commits_limit: int = 50) -> "ContextGraph":
        entities: list[dict] = []
        rels: list[dict] = []

        def ent(eid: str, etype: str, **props):
            e = {"id": eid, "type": etype, **props}
            entities.append(e)
            self._by_id[eid] = e
            return eid

        def rel(src, tgt, rtype, **props):
            rels.append({"source": src, "target": tgt, "type": rtype, **props})

        repo_id = ent(f"repo:{self.repo.name}", "Repository", path=str(self.repo))

        # 文件 + 符号
        for file in idx.files:
            fid = ent(f"file:{file}", "File", path=file)
            rel(repo_id, fid, "CONTAINS")
        for s in idx.symbols:
            kind = {"arrow_const": "Function", "function": "Function",
                    "method": "Method", "class": "Class",
                    "component": "Component", "page_component": "Component"}.get(
                        s.kind, s.kind.capitalize())
            sid = ent(f"sym:{s.qualified}", kind,
                      name=s.name, file=s.file, line=s.line_start,
                      exported=s.exported)
            rel(f"file:{s.file}", sid, "DEFINES")
        # 导入
        for ri in idx.import_edges:
            if ri.source_file:
                rel(f"file:{ri.importer}", f"file:{ri.source_file}", "IMPORTS",
                    names=",".join(ri.names))
        # 调用
        for e in idx.calls:
            src = f"scope:{e.caller}"
            if src not in self._by_id:
                ent(src, "Scope", name=e.caller, file=e.caller_file)
            # 边指向*名字*；解析到定义发生在查询时（一个名字可能对应
            # 0..n 个定义）
            tgt = f"callname:{e.callee_short}"
            if tgt not in self._by_id:
                ent(tgt, "CallName", name=e.callee_short)
            rel(src, tgt, "CALLS", at=f"{e.file}:{e.line}")
        # JSX 引用
        for fp in idx.parses.values():
            for r in fp.jsx_refs:
                rel(f"file:{r.file}", f"callname:{r.component}", "REFERENCES",
                    at=f"{r.file}:{r.line}")
        # API 端点
        for ep in idx.api_endpoints:
            eid = ent(f"api:{ep.file}", "APIEndpoint", route=ep.route_path,
                      methods=ep.methods, file=ep.file)
            rel(f"file:{ep.file}", eid, "DEFINES")
        # UI 字符串（字符串字面量 + JSX 文本）
        for fp in idx.parses.values():
            for u in fp.ui_strings:
                uid = f"uistr:{u.file}:{u.line}:{zlib.crc32(u.text.encode('utf-8'))}"
                if uid not in self._by_id:
                    ent(uid, "UIString", text=u.text[:200], file=u.file, line=u.line)
                    rel(f"file:{u.file}", uid, "CONTAINS")

        # 名字解析：callname:X -> 每个名为 X 的定义。没有这些边图是断的
        #（scope -> callname 没有出边），多跳路径必须经过它们。
        syms_by_name: dict[str, list[str]] = {}
        for e in entities:
            if e["id"].startswith("sym:"):
                syms_by_name.setdefault(e.get("name", ""), []).append(e["id"])
        for e in entities:
            if e["id"].startswith("callname:"):
                for sid in syms_by_name.get(e.get("name", ""), []):
                    rel(e["id"], sid, "REFERENCES", resolved="possible")

        # git 层
        if git_api is not None:
            try:
                commits = git_api.log(max_count=commits_limit)
                co = git_api.co_change_pairs(max_count=commits_limit)
                for c in commits:
                    cid = ent(f"commit:{c.sha}", "Commit", sha=c.sha, subject=c.subject,
                              date=c.date, author=c.author)
                    for f in c.files:
                        if f in idx.files or any(f == s.file for s in idx.symbols):
                            rel(cid, f"file:{f}", "MODIFIES")
                for (a, b), n in co.items():
                    if a in idx.files and b in idx.files:
                        rel(f"file:{a}", f"file:{b}", "CO_CHANGED_WITH", support=n)
            except Exception as e:  # 建图不能死于 git 的小毛病
                if self.rec:
                    self.rec.warn(f"context graph: git layer skipped ({e})")

        KG, GB = self._KG, self._GB
        self.kg = KG(entities=entities, relationships=rels,
                     metadata={"repo": str(self.repo), "builder": "dataAgent-semgraph"})
        builder = GB()
        # 输入是预抽取的 dict —— 关掉文本抽取
        self.graph_result = builder.build(
            {"entities": entities, "relationships": rels}, extract=False)
        self._build_adj()
        self._build_nx()
        if self.rec:
            self.rec.tool("semgraph:build")
        return self

    def _build_adj(self) -> None:
        self._adj.clear()
        for r in self.kg.relationships:
            s, t = r["source"], r["target"]
            self._adj.setdefault(s, []).append((r["type"], "out", t))
            self._adj.setdefault(t, []).append((r["type"], "in", s))

    def _build_nx(self) -> None:
        """无向 networkx 投影，让 semantica PathFinder 能跑真查询。刻意
        无向：caller 与 callee 之间的"关联链"需要双向穿过 CALLS 边。"""
        import networkx as nx
        self.nx = nx.Graph()
        for e in self.kg.entities:
            self.nx.add_node(e["id"])
        for r in self.kg.relationships:
            self.nx.add_edge(r["source"], r["target"], type=r["type"])

    # ---------------------------------------------------------------- 查询
    def neighbors(self, node_id: str, rel_types: set[str] | None = None,
                  direction: str = "both", depth: int = 1) -> list[dict]:
        """BFS 邻域；返回实体 dict（去重、不含起点）。"""
        seen: dict[str, int] = {}
        frontier = [node_id]
        for _ in range(depth):
            nxt = []
            for cur in frontier:
                for rel_t, d, other in self._adj.get(cur, []):
                    if rel_types and rel_t not in rel_types:
                        continue
                    if direction != "both" and d != direction:
                        continue
                    if other not in seen and other != node_id:
                        seen[other] = 1
                        nxt.append(other)
            frontier = nxt
        return [self._by_id[i] for i in seen if i in self._by_id]

    def path(self, start: str, end: str, max_hops: int = 6) -> list[str] | None:
        """经 semantica PathFinder 在 nx 投影上查关联路径。"""
        if start == end:
            return [start]
        try:
            pf = self._PF()
            found = pf.find_shortest_path(self.nx, start, end)
        except Exception as e:
            raise GraphError(f"PathFinder query failed ({start} -> {end}): {e}") from e
        return list(found) if found else None

    def find_symbols(self, name: str) -> list[dict]:
        return [e for e in self.kg.entities
                if e.get("name") == name and e["type"] in
                ("Function", "Method", "Class", "Component")]

    def callname_defs(self, name: str) -> list[dict]:
        """把 CallName 解析到同名的定义符号。"""
        return [e for e in self.kg.entities
                if e.get("name") == name and e["type"] in
                ("Function", "Method", "Class", "Component")]

    def route_reaching(self, symbol_id: str, max_hops: int = 4) -> list[dict]:
        """在 max_hops 跳 CALLS 内触达该符号的 API 端点。

        向内走 CALLS 边（caller 的 caller……），把每个 CallName 解析到
        定义；路由的定义文件同时 DEFINES 一个 APIEndpoint 才算数。
        """
        result: list[dict] = []
        seen_defs: set[str] = set()
        frontier = {symbol_id}
        for _ in range(max_hops):
            callers: set[str] = set()
            for cur in frontier:
                for e in self.neighbors(cur, rel_types={"CALLS"}, direction="in", depth=1):
                    callers.add(e["id"])
                    if not e["id"].startswith("callname:"):
                        continue
                    for d in self.callname_defs(e.get("name", "")):
                        if d["id"] in seen_defs or not d.get("file"):
                            continue
                        seen_defs.add(d["id"])
                        for f in self.neighbors(d["id"], rel_types={"DEFINES"}, direction="in"):
                            for api in self.neighbors(f["id"], rel_types={"DEFINES"}, direction="out"):
                                if api["type"] == "APIEndpoint":
                                    result.append(api)
            # callname -> 各自定义的 scope，让游走能继续向上
            frontier = set()
            for cid in callers:
                if cid.startswith("scope:"):
                    frontier.add(cid)
                elif cid.startswith("callname:"):
                    name = self._by_id[cid].get("name", "")
                    for d in self.callname_defs(name):
                        frontier.add(f"scope:{d['id'][4:]}")  # scope 按限定名做 key
            if not frontier:
                break
        return result

    def ui_strings_matching(self, substr: str) -> list[dict]:
        return [e for e in self.kg.entities if e["type"] == "UIString"
                and substr in str(e.get("text", ""))]

    # ---------------------------------------------------------------- 溯源
    def track(self, entity_id: str, source: str, **details) -> None:
        self.prov.track_entity(entity_id, source, metadata=dict(details))

    def sources_of(self, entity_id: str) -> list[dict]:
        return self.prov.get_all_sources(entity_id)

    # ---------------------------------------------------------------- io
    def dump(self, path: Path) -> None:
        data = {"entities": self.kg.entities, "relationships": self.kg.relationships,
                "metadata": self.kg.metadata}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def stats(self) -> dict:
        return {"entities": len(self.kg.entities),
                "relationships": len(self.kg.relationships)}
