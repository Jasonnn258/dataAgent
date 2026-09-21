"""Phase 10 tests: G0-G4 graph-layer ablation + acceptance demo contract."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.config import Settings  # noqa: E402,F401
from src.schema import ToolRecorder  # noqa: E402

QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
KEEP = "保留同 commit 中已经改好的系统标题"


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


LEVELS = {
    "G0": {"code"},
    "G1": {"code", "semantic"},
    "G2": {"code", "semantic", "change"},
    "G3": {"code", "semantic", "change", "taskview"},
    "G4": {"code", "semantic", "change", "taskview", "evidence", "decision"},
}


@pytest.fixture(scope="module")
def reports(seeded):
    from src.agents import Orchestrator
    from src.errors import DataAgentError
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    out = {}
    for name, layers in LEVELS.items():
        b = ContextBroker(FIXTURE, ToolRecorder(), layers=layers)
        if "change" in layers:
            build_change_graph(b)
        try:
            out[name] = ("ok", Orchestrator(b).run(QUERY, keep_hint=KEEP), b)
        except DataAgentError as e:
            out[name] = ("fail", str(e), b)
    return out


class TestGAblation:
    def test_g0_code_only_cannot_enter_fuzzy(self, reports):
        status, payload, _ = reports["G0"]
        assert status == "fail" and "cannot resolve target" in payload

    def test_g0_graph_has_no_change_or_semantic_nodes(self, reports):
        from src.semgraph.schema_v2 import NodeType
        _, _, b = reports["G0"]
        assert b.graph.nodes_of_type(NodeType.CHANGE_UNIT) == []
        assert b.graph.nodes_of_type(NodeType.FEATURE) == []
        assert b.graph.nodes_of_type(NodeType.FUNCTION)      # code survives

    def test_g1_semantic_navigates_but_no_units(self, reports):
        status, r, b = reports["G1"]
        assert status == "ok"
        assert r.navigation.feature_id == "feature:AuthLogin"
        assert r.problem_units == [] and r.keep_units == []

    def test_g2_change_layer_splits_units_without_gate(self, reports):
        status, r, b = reports["G2"]
        assert status == "ok"
        assert any("bbdc659f-U2" in u.unit_id for u in r.problem_units)
        assert any("bbdc659f-U1" in u.unit_id for u in r.keep_units)
        assert r.plan.policy_result is None            # decision layer off
        assert "UNGATED" in r.plan.recommendation

    def test_g3_task_view_is_bounded(self, reports):
        status, r, b = reports["G3"]
        assert status == "ok"
        s = r.slice.stats()
        assert 0 < s["task_graph_nodes"] < b.graph.stats()["nodes"]

    def test_g4_full_verification_and_gate(self, reports):
        status, r, b = reports["G4"]
        assert status == "ok"
        assert r.verdicts
        assert any(f.status == "verified" for f in b.all_findings())
        assert r.plan.policy_result.action.value == "HUMAN_REVIEW"

    def test_monotonic_layer_growth(self, reports):
        """Each level's active layer set is a subset of the next."""
        names = list(LEVELS)
        for a, nxt in zip(names, names[1:]):
            assert LEVELS[a] < LEVELS[nxt]


class TestLayerGating:
    def test_task_view_disabled_raises(self, seeded):
        from src.errors import DataAgentError
        from src.semgraph.context_broker import ContextBroker
        b = ContextBroker(FIXTURE, ToolRecorder(), layers={"code"})
        with pytest.raises(DataAgentError):
            b.create_task_view("t", ["sym:src/lib/retry.ts::generateWithRetry"])

    def test_unknown_layer_raises(self, seeded):
        from src.errors import DataAgentError
        from src.semgraph.context_broker import ContextBroker
        with pytest.raises(DataAgentError):
            ContextBroker(FIXTURE, ToolRecorder(), layers={"code", "bogus"})

    def test_prune_conserves_edges_within_layers(self, seeded):
        from src.semgraph.schema_v2 import EdgeType, GraphV2
        from src.semgraph.enrich import get_context_graph
        full = GraphV2.from_v1(get_context_graph(FIXTURE, ToolRecorder()))
        code_only = full.prune_to_layers({"code"})
        # no cross-layer edges survive
        from src.semgraph.schema_v2 import LAYER_OF_EDGE
        assert all(LAYER_OF_EDGE[e.type] == "code"
                   for e in code_only.all_edges())
        assert code_only.stats()["edges"] < full.stats()["edges"]
