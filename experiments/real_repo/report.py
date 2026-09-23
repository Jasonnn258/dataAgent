"""真实 repo 报告输出（Phase 12A）：JSONL 原始 + Markdown 汇总。

不抹平细节：per-task 行、失败案例、pending 任务单列 —— 平均值只是
最后一行，不是全部。
"""
from __future__ import annotations

import json
from pathlib import Path

METRIC_ORDER = ("file_precision", "file_recall", "symbol_hit",
                "caller_precision", "caller_recall", "route_precision",
                "route_recall", "commit_recall", "rollback_precision",
                "rollback_recall", "preservation_rate",
                "collateral_damage", "policy_action_match", "unresolved")

# 执行环指标（12A / Phase 13 集成）：评的是沙箱实际 diff 文件面
EXEC_METRIC_ORDER = ("exec_applied", "exec_verified", "exec_precision",
                     "exec_recall", "exec_preservation", "exec_collateral")


def write_jsonl(rows: list[dict], path: Path) -> None:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r.get("scores", {}).get("scored")]
    pending = [r for r in rows if not r.get("scores", {}).get("scored")]
    failed = [r for r in rows if r.get("status") in ("failed", "error")]
    by_type: dict[str, list[dict]] = {}
    for r in scored:
        by_type.setdefault(r["task_type"], []).append(r)

    def avg(vals: list) -> float:
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    type_summary = {}
    for t, rs in sorted(by_type.items()):
        s = {"n": len(rs)}
        for m in METRIC_ORDER:
            a = avg([r["scores"].get(m) for r in rs
                     if m in r.get("scores", {})])
            if a is not None:
                s[m] = a
        type_summary[t] = s
    return {"total": len(rows), "scored": len(scored),
            "pending": len(pending), "failed": len(failed),
            "by_type": type_summary,
            "avg_latency_ms": avg([r.get("latency_ms") for r in rows]),
            "avg_evidence": avg([r.get("evidence_count") for r in rows]),
            "total_llm_calls": sum(r.get("llm_calls", 0) for r in rows)}


def summarize_exec(rows: list[dict]) -> dict:
    """执行环汇总（exec_results.jsonl → 平均指标 + 停车点分布）。"""
    from collections import Counter

    scored = [r for r in rows if r.get("exec_scores", {}).get("scored")]
    stops = Counter(r.get("stopped_at") or "-" for r in rows)

    def avg(vals: list) -> float:
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    out = {"n": len(rows), "scored": len(scored),
           "stopped_at": dict(stops),
           "avg_changed_files": avg(
               [len(r.get("exec_changed_files", [])) for r in rows])}
    for m in EXEC_METRIC_ORDER:
        a = avg([r["exec_scores"].get(m) for r in scored
                 if m in r.get("exec_scores", {})])
        if a is not None:
            out[m] = a
    return out


def write_markdown(summary: dict, rows: list[dict], path: Path,
                   exec_rows: list[dict] | None = None,
                   exec_summary: dict | None = None,
                   oracle_rows: list[dict] | None = None,
                   oracle_summary: dict | None = None) -> None:
    lines = ["# Real Repository Benchmark (Phase 12A)", "",
             f"tasks: {summary['total']} | scored: {summary['scored']} "
             f"| pending gold: {summary['pending']} | "
             f"failed: {summary['failed']}", ""]

    lines += ["## Per-task results", "",
              "| task | repo | type | status | scored | key metrics |",
              "|---|---|---|---|---|---|"]
    for r in rows:
        sc = r.get("scores", {})
        if sc.get("scored"):
            metrics = " ".join(
                f"{m}={sc[m]}" for m in METRIC_ORDER if m in sc)
        else:
            metrics = f"*{sc.get('reason', 'pending')}*"
        lines.append(f"| {r['task_id']} | {r['repo']} | {r['task_type']} "
                     f"| {r.get('status', '?')} "
                     f"| {'yes' if sc.get('scored') else 'no'} "
                     f"| {metrics} |")

    lines += ["", "## Summary by task type", "",
              "| type | n | " + " | ".join(METRIC_ORDER) + " |",
              "|---|---|" + "---|" * len(METRIC_ORDER)]
    for t, s in summary["by_type"].items():
        cells = [str(s.get(m, "")) for m in METRIC_ORDER]
        lines.append(f"| {t} | {s['n']} | " + " | ".join(cells) + " |")

    failed = [r for r in rows if r.get("status") in ("failed", "error")]
    if failed:
        lines += ["", "## Failure cases", ""]
        for r in failed:
            skill_fail = {k: v for k, v in
                          r.get("skill_statuses", {}).items()
                          if v == "failed"}
            lines += [f"### {r['task_id']} ({r['repo']}, {r['task_type']})",
                      f"- error: `{r.get('error', '')[:200]}`",
                      f"- failed skills: `{skill_fail or '—'}`", ""]

    lines += ["", "## Execution metrics", "",
              f"- avg latency: {summary['avg_latency_ms']} ms",
              f"- avg evidence per task: {summary['avg_evidence']}",
              f"- total llm calls: {summary['total_llm_calls']}"]

    # ---- 执行环（Phase 13 集成；exec_rows 由 execution_bench 产出）----
    def _exec_section(title: str, rows_x: list[dict] | None,
                      es: dict | None) -> None:
        if not rows_x:
            return
        es = es or summarize_exec(rows_x)
        lines.extend(["", f"## {title}", "",
                      f"tasks: {es['n']} | scored: {es['scored']} | "
                      f"stopped_at: `{es['stopped_at']}`", "",
                      "| task | repo | status | stopped_at | gates | "
                      "changed | key exec metrics |",
                      "|---|---|---|---|---|---|---|"])
        for r in rows_x:
            sc = r.get("exec_scores", {})
            if sc.get("scored"):
                metrics = " ".join(f"{m}={sc[m]}"
                                   for m in EXEC_METRIC_ORDER if m in sc)
            else:
                metrics = f"*{sc.get('reason', 'pending')}*"
            gates = f"{r.get('pre_gate') or '-'}/" \
                    f"{r.get('post_gate') or '-'}"
            lines.append(
                f"| {r['task_id']} | {r['repo']} "
                f"| {r.get('exec_status') or '-'} "
                f"| {r.get('stopped_at') or '-'} | {gates} "
                f"| {len(r.get('exec_changed_files', []))} "
                f"| {metrics} |")
        agg = " | ".join(f"{m}={es[m]}" for m in EXEC_METRIC_ORDER
                         if m in es)
        lines.extend(["", f"**aggregates**: {agg} "
                      f"(avg_changed_files={es['avg_changed_files']})"])

    _exec_section("Execution loop — analysis plan (sandbox, Phase 13)",
                  exec_rows, exec_summary)
    # oracle 对照：gold commits 直取单元，只量执行环（分析层好坏见上表）
    _exec_section("Execution loop — oracle plan (analysis bypassed)",
                  oracle_rows, oracle_summary)
    path.write_text("\n".join(lines) + "\n")
