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
    # "taskview" is a pseudo-layer: it gates bounded task views, not graph
    # content (G3 in the Phase 10 ablation)
    ALL_LAYERS = {"code", "semantic", "change", "evidence", "decision",
                  "taskview"}

    def __init__(self, repo: Path, rec: ToolRecorder | None = None,
                 layers: set[str] | None = None):
        """layers: which layers are active for this run (G0..G4 ablation).
        None = everything currently built. Inactive graph layers are pruned
        from the v2 projection; inactive capabilities raise loudly when an
        agent tries to use them."""
        from src.semgraph.enrich import get_context_graph
        self.repo = repo
        self.rec = rec or ToolRecorder()
        self._v1 = get_context_graph(repo, self.rec)
        self.graph = GraphV2.from_v1(self._v1)
        self.layers = (set(layers) | {"code"}) if layers is not None \
            else set(self.ALL_LAYERS)
        unknown = self.layers - self.ALL_LAYERS
        if unknown:
            raise DataAgentError(f"unknown layers {sorted(unknown)}")
        if layers is not None:
            self.graph = self.graph.prune_to_layers(self.layers)
        # object stores (evidence/decision registries are process-local v1)
        self._evidence: dict[str, Evidence] = {}
        self._findings: dict[str, Finding] = {}
        self._conflicts: list[Conflict] = []
        self._decisions: dict[str, Decision] = {}
        self._views: dict[str, TaskGraphView] = {}
        self._counter = 0

    def layer_active(self, name: str) -> bool:
        return name in self.layers

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
        if not self.layer_active("taskview"):
            raise DataAgentError(
                "task views are disabled (taskview layer inactive, G<3)")
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
        props = {"type": ev.type.value, "source": ev.source,
                 "target": ev.target, "location": ev.location,
                 "payload": ev.payload[:300], "producer": ev.producer,
                 "timestamp": ev.timestamp}
        if ev.provenance:  # upstream evidence ids — audit chains (9F)
            props["provenance"] = dict(list(ev.provenance.items())[:5])
        self.graph.add_node(Node(ev.id, NodeType.EVIDENCE, props=props))
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
            # same subject symbols, disagreeing predicate => conflict.
            # Shared boilerplate alone (labels like 'auth', words like
            # 'candidate') is not a dispute — require a specific identifier.
            shared = {s for s in fkey & okey if _is_specific(s)}
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

    # ------------------------------------------------------------ verification (9F/9G)
    def evidence_about(self, target_id: str,
                       ev_type: EvidenceType | None = None) -> list[Evidence]:
        """All registered evidence about a graph node (optionally by type)."""
        out = [e for e in self._evidence.values() if e.target == target_id]
        if ev_type is not None:
            out = [e for e in out if e.type == ev_type]
        return sorted(out, key=lambda e: e.timestamp)

    def unresolved_conflicts(self) -> list[Conflict]:
        return [c for c in self._conflicts if not c.resolved]

    def conflicts_involving(self, finding_id: str) -> list[Conflict]:
        return [c for c in self._conflicts
                if finding_id in (c.finding_a, c.finding_b)]

    def unsupported_findings(self) -> list[Finding]:
        """Findings citing evidence ids that were never registered — their
        SUPPORTED_BY edges point nowhere. Feeds the policy gate."""
        return [f for f in self._findings.values()
                if f.evidence_ids and not all(e in self._evidence
                                              for e in f.evidence_ids)]

    def set_finding_status(self, finding_id: str, status: str,
                           verifier: str = "") -> Finding:
        """Status transitions with guards (9F/9G):
        - verified requires registered evidence AND no unresolved conflict
        - the verifier's name lands on the graph node (audit, not CoT)"""
        f = self._findings.get(finding_id)
        if f is None:
            raise DataAgentError(f"unknown finding {finding_id!r}")
        if status not in ("proposed", "verified", "unsupported", "contradicted"):
            raise DataAgentError(f"invalid finding status {status!r}")
        if status == "verified":
            if not f.evidence_ids:
                raise DataAgentError(
                    f"cannot verify {finding_id!r}: no evidence cited — "
                    "a finding without evidence is unsupported, never verified")
            missing = [e for e in f.evidence_ids if e not in self._evidence]
            if missing:
                raise DataAgentError(
                    f"cannot verify {finding_id!r}: unregistered evidence {missing}")
            open_c = [c for c in self.conflicts_involving(finding_id)
                      if not c.resolved]
            if open_c:
                raise DataAgentError(
                    f"cannot verify {finding_id!r}: {len(open_c)} unresolved "
                    "conflict(s) — resolve the conflict first")
        f.status = status
        node = self.graph.node(finding_id)
        if node is not None:
            node.props["status"] = status
            if verifier:
                node.props[f"{status}_by"] = verifier
        return f

    def resolve_conflict(self, conflict: Conflict, resolution: str,
                         winner: str | None = None,
                         resolver: str = "verifier") -> Conflict:
        """The verifier owns conflict resolution. Both findings survive in
        the registry; the loser is marked contradicted, and the resolution
        is recorded as a Decision (audit trail, 9H integration)."""
        if conflict not in self._conflicts:
            raise DataAgentError("unknown conflict — not registered by this broker")
        conflict.resolved = True
        conflict.resolution = resolution
        if winner:
            loser = next(fid for fid in (conflict.finding_a, conflict.finding_b)
                         if fid != winner)
            self.set_finding_status(loser, "contradicted", verifier=resolver)
        self.record_decision(Decision.make(
            "conflict_resolution", resolution, target=conflict.topic,
            related_findings=[conflict.finding_a, conflict.finding_b],
            risk="medium", decision_maker=resolver,
            reason_summary=f"winner={winner or 'none'}"))
        return conflict

    # ------------------------------------------------------------ decisions (9H)
    # hard cap on audit text: decisions store reason summaries, never a
    # model's hidden chain of thought (spec 9H)
    REASON_SUMMARY_CAP = 500

    def record_decision(self, d: Decision) -> Decision:
        if len(d.reason_summary) > self.REASON_SUMMARY_CAP:
            self.rec.warn(
                f"decision {d.id}: reason_summary {len(d.reason_summary)} chars "
                f"capped to {self.REASON_SUMMARY_CAP} — hidden CoT is not stored")
            d.reason_summary = d.reason_summary[:self.REASON_SUMMARY_CAP - 1] + "…"
        self._decisions[d.id] = d
        self.graph.add_node(Node(d.id, NodeType.DECISION, props={
            "category": d.category, "outcome": d.outcome, "task_id": d.task_id,
            "risk": d.risk, "decision_maker": d.decision_maker,
            "reason_summary": d.reason_summary, "policy": d.policy}))
        for eid in d.evidence_ids:
            if self._evidence.get(eid):
                self.graph.add_edge(Edge(d.id, eid, EdgeType.SUPPORTED_BY))
        for fid in d.related_findings:
            if self._findings.get(fid):
                self.graph.add_edge(Edge(d.id, fid, EdgeType.DERIVED_FROM))
        return d

    def get_precedents(self, category: str = "", target: str = "",
                       query: str = "") -> list[Decision]:
        """Decision memory lookup: by category, by target node, and/or by
        free-text query whose symbols must appear in the decision blob."""
        qsyms = _topic_symbols(query) if query else frozenset()
        out = []
        for d in self._decisions.values():
            if category and d.category != category:
                continue
            if target and target not in (d.target or ""):
                continue
            if qsyms:
                blob = f"{d.outcome} {d.reason_summary} {d.target}"
                if not (qsyms & _topic_symbols(blob)):
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

    def run_policy_gate(self, context: dict, task_id: str = "") -> PolicyResult:
        """Evaluate the full rule set and record the outcome as an auditable
        decision. Returns the most severe triggered action. Honoring BLOCK
        is the orchestrator's contract — the gate only decides."""
        from src.semgraph.policy import gate
        result = gate(context)
        risk = {"PASS": "low", "HUMAN_REVIEW": "medium",
                "BLOCK": "high"}[result.action.value]
        self.record_decision(Decision.make(
            "policy_gate", result.action.value, task_id=task_id, risk=risk,
            decision_maker="PolicyGate",
            reason_summary=result.detail[:200],
            policy=(f"{result.rule.name} v{result.rule.version} -> "
                    f"{result.action.value}") if result.rule else "none-triggered"))
        return result

    # ------------------------------------------------------------ util
    def node(self, node_id: str) -> Node | None:
        return self.graph.node(node_id)

    def find_change_units(self, label: str = "", file: str = "",
                          commit: str = "") -> list[Node]:
        """ChangeUnit lookup for the change-intelligence agent (file is a
        substring match over unit files)."""
        out = []
        for cu in self.graph.nodes_of_type(NodeType.CHANGE_UNIT):
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
        """Direct IMPORTS edges between two file sets, both directions —
        the collateral-damage signal for rollback planning."""
        a = {f"file:{f}" for f in files_a}
        b = {f"file:{f}" for f in files_b}
        out = []
        for fa in a:
            for e in self.graph.edges_from(fa):
                if e.type == EdgeType.IMPORTS and e.dst in b and e.dst != fa:
                    out.append(f"{fa[len('file:'):]} imports {e.dst[len('file:'):]}")
        for fb in b:
            for e in self.graph.edges_from(fb):
                if e.type == EdgeType.IMPORTS and e.dst in a and e.dst != fb:
                    out.append(f"{fb[len('file:'):]} imports {e.dst[len('file:'):]}")
        return sorted(set(out))

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
    'X only affects B'. Verb/stop words and sha-like hex tokens are
    excluded (a shared commit hash is context, not a subject)."""
    import re
    stop = {"affects", "affect", "impacts", "impact", "only", "the", "and",
            "in", "on", "to", "of", "is", "are", "was", "route", "routes",
            "api", "via", "not", "unit", "units", "label", "feature",
            "query", "commit", "change", "matches", "description",
            "targets", "files", "symbols", "seed", "alias", "candidate",
            "modifying", "callers", "caller", "same", "exact", "claim",
            "here", "bare", "without", "evidence"}
    syms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", statement)
    return frozenset(s for s in syms
                     if s not in stop and not re.fullmatch(r"[0-9a-f]{6,}", s))


def _is_specific(token: str) -> bool:
    """Identifier-like: camelCase/snake_case/digit/CJK. Plain lowercase
    English words ('auth', 'navbar', 'candidate') are vocabulary, not
    subjects — a single shared word like that does not allege a dispute."""
    return (any(c.isupper() for c in token) or "_" in token
            or any(c.isdigit() for c in token)
            or any(ord(c) > 0x2E80 for c in token))
