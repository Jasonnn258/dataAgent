"""Skill-level evaluation（Phase 11J）。

已有的 task evaluation / graph ablation 回答"整条链行不行"；本脚本
回答"**哪个 skill** 行不行"：Agent 失败时，来这里定位是哪一环。

每个 skill 记录一行（统一来自 SkillResult + ExecutionRecorder 事件）：

  skill_name | status | latency_ms | broker_calls | physical_tool_calls
  | llm_calls | evidence_count | （各 skill 专属正确性指标）

正确性指标对照 fixture 的已知答案（ground truth）：

  resolve_target        target_correct
  change_unit_analysis  unit_match_correct
  coupling_analysis     precision / recall
  impact_analysis       routes precision / recall
  safe_rollback         rollback_precision / preservation_rate /
                        collateral_damage
  evidence_verification supported_fraction
  policy_check          action_correct

用法：
  python experiments/skill_eval.py [--repo PATH] [--out PATH]
                                   [--query Q] [--keep-hint Q] [--llm]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# fixture 已知答案（与 tests/test_phase11.py 同一真相源）
GT_FEATURE_PROBLEM = "feature:AuthLogin"
GT_FEATURE_KEEP = "feature:SystemBranding"
GT_UNIT_PROBLEM = "bbdc659f-U2"      # auth 单元（登录逻辑）
GT_UNIT_KEEP = "bbdc659f-U1"         # title 单元（系统标题）
GT_COUPLING = {"src/lib/ai-helpers.ts imports src/lib/retry.ts"}
GT_ROUTES = {"/api/auth/login"}
GT_IMPACT_TARGET = "sym:src/lib/auth.ts::validateAccount"   # 场景输入
GT_POLICY_ACTION = "HUMAN_REVIEW"

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"


def prf(pred: list[str], gold: set[str]) -> tuple[float, float]:
    """集合版 precision / recall（任一侧为空时该侧记 0）。"""
    p = set(pred)
    if not p:
        return (0.0, 1.0 if not gold else 0.0)
    tp = len(p & gold)
    prec = tp / len(p) if p else 0.0
    rec = tp / len(gold) if gold else 1.0
    return (round(prec, 3), round(rec, 3))


def _run_one(rt, rec, name: str, context: dict, n_events: int) -> dict:
    """跑一个 skill 并从 SkillResult + 新增执行事件里取一行指标。"""
    result = rt.run(name, context)
    new_events = rec.events[n_events:]
    spans = [e for e in new_events
             if e.layer == "skill" and e.actor == name and e.action == "run"]
    latency = spans[-1].duration_ms if spans else 0.0
    row = {
        "skill": name,
        "status": result.status,
        "latency_ms": latency,
        "broker_calls": result.broker_calls,
        "physical_tool_calls": sum(1 for e in new_events
                                   if e.layer == "tool"),
        "llm_calls": sum(1 for e in new_events if e.layer == "llm"),
        "evidence_count": len(result.evidence_ids or []),
        "capabilities_used": result.capabilities_used,
        "warnings": len(result.warnings or []),
        "error": result.error,
    }
    return row, result


def run_skill_eval(repo: Path, query: str = DEMO_QUERY,
                   keep_hint: str = DEMO_KEEP,
                   llm=None) -> dict:
    """在 repo 上跑完整 skill 链并汇总每技能指标（测试直接调用）。"""
    from src.execution import ExecutionRecorder
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    from src.skills import SkillRuntime

    rec = ExecutionRecorder()
    broker = ContextBroker(repo, rec)
    build_change_graph(broker)
    rt = SkillRuntime(broker, llm=llm)
    rows: list[dict] = []

    def step(name: str, ctx: dict, prereq_ok: bool = True):
        """跑一个 skill；上游断了就记一行 failed（定位是哪环失败）。"""
        if not prereq_ok:
            rows.append({"skill": name, "status": "failed",
                         "latency_ms": 0.0, "broker_calls": 0,
                         "physical_tool_calls": 0, "llm_calls": 0,
                         "evidence_count": 0, "capabilities_used": [],
                         "warnings": 0,
                         "error": "upstream skill failed", "metrics": {}})
            return None
        row, result = _run_one(rt, rec, name, ctx, len(rec.events))
        rows.append(row)
        return result

    # ---- 1. 导航（问题侧 + keep 侧）----
    nav = step("resolve_target", {"query": query, "task_id": "eval"})
    nav_keep = step("resolve_target", {"query": keep_hint, "task_id": "eval"})
    rows[-2]["metrics"] = {
        "target_correct": int(nav.status == "success"
                              and nav.data.get("feature_id") == GT_FEATURE_PROBLEM)}
    rows[-1]["metrics"] = {
        "target_correct": int(nav_keep.status == "success"
                              and nav_keep.data.get("feature_id") == GT_FEATURE_KEEP)}

    # ---- 2. 变更单元匹配（两侧；上游断 → failed 行，链条不断）----
    cu = step("change_unit_analysis",
              {"terms": nav.data.get("terms", []) if nav else [],
               "task_id": "eval"},
              prereq_ok=bool(nav and nav.ok))
    cu_keep = step("change_unit_analysis",
                   {"terms": nav_keep.data.get("terms", []) if nav_keep else [],
                    "task_id": "eval"},
                   prereq_ok=bool(nav_keep and nav_keep.ok))
    for row, res, gold in ((rows[-2], cu, GT_UNIT_PROBLEM),
                           (rows[-1], cu_keep, GT_UNIT_KEEP)):
        matches = res.data.get("matches", []) if res else []
        row["metrics"] = {
            "unit_match_correct": int(
                any(gold in m.get("unit_id", "") for m in matches)),
            "n_matches": len(matches)}

    # ---- 3. task view + impact（锚点是场景输入：demo 任务指向
    # validateAccount；换 repo 时退回导航 related_symbols）----
    target = (GT_IMPACT_TARGET
              if broker.node(GT_IMPACT_TARGET) is not None
              else (nav.data.get("related_symbols") or [""])[0])
    step("build_task_view", {"task_id": "eval", "target_ids": [target]})
    rows[-1]["metrics"] = {
        "task_graph_nodes": rows[-1]["status"] == "success" and
        _view_nodes(broker, "eval") or 0}
    imp = step("impact_analysis", {"target_ids": [target], "task_id": "eval"})
    routes = imp.data.get("routes", []) if imp.ok else []
    p, r = prf(routes, GT_ROUTES)
    rows[-1]["metrics"] = {"routes_precision": p, "routes_recall": r,
                           "callers_found": len(imp.data.get("caller_ids", []))
                           if imp.ok else 0}

    # ---- 4. 耦合 ----
    cop = step("coupling_analysis", {
        "files_a": ["src/lib/ai-helpers.ts"],
        "files_b": ["src/lib/retry.ts"]})
    p, r = prf(cop.data.get("couplings", []), GT_COUPLING)
    rows[-1]["metrics"] = {"precision": p, "recall": r}

    # ---- 5. 验证 ----
    finding_ids = ([m["finding_id"] for m in cu.data.get("matches", [])]
                   if cu and cu.ok else [])
    ver = step("evidence_verification", {"finding_ids": finding_ids,
                                         "task_id": "eval"})
    verdicts = ver.data.get("verdicts", []) if ver.ok else []
    rows[-1]["metrics"] = {
        "supported_fraction": round(
            sum(1 for v in verdicts if v.status.value == "SUPPORTED")
            / len(verdicts), 3) if verdicts else 0.0,
        "n_verdicts": len(verdicts)}

    # ---- 6. 仲裁 + 策略门 ----
    sr = step("safe_rollback", {
        "problem_matches": cu.data.get("matches", []) if cu and cu.ok else [],
        "keep_matches": cu_keep.data.get("matches", []) if cu_keep and cu_keep.ok else [],
        "affected_routes": list(GT_ROUTES), "task_id": "eval"})
    rb_ids = {u["id"] for u in sr.data.get("rollback_units", [])}
    keep_ids = {u["id"] for u in sr.data.get("keep_units", [])}
    rb_hit = {i for i in rb_ids if GT_UNIT_PROBLEM in i}
    keep_hit = {i for i in keep_ids if GT_UNIT_KEEP in i}
    collateral = {i for i in rb_ids if GT_UNIT_KEEP in i}
    rows[-1]["metrics"] = {
        "rollback_precision": round(len(rb_hit) / len(rb_ids), 3) if rb_ids else 0.0,
        # fixture 期望保住的 keep 单元恰 1 个：命中数即保留率
        "preservation_rate": min(len(keep_hit), 1),
        "collateral_damage": len(collateral)}

    pol = step("policy_check", {
        "rollback_symbols": sr.data.get("rollback_symbols", []),
        "keep_symbols": sr.data.get("keep_symbols", []),
        "affected_routes": list(GT_ROUTES), "task_id": "eval"})
    rows[-1]["metrics"] = {
        "action_correct": int(pol.data.get("action") == GT_POLICY_ACTION),
        "action": pol.data.get("action", "")}

    summary = {
        "skills_run": len(rows),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "partial": sum(1 for r in rows if r["status"] == "partial"),
        "llm_calls_total": sum(r["llm_calls"] for r in rows),
        "evidence_total": sum(r["evidence_count"] for r in rows),
        "recorder_summary": rec.summary(),
    }
    return {"rows": rows, "summary": summary}


def _view_nodes(broker, task_id: str) -> int:
    """task view 节点数（build_task_view 行的规模指标）。"""
    try:
        view = broker.get_task_view(task_id)
        return len(view.selected_nodes) if view else 0
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path,
                    default=ROOT / "experiments" / "fixtures" / "fixture_repo")
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "skill_eval.json")
    ap.add_argument("--query", default=DEMO_QUERY)
    ap.add_argument("--keep-hint", default=DEMO_KEEP)
    ap.add_argument("--llm", action="store_true",
                    help="启用 LLM 语义精化（默认全确定性，llm_calls=0）")
    args = ap.parse_args()

    llm = None
    if args.llm:
        from src.config import LLMConfig
        from src.llm.client import LLMClient
        cfg = LLMConfig()
        llm = LLMClient(cfg) if cfg.available else None
        if llm is None:
            print("LLM not configured (LLM_BASE_URL/LLM_API_KEY) — "
                  "running deterministic", file=sys.stderr)

    report = run_skill_eval(args.repo, args.query, args.keep_hint, llm=llm)

    cols = ("skill", "status", "latency_ms", "broker_calls",
            "physical_tool_calls", "llm_calls", "evidence_count")
    widths = [max(len(c), *(len(str(r[c])) for r in report["rows"]))
              for c in cols]
    print(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in report["rows"]:
        print(" | ".join(str(r[c]).ljust(w)
                        for c, w in zip(cols, widths)))
        if r["metrics"]:
            print("    metrics:", json.dumps(r["metrics"], ensure_ascii=False))
    print("\nsummary:", json.dumps(report["summary"], ensure_ascii=False))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nwrote {args.out}")
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
