"""真实 repo benchmark 执行器（Phase 12A）。

按 task_type 走**真实 skill 链**（与 skill_eval 同一套 SkillRuntime /
ExecutionRecorder），不做任何 repo 写操作。每任务产出：

  status / latency_ms / skills[] / evidence_count / llm_calls
  / task_graph 覆盖 / 各类型原始输出（evaluator 消费）

用法：
  python -m experiments.real_repo.benchmark --repos chalk,zustand
         [--tasks tasks.jsonl] [--out-dir results]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from experiments.real_repo.loader import BrokerPool, check_repo  # noqa: E402
from experiments.real_repo.schema import Task, load_tasks  # noqa: E402

TASKS_PATH = Path(__file__).resolve().parent / "tasks.jsonl"


def _terms_from_query(query: str) -> list[str]:
    """query → 检索词表（与 resolve_target 同源的确定性词元）。"""
    from src.search.keywords import extract_terms
    qt = extract_terms(query)
    terms = {t.lower() for t, _ in qt.all_search_terms()}
    terms |= set(qt.cjk_segments) | set(qt.cjk_subterms)
    return sorted(t for t in terms if t)


def _usable_terms(terms: list[str] | None, query: str) -> list[str]:
    """词元可用性归一（12A harness 修复，不动 skill 保基线零漂移）。

    resolve_target 的确定性退路返回 terms=[query]（整句单元素），cu
    匹配必然落空；resolve 失败时 terms=None。两种情况都换成确定性
    词表 —— cu 消费的是词元，不该因为导航失败整链量不到东西。
    """
    if not terms or list(terms) == [query]:
        return _terms_from_query(query)
    return list(terms)


def _skill_row(rt, rec, name: str, ctx: dict) -> dict:
    """跑一个 skill，取 SkillResult + 该区间的执行事件统计。"""
    n0 = len(rec.events)
    t0 = time.perf_counter()
    result = rt.run(name, ctx)
    wall = round((time.perf_counter() - t0) * 1000, 1)
    new = rec.events[n0:]
    spans = [e for e in new if e.layer == "skill" and e.actor == name]
    return {"skill": name, "status": result.status,
            "latency_ms": spans[-1].duration_ms if spans else wall,
            "evidence_ids": list(result.evidence_ids or []),
            "llm_calls": sum(1 for e in new if e.layer == "llm"),
            "error": result.error,
            "_result": result}


class RealRepoBenchmark:
    def __init__(self, pool: BrokerPool | None = None,
                 commits_limit: int = 900):
        # 真实 repo 默认大窗口（zustand 最远 gold 在 803 commits 前）；
        # fixture 路径不受影响（skill_eval/g_ablation 自建 broker）
        self.pool = pool or BrokerPool(commits_limit=commits_limit)

    # ------------------------------------------------------------ 各任务链
    def run_task(self, task: Task) -> dict:
        entry = self.pool.get(task.repo)
        broker, rt, rec = entry["broker"], entry["runtime"], entry["broker"].rec
        n0 = len(rec.events)
        t0 = time.perf_counter()
        rows: list[dict] = []
        raw: dict = {"task_id": task.id, "repo": task.repo,
                     "task_type": task.task_type, "query": task.query,
                     "gold_source": task.gold_source}

        if task.task_type == "locate":
            nav = _skill_row(rt, rec, "resolve_target",
                             {"query": task.query})
            rows.append(nav)
            res = nav["_result"]
            raw["targets"] = {
                "feature_id": res.data.get("feature_id") if res else None,
                "related_symbols": list(res.data.get("related_symbols", []))
                if res else [],
                "files": _files_of(broker,
                                   res.data.get("related_symbols", []))
                if res else []}

        elif task.task_type == "impact":
            anchor = _resolve_anchor(broker, task.anchor)
            if not task.anchor:
                nav = _skill_row(rt, rec, "resolve_target",
                                 {"query": task.query})
                rows.append(nav)
                res = nav["_result"]
                anchor = ((res.data.get("related_symbols") or [""])[0]
                          if res else "")
            v = _skill_row(rt, rec, "build_task_view",
                           {"task_id": f"tv-{task.id}", "target_ids": [anchor]})
            rows.append(v)
            imp = _skill_row(rt, rec, "impact_analysis",
                             {"target_ids": [anchor], "task_id": f"tv-{task.id}"})
            rows.append(imp)
            res = imp["_result"]
            raw["targets"] = {
                "anchor": anchor,
                "callers": list(res.data.get("caller_ids", [])) if res else [],
                "routes": list(res.data.get("routes", [])) if res else [],
                "files": _files_of(broker,
                                   res.data.get("caller_ids", [])) if res else []}

        elif task.task_type == "history":
            terms = task.terms or _terms_from_query(task.query)
            cu = _skill_row(rt, rec, "change_unit_analysis",
                            {"terms": terms, "task_id": task.id})
            rows.append(cu)
            res = cu["_result"]
            matches = res.data.get("matches", []) if res else []
            raw["targets"] = {
                "terms": terms,
                "commits": sorted({m.get("commit", "") for m in matches
                                   if m.get("commit")}),
                "unit_ids": [m.get("unit_id") for m in matches],
                "files": sorted({f for m in matches
                                 for f in (m.get("files") or [])})}

        elif task.task_type in ("rollback", "compound"):
            # rollback/compound 都需要先定位 + 双侧单元。resolve 失败不整链
            # 跳过：cu 消费的是词表，退到确定性词元（history 分支同形），
            # resolve 状态如实记录 —— 导航失败是 12B 的被测对象，不该让
            # 整条回退分支在真实 repo 上量不到任何东西。
            nav = _skill_row(rt, rec, "resolve_target",
                             {"query": task.query, "task_id": task.id})
            rows.append(nav)
            nr = nav["_result"]
            terms_p = _usable_terms(nr.data.get("terms") if nr else None,
                                    task.query)
            cu = _skill_row(rt, rec, "change_unit_analysis",
                            {"terms": terms_p, "task_id": task.id})
            rows.append(cu)
            keep_matches: list[dict] = []
            keep_resolve = ""
            if task.keep_hint:
                navk = _skill_row(rt, rec, "resolve_target",
                                  {"query": task.keep_hint,
                                   "task_id": task.id})
                rows.append(navk)
                kr = navk["_result"]
                keep_terms = _usable_terms(kr.data.get("terms") if kr else None,
                                           task.keep_hint)
                keep_resolve = navk["status"]
                cuk = _skill_row(rt, rec, "change_unit_analysis",
                                 {"terms": keep_terms, "task_id": task.id})
                rows.append(cuk)
                keep_matches = (cuk["_result"].data.get("matches", [])
                                if cuk["_result"] else [])
            pm = cu["_result"]
            problem_matches = (pm.data.get("matches", [])
                               if pm else [])
            sr = _skill_row(rt, rec, "safe_rollback", {
                "problem_matches": problem_matches,
                "keep_matches": keep_matches,
                "affected_routes": [], "task_id": task.id})
            rows.append(sr)
            srres = sr["_result"]
            raw["targets"] = {
                "feature_id": nr.data.get("feature_id") if nr else None,
                "resolve_status": nav["status"],
                "keep_resolve_status": keep_resolve,
                "terms": terms_p,
                "rollback_files": list(srres.data.get("rollback_files", []))
                if srres else [],
                "keep_files": list(srres.data.get("keep_files", []))
                if srres else [],
                "policy_action": srres.data.get("policy_action")
                if srres else None,
                "commits": sorted({m.get("commit", "")
                                   for m in problem_matches
                                   if m.get("commit")}),
            }
            if task.task_type == "compound":
                # compound 追加验证 + 策略（含 history 块的 commit 集合）
                pm = rows[1]["_result"]
                fids = ([m["finding_id"] for m in pm.data.get("matches", [])]
                        if pm else [])
                ver = _skill_row(rt, rec, "evidence_verification",
                                 {"finding_ids": fids, "task_id": task.id})
                rows.append(ver)
                vr = ver["_result"]
                verdicts = vr.data.get("verdicts", []) if vr else []
                raw["targets"]["verified_fraction"] = round(
                    sum(1 for v in verdicts
                        if v.status.value == "SUPPORTED") / len(verdicts), 3) \
                    if verdicts else 0.0

        # ---- 汇总执行面 ----
        new_events = rec.events[n0:]
        raw["status"] = ("failed" if any(r["status"] == "failed" for r in rows)
                         else "success")
        raw["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        raw["skills"] = [{k: v for k, v in r.items() if k != "_result"}
                         for r in rows]
        raw["skill_statuses"] = {r["skill"]: r["status"] for r in rows}
        raw["evidence_count"] = sum(len(r["evidence_ids"]) for r in rows)
        raw["llm_calls"] = sum(r["llm_calls"] for r in rows)
        raw["tool_calls"] = sum(1 for e in new_events if e.layer == "tool")
        raw["policy_events"] = sum(1 for e in new_events
                                   if e.layer == "policy")
        raw["events"] = len(new_events)
        return raw


def _resolve_anchor(broker, anchor: str) -> str:
    """锚点归一化：限定名 → 图里真实存在的节点 id。

    任务作者写 `lib/utils.js::etag` 这类限定名；图节点带 sym: 前缀。
    归一化只做前缀补齐（不猜测模糊匹配），找不到就原样返回让
    impact_analysis 大声失败 —— 那是被测行为，不是 runner bug。
    """
    if not anchor or broker.node(anchor) is not None:
        return anchor
    for candidate in (f"sym:{anchor}", f"scope:{anchor}", f"file:{anchor}"):
        if broker.node(candidate) is not None:
            return candidate
    return anchor


def _files_of(broker, node_ids: list[str]) -> list[str]:
    """sym:/api: 节点 id → 相对文件路径（去重排序）。"""
    files = []
    for nid in node_ids or []:
        if nid.startswith("sym:"):
            qual = nid[4:]
            files.append(qual.rsplit("::", 1)[0])
        elif nid.startswith("api:"):
            node = broker.node(nid)
            if node is not None and node.props.get("file"):
                files.append(node.props["file"])
        elif nid.startswith("file:"):
            files.append(nid[5:])
    return sorted(set(files))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repos", default="chalk,zustand,express")
    ap.add_argument("--tasks", type=Path, default=TASKS_PATH)
    ap.add_argument("--out-dir", type=Path,
                    default=Path(__file__).resolve().parent / "results")
    ap.add_argument("--types", default="",
                    help="只跑指定 task_type（逗号分隔；默认全部）")
    args = ap.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    for r in repos:
        info = check_repo(r)
        if not (info["exists"] and info["has_git"]):
            print(f"SKIP {r}: not ready {info}")
    tasks = [t for t in load_tasks(args.tasks) if t.repo in repos]
    if args.types:
        keep = {t.strip() for t in args.types.split(",")}
        tasks = [t for t in tasks if t.task_type in keep]

    from experiments.real_repo.evaluator import score_task
    from experiments.real_repo.report import (summarize, write_jsonl,
                                              write_markdown)
    bench = RealRepoBenchmark()
    rows = []
    for t in tasks:
        try:
            raw = bench.run_task(t)
        except Exception as e:   # 单任务失败不拖垮整批
            raw = {"task_id": t.id, "repo": t.repo, "task_type": t.task_type,
                   "query": t.query, "gold_source": t.gold_source,
                   "status": "error", "error": f"{type(e).__name__}: {e}"[:300],
                   "skills": []}
        row = dict(raw)
        row["scores"] = score_task(t, raw)
        rows.append(row)
        print(f"{t.id:42s} {row['status']:8s} "
              f"{'scored' if row['scores'].get('scored') else t.gold_source}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(rows, args.out_dir / "results.jsonl")
    # 执行面跑过则一并合并进 results.md（与 execution_bench 对称）
    import json

    def _load(p):
        return [json.loads(l) for l in p.open() if l.strip()] \
            if p.exists() else []

    exec_rows = _load(args.out_dir / "exec_results.jsonl")
    oracle_rows = _load(args.out_dir / "exec_oracle_results.jsonl")
    write_markdown(summarize(rows), rows, args.out_dir / "results.md",
                   exec_rows=exec_rows or None,
                   oracle_rows=oracle_rows or None)
    print(f"\nwrote {args.out_dir}/results.jsonl and results.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
