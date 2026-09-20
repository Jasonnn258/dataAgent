"""Phase 5 tests: semantica context graph build/query + graph evidence layers."""
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
from src.tasks.locate import run_locate  # noqa: E402
from src.tasks.impact import run_impact  # noqa: E402
from src.tasks.rollback import run_rollback  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


def _graph(seeded):
    from src.semgraph.enrich import get_context_graph
    return get_context_graph(FIXTURE, ToolRecorder())


# ---------------------------------------------------------------- error path
def test_missing_semantica_raises_graph_error(seeded, monkeypatch):
    """If semantica truly is absent, mode=semantica must fail loudly."""
    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("semantica"):
            raise ImportError("No module named 'semantica' (simulated)")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    from src.semgraph.graph import ContextGraph
    with pytest.raises(GraphError, match="pip install semantica"):
        ContextGraph(FIXTURE, ToolRecorder())


# ---------------------------------------------------------------- graph build
def test_graph_build_counts(seeded):
    cg = _graph(seeded)
    types = {}
    for e in cg.kg.entities:
        types[e["type"]] = types.get(e["type"], 0) + 1
    assert types.get("File") == 12          # fixture file count
    assert types.get("Commit") == 5
    assert types.get("APIEndpoint") == 2    # login + generate routes
    assert types.get("UIString", 0) > 0
    assert cg.stats()["relationships"] > 30
    rel_types = {r["type"] for r in cg.kg.relationships}
    for expected in ("CONTAINS", "DEFINES", "IMPORTS", "CALLS", "MODIFIES",
                     "CO_CHANGED_WITH"):
        assert expected in rel_types, f"edge type {expected} missing"


def test_graph_pathfinder_finds_chain(seeded):
    cg = _graph(seeded)
    # login route file scope must reach auth functions via CALLS
    scopes = [e for e in cg.kg.entities
              if e["id"].startswith("scope:") and e.get("file") == "src/app/api/auth/login/route.ts"]
    assert scopes
    auth_syms = [e for e in cg.kg.entities
                 if e["type"] == "Function" and e.get("name") == "verifyPassword"]
    assert auth_syms
    chain = cg.path(scopes[0]["id"], auth_syms[0]["id"])
    assert chain and len(chain) >= 2, "route -> auth call chain must exist in graph"


def test_graph_dump_roundtrip(seeded, tmp_path):
    cg = _graph(seeded)
    p = tmp_path / "graph.json"
    cg.dump(p)
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data["entities"]) == cg.stats()["entities"]


# ---------------------------------------------------------------- e2e per task
def _locate(mode="semantica"):
    s = Settings(repo=FIXTURE, mode=mode, task="locate", no_llm=True, top_k=5)
    return run_locate(s, "系统标题在哪里修改")


def test_locate_semantica_keeps_layout_top(seeded):
    out = _locate()
    top = out.result.candidates[0]
    assert top.file == "src/app/layout.tsx"
    assert any(t.startswith("semgraph:") for t in out.system.tool_calls)


def test_impact_semantica_has_graph_evidence(seeded):
    s = Settings(repo=FIXTURE, mode="semantica", task="impact", no_llm=True)
    out = run_impact(s, "generateWithRetry")
    assert out.result.target.file == "src/lib/retry.ts"
    routes = [r for r in out.result.related if r.kind == "api_route"]
    assert routes, "semantica mode must still surface api routes"
    graph_ev = [e for r in out.result.related for e in r.evidence
                if e.kind in ("graph_path", "graph_provenance")]
    assert graph_ev, "graph layer should attach PathFinder/provenance evidence"


def mixed_sha() -> str:
    from src.git_history.api import GitAPI
    api = GitAPI(FIXTURE, ToolRecorder())
    return next(c.sha for c in api.log() if "reword system title" in c.subject)


def test_rollback_semantica_decision_matches_structural(seeded):
    q = "登录逻辑昨天改坏了，需要撤销，但保留同一次提交中的标题修改"
    out = run_rollback(Settings(repo=FIXTURE, mode="semantica", task="rollback", no_llm=True),
                       q, mixed_sha())
    r = out.result
    assert len(r.change_units) == 2
    rb = [u for u in r.change_units if u.unit_id in r.changes_to_rollback]
    keep = [u for u in r.change_units if u.unit_id in r.changes_to_keep]
    assert len(rb) == 1 and rb[0].semantic_label == "auth"
    assert len(keep) == 1 and keep[0].semantic_label == "title"
    assert r.collateral_damage_risk == 0.0
    assert any(t.startswith("semgraph:") for t in out.system.tool_calls)


# ---------------------------------------------------------------- eval metrics
def test_prf_and_collateral():
    from src.eval.metrics import _prf
    m = _prf({1, 2, 3}, {2, 3, 4})
    assert m["precision"] == round(2 / 3, 4) and m["recall"] == round(2 / 3, 4)
    assert m["f1"] == round(2 / 3, 4)


def test_eval_runner_fixture_tasks_exist():
    from src.eval.runner import load_tasks
    tasks = load_tasks(ROOT / "experiments" / "tasks.jsonl")
    fx = [t for t in tasks if t["gold_source"] == "by_construction"]
    manual = [t for t in tasks if t["gold_source"] == "manual"]
    assert len(fx) == 5 and all(t["gold"] for t in fx)
    assert len(manual) == 4 and all(t["gold"] is None for t in manual)
    assert {t["task_type"] for t in tasks} == {"locate", "impact", "rollback"}
