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
        cg = get_context_graph(FIXTURE, ToolRecorder())
        g2 = GraphV2.from_v1(cg)
        g2.add_node(Node("feature:t9", NodeType.FEATURE, props={}))
        g2.add_edge(Edge("feature:t9", "file:src/lib/retry.ts", EdgeType.IMPLEMENTS))
        g2.sync_back_to_v1(cg)
        assert cg.path("file:src/lib/retry.ts", "feature:t9")

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
