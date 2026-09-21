"""Phase 9 tests: schema v2, context broker, task view, agents, policy.

Organized by phase section; grows as phases land.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.config import Settings  # noqa: E402
from src.errors import GraphError  # noqa: E402
from src.schema import ToolRecorder  # noqa: E402


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(scope="module")
def v2(seeded):
    from src.semgraph.schema_v2 import GraphV2
    from src.semgraph.enrich import get_context_graph
    cg = get_context_graph(FIXTURE, ToolRecorder())
    return GraphV2.from_v1(cg)


# ================================================================ 9A schema v2
class TestSchemaV2:
    def test_roundtrip_conserves_information(self, seeded):
        from src.semgraph.enrich import get_context_graph
        from src.semgraph.schema_v2 import GraphV2
        cg = get_context_graph(FIXTURE, ToolRecorder())
        v1_edges = len(cg.kg.relationships)
        g2 = GraphV2.from_v1(cg)
        # parallel v1 edges merge into counted v2 edges — sum(count) conserved
        assert sum(e.props.get("count", 1) for e in g2.all_edges()) == v1_edges
        assert {n.id for n in g2.all_nodes()} >= {e["id"] for e in cg.kg.entities}

    def test_node_type_layers(self, v2):
        s = v2.layer_stats()
        assert s["code"]["nodes"] > 50 and s["change"]["nodes"] >= 5

    def test_conflicting_type_raises(self, v2):
        from src.semgraph.schema_v2 import Node, NodeType
        existing = next(n for n in v2.all_nodes())
        with pytest.raises(GraphError):
            v2.add_node(Node(existing.id, NodeType.POLICY))

    def test_prop_conflict_recorded_not_overwritten(self, v2):
        from src.semgraph.schema_v2 import Node, NodeType
        nid = "feature:probe-x"
        v2.add_node(Node(nid, NodeType.FEATURE, props={"label": "first"}))
        v2.add_node(Node(nid, NodeType.FEATURE, props={"label": "second"}))
        n = v2.node(nid)
        assert n.props["label"] == "first"
        assert n.props["_prop_conflicts"]["label"] == "second"

    def test_temporal_edge_fields(self, v2):
        from src.semgraph.schema_v2 import Edge, EdgeType
        e = Edge("commit:x", "file:y", EdgeType.MODIFIES).set_validity(
            "c0", "c9", "2026-09-20T00:00:00")
        assert e.props["valid_from_commit"] == "c0"
        assert e.props["valid_to_commit"] == "c9"
        assert e.props["observed_at"] == "2026-09-20T00:00:00"

    def test_sync_back_makes_v2_visible_to_pathfinder(self, seeded):
        import src.semgraph.enrich as se
        se._graph_cache.clear()
        from src.semgraph.enrich import get_context_graph
        from src.semgraph.schema_v2 import (Edge, EdgeType, GraphV2, Node,
                                             NodeType)
        try:
            cg = get_context_graph(FIXTURE, ToolRecorder())
            g2 = GraphV2.from_v1(cg)
            g2.add_node(Node("feature:t9", NodeType.FEATURE, props={}))
            g2.add_edge(Edge("feature:t9", "file:src/lib/retry.ts", EdgeType.IMPLEMENTS))
            g2.sync_back_to_v1(cg)
            assert cg.path("file:src/lib/retry.ts", "feature:t9")
        finally:
            # the v1 graph cache is process-shared; never leak test nodes
            se._graph_cache.clear()

    def test_edge_requires_known_nodes(self, v2):
        from src.semgraph.schema_v2 import Edge, EdgeType
        with pytest.raises(GraphError):
            v2.add_edge(Edge("nope:1", "nope:2", EdgeType.RELATED_TO))

    def test_stable_id_deterministic(self):
        from src.semgraph.schema_v2 import stable_id
        assert stable_id("a", "b") == stable_id("a", "b")
        assert stable_id("a", "b") != stable_id("b", "a")


# ================================================================ 9B broker
@pytest.fixture(scope="module")
def broker(seeded):
    from src.semgraph.context_broker import ContextBroker
    return ContextBroker(FIXTURE, ToolRecorder())


class TestContextBroker:
    def test_resolve_target_prefers_symbols(self, broker):
        t = broker.resolve_target("修改 generateWithRetry 会影响什么")
        assert t.id == "sym:src/lib/retry.ts::generateWithRetry"

    def test_resolve_target_filename_fallback(self, broker):
        t = broker.resolve_target("改动 retry.ts 里的逻辑")
        assert t.type.value == "File"

    def test_resolve_target_failure_is_loud(self, broker):
        from src.errors import DataAgentError
        with pytest.raises(DataAgentError):
            broker.resolve_target("完全无关的 查询 zzz")

    def test_target_context_neighborhood(self, broker):
        t = broker.resolve_target("generateWithRetry")
        ctx = broker.get_target_context(t.id)
        assert {c.id for c in ctx.direct_callers} == {
            "scope:src/lib/ai-helpers.ts::generateDesign",
            "scope:src/lib/ai-helpers.ts::generateReview"}
        assert [i.id for i in ctx.importers] == ["file:src/lib/ai-helpers.ts"]
        assert [r.id for r in ctx.related_routes] == ["api:src/app/api/generate/route.ts"]
        assert ctx.evidence_ids, "context read must mint evidence"

    def test_route_discovery_through_call_chain(self, broker):
        t = broker.resolve_target("validateAccount")
        ctx = broker.get_target_context(t.id)
        assert [r.id for r in ctx.related_routes] == ["api:src/app/api/auth/login/route.ts"]

    def test_unknown_target_raises(self, broker):
        from src.errors import GraphError
        with pytest.raises(GraphError):
            broker.get_target_context("sym:nope.ts::nothing")

    def test_change_context_finds_commits(self, broker):
        t = broker.resolve_target("generateWithRetry")
        cc = broker.get_change_context(t.id)
        assert any(c.type.value == "Commit" for c in cc.last_commits)


# ================================================================ 9C task view
class TestTaskGraphView:
    def test_select_is_local_not_whole_graph(self, broker):
        t = broker.resolve_target("generateWithRetry")
        v = broker.create_task_view("tv-1", [t.id])
        s = v.stats()
        assert 2 <= s["task_graph_nodes"] < broker.graph.stats()["nodes"] // 2
        assert s["expansion_count"] == 1
        assert t.id in v.selected_nodes

    def test_expand_audited_and_bounded(self, broker):
        from src.semgraph.schema_v2 import EdgeType
        t = broker.resolve_target("generateWithRetry")
        v = broker.get_task_view("tv-1")
        n0 = v.stats()["task_graph_nodes"]
        v = broker.expand_task_view(
            "tv-1", ["scope:src/lib/ai-helpers.ts::generateDesign"],
            relations={EdgeType.CALLS, EdgeType.IMPORTS}, depth=1,
            trigger="impact-agent: importer closure")
        s = v.stats()
        assert s["task_graph_nodes"] >= n0
        assert s["expansion_count"] == 2
        exp = v.expansion_history[-1]
        assert exp.trigger.startswith("impact-agent")
        assert exp.added_nodes, "expansion must record what it admitted"

    def test_dump_respects_budget(self, broker):
        v = broker.get_task_view("tv-1")
        text = v.dump(budget_chars=300)
        assert len(text) < 400 and "view budget hit" in text

    def test_view_grows_only_via_expand(self, broker):
        # nodes outside the view stay outside until an expand admits them
        v = broker.get_task_view("tv-1")
        assert "file:src/lib/db.ts" not in v.selected_nodes

    def test_stats_shape(self, broker):
        s = broker.get_task_view("tv-1").stats()
        assert set(s) == {"task_graph_nodes", "task_graph_edges", "expansion_count"}


# ================================================================ 9B objects/registry
class TestEvidenceFindingsConflicts:
    def test_evidence_id_deterministic(self):
        from src.semgraph.objects import Evidence, EvidenceType
        a = Evidence.make(EvidenceType.AST, "t", "sym:x", location="a.ts:1")
        b = Evidence.make(EvidenceType.AST, "t", "sym:x", location="a.ts:1")
        assert a.id == b.id

    def test_add_finding_registers_graph_nodes(self, broker):
        from src.semgraph.objects import Evidence, EvidenceType, Finding
        ev = Evidence.make(EvidenceType.GIT_DIFF, "test", "sym:x", payload="d")
        broker.add_evidence(ev)
        f = Finding.make("probe finding for registry", "test-agent", [ev.id])
        broker.add_finding(f)
        assert broker.graph.node(f.id) is not None
        assert broker.get_evidence([ev.id])[0].payload == "d"

    def test_contradicting_findings_both_survive(self, broker):
        from src.semgraph.objects import Evidence, EvidenceType, Finding
        ev = Evidence.make(EvidenceType.GRAPH_PATH, "test2", "sym:y", payload="p")
        broker.add_evidence(ev)
        f1 = Finding.make("logoutBtn affects settings page", "A", [ev.id])
        f2 = Finding.make("logoutBtn only affects navbar", "B", [ev.id])
        broker.add_finding(f1)
        broker.add_finding(f2)
        assert broker.get_evidence([ev.id])  # both still there
        assert f1.id in f2.contradicts and f2.id in f1.contradicts
        assert any(c.finding_a == f1.id and c.finding_b == f2.id
                   for c in broker.conflicts)
        # graph carries the explicit CONTRADICTS edge (either direction)
        assert (broker.graph.edge_between(f1.id, f2.id)
                or broker.graph.edge_between(f2.id, f1.id))

    def test_same_statement_no_false_conflict(self, broker):
        from src.semgraph.objects import Finding
        f1 = Finding.make("same exact claim here", "A")
        f2 = Finding.make("same exact claim here", "B")
        broker.add_finding(f1)
        n_before = len(broker.conflicts)
        broker.add_finding(f2)
        assert len(broker.conflicts) == n_before

    def test_finding_without_evidence_unsupported(self, broker):
        from src.semgraph.objects import Finding
        f = Finding.make("bare claim with no evidence", "X")
        broker.add_finding(f)
        assert not f.supported

    def test_decisions_and_precedents(self, broker):
        from src.semgraph.objects import Decision
        broker.record_decision(Decision.make(
            "keep", "keep title unit", task_id="t-9b", target="file:x",
            risk="low", decision_maker="RollbackPlanner",
            reason_summary="no shared symbols"))
        precs = broker.get_precedents(category="keep")
        assert any(d.outcome == "keep title unit" for d in precs)
        assert broker.get_precedents(category="nonexistent") == []


# ================================================================ 9F/9G verification & conflicts
class TestVerificationAndConflicts:
    def test_verify_requires_registered_evidence(self, broker):
        from src.errors import DataAgentError
        from src.semgraph.objects import Finding
        f = Finding.make("bare claim cannot be verified at all", "V0")
        broker.add_finding(f)
        with pytest.raises(DataAgentError):
            broker.set_finding_status(f.id, "verified", verifier="V0")

    def test_verify_blocked_by_unresolved_conflict(self, broker):
        from src.errors import DataAgentError
        from src.semgraph.objects import Evidence, EvidenceType, Finding
        ev = Evidence.make(EvidenceType.GIT_BLAME, "t-vc", "sym:z1", payload="b")
        broker.add_evidence(ev)
        f1 = Finding.make("btnQ affects navbar only", "A9", [ev.id])
        f2 = Finding.make("btnQ affects settings too", "B9", [ev.id])
        broker.add_finding(f1)
        broker.add_finding(f2)
        assert broker.conflicts_involving(f1.id)
        with pytest.raises(DataAgentError) as err:
            broker.set_finding_status(f1.id, "verified", verifier="V")
        assert "conflict" in str(err.value).lower()

    def test_resolve_conflict_lets_winner_verify(self, broker):
        from src.errors import DataAgentError
        from src.semgraph.objects import Evidence, EvidenceType, Finding
        ev = Evidence.make(EvidenceType.GIT_BLAME, "t-rc", "sym:z2", payload="b")
        broker.add_evidence(ev)
        f1 = Finding.make("btnW affects navbar only", "A8", [ev.id])
        f2 = Finding.make("btnW affects settings too", "B8", [ev.id])
        # f3 shares the btnW/navbar topic with f1 — a *chained* dispute that
        # must independently block verification (topic model is coarse by
        # design; the verifier resolves disputes one pair at a time)
        f3 = Finding.make("btnW affects navbar and footer", "C8", [ev.id])
        broker.add_finding(f1)
        broker.add_finding(f2)
        broker.add_finding(f3)
        c = next(c for c in broker.conflicts_involving(f1.id)
                 if f2.id in (c.finding_a, c.finding_b))
        assert c in broker.unresolved_conflicts()
        broker.resolve_conflict(c, "navbar-only is what blame shows",
                                winner=f1.id, resolver="EvidenceVerifier")
        assert c not in broker.unresolved_conflicts()
        assert broker._findings[f2.id].status == "contradicted"
        # strict guard: the open f1-f3 dispute still blocks verification
        with pytest.raises(DataAgentError):
            broker.set_finding_status(f1.id, "verified", verifier="EvidenceVerifier")
        for chained in [x for x in broker.conflicts_involving(f1.id)
                        if not x.resolved]:
            broker.resolve_conflict(chained, "chained topic dispute resolved",
                                    winner=f1.id, resolver="EvidenceVerifier")
        w = broker.set_finding_status(f1.id, "verified", verifier="EvidenceVerifier")
        assert w.status == "verified"
        node = broker.graph.node(f1.id)
        assert node.props["status"] == "verified"
        assert node.props["verified_by"] == "EvidenceVerifier"
        # the resolution itself is an auditable decision
        assert any(d.category == "conflict_resolution"
                   for d in broker.get_precedents(category="conflict_resolution"))

    def test_verify_happy_path(self, broker):
        from src.semgraph.objects import Evidence, EvidenceType, Finding
        ev = Evidence.make(EvidenceType.TEST, "t-vh", "sym:vh1", payload="tests pass")
        broker.add_evidence(ev)
        f = Finding.make("hunkHeader renders commit headers", "V1", [ev.id])
        broker.add_finding(f)
        out = broker.set_finding_status(f.id, "verified",
                                        verifier="DeterministicVerifier")
        assert out.status == "verified"

    def test_evidence_about_target(self, broker):
        from src.semgraph.objects import Evidence, EvidenceType
        ev = Evidence.make(EvidenceType.AST, "t-ea", "sym:zz9", payload="p")
        broker.add_evidence(ev)
        assert any(e.id == ev.id for e in broker.evidence_about("sym:zz9"))
        assert broker.evidence_about("sym:zz9", EvidenceType.GIT_DIFF) == []

    def test_unsupported_findings_detection(self, broker):
        from src.semgraph.objects import Finding
        f = Finding.make("claims evidence that was never registered", "GHOST1")
        f.evidence_ids.append("evid:never-registered")
        broker.add_finding(f)
        assert f.id in {x.id for x in broker.unsupported_findings()}

    def test_evidence_provenance_chain(self, broker):
        from src.semgraph.objects import Evidence, EvidenceType
        base = Evidence.make(EvidenceType.AST, "t-pc", "sym:pc1", payload="base")
        broker.add_evidence(base)
        derived = Evidence.make(EvidenceType.GRAPH_PATH, "t-pc2", "sym:pc1",
                                payload="derived",
                                provenance={"derived_from": [base.id]})
        broker.add_evidence(derived)
        node = broker.graph.node(derived.id)
        assert node.props.get("provenance", {}).get("derived_from") == [base.id]



# ================================================================ 9H/9I decision memory + gate
class TestDecisionMemoryAndGate:
    def test_reason_summary_capped_not_cot(self, broker):
        from src.semgraph.objects import Decision
        long_reason = "because " * 200  # 1600 chars of pseudo-CoT
        d = broker.record_decision(Decision.make(
            "keep", "keep unit X", target="file:x",
            decision_maker="RollbackPlanner", reason_summary=long_reason))
        assert len(d.reason_summary) <= broker.REASON_SUMMARY_CAP
        assert any("capped" in w for w in broker.rec.warnings)

    def test_decision_records_policy_provenance(self, broker):
        from src.semgraph.objects import Decision
        broker.record_decision(Decision.make(
            "rollback", "rollback unit Y only", target="file:y",
            decision_maker="RollbackPlanner", risk="medium",
            policy="rollback_public_api v1.0.0 -> HUMAN_REVIEW",
            reason_summary="route touched"))
        node = broker.graph.node(
            next(d.id for d in broker.get_precedents(category="rollback")
                 if d.outcome == "rollback unit Y only"))
        assert node.props["policy"].startswith("rollback_public_api")

    def test_gate_clean_context_passes(self, broker):
        r = broker.run_policy_gate({"rollback_symbols": ["a"],
                                    "keep_symbols": ["b"]}, task_id="t-clean")
        assert r.action.value == "PASS" and r.rule is None
        assert any(d.outcome == "PASS" and d.task_id == "t-clean"
                   for d in broker.get_precedents(category="policy_gate"))

    def test_gate_block_beats_human_review(self, broker):
        r = broker.run_policy_gate({
            "rollback_symbols": ["s"], "keep_symbols": ["s"],   # HUMAN_REVIEW
            "unsupported_findings": 3})                          # BLOCK
        assert r.action.value == "BLOCK" and r.rule.name == "unsupported_finding"

    def test_gate_human_review_when_only_review_rules_trigger(self, broker):
        r = broker.run_policy_gate({
            "rollback_symbols": ["a"], "keep_symbols": ["a"],
            "changed_files": ["db/migrate/0007.sql"]})
        assert r.action.value == "HUMAN_REVIEW" and r.rule is not None

    def test_precedents_by_query_symbols(self, broker):
        from src.semgraph.objects import Decision
        broker.record_decision(Decision.make(
            "keep", "keep titleUnitZ branding change", target="file:z",
            decision_maker="RollbackPlanner", reason_summary="no coupling"))
        hits = broker.get_precedents(query="branding titleUnitZ")
        assert any(d.outcome == "keep titleUnitZ branding change" for d in hits)
        assert broker.get_precedents(query="qqqzzz nonexistent") == []


# ================================================================ 9I policy
class TestPolicyGate:
    def test_shared_symbol_human_review(self, broker):
        r = broker.check_policy("rollback_keep_same_symbol", {
            "rollback_symbols": ["validateAccount"],
            "keep_symbols": ["validateAccount", "rewordTitle"]})
        assert r.action.value == "HUMAN_REVIEW" and r.rule is not None

    def test_no_shared_symbol_passes(self, broker):
        r = broker.check_policy("rollback_keep_same_symbol", {
            "rollback_symbols": ["validateAccount"],
            "keep_symbols": ["rewordTitle"]})
        assert r.action.value == "PASS" and r.rule is None

    def test_public_api_rule(self, broker):
        r = broker.check_policy("rollback_public_api", {
            "affected_routes": ["/api/auth/login"]})
        assert r.action.value == "HUMAN_REVIEW"

    def test_block_rules(self, broker):
        assert broker.check_policy(
            "unsupported_finding", {"unsupported_findings": 2}).action.value == "BLOCK"
        assert broker.check_policy(
            "invalid_graph_path", {"invalid_graph_paths": 1}).action.value == "BLOCK"
        assert broker.check_policy(
            "unresolved_test_failure", {"unresolved_test_failures": 1}).action.value == "BLOCK"

    def test_db_migration_rule(self, broker):
        r = broker.check_policy("db_migration_change", {
            "changed_files": ["drizzle/0001_init.sql"]})
        assert r.action.value == "HUMAN_REVIEW"

    def test_unknown_rule_raises(self, broker):
        from src.errors import DataAgentError
        with pytest.raises(DataAgentError):
            broker.check_policy("no_such_rule", {})

    def test_policies_are_versioned_objects(self):
        from src.semgraph.policy import POLICY_RULES
        for name, rule in POLICY_RULES.items():
            assert rule.version and rule.name == name and rule.description


# ================================================================ 9E change graph
from src.semgraph.schema_v2 import EdgeType as E2, NodeType as N2  # noqa: E402


@pytest.fixture(scope="module")
def broker_cg(seeded):
    """Broker with the change layer built (fresh instance; does not disturb
    the plain `broker` fixture used by earlier sections)."""
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    b = ContextBroker(FIXTURE, ToolRecorder())
    build_change_graph(b)
    return b


class TestChangeGraph:
    def test_commit_contains_change_units(self, broker_cg):
        g = broker_cg.graph
        commits_with_units = [n for n in g.nodes_of_type(N2.COMMIT)
                              if any(e.type == E2.CONTAINS_CHANGE
                                     for e in g.edges_from(n.id))]
        assert commits_with_units, "CONTAINS_CHANGE edges must exist"

    def test_mixed_commit_splits_into_units(self, broker_cg):
        """The acceptance-demo commit: one commit, distinct title/auth units.
        commit == change unit must NOT be assumed."""
        g = broker_cg.graph
        by_label: dict[str, list] = {}
        for cu in g.nodes_of_type(N2.CHANGE_UNIT):
            by_label.setdefault(cu.props["commit"], []).append(cu)
        multi = {sha: cus for sha, cus in by_label.items() if len(cus) >= 2}
        assert multi, "fixture has mixed commits; at least one must split"
        labels = {u.props["semantic_label"]
                  for cus in multi.values() for u in cus}
        assert "title" in labels and "auth" in labels

    def test_unit_modifies_and_temporal_props(self, broker_cg):
        g = broker_cg.graph
        cu = next(n for n in g.nodes_of_type(N2.CHANGE_UNIT)
                  if n.props.get("semantic_label") == "auth")
        outs = g.edges_from(cu.id)
        mod_files = [e for e in outs
                     if e.type == E2.MODIFIES and e.dst.startswith("file:")]
        assert mod_files
        for e in mod_files:
            assert e.props.get("valid_from_commit") == cu.props["commit"]
            assert "observed_at" in e.props
        # upward edge: the unit knows the commit that introduced it
        assert any(e.type == E2.INTRODUCED_BY
                   and e.dst == f"commit:{cu.props['commit']}" for e in outs)

    def test_units_carry_change_unit_evidence(self, broker_cg):
        cu = next(n for n in broker_cg.graph.nodes_of_type(N2.CHANGE_UNIT))
        ev_id = cu.props.get("evidence_id")
        assert ev_id and broker_cg.get_evidence([ev_id]), \
            "facts enter the graph with provenance (principle 5)"

    def test_change_context_prefers_units_over_commits(self, broker_cg):
        t = broker_cg.resolve_target("validateAccount")
        cc = broker_cg.get_change_context(t.id)
        assert cc.change_units, "9E change context must surface units"
        assert any(u.props["semantic_label"] == "auth" for u in cc.change_units)
        assert cc.last_commits and cc.evidence_ids

    def test_hunks_are_atomic_units(self, broker_cg):
        hunks = broker_cg.graph.nodes_of_type(N2.HUNK)
        assert hunks and all("file" in h.props and "new_start" in h.props
                             for h in hunks)

    def test_unit_modifies_feature_when_seeded(self, broker_cg):
        """With the semantic layer seeded, the title unit reaches the
        SystemBranding feature (demo path: keep the good title change)."""
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.semantic_mapper import SemanticMapper
        b = broker_cg
        SemanticMapper(b.graph, b.rec).seed_deterministic()
        build_change_graph(b)  # idempotent re-run links units -> features
        in_edges = [e for e in b.graph.edges_to("feature:SystemBranding")
                    if e.type == E2.MODIFIES]
        assert in_edges, "ChangeUnit MODIFIES Feature must exist after seeding"


# ================================================================ 9D semantic
@pytest.fixture(scope="module")
def mapper(seeded):
    from src.semgraph.context_broker import ContextBroker
    from src.semgraph.semantic_mapper import SemanticMapper
    b = ContextBroker(FIXTURE, ToolRecorder())
    m = SemanticMapper(b.graph, b.rec)
    m.seed_deterministic()
    return m


class TestSemanticMapper:
    def test_routes_and_components_seed_features(self, mapper):
        names = {n.props.get("name") for n in
                 mapper.g.nodes_of_type(__import__("src.semgraph.schema_v2",
                                                   fromlist=["NodeType"]).NodeType.FEATURE)}
        assert "AuthLogin" in names and "AuthNav" in names

    def test_root_layout_seeds_system_branding(self, mapper):
        f = mapper.g.node("feature:SystemBranding")
        assert f is not None and f.props["status"] == "seeded"
        # IMPLEMENTS edge points at the layout file
        dsts = [e.dst for e in mapper.g.edges_from("feature:SystemBranding")]
        assert "file:src/app/layout.tsx" in dsts

    def test_seeded_features_carry_evidence(self, mapper):
        f = mapper.g.node("feature:SystemBranding")
        assert "evidence_id" in f.props

    def test_zh_query_maps_to_feature(self, mapper):
        cands = mapper.map_query("系统标题在哪里修改")
        assert cands[0].feature_id == "feature:SystemBranding"
        assert cands[0].related_symbols == ["file:src/app/layout.tsx"]
        assert cands[0].evidence, "candidates must carry their evidence"

    def test_login_query_maps_to_auth_feature(self, mapper):
        cands = mapper.map_query("登录验证的逻辑在哪里")
        assert cands[0].feature_id == "feature:AuthLogin"

    def test_symbol_query_still_resolves(self, mapper):
        cands = mapper.map_query("generateWithRetry")
        assert cands and cands[0].feature_id == "feature:Generate"

    def test_llm_absent_stays_deterministic(self, mapper):
        # no llm configured: mapper works, returns only lexical/alias hits
        cands = mapper.map_query("登录验证的逻辑在哪里", llm=None)
        assert all(c.mapping_method in ("lexical", "seed-alias") for c in cands)

    def test_llm_candidates_dropped_if_hallucinated(self, mapper):
        class FakeLLM:
            available = True
            def chat_json(self, system, user):
                return {"candidates": [{"feature_id": "feature:Ghost", "reason": "x"}]}
        cands = mapper.map_query("任意查询", llm=FakeLLM())
        assert not any(c.feature_id == "feature:Ghost" for c in cands)

    def test_llm_valid_pick_is_candidate(self, mapper):
        class FakeLLM:
            available = True
            def chat_json(self, system, user):
                return {"candidates": [{"feature_id": "feature:AuthLogin",
                                        "reason": "query mentions login"}]}
        cands = mapper.map_query("怎么改账号校验", llm=FakeLLM())
        hit = [c for c in cands if c.feature_id == "feature:AuthLogin"
               and c.mapping_method == "llm"]
        assert hit and hit[0].status == "candidate"  # never auto-fact
        # and it was NOT promoted into the graph as an unevidenced fact
        node = mapper.g.node("feature:AuthLogin")
        assert node.props.get("status") == "seeded"
