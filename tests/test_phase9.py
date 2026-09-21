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
