"""变更 + 时间图（Phase 9E）。

原样复用 git_history/ 与 change_units/ 的产出（不重解析 git、不重实现
hunk 聚类），把变更层写进 ContextBroker 背后的 GraphV2：

    Commit   -CONTAINS_CHANGE->    ChangeUnit
    ChangeUnit -INTRODUCED_BY->    Commit          （向上的查询方向）
    ChangeUnit -MODIFIES->         File / ChangedSymbol / Feature
    ChangeUnit -CONTAINS_CHANGE->  Hunk            （原子回退粒度）
    File      -CHANGED_BY->        Commit          （时近方向）
    File      -CO_CHANGED_WITH->   File            （带 support 计数，来自 v1）

每条变更层边都带时间字段（valid_from_commit / observed_at）。回退分析
必须在这里按 ChangeUnit 消费 —— 绝不假设 commit == change unit
（spec 9E）：一个 commit 可以拆成多个单元，只有单元级视图才能表达
"回退登录修复、保留同 commit 里的标题改写"。
"""
from __future__ import annotations

from pathlib import Path

from src.change_units.units import characterize_hunks, cluster_into_units
from src.git_history.api import GitAPI
from src.schema import ToolRecorder
from src.structural.enrich import build_index
from src.semgraph.objects import Evidence, EvidenceType
from src.semgraph.schema_v2 import Edge, EdgeType, Node, NodeType


def build_change_graph(broker, repo: Path | None = None,
                       commits_limit: int = 50) -> dict:
    """填充 broker 图的变更层。幂等：重复运行合并进已有节点/边。
    返回统计。"""
    repo = repo or broker.repo
    rec = broker.rec
    g = broker.graph
    api = GitAPI(repo, rec)
    idx = build_index(repo, rec)

    n_commits = n_units = n_symbols = 0
    for commit in api.log(max_count=commits_limit):
        sha = commit.sha
        cid = f"commit:{sha}"
        if g.node(cid) is None:
            g.add_node(Node(cid, NodeType.COMMIT, props={
                "sha": sha, "subject": commit.subject, "author": commit.author,
                "date": commit.date, "files": list(commit.files)}))
        n_commits += 1

        diff = api.diff(sha)
        infos = characterize_hunks(diff, idx, rec)
        units = cluster_into_units(sha, infos, idx, rec)
        unit_ids = []
        for unit in units:
            uid = f"cu:{unit.unit_id}"
            unit_ids.append(uid)
            if g.node(uid) is None:
                ev = Evidence.make(
                    EvidenceType.CHANGE_UNIT, source=f"git:{sha[:8]}",
                    target=uid, location=unit.files[0] if unit.files else "",
                    payload=f"[{unit.semantic_label}] {unit.summary} | "
                            f"files={list(unit.files)} symbols={list(unit.symbols)[:8]}")
                broker.add_evidence(ev)
                g.add_node(Node(uid, NodeType.CHANGE_UNIT, props={
                    "unit_id": unit.unit_id, "commit": sha,
                    "semantic_label": unit.semantic_label,
                    "summary": unit.summary, "files": list(unit.files),
                    "symbols": list(unit.symbols),
                    "ui_strings": list(unit.ui_strings)[:20],
                    "cohesion": unit.cohesion, "evidence_id": ev.id}))
                n_units += 1
            tprops = {"valid_from_commit": sha, "observed_at": commit.date}
            g.add_edge(Edge(cid, uid, EdgeType.CONTAINS_CHANGE, props=tprops))
            g.add_edge(Edge(uid, cid, EdgeType.INTRODUCED_BY, props=tprops))
            # 单元 -> 它修改的文件
            for f in unit.files:
                fid = f"file:{f}"
                if g.node(fid) is None:
                    g.add_node(Node(fid, NodeType.FILE, props={"path": f}))
                g.add_edge(Edge(uid, fid, EdgeType.MODIFIES, props=tprops))
            # 单元 -> 它触到表面的 feature（尽力而为：仅当语义层已给
            # 该文件种了 IMPLEMENTS 方向的 feature）
            for fid in {f"file:{f}" for f in unit.files}:
                for feat in g.neighbors(fid, rel_types={EdgeType.IMPLEMENTS},
                                        direction="in"):
                    if feat.type == NodeType.FEATURE:
                        g.add_edge(Edge(uid, feat.id, EdgeType.MODIFIES,
                                        props=tprops))
            # 单元 -> ChangedSymbol 节点
            for sym in unit.symbols:
                sid = f"csym:{sha[:10]}:{sym}"
                if g.node(sid) is None:
                    g.add_node(Node(sid, NodeType.CHANGED_SYMBOL, props={
                        "symbol": sym, "commit": sha, "unit": unit.unit_id}))
                    n_symbols += 1
                g.add_edge(Edge(uid, sid, EdgeType.MODIFIES, props=tprops))
                # 存在代码层定义时连过去（unit 符号是 file::name 限定式，
                # 与 sym: id 同构）
                if "::" in sym and g.node(f"sym:{sym}"):
                    g.add_edge(Edge(sid, f"sym:{sym}", EdgeType.REPRESENTS,
                                    props=tprops))
            # hunk 级节点（原子回退粒度）
            for href in unit.hunks:
                hid = f"hunk:{unit.unit_id}:{href.file}:{href.hunk_idx}"
                if g.node(hid) is None:
                    g.add_node(Node(hid, NodeType.HUNK, props={
                        "file": href.file, "hunk_idx": href.hunk_idx,
                        "old_start": href.old_start, "new_start": href.new_start,
                        "old_lines": href.old_lines, "new_lines": href.new_lines}))
                    g.add_edge(Edge(uid, hid, EdgeType.CONTAINS_CHANGE,
                                    props=tprops))

        # file -> commit 的时近边（v1 只写了 commit -> file 的 MODIFIES；
        # CHANGED_BY 才是时间查询起步的方向）
        for f in commit.files:
            fid = f"file:{f}"
            if g.node(fid) is not None and not g.edge_between(fid, cid, EdgeType.CHANGED_BY):
                g.add_edge(Edge(fid, cid, EdgeType.CHANGED_BY,
                                props={"valid_from_commit": sha,
                                       "observed_at": commit.date}))

    # 共变支持边（文件级，跨历史聚合）
    for (a, b), n in api.co_change_pairs(max_count=commits_limit).items():
        fa, fb = f"file:{a}", f"file:{b}"
        if g.node(fa) and g.node(fb) and not g.edge_between(fa, fb, EdgeType.CO_CHANGED_WITH):
            g.add_edge(Edge(fa, fb, EdgeType.CO_CHANGED_WITH,
                            props={"support": n}))

    rec.tool("change_graph:build")
    return {"commits": n_commits, "units": n_units, "changed_symbols": n_symbols}
