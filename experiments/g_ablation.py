"""Phase 10: G0-G4 graph-layer ablation.

One scenario (the acceptance demo: "登录改坏了，回退但保留同 commit 的系统标题")
run with graph layers switched on progressively:

    G0  code              G1  +semantic          G2  +change
    G3  +taskview         G4  +evidence/decision/policy

Metrics per level: navigation ok, problem/keep unit hit, policy gated,
verification pass rate, evidence coverage, task_graph_nodes, report size,
tool calls, llm calls. Answers:

    Q1  is the semantic layer needed to even enter via fuzzy NL? (G0->G1)
    Q2  is the change layer needed for unit-level rollback?       (G1->G2)
    Q3  does the task view bound the context?                     (G2->G3)
    Q4  do evidence/decision/policy make it verifiable?           (G3->G4)

Usage: python experiments/g_ablation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.agents import Orchestrator                       # noqa: E402
from src.errors import DataAgentError                     # noqa: E402
from src.schema import ToolRecorder                       # noqa: E402
from src.semgraph.change_graph import build_change_graph  # noqa: E402
from src.semgraph.context_broker import ContextBroker     # noqa: E402

QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
KEEP = "保留同 commit 中已经改好的系统标题"
GOLD_PROBLEM = "bbdc659f-U2"
GOLD_KEEP = "bbdc659f-U1"

LEVELS = [
    ("G0", {"code"}),
    ("G1", {"code", "semantic"}),
    ("G2", {"code", "semantic", "change"}),
    ("G3", {"code", "semantic", "change", "taskview"}),
    ("G4", {"code", "semantic", "change", "taskview", "evidence", "decision"}),
]


def run_level(name: str, layers: set[str]) -> dict:
    rec = ToolRecorder()
    broker = ContextBroker(FIXTURE, rec, layers=layers)
    if "change" in layers:
        build_change_graph(broker)
    orch = Orchestrator(broker)
    m: dict = {"level": name, "nav": "fail", "problem": 0, "keep": 0,
               "policy": "-", "verified": 0, "verdicts": 0,
               "findings": 0, "evidence": 0, "view_nodes": 0,
               "report_chars": 0, "tool_calls": 0, "error": ""}
    try:
        r = orch.run(QUERY, keep_hint=KEEP)
    except DataAgentError as e:
        m["error"] = str(e)[:80]
        m["tool_calls"] = len(rec.calls)
        return m
    m["nav"] = "ok" if r.navigation and r.navigation.feature_id else "none"
    m["problem"] = int(any(GOLD_PROBLEM in u.unit_id for u in r.problem_units))
    m["keep"] = int(any(GOLD_KEEP in u.unit_id for u in r.keep_units))
    if r.plan is not None and r.plan.policy_result is not None:
        m["policy"] = r.plan.policy_result.action.value
    elif r.plan is not None:
        m["policy"] = "ungated" if r.plan.rollback_units else "-"
    m["verdicts"] = len(r.verdicts)
    m["verified"] = sum(1 for f in broker.all_findings()
                        if f.status == "verified")
    m["findings"] = len(broker.all_findings())
    m["evidence"] = len(broker.all_evidence())
    m["view_nodes"] = r.slice.stats().get("task_graph_nodes", 0) \
        if r.slice and r.slice.view else 0
    m["report_chars"] = len(r.dump())
    m["tool_calls"] = len(rec.calls)
    return m


def main() -> None:
    rows = [run_level(n, ls) for n, ls in LEVELS]
    cols = ["level", "nav", "problem", "keep", "policy", "verdicts",
            "verified", "findings", "evidence", "view_nodes",
            "report_chars", "tool_calls", "error"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print(" | ".join(c.ljust(widths[c]) for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r[c]).ljust(widths[c]) for c in cols))
    print()
    g = {r["level"]: r for r in rows}
    print("Q1 semantic needed for fuzzy entry:      ",
          f"G0 nav={g['G0']['nav']} -> G1 nav={g['G1']['nav']}")
    print("Q2 change layer needed for unit split:   ",
          f"G1 problem/keep={g['G1']['problem']}/{g['G1']['keep']} -> "
          f"G2 {g['G2']['problem']}/{g['G2']['keep']}")
    print("Q3 task view bounds context:             ",
          f"G2 view_nodes={g['G2']['view_nodes']} "
          f"G3 view_nodes={g['G3']['view_nodes']} "
          f"(report {g['G2']['report_chars']} vs {g['G3']['report_chars']} chars)")
    print("Q4 evidence/decision make it verifiable:",
          f"G3 verdicts={g['G3']['verdicts']} policy={g['G3']['policy']} -> "
          f"G4 verdicts={g['G4']['verdicts']} verified={g['G4']['verified']} "
          f"policy={g['G4']['policy']}")


if __name__ == "__main__":
    main()
