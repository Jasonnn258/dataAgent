"""Change + Temporal Graph (Phase 9E).

Reuses git_history/ and change_units/ output as-is (no re-parsing of git,
no re-implementation of hunk clustering). Writes the change layer into the
GraphV2 behind a ContextBroker:

    Commit  -CONTAINS_CHANGE->   ChangeUnit
    ChangeUnit -INTRODUCED_BY->  Commit          (upward queries)
    ChangeUnit -MODIFIES->       File / ChangedSymbol / Feature
    ChangeUnit -CONTAINS_CHANGE-> Hunk            (atomic rollback unit)
    File     -CHANGED_BY->       Commit          (recency direction)
    File     -CO_CHANGED_WITH->  File            (support count, from v1)

Temporal fields live on every change-layer edge (valid_from_commit /
observed_at). Rollback analysis must consume ChangeUnits here — commit ==
change unit is never assumed (spec 9E): a commit may split into several
units, and only the unit-level view can express "roll back the login fix
but keep the title reword from the same commit".
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
    """Populate the change layer of a broker's graph. Idempotent: re-running
    merges into existing nodes/edges. Returns stats."""
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
            # unit -> files it modifies
            for f in unit.files:
                fid = f"file:{f}"
                if g.node(fid) is None:
                    g.add_node(Node(fid, NodeType.FILE, props={"path": f}))
                g.add_edge(Edge(uid, fid, EdgeType.MODIFIES, props=tprops))
            # unit -> features whose surfaces it touches (best effort: only
            # if the semantic layer seeded features IMPLEMENTS-ing the file)
            for fid in {f"file:{f}" for f in unit.files}:
                for feat in g.neighbors(fid, rel_types={EdgeType.IMPLEMENTS},
                                        direction="in"):
                    if feat.type == NodeType.FEATURE:
                        g.add_edge(Edge(uid, feat.id, EdgeType.MODIFIES,
                                        props=tprops))
            # unit -> ChangedSymbol nodes
            for sym in unit.symbols:
                sid = f"csym:{sha[:10]}:{sym}"
                if g.node(sid) is None:
                    g.add_node(Node(sid, NodeType.CHANGED_SYMBOL, props={
                        "symbol": sym, "commit": sha, "unit": unit.unit_id}))
                    n_symbols += 1
                g.add_edge(Edge(uid, sid, EdgeType.MODIFIES, props=tprops))
                # link to the definition if it exists in the code layer
                # (unit symbols are qualified file::name, same as sym: ids)
                if "::" in sym and g.node(f"sym:{sym}"):
                    g.add_edge(Edge(sid, f"sym:{sym}", EdgeType.REPRESENTS,
                                    props=tprops))
            # hunk-level nodes (atomic rollback granularity)
            for href in unit.hunks:
                hid = f"hunk:{unit.unit_id}:{href.file}:{href.hunk_idx}"
                if g.node(hid) is None:
                    g.add_node(Node(hid, NodeType.HUNK, props={
                        "file": href.file, "hunk_idx": href.hunk_idx,
                        "old_start": href.old_start, "new_start": href.new_start,
                        "old_lines": href.old_lines, "new_lines": href.new_lines}))
                    g.add_edge(Edge(uid, hid, EdgeType.CONTAINS_CHANGE,
                                    props=tprops))

        # file -> commit recency edge (v1 only wrote commit -> file MODIFIES;
        # CHANGED_BY is the direction temporal queries start from)
        for f in commit.files:
            fid = f"file:{f}"
            if g.node(fid) is not None and not g.edge_between(fid, cid, EdgeType.CHANGED_BY):
                g.add_edge(Edge(fid, cid, EdgeType.CHANGED_BY,
                                props={"valid_from_commit": sha,
                                       "observed_at": commit.date}))

    # co-change support edges (file level, aggregated over history)
    for (a, b), n in api.co_change_pairs(max_count=commits_limit).items():
        fa, fb = f"file:{a}", f"file:{b}"
        if g.node(fa) and g.node(fb) and not g.edge_between(fa, fb, EdgeType.CO_CHANGED_WITH):
            g.add_edge(Edge(fa, fb, EdgeType.CO_CHANGED_WITH,
                            props={"support": n}))

    rec.tool("change_graph:build")
    return {"commits": n_commits, "units": n_units, "changed_symbols": n_symbols}
