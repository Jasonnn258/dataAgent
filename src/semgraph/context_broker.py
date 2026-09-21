"""Context Broker (Phase 9B): the only door between agents and Semantica.

Agents never touch GraphV2 internals, semantica.kg objects, CodeIndex or
GitAPI directly — everything goes through this facade, and everything it
returns is a project-owned dataclass (spec 9B).

Why the indirection: it is the seam where evidence is minted at the moment
a fact enters the system (principle 5), where task views are budgeted
(principle: no whole-graph retrieval), and where the G0-G4 ablation can
switch layers on and off (Phase 10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.errors import DataAgentError, GraphError
from src.schema import ToolRecorder
from src.semgraph.objects import (Conflict, Decision, Evidence, EvidenceType,
                                  Finding, PolicyResult)
from src.semgraph.schema_v2 import (Edge, EdgeType, GraphV2, Node, NodeType,
                                    now_iso, stable_id)
from src.semgraph.task_view import TaskGraphView


@dataclass
class TargetContext:
    """Deterministic neighborhood facts around a resolved target."""
    target: Node
    definition: str = ""                # file:line range
    direct_callers: list[Node] = field(default_factory=list)
    direct_callees: list[Node] = field(default_factory=list)
    importers: list[Node] = field(default_factory=list)
    related_routes: list[Node] = field(default_factory=list)
    related_tests: list[Node] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ChangeContext:
    """Temporal facts: what change units last touched the target."""
    target: str = ""
    last_commits: list[Node] = field(default_factory=list)
    change_units: list[Node] = field(default_factory=list)   # 9E fills these
    evidence_ids: list[str] = field(default_factory=list)


class ContextBroker:
    def __init__(self, repo: Path, rec: ToolRecorder | None = None,
                 layers: set[str] | None = None):
        """layers: which graph layers are active for this run (G0..G4
        ablation). None = all currently built layers."""
        from src.semgraph.enrich import get_context_graph
        self.repo = repo
        self.rec = rec or ToolRecorder()
        self._v1 = get_context_graph(repo, self.rec)
        self.graph = GraphV2.from_v1(self._v1)
        self.layers = layers
        # object stores (evidence/decision registries are process-local v1)
        self._evidence: dict[str, Evidence] = {}
        self._findings: dict[str, Finding] = {}
        self._conflicts: list[Conflict] = []
        self._decisions: dict[str, Decision] = {}
        self._views: dict[str, TaskGraphView] = {}
        self._counter = 0

    # ------------------------------------------------------------ target
    def resolve_target(self, query: str) -> Node:
        """Deterministic target resolution: prefer exact symbol names in the
        query (camelCase tokens), fall back to filename matches. Fuzzy
        natural-language matching is the semantic mapper's job (9D), never
        this method's."""
        import re
        tokens = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
                  if any(c.isupper() for c in t[1:]) or "_" in t]
        for tok in tokens:
            for n in self.graph.nodes_of_type(NodeType.FUNCTION, NodeType.METHOD,
                                              NodeType.CLASS, NodeType.COMPONENT):
                if n.props.get("name") == tok:
                    return n
        # filename fallback
        for n in self.graph.nodes_of_type(NodeType.FILE):
            name = n.id.split("/")[-1]
            if name in query or name.rsplit(".", 1)[0] in query:
                return n
        raise DataAgentError(f"cannot resolve target from query: {query!r}")

    def get_target_context(self, target_id: str) -> TargetContext:
        node = self.graph.node(target_id)
        if node is None:
            raise GraphError(f"unknown target node {target_id}")
        ctx = TargetContext(target=node)
        g = self.graph

        def is_def(n: Node) -> bool:
            return n.type in (NodeType.FUNCTION, NodeType.METHOD, NodeType.CLASS,
                              NodeType.COMPONENT)

        # callers: scope -CALLS-> callname -REFERENCES-> sym
        for callname in g.neighbors(target_id, rel_types={EdgeType.REFERENCES},
                                    direction="in"):
            for scope in g.neighbors(callname.id, rel_types={EdgeType.CALLS},
                                     direction="in"):
                ctx.direct_callers.append(scope)
        # callees: sym -REFERENCES-> callname <-CALLS- scope (we want the defs
        # reachable from this symbol's scope)
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
        # routes reaching the target: walk UP the call chain from every
        # caller (v1 api_routes_reaching semantics, deterministic). One
        # logical step up = callname:{caller short name} <-CALLS- scopes.
        # Depth capped: this is route discovery, not full transitive closure.
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
        # evidence minted at read time (principle 5)
        ev = Evidence.make(type=EvidenceType.AST,
                           source="broker:get_target_context", target=target_id,
                           location=ctx.definition,
                           payload=f"{len(ctx.direct_callers)} callers, "
                                   f"{len(ctx.direct_callees)} callees, "
                                   f"{len(ctx.importers)} importers")
        self.add_evidence(ev)
        ctx.evidence_ids.append(ev.id)
        return ctx

    # ------------------------------------------------------------ views
    def create_task_view(self, task_id: str, target_ids: list[str],
                         relations: set[EdgeType] | None = None) -> TaskGraphView:
        view = TaskGraphView.select(self.graph, task_id, target_ids,
                                    rel_types=relations, trigger="init")
        self._views[task_id] = view
        return view

    def expand_task_view(self, task_id: str, seeds: list[str],
                         relations: set[EdgeType] | None = None,
                         depth: int = 1, trigger: str = "") -> TaskGraphView:
        view = self._views.get(task_id)
        if view is None:
            raise DataAgentError(f"no task view {task_id!r} — create it first")
        view.expand(self.graph, seeds, relations=relations, depth=depth,
                    trigger=trigger)
        return view

    def get_task_view(self, task_id: str) -> TaskGraphView:
        return self._views[task_id]

    # ------------------------------------------------------------ change
    def get_change_context(self, target_id: str) -> ChangeContext:
        """Temporal context: the ChangeUnits and Commits that last touched
        the target, most recent first, with the HEAD association explicit.
        Rollback planning starts from the units here — never from 'the
        commit' (spec 9E: commit != change unit)."""
        ctx = ChangeContext(target=target_id)
        g = self.graph
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
        # provenance minted at read time (principle 5)
        ev = Evidence.make(
            EvidenceType.CHANGE_UNIT, source="broker:get_change_context",
            target=target_id, location=fid,
            payload=f"{len(ctx.change_units)} change unit(s), "
                    f"{len(ctx.last_commits)} commit(s) touch {fid}")
        self.add_evidence(ev)
        ctx.evidence_ids.append(ev.id)
        return ctx

    def _commit_date_of(self, node: Node) -> str:
        if node.type == NodeType.CHANGE_UNIT:
            cn = self.graph.node(f"commit:{node.props.get('commit', '')}")
            return cn.props.get("date", "") if cn else ""
        return node.props.get("date", "")

    def _file_of(self, node_id: str) -> str | None:
        n = self.graph.node(node_id)
        if n is None:
            return None
        if n.type == NodeType.FILE:
            return node_id
        f = n.props.get("file")
        return f"file:{f}" if f else None

    # ------------------------------------------------------------ evidence
    def add_evidence(self, ev: Evidence) -> Evidence:
        existing = self._evidence.get(ev.id)
        if existing:  # id collisions with different content are a bug
            if (existing.type, existing.target, existing.location) != \
                    (ev.type, ev.target, ev.location):
                raise GraphError(f"evidence id collision with different facts: {ev.id}")
            return existing
        self._evidence[ev.id] = ev
        self.graph.add_node(Node(ev.id, NodeType.EVIDENCE,
                                 props={"type": ev.type.value, "source": ev.source,
                                        "target": ev.target, "location": ev.location,
                                        "payload": ev.payload[:300]}))
        if self.graph.node(ev.target):
            self.graph.add_edge(Edge(ev.target, ev.id, EdgeType.SUPPORTED_BY,
                                     props={"role": "about"}))
        return ev

    def get_evidence(self, ids: list[str]) -> list[Evidence]:
        return [self._evidence[i] for i in ids if i in self._evidence]

    def all_evidence(self) -> list[Evidence]:
        return list(self._evidence.values())

    # ------------------------------------------------------------ findings
    def add_finding(self, finding: Finding) -> Finding:
        """Register a finding. Never overwrites; contradictions with existing
        findings are recorded as conflicts for the verifier (principle 6)."""
        fid = finding.id
        if fid in self._findings:
            return self._findings[fid]
        self._findings[fid] = finding
        self.graph.add_node(Node(fid, NodeType.FINDING,
                                 props={"statement": finding.statement,
                                        "producer": finding.producer,
                                        "status": finding.status}))
        for eid in finding.evidence_ids:
            if self._evidence.get(eid):
                self.graph.add_edge(Edge(fid, eid, EdgeType.SUPPORTED_BY))
        # explicit conflict detection (same target, disagreeing statements)
        self._register_conflicts(finding)
        return finding

    def _register_conflicts(self, finding: Finding) -> None:
        fkey = _topic_symbols(finding.statement)
        for other in self._findings.values():
            if other.id == finding.id or other.id in finding.contradicts:
                continue
            okey = _topic_symbols(other.statement)
            # same subject symbols, disagreeing predicate => conflict
            shared = fkey & okey
            if shared and fkey != okey:
                topic = " ".join(sorted(shared))
                c = Conflict(finding_a=other.id, finding_b=finding.id,
                             topic=topic)
                self._conflicts.append(c)
                finding.contradicts.append(other.id)
                other.contradicts.append(finding.id)
                self.graph.add_edge(Edge(finding.id, other.id,
                                         EdgeType.CONTRADICTS,
                                         props={"topic": topic}))

    def all_findings(self) -> list[Finding]:
        return list(self._findings.values())

    @property
    def conflicts(self) -> list[Conflict]:
        return list(self._conflicts)

    # ------------------------------------------------------------ decisions
    def record_decision(self, d: Decision) -> Decision:
        self._decisions[d.id] = d
        self.graph.add_node(Node(d.id, NodeType.DECISION, props={
            "category": d.category, "outcome": d.outcome, "task_id": d.task_id,
            "risk": d.risk, "decision_maker": d.decision_maker}))
        for eid in d.evidence_ids:
            if self._evidence.get(eid):
                self.graph.add_edge(Edge(d.id, eid, EdgeType.SUPPORTED_BY))
        for fid in d.related_findings:
            if self._findings.get(fid):
                self.graph.add_edge(Edge(d.id, fid, EdgeType.DERIVED_FROM))
        return d

    def get_precedents(self, category: str = "", target: str = "") -> list[Decision]:
        out = []
        for d in self._decisions.values():
            if category and d.category != category:
                continue
            if target and target not in (d.target or ""):
                continue
            out.append(d)
        return sorted(out, key=lambda d: d.timestamp)

    # ------------------------------------------------------------ policy (9I)
    def check_policy(self, rule_name: str, context: dict) -> PolicyResult:
        from src.semgraph.policy import POLICY_RULES
        rule = POLICY_RULES.get(rule_name)
        if rule is None:
            raise DataAgentError(f"unknown policy rule {rule_name!r}")
        return rule.evaluate(context)

    # ------------------------------------------------------------ util
    def path(self, a: str, b: str) -> list[str] | None:
        """Multi-hop relation path via the v1 PathFinder (kept in sync)."""
        self.graph.sync_back_to_v1(self._v1)
        return self._v1.path(a, b)

    def stats(self) -> dict:
        return {"graph": self.graph.stats(),
                "evidence": len(self._evidence),
                "findings": len(self._findings),
                "conflicts": len(self._conflicts),
                "decisions": len(self._decisions),
                "views": {t: v.stats() for t, v in self._views.items()}}


def _topic_symbols(statement: str) -> frozenset[str]:
    """Symbols a finding is ABOUT (subject set). Two findings conflict when
    their subject sets intersect but are not equal — e.g. 'X affects A' vs
    'X only affects B'. Verb/stop words are excluded."""
    import re
    stop = {"affects", "affect", "impacts", "impact", "only", "the", "and",
            "in", "on", "to", "of", "is", "are", "was", "route", "routes",
            "api", "via", "not"}
    syms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", statement)
    return frozenset(s for s in syms if s not in stop)
