"""Semantica Context Graph adapter (mode=semantica).

Builds the code+git knowledge into semantica's canonical in-memory
KnowledgeGraph (entities/relationships dicts), backed by:

- semantica.kg.KnowledgeGraph          — the graph container
- semantica.kg.GraphBuilder            — construction/validation
- semantica.kg.PathFinder              — multi-hop path queries
- semantica.provenance.ProvenanceManager — evidence/provenance per entity
  (kg.ProvenanceTracker is deprecated in 0.6.8)

Node types (spec §3): Repository, File, Function, Class, Component,
APIEndpoint, UIString, Commit, ChangeUnit.
Edge types: DEFINES, IMPORTS, CALLS, REFERENCES, CONTAINS, CHANGED_BY,
MODIFIES, CO_CHANGED_WITH.

This is a *queryable index over structural+git facts* — Semantica organizes
context/provenance; it never replaces the AST parser (spec §3).
All queries here are read-only graph reads.
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
    except Exception as e:  # ImportError or ABI issues — surface, never swallow
        raise GraphError(
            f"semantica is required for mode=semantica: {e}. "
            f"Install with: pip install --no-deps semantica==0.6.8 "
            f"(or full: pip install semantica==0.6.8)") from e


class ContextGraph:
    """Thin wrapper: build from CodeIndex+Git facts, query for task contexts."""

    def __init__(self, repo: Path, rec: ToolRecorder | None = None):
        self.repo = repo
        self.rec = rec
        KG, GB, PF, PT = _import_semantica()
        self._KG, self._GB, self._PF, self._PT = KG, GB, PF, PT
        self.kg = None            # semantica KnowledgeGraph
        self.graph_result = None  # GraphBuilder.build() result dict
        self.prov = PT()
        self._by_id: dict[str, dict] = {}
        self._adj: dict[str, list[tuple[str, str, str]]] = {}  # id -> [(rel, dir, other)]

    # ---------------------------------------------------------------- build
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

        # files + symbols
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
        # imports
        for ri in idx.import_edges:
            if ri.source_file:
                rel(f"file:{ri.importer}", f"file:{ri.source_file}", "IMPORTS",
                    names=",".join(ri.names))
        # calls
        for e in idx.calls:
            src = f"scope:{e.caller}"
            if src not in self._by_id:
                ent(src, "Scope", name=e.caller, file=e.caller_file)
            # edge targets the *name*; resolution to a definition happens at
            # query time (there may be 0..n definitions per name)
            tgt = f"callname:{e.callee_short}"
            if tgt not in self._by_id:
                ent(tgt, "CallName", name=e.callee_short)
            rel(src, tgt, "CALLS", at=f"{e.file}:{e.line}")
        # jsx references
        for fp in idx.parses.values():
            for r in fp.jsx_refs:
                rel(f"file:{r.file}", f"callname:{r.component}", "REFERENCES",
                    at=f"{r.file}:{r.line}")
        # api endpoints
        for ep in idx.api_endpoints:
            eid = ent(f"api:{ep.file}", "APIEndpoint", route=ep.route_path,
                      methods=ep.methods, file=ep.file)
            rel(f"file:{ep.file}", eid, "DEFINES")
        # ui strings (string literals + jsx text)
        for fp in idx.parses.values():
            for u in fp.ui_strings:
                uid = f"uistr:{u.file}:{u.line}:{zlib.crc32(u.text.encode('utf-8'))}"
                if uid not in self._by_id:
                    ent(uid, "UIString", text=u.text[:200], file=u.file, line=u.line)
                    rel(f"file:{u.file}", uid, "CONTAINS")

        # name resolution: callname:X -> every definition named X.
        # Without these edges the graph is disconnected (scope -> callname
        # has no onward edge), so multi-hop paths must traverse them.
        syms_by_name: dict[str, list[str]] = {}
        for e in entities:
            if e["id"].startswith("sym:"):
                syms_by_name.setdefault(e.get("name", ""), []).append(e["id"])
        for e in entities:
            if e["id"].startswith("callname:"):
                for sid in syms_by_name.get(e.get("name", ""), []):
                    rel(e["id"], sid, "REFERENCES", resolved="possible")

        # git layer
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
            except Exception as e:  # graph build shouldn't die on git hiccups
                if self.rec:
                    self.rec.warn(f"context graph: git layer skipped ({e})")

        KG, GB = self._KG, self._GB
        self.kg = KG(entities=entities, relationships=rels,
                     metadata={"repo": str(self.repo), "builder": "dataAgent-semgraph"})
        builder = GB()
        # our source is a pre-extracted dict — disable text extraction
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
        """Undirected networkx projection so semantica PathFinder can run
        real queries. Undirected on purpose: a "chain of relevance" between
        caller and callee must traverse CALLS edges in both directions."""
        import networkx as nx
        self.nx = nx.Graph()
        for e in self.kg.entities:
            self.nx.add_node(e["id"])
        for r in self.kg.relationships:
            self.nx.add_edge(r["source"], r["target"], type=r["type"])

    # ---------------------------------------------------------------- queries
    def neighbors(self, node_id: str, rel_types: set[str] | None = None,
                  direction: str = "both", depth: int = 1) -> list[dict]:
        """BFS neighborhood; returns entity dicts (deduped, source excluded)."""
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
        """Relation path via semantica PathFinder over the nx projection."""
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
        """Resolve a CallName to definition symbols with that name."""
        return [e for e in self.kg.entities
                if e.get("name") == name and e["type"] in
                ("Function", "Method", "Class", "Component")]

    def route_reaching(self, symbol_id: str, max_hops: int = 4) -> list[dict]:
        """API endpoints that reach the symbol within max_hops CALLS hops.

        Walks CALLS edges inward (callers of callers...), resolving each
        CallName to its definitions; a route counts if its defining file
        also DEFINES an APIEndpoint.
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
            # map callnames -> their defs' scopes so the walk can continue upward
            frontier = set()
            for cid in callers:
                if cid.startswith("scope:"):
                    frontier.add(cid)
                elif cid.startswith("callname:"):
                    name = self._by_id[cid].get("name", "")
                    for d in self.callname_defs(name):
                        frontier.add(f"scope:{d['id'][4:]}")  # scope keyed by qualified name
            if not frontier:
                break
        return result

    def ui_strings_matching(self, substr: str) -> list[dict]:
        return [e for e in self.kg.entities if e["type"] == "UIString"
                and substr in str(e.get("text", ""))]

    # ---------------------------------------------------------------- provenance
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
