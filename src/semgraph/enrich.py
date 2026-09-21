"""semantica 模式的图邻域证据（Phase 5）。

结构层回答"这里有什么"；图层回答"与这里*相连*的是什么" —— 纯符号级
查询拿不到的跨文件聚合：

- locate：与已命中文件相连（IMPORTS / CO_CHANGED_WITH）的文件获得
  加成；lexical/structural 候选里没有的按名符号与 UIString 命中，
  带图 provenance 进入列表。
- impact：PathFinder 从目标符号到 API 端点的路径变成相关路由上的
  `via` 链。
- rollback：保留侧与回退侧文件之间的耦合成为显式证据（图形态的
  附带损伤信号）。

所有加成都是加法式且克制：图是第 4 层上下文，不是前三层的替代品。
"""
from __future__ import annotations

from pathlib import Path

from src.schema import Evidence, ToolRecorder

# 加成权重 —— 刻意小于 structural/git 层的权重
GRAPH_IMPORT_BOOST = 1.3     # 命中文件 B import 命中文件 A
GRAPH_COOCCUR_BOOST = 1.15   # git 共变邻居也命中
GRAPH_SYMBOL_NEW = 8.0       # 经图按精确名找到的符号
GRAPH_UISTR_BOOST = 1.4      # 文件含匹配中文查询的 UI 字符串

_graph_cache: dict[str, "ContextGraph"] = {}


def get_context_graph(repo: Path, rec: ToolRecorder | None = None):
    """构建（或复用）本进程内某 repo 的 ContextGraph。"""
    from src.semgraph.graph import ContextGraph
    key = str(repo.resolve())
    cg = _graph_cache.get(key)
    if cg is None:
        from src.git_history.api import GitAPI
        from src.structural.enrich import build_index
        idx = build_index(repo, rec)
        git_api = GitAPI(repo, rec) if (repo / ".git").exists() else None
        cg = ContextGraph(repo, rec).build(idx, git_api)
        _graph_cache[key] = cg
    return cg


# ---------------------------------------------------------------- locate（定位）
def enrich_locate_with_graph(repo: Path, cands: list, qt, rec: ToolRecorder) -> list:
    cg = get_context_graph(repo, rec)
    rec.tool("semgraph:query")

    matched = {c.file for c in cands}

    def graph_evidence(kind: str, detail: str, source: str) -> Evidence:
        return Evidence(kind=kind, source=source, detail=detail, snippet="")

    # 1) 邻域加成：与已命中文件相连的文件
    for c in cands:
        fid = f"file:{c.file}"
        importers = {e["id"][len("file:"):] for e in cg.neighbors(
            fid, rel_types={"IMPORTS"}, direction="in")}
        co_neighbours = {e["id"][len("file:"):] for e in cg.neighbors(
            fid, rel_types={"CO_CHANGED_WITH"})}
        linked_import = next((f for f in importers if f in matched and f != c.file), None)
        linked_co = next((f for f in co_neighbours if f in matched and f != c.file), None)
        boost = 1.0
        if linked_import:
            boost *= GRAPH_IMPORT_BOOST
            c.evidence.append(graph_evidence(
                "graph_import", f"imported by matched file {linked_import}", linked_import))
        if linked_co:
            boost *= GRAPH_COOCCUR_BOOST
            c.evidence.append(graph_evidence(
                "graph_cochange", f"git co-change with matched file {linked_co}", linked_co))
        if boost > 1.0:
            c.reason += f" +graph({boost:.2f}x)"
            c.score = round(c.score * boost, 3)

    # 2) 经图做精确符号名查询
    from src.structural.parser import Symbol  # noqa: F401  （仅类型标注用）
    seen_files = {c.file for c in cands}
    for term, origin in (qt.all_search_terms() if qt else []):
        if len(term) < 4 or not term[0].isalpha():
            continue
        for sym in cg.find_symbols(term):
            f = sym.get("file")
            if not f or f in seen_files:
                continue
            seen_files.add(f)
            from src.schema import LocatedCandidate
            cands.append(LocatedCandidate(
                file=f, symbol=sym.get("name"), kind="function",
                line_start=sym.get("line"), line_end=sym.get("line"),
                score=GRAPH_SYMBOL_NEW,
                reason=f"graph: symbol '{term}' defined here ({sym['type']})",
                evidence=[graph_evidence(
                    "graph_symbol", f"graph entity {sym['id']} type={sym['type']} "
                    f"matched query term '{term}' [{origin}]", sym["id"])]))

    # 3) 中文逐字片段对 UIString 节点
    for seg in (qt.cjk_segments if qt else []):
        if len(seg) < 2:
            continue
        hits = cg.ui_strings_matching(seg)
        if not hits:
            continue
        by_file: dict[str, Evidence] = {}
        for u in hits[:5]:
            f = u.get("file")
            if f and f not in by_file:
                by_file[f] = graph_evidence(
                    "graph_uistring", f"UI string contains '{seg}': \"{u.get('text', '')[:60]}\"",
                    f"{f}:{u.get('line')}")
        for f, ev in by_file.items():
            existing = next((c for c in cands if c.file == f), None)
            if existing is not None:
                existing.score = round(existing.score * GRAPH_UISTR_BOOST, 3)
                existing.evidence.append(ev)
            else:
                from src.schema import LocatedCandidate
                cands.append(LocatedCandidate(
                    file=f, kind="ui_string", score=GRAPH_SYMBOL_NEW * 0.6,
                    reason=f"graph: UI text mentions '{seg}'",
                    evidence=[ev]))
    return cands


