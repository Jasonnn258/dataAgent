"""ChangeUnit 语义标签消融（Phase 12C）。

C0（现有确定性 characterize：domain 目录触发词打标）vs
C1（+ChangeUnitLabelSkill：LLM 批量标 label/intent/candidate_features，
严格 JSON + 白名单 + 图验证，输出只作 candidate evidence，绝不写回
单元的确定性标签）。三个度量面：

1. 标签质量（gold_intents.jsonl 人工标注 47 单元）：
   change_label_accuracy / change_intent_accuracy（C0 无 intent 输出，
   如实记 n/a）/ feature_mapping_accuracy（candidate_features 命中
   单元自身符号 ∩ 12B feature 词表）
2. 回退计划质量：C1 在 harness 层用 graph-verified feature 重打分
   （+2.0 ≈ W_SYMBOL；只加不减，保守），对比 rollback
   precision/recall/preservation（evaluator.score_task 同 12A）
3. LLM 成本：calls / tokens / latency

用法：
  python -m experiments.real_repo.label_ablation [--arms c0,c1]
      [--llm-base-url http://127.0.0.1:8000/v1 --llm-model ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from experiments.real_repo.benchmark import (_skill_row, _terms_from_query,  # noqa: E402
                                             _usable_terms)
from experiments.real_repo.loader import BrokerPool  # noqa: E402
from experiments.real_repo.schema import Task, load_tasks  # noqa: E402

TASKS_PATH = Path(__file__).resolve().parent / "tasks.jsonl"
GOLD_PATH = Path(__file__).resolve().parent / "gold_intents.jsonl"

# C1 重打分：graph-verified feature 与 resolve 目标重叠的加分（≈W_SYMBOL）
C1_FEATURE_BONUS = 2.0
# 每任务送标的 problem 匹配上限（12C：LLM 调用有界）
LABEL_TOP_K = 10


def load_gold(path: Path = GOLD_PATH) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["unit_id"]] = d
    return out


class LabelAblation:
    def __init__(self, pool: BrokerPool, llm=None):
        self.pool = pool
        self.llm = llm

    # ------------------------------------------------------------ 回退链
    def _rollback_chain(self, entry: dict, task: Task, arm: str) -> dict:
        """resolve → cu 匹配 →（c1/c1r: 标签重打分）→ safe_rollback。

        与 12A benchmark 的 rollback 分支同构（含 resolve 失败降级）。
        - c1：resolve 也带 LLM（12B d1 路径）—— 与 c0 的差是复合效应
        - c1r：resolve 保持确定性，只有标签重打分 —— 隔离标签净效应
        """
        from src.skills import SkillRuntime

        broker, rec = entry["broker"], entry["broker"].rec
        rt = SkillRuntime(broker, llm=self.llm if arm == "c1" else None)
        # c1r 的标签调用单独走带 LLM 的 runtime（resolve 侧不掺 LLM）
        rt_lab = (SkillRuntime(broker, llm=self.llm)
                  if arm == "c1r" and self.llm else rt)
        rows: list[dict] = []

        nav = _skill_row(rt, rec, "resolve_target",
                         {"query": task.query, "task_id": task.id})
        rows.append(nav)
        nr = nav["_result"]
        terms_p = _usable_terms(nr.data.get("terms") if nr else None,
                                task.query)

        cu = _skill_row(rt, rec, "change_unit_analysis",
                        {"terms": terms_p, "task_id": task.id})
        rows.append(cu)
        pm = cu["_result"]
        problem_matches = list(pm.data.get("matches", [])) if pm else []

        keep_matches: list[dict] = []
        if task.keep_hint:
            navk = _skill_row(rt, rec, "resolve_target",
                              {"query": task.keep_hint, "task_id": task.id})
            rows.append(navk)
            kr = navk["_result"]
            keep_terms = _usable_terms(kr.data.get("terms") if kr else None,
                                       task.keep_hint)
            cuk = _skill_row(rt, rec, "change_unit_analysis",
                             {"terms": keep_terms, "task_id": task.id})
            rows.append(cuk)
            keep_matches = list(cuk["_result"].data.get("matches", [])
                                if cuk["_result"] else [])

        label_rows: list[dict] = []
        if arm in ("c1", "c1r"):
            # 12C 增量：top-K 匹配单元送 LLM 标签，用图验证 feature 重打分
            top_ids = [m["unit_id"] for m in problem_matches[:LABEL_TOP_K]]
            n0 = len(rec.events)
            lab = _skill_row(rt_lab, rec, "change_unit_label",
                             {"unit_ids": top_ids, "hint_terms": terms_p,
                              "task_id": task.id})
            rows.append(lab)
            lr = lab["_result"]
            labels = list(lr.data.get("labels", [])) if lr else []
            # resolve top-3 feature 名（重叠判定的锚）
            cand = nr.data.get("candidate_details", []) if nr else []
            feat_names = {c.name for c in cand[:3]}
            by_unit = {l["unit_id"]: l for l in labels}
            for m in problem_matches:
                l = by_unit.get(m["unit_id"])
                if not l:
                    continue
                overlap = set(l["candidate_features"]) & feat_names
                m["score"] = m.get("score", 0.0) + (
                    C1_FEATURE_BONUS if overlap else 0.0)
                label_rows.append({"unit_id": m["unit_id"],
                                   "label": l["label"], "intent": l["intent"],
                                   "features": l["candidate_features"],
                                   "feature_overlap": bool(overlap)})
            problem_matches.sort(key=lambda m: -m.get("score", 0.0))

        sr = _skill_row(rt, rec, "safe_rollback", {
            "problem_matches": problem_matches,
            "keep_matches": keep_matches,
            "affected_routes": [], "task_id": task.id})
        rows.append(sr)
        srres = sr["_result"]
        raw = {
            "task_id": task.id, "repo": task.repo, "arm": arm,
            "task_type": task.task_type, "query": task.query,
            "gold_source": task.gold_source,
            "status": ("failed" if any(r["status"] == "failed" for r in rows)
                       else "success"),
            "skills": [{k: v for k, v in r.items() if k != "_result"}
                       for r in rows],
            "skill_statuses": {r["skill"]: r["status"] for r in rows},
            "llm_calls": sum(r["llm_calls"] for r in rows),
            "latency_ms": sum(r["latency_ms"] for r in rows),
            "labels": label_rows,
            "targets": {
                "terms": terms_p,
                "commits": sorted({m.get("commit", "")
                                   for m in problem_matches if m.get("commit")}),
                "rollback_files": list(srres.data.get("rollback_files", []))
                if srres else [],
                "keep_files": list(srres.data.get("keep_files", []))
                if srres else [],
                "policy_action": srres.data.get("policy_action")
                if srres else None,
            },
        }
        return raw

    # ------------------------------------------------------------ gold 单元标定
    def label_gold_units(self, entry: dict, gold_units: list[dict],
                         repo: str) -> dict:
        """对 gold commit 的全部单元跑 C0 标签统计 + C1 LLM 批量标签。

        返回逐单元对照（c0_label 来自图 props —— 确定性事实原样读出）。
        """
        from src.skills import SkillRuntime

        broker = entry["broker"]
        rec = broker.rec
        rt = SkillRuntime(broker, llm=self.llm)
        # 特征词表（12B 播种后的图内 feature 名）
        from src.semgraph.schema_v2 import NodeType
        vocab = {f.props.get("name", "") for f in
                 broker.graph.nodes_of_type(NodeType.FEATURE)
                 if f.props.get("name")}

        rows: list[dict] = []
        # C1 批量：每批 ≤12（prompt 有界）
        todo = [g for g in gold_units]
        llm_rows: dict[str, dict] = {}
        u0 = dict(self.llm.usage) if self.llm else None
        t0 = time.perf_counter()
        for i in range(0, len(todo), 12):
            batch = todo[i:i + 12]
            # unit id 双形态：gold 文件记的是 props 形态（无前缀），skill
            # 侧有容错，这里同样显式补 cu: 前缀走图节点 id 主路径
            lab = _skill_row(rt, rec, "change_unit_label", {
                "unit_ids": [g["unit_id"] if g["unit_id"].startswith("cu:")
                             else f"cu:{g['unit_id']}" for g in batch],
                "hint_terms": [], "task_id": "gold"})
            lr = lab["_result"]
            for l in (lr.data.get("labels", []) if lr else []):
                # 归一到无前缀形态与 gold 对齐
                llm_rows[l["unit_id"].removeprefix("cu:")] = l
        wall = round((time.perf_counter() - t0) * 1000, 1)
        usage_delta = {}
        batches = (len(todo) + 11) // 12
        if self.llm and u0 is not None:
            u1 = self.llm.usage
            usage_delta = {"prompt_tokens": u1["prompt_tokens"] - u0["prompt_tokens"],
                           "completion_tokens": u1["completion_tokens"] - u0["completion_tokens"],
                           "calls": batches}

        for g in gold_units:
            node = broker.node(f"cu:{g['unit_id']}")
            c0_label = node.props.get("semantic_label", "") if node else ""
            syms = {s.rsplit("::", 1)[-1] for s in
                    (node.props.get("symbols", []) if node else [])}
            gold_feats = syms & vocab
            l = llm_rows.get(g["unit_id"], {})
            rows.append({
                "unit_id": g["unit_id"], "repo": repo,
                "gold_label": g["gold_label"], "gold_intent": g["gold_intent"],
                "c0_label": c0_label,
                "c0_label_correct": c0_label == g["gold_label"],
                "c1_label": l.get("label", ""),
                "c1_label_correct": l.get("label") == g["gold_label"],
                "c1_intent": l.get("intent", ""),
                "c1_intent_correct": l.get("intent") == g["gold_intent"],
                "gold_features": sorted(gold_feats),
                "c1_features": l.get("candidate_features", []),
                "c1_feature_hit": bool(set(l.get("candidate_features", []))
                                       & gold_feats) if gold_feats else None,
            })
        return {"rows": rows, "llm_latency_ms": wall,
                "llm_calls": batches,
                "usage": usage_delta}


def _summarize_gold(gold_rows: list[dict]) -> dict:
    n = len(gold_rows) or 1
    with_feats = [r for r in gold_rows if r["c1_feature_hit"] is not None]
    return {
        "units": n,
        "c0_label_accuracy": round(
            sum(r["c0_label_correct"] for r in gold_rows) / n, 3),
        "c1_label_accuracy": round(
            sum(r["c1_label_correct"] for r in gold_rows) / n, 3),
        "c1_intent_accuracy": round(
            sum(r["c1_intent_correct"] for r in gold_rows) / n, 3),
        "c1_feature_mapping_accuracy": round(
            sum(1 for r in with_feats if r["c1_feature_hit"])
            / max(1, len(with_feats)), 3),
        "c1_feature_coverage": round(len(with_feats) / n, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repos", default="chalk,zustand,express")
    ap.add_argument("--tasks", type=Path, default=TASKS_PATH)
    ap.add_argument("--gold", type=Path, default=GOLD_PATH)
    ap.add_argument("--arms", default="c0,c1")
    ap.add_argument("--out-dir", type=Path,
                    default=Path(__file__).resolve().parent / "results")
    ap.add_argument("--llm-base-url", default=None)
    ap.add_argument("--llm-api-key", default=None)
    ap.add_argument("--llm-model", default=None)
    args = ap.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    tasks = [t for t in load_tasks(args.tasks)
             if t.repo in repos and t.task_type in ("rollback", "compound")
             and t.gold_source == "manual"]
    gold = load_gold(args.gold)

    llm = None
    if any(a.startswith("c1") for a in arms):
        import os
        from src.config import LLMConfig
        from src.llm.client import LLMClient
        cfg = LLMConfig(
            base_url=args.llm_base_url or os.environ.get("LLM_BASE_URL"),
            api_key=args.llm_api_key or os.environ.get("LLM_API_KEY"),
            model=args.llm_model or os.environ.get("LLM_MODEL",
                                                   "gpt-4o-mini"))
        if not cfg.available:
            print("REFUSED: c1 臂需要 LLM 配置")
            return 2
        llm = LLMClient(cfg)
        print(f"llm: {cfg.base_url} model={cfg.model}")

    pool = BrokerPool(commits_limit=900)
    bench = LabelAblation(pool, llm=llm)
    from experiments.real_repo.evaluator import score_task

    rollback_rows: list[dict] = []
    gold_rows: list[dict] = []
    for repo in repos:
        rtasks = [t for t in tasks if t.repo == repo]
        gold_units = [g for g in gold.values() if g["repo"] == repo]
        if not rtasks and not gold_units:
            continue
        entry = pool.get(repo)          # 变更层 900 commits（同 12A）
        entry["broker"].seed_public_symbols()   # 12B feature 词表
        for arm in arms:
            for t in rtasks:
                raw = bench._rollback_chain(entry, t, arm)
                raw["scores"] = score_task(t, raw)
                rollback_rows.append(raw)
                print(f"{t.id:40s} {arm}  {raw['status']:8s} "
                      f"r_prec={raw['scores'].get('rollback_precision')}")
        if gold_units and any(a.startswith("c1") for a in arms):
            got = bench.label_gold_units(entry, gold_units, repo)
            gold_rows += got["rows"]
            print(f"{repo}: gold units {len(got['rows'])} "
                  f"llm {got['llm_latency_ms']}ms {got['usage']}")

    # 汇总 + 落盘
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"gold": _summarize_gold(gold_rows) if gold_rows else None,
               "arms": {}}
    for arm in arms:
        rs = [r for r in rollback_rows if r["arm"] == arm]
        sc = [r for r in rs if r["scores"].get("scored")]
        def _avg(k):
            vs = [r["scores"][k] for r in sc if r["scores"].get(k) is not None]
            return round(sum(vs) / len(vs), 3) if vs else None
        summary["arms"][arm] = {
            "tasks": len(rs),
            "rollback_precision": _avg("rollback_precision"),
            "rollback_recall": _avg("rollback_recall"),
            "preservation_rate": _avg("preservation_rate"),
            "collateral_damage": _avg("collateral_damage"),
            "llm_calls": sum(r["llm_calls"] for r in rs),
            "avg_latency_ms": round(
                sum(r["latency_ms"] for r in rs) / max(1, len(rs)), 1),
        }
    out = {"summary": summary,
           "rollback": [{k: v for k, v in r.items() if k != "labels"}
                        for r in rollback_rows],
           "gold_units": gold_rows}
    (args.out_dir / "label_ablation.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, default=str)
                  for r in rollback_rows + gold_rows) + "\n")
    _write_md(summary, rollback_rows, gold_rows,
              args.out_dir / "label_ablation.md")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _write_md(summary: dict, rollback_rows: list[dict],
              gold_rows: list[dict], path: Path) -> None:
    lines = ["# ChangeUnit Label Ablation (Phase 12C)", ""]
    if summary.get("gold"):
        g = summary["gold"]
        lines += [
            "## 标签质量（人工 gold 47 单元）",
            "",
            "| units | c0_label_acc | c1_label_acc | c1_intent_acc "
            "| c1_feature_map | feature 覆盖 |",
            "|---|---|---|---|---|---|",
            f"| {g['units']} | {g['c0_label_accuracy']} "
            f"| {g['c1_label_accuracy']} | {g['c1_intent_accuracy']} "
            f"| {g['c1_feature_mapping_accuracy']} | {g['c1_feature_coverage']} |",
            "",
            "> C0 无 intent 输出（n/a）；c1_feature_map = candidate_features "
            "命中单元自身符号∩feature 词表（LLM 可见 summary 里的符号 —— "
            "这是管线 floor-check，不是难例 NLU 测试）", ""]
    lines += ["## 回退计划质量（C1 = 图验证 feature 重打分 +2.0，只加不减）", ""]
    lines.append("| arm | tasks | rollback_prec | rollback_rec "
                 "| preservation | collateral | llm_calls | avg_ms |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for arm, s in summary["arms"].items():
        lines.append(f"| {arm} | {s['tasks']} | {s['rollback_precision']} "
                     f"| {s['rollback_recall']} | {s['preservation_rate']} "
                     f"| {s['collateral_damage']} | {s['llm_calls']} "
                     f"| {s['avg_latency_ms']} |")
    lines += ["", "## 逐任务", "",
              "| task | arm | status | rollback_files 数 | scores |",
              "|---|---|---|---|---|"]
    for r in rollback_rows:
        lines.append(
            f"| {r['task_id']} | {r['arm']} | {r['status']} "
            f"| {len(r['targets']['rollback_files'])} "
            f"| prec={r['scores'].get('rollback_precision')} "
            f"rec={r['scores'].get('rollback_recall')} "
              f"pres={r['scores'].get('preservation_rate')} |")
    if gold_rows:
        lines += ["", "## gold 单元逐条（C0 vs C1 标签）", "",
                  "| unit | gold | c0 | c1 label | c1 intent | feat hit |",
                  "|---|---|---|---|---|---|"]
        for r in gold_rows:
            lines.append(
                f"| {r['unit_id']} | {r['gold_label']}/{r['gold_intent']} "
                f"| {r['c0_label']} | {r['c1_label']} "
                f"| {r['c1_intent']} | {r['c1_feature_hit']} |")
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
