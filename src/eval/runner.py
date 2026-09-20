"""Ablation runner: tasks.jsonl x {lexical, structural, structural_git, semantica}.

Usage:
    python -m src.eval.runner [--tasks experiments/tasks.jsonl]
                              [--modes lexical,structural,structural_git,semantica]
                              [--out experiments/results.jsonl]
                              [--report experiments/results.md]

Each task record may pin a specific commit (rollback) and carry gold.
Records without gold are executed but skipped by scoring (gold_source=manual
means a human must fill it in later — we never fabricate real-repo ground
truth, per spec).
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from src.config import Settings, VALID_MODES
from src.errors import DataAgentError
from src.eval.metrics import compute_metrics
from src.tasks import get_runner

ROOT = Path(__file__).resolve().parent.parent.parent


def load_tasks(path: Path) -> list[dict]:
    tasks = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("//") and not ln.startswith("#"):
            tasks.append(json.loads(ln))
    return tasks


def resolve_repo(repo_field: str) -> Path:
    p = Path(repo_field)
    return p if p.is_absolute() else (ROOT / p)


def run_one(task: dict, mode: str) -> dict:
    repo = resolve_repo(task["target_repo"])
    s = Settings(repo=repo, mode=mode, task=task["task_type"], no_llm=True)
    s.validate()
    runner = get_runner(task["task_type"])
    t0 = time.perf_counter()
    output = runner(s, task["query"], task.get("commit"))
    wall = round((time.perf_counter() - t0) * 1000, 1)

    rec = {
        "task_id": task["id"], "task_type": task["task_type"], "mode": mode,
        "query": task["query"],
        "wall_ms": wall,
        "system": output.system.model_dump(),
    }
    gold = task.get("gold") or {}
    if gold:
        rec["metrics"] = compute_metrics(task["task_type"], output, gold,
                                         k=s.top_k)
    else:
        rec["metrics"] = None
        rec["note"] = "no gold — manual annotation pending"
    return rec


def summarize(records: list[dict]) -> list[dict]:
    """Aggregate mean metrics per (task_type, mode) over scored records."""
    agg: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "sums": defaultdict(float),
                                                  "counted": defaultdict(int)})
    for r in records:
        m = r.get("metrics")
        if not m:
            continue
        key = (r["task_type"], r["mode"])
        a = agg[key]
        a["n"] += 1
        for mk, mv in m.items():
            if isinstance(mv, (int, float)) and mv is not None:
                a["sums"][mk] += mv
                a["counted"][mk] += 1
    out = []
    for (task_type, mode), a in sorted(agg.items()):
        row = {"task_type": task_type, "mode": mode, "n": a["n"]}
        for mk, total in a["sums"].items():
            row[mk] = round(total / a["counted"][mk], 4)
        out.append(row)
    return out


def write_report(rows: list[dict], records: list[dict], path: Path) -> None:
    cols_by_task = {
        "locate": ["file_recall_at_k", "file_precision_at_k"],
        "impact": ["precision", "recall", "f1"],
        "rollback": ["precision", "recall", "preservation_rate",
                     "collateral_damage_rate"],
    }
    lines = ["# Ablation results", "",
             f"- total runs: {len(records)}  scored: {sum(1 for r in records if r.get('metrics'))}",
             ""]
    for task_type, cols in cols_by_task.items():
        sub = [r for r in rows if r["task_type"] == task_type]
        if not sub:
            continue
        lines += [f"## {task_type}", "",
                  "| mode | n | " + " | ".join(cols) + " |",
                  "|---|" + "---|" * (len(cols) + 1)]
        for r in sub:
            vals = " | ".join(str(r.get(c, "—")) for c in cols)
            lines.append(f"| {r['mode']} | {r['n']} | {vals} |")
        lines.append("")
    # system metrics across all tasks
    lines += ["## system", "",
              "| mode | mean latency ms | mean context chars | mean tool calls | n |",
              "|---|---|---|---|---|"]
    sys_agg = defaultdict(list)
    for r in records:
        if "system" in r:
            sys_agg[r["mode"]].append(r["system"])
    n_err = sum(1 for r in records if r.get("error"))
    if n_err:
        lines.insert(2, f"- errored runs: {n_err} (see results.jsonl `error` field)")
    for mode, syss in sorted(sys_agg.items()):
        lat = sum(s["latency_s"] * 1000 for s in syss) / len(syss)
        ctx = sum(s["retrieved_context_chars"] for s in syss) / len(syss)
        tools = sum(s["tool_call_count"] for s in syss) / len(syss)
        lines.append(f"| {mode} | {lat:.0f} | {ctx:.0f} | {tools:.1f} | {len(syss)} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(ROOT / "experiments" / "tasks.jsonl"))
    ap.add_argument("--modes", default=",".join(VALID_MODES))
    ap.add_argument("--out", default=str(ROOT / "experiments" / "results.jsonl"))
    ap.add_argument("--report", default=str(ROOT / "experiments" / "results.md"))
    ap.add_argument("--task-id", action="append",
                    help="run only these task ids (repeatable)")
    args = ap.parse_args()

    tasks = load_tasks(Path(args.tasks))
    if args.task_id:
        tasks = [t for t in tasks if t["id"] in set(args.task_id)]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    records: list[dict] = []
    for task in tasks:
        for mode in modes:
            try:
                rec = run_one(task, mode)
            except (DataAgentError, FileNotFoundError) as e:
                rec = {"task_id": task["id"], "task_type": task["task_type"],
                       "mode": mode, "error": f"{type(e).__name__}: {e}"}
            records.append(rec)
            if rec.get("error"):
                status = f"ERROR: {rec['error'][:60]}"
            elif rec.get("metrics"):
                status = "scored"
            else:
                status = "ran (no gold)"
            print(f"[{task['id']}] {mode:15s} -> {status}")

    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    rows = summarize(records)
    write_report(rows, records, Path(args.report))
    print(f"\nwrote {out} and {Path(args.report)}")


if __name__ == "__main__":
    main()