# ---------------------------------------------------------------- impact（影响）
def enrich_impact_with_graph(repo: Path, name: str, result, rec: ToolRecorder) -> None:
    """就地给 ImpactResult 补 PathFinder 路由链与 provenance。"""
    cg = get_context_graph(repo, rec)
    rec.tool("semgraph:query")

    target = result.target
    if target is None or not target.name:
        return
    sym_id = f"sym:{target.file}::{target.name}"
    if sym_id not in cg._by_id:
        # 定义可能嵌套（带类前缀的限定名）
        cands = [e for e in cg.kg.entities
                 if e["id"].startswith("sym:") and e["id"].endswith(f"::{target.name}")]
        if not cands:
            return
        sym_id = cands[0]["id"]

    # provenance：注册 + 读回（分析的审计轨迹）
    cg.track(sym_id, f"impact:{target.file}",
             symbol=target.name, task="impact")
    srcs = cg.sources_of(sym_id)
    if srcs:
        target.evidence.append(Evidence(
            kind="graph_provenance", source=sym_id,
            detail=f"ProvenanceTracker: {len(srcs)} source(s); first={srcs[0]}"))

    # PathFinder 路由链：路由文件 --...-- 符号
    for side in result.related:
        if side.kind != "api_route":
            continue
        route_file = side.file
        # 找该文件的一个 scope 实体（其中定义的任意符号）
        scopes = [e for e in cg.kg.entities
                  if e["id"].startswith("scope:") and e.get("file") == route_file]
        chain = None
        for sc in scopes:
            chain = cg.path(sc["id"], sym_id)
            if chain:
                break
        if chain:
            readable = [cg._by_id[i].get("name", i) for i in chain
                        if i in cg._by_id and not i.startswith("callname:")]
            side.via = (side.via + " | graph: " if side.via else "graph: ") + \
                " -> ".join(readable)
            side.evidence.append(Evidence(
                kind="graph_path", source=sym_id,
                detail=f"PathFinder chain ({len(chain) - 1} hops): " +
                       " -> ".join(chain)))


# ---------------------------------------------------------------- rollback（回退）
def enrich_rollback_with_graph(repo: Path, result, rec: ToolRecorder) -> None:
    """跨单元耦合证据：回退侧文件调用保留侧代码。"""
    cg = get_context_graph(repo, rec)
    rec.tool("semgraph:query")

    rb_files = {f for u in result.change_units
                if u.unit_id in result.changes_to_rollback for f in u.files}
    keep_files = {f for u in result.change_units
                  if u.unit_id in result.changes_to_keep for f in u.files}
    if not rb_files or not keep_files:
        return

    couplings = 0

    for u in result.change_units:
        for f in u.files:
            fid = f"file:{f}"
            importers = {e["id"][len("file:")]: e for e in cg.neighbors(
                fid, rel_types={"IMPORTS"}, direction="in")}
            for imp_file in importers:
                if imp_file in keep_files:
                    couplings += 1
                    u.evidence.append(Evidence(
                        kind="graph_coupling", source=imp_file,
                        detail=f"keep-side file {imp_file} imports rollback file {f}"))
            for e in cg.neighbors(fid, rel_types={"IMPORTS"}, direction="out"):
                other = e["id"][len("file:"):]
                if other in keep_files:
                    couplings += 1
                    u.evidence.append(Evidence(
                        kind="graph_coupling", source=other,
                        detail=f"rollback file {f} imports keep-side file {other}"))

    # 两种结果都显式给出 —— "查过、无耦合"本身也是证据
    result.evidence.append(Evidence(
        kind="graph_coupling_check", source="semantica:context_graph",
        detail=(f"{couplings} import coupling(s) between rollback-side "
                f"({len(rb_files)} files) and keep-side ({len(keep_files)} files)")))
