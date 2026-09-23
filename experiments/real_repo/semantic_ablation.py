"""Semantic ResolveTarget 消融（Phase 12B）。

D0（纯确定性词面）vs D1（LLM 辅助挑选）的 resolve_target 对照，外加
raw 对照臂（12A 原状：只播 Next.js 形状种子）——先量化"词表本身为空"
这个结构性前提，再量 LLM 的增量：

  raw  图上只有 route/component/layout 播种（真实库上 FEATURE≈0，
       复现 12A 语义候选全灭的条件），确定性路径
  d0   + seed_public_symbols（源码目录符号 → Feature，确定性事实），
       仍确定性词面/别名匹配（llm 不注入）
  d1   同 d0 播种，SkillRuntime 注入 LLM —— LLM 只能挑已有 feature
       名（adapter 幻觉过滤 = 图验证），对比词面匹配的增量

指标：target_recall@1/@3（top-k 候选文件覆盖 gold 文件比例）、
feature_mapping_accuracy（top-1 feature 名命中 gold 符号）、
unresolved_rate（零候选）、vocab_coverage（gold 符号在词表中的比例，
解释 miss 来源）、llm_calls/prompt_tokens/completion_tokens/latency。

用法：
  python -m experiments.real_repo.semantic_ablation --arms raw,d0,d1
      [--repos chalk,zustand,express] [--out-dir results]
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

from experiments.real_repo.benchmark import _files_of  # noqa: E402
from experiments.real_repo.loader import check_repo, repo_path  # noqa: E402
from experiments.real_repo.schema import Task, load_tasks  # noqa: E402

TASKS_PATH = Path(__file__).resolve().parent / "tasks.jsonl"

# gold → 目标文件/符号 的统一抽取（按 task_type）
def _gold_targets(task: Task) -> tuple[list[str], list[str]]:
    """返回 (gold 目标文件, gold 目标符号)。

    locate: files+symbols；rollback/compound: 问题侧 rollback_files
    （compound 另有 files）；impact: callers 文件 + anchor 符号；
    history/pending: 无目标面，不参与评分。
    """
    g = task.gold or {}
    files: list[str] = []
    syms: list[str] = []
    if task.task_type == "locate":
        files = list(g.get("files", []))
        syms = list(g.get("symbols", []))
    elif task.task_type in ("rollback", "compound"):
        files = list(g.get("rollback_files", [])) or list(g.get("files", []))
    elif task.task_type == "impact":
        files = list(g.get("callers", []))
        if task.anchor and "::" in task.anchor:
            syms = [task.anchor.rsplit("::", 1)[1]]
    return files, syms


class SemanticAblation:
    def __init__(self, llm=None):
        self.llm = llm
        self._brokers: dict[str, object] = {}

    def _broker(self, repo: str):
        """每 repo 一个 broker（只建 code+semantic 层，不建变更层 ——
        resolve_target 不消费 ChangeUnit，省数分钟构建时间）。"""
        if repo not in self._brokers:
            from src.execution import ExecutionRecorder
            from src.semgraph.context_broker import ContextBroker
            self._brokers[repo] = ContextBroker(
                repo_path(repo), ExecutionRecorder())
        return self._brokers[repo]

    # ------------------------------------------------------------ 单臂运行
    def run_arm(self, repo: str, tasks: list[Task], arm: str) -> list[dict]:
        """一个 repo × 一个臂 → 每任务一行。

        臂间共享 broker：raw 必须先跑（未播种态），d0/d1 在
        seed_public_symbols() 之后跑 —— 顺序由 run() 保证。
        """
        from src.skills import SkillRuntime

        broker = self._broker(repo)
        rec = broker.rec
        rt = SkillRuntime(broker, llm=self.llm if arm == "d1" else None)
        rows: list[dict] = []
        for task in tasks:
            n0 = len(rec.events)
            u0 = dict(self.llm.usage) if (arm == "d1" and self.llm) else None
            t0 = time.perf_counter()
            result = rt.run("resolve_target",
                            {"query": task.query, "task_id": task.id})
            wall = round((time.perf_counter() - t0) * 1000, 1)
            spans = [e for e in rec.events[n0:]
                     if e.layer == "skill" and e.actor == "resolve_target"]
            data = result.data if result else {}
            cands = list(data.get("candidate_details", []))
            cand_files = sorted({f for c in cands
                                 for f in _files_of(broker,
                                                    c.related_symbols)})
            # 零候选退路：feature_id 是确定性退路的节点 id
            fallback_id = data.get("feature_id") if not cands else None
            if fallback_id:
                cand_files = _files_of(broker, [fallback_id])
            row = {
                "task_id": task.id, "repo": repo, "arm": arm,
                "task_type": task.task_type, "query": task.query,
                "status": result.status if result else "error",
                "candidates": len(cands),
                "top": ([{"name": cands[0].name,
                          "method": cands[0].mapping_method,
                          "score": cands[0].score}] if cands else []),
                # 按候选顺序的文件列表（打分用，下划线键不落盘）
                "_cand_files": [_files_of(broker, c.related_symbols)
                                for c in cands],
                "candidate_files": cand_files,
                "fallback_id": fallback_id,
                "latency_ms": spans[-1].duration_ms if spans else wall,
                "llm_calls": sum(1 for e in rec.events[n0:]
                                 if e.layer == "llm"),
            }
            if u0 is not None and self.llm is not None:
                u1 = self.llm.usage
                row["prompt_tokens"] = u1["prompt_tokens"] - u0["prompt_tokens"]
                row["completion_tokens"] = (u1["completion_tokens"]
                                            - u0["completion_tokens"])
            rows.append(row)
        return rows

    # ------------------------------------------------------------ 全量
    def run(self, tasks: list[Task], repos: list[str],
            arms: list[str]) -> tuple[list[dict], dict]:
        all_rows: list[dict] = []
        seed_counts: dict[str, int] = {}
        for repo in repos:
            tasks_r = [t for t in tasks if t.repo == repo]
            if not tasks_r:
                continue
            # raw 先跑（未播种态），之后播种一次，d0/d1 复用同一张图
            if "raw" in arms:
                all_rows += self.run_arm(repo, tasks_r, "raw")
            if any(a in arms for a in ("d0", "d1")):
                seed_counts[repo] = self._broker(repo).seed_public_symbols()
                if "d0" in arms:
                    all_rows += self.run_arm(repo, tasks_r, "d0")
                if "d1" in arms:
                    all_rows += self.run_arm(repo, tasks_r, "d1")
        return all_rows, seed_counts


# ---------------------------------------------------------------- 评分
def score_rows(rows: list[dict], tasks: list[Task],
               seed_counts: dict) -> tuple[list[dict], dict]:
    """对每行算 target_recall@1/@3、mapping accuracy、unresolved。"""
    by_id = {t.id: t for t in tasks}
    scored_rows: list[dict] = []
    for r in rows:
        task = by_id[r["task_id"]]
        gold_files, gold_syms = _gold_targets(task)
        row = dict(r)
        row["gold_files"] = gold_files
        row["gold_symbols"] = gold_syms
        row["scored"] = bool(gold_files)
        if gold_files:
            topk = row.get("_cand_files", [])
            row["target_recall_at_1"] = _files_recall(
                [f for fs in topk[:1] for f in fs], gold_files)
            row["target_recall_at_3"] = _files_recall(
                [f for fs in topk[:3] for f in fs], gold_files)
        if gold_syms:
            names = [c["name"].lower() for c in row.get("top", [])]
            want = {s.rsplit(".", 1)[-1].lower() for s in gold_syms}
            row["feature_mapping_accuracy"] = \
                1.0 if any(n in want for n in names) else 0.0
        scored_rows.append(row)
    # 汇总
    summary = {}
    for arm in sorted({r["arm"] for r in scored_rows}):
        rs = [r for r in scored_rows if r["arm"] == arm]
        sc = [r for r in rs if r.get("scored")]
        n = len(sc) or 1
        with_sym = [r for r in sc if r.get("gold_symbols")]
        summary[arm] = {
            "tasks": len(rs), "scored": len(sc),
            "unresolved_rate": round(
                sum(1 for r in rs if r["candidates"] == 0) / len(rs), 3),
            "target_recall_at_1": round(
                sum(r.get("target_recall_at_1", 0.0) for r in sc) / n, 3),
            "target_recall_at_3": round(
                sum(r.get("target_recall_at_3", 0.0) for r in sc) / n, 3),
            "feature_mapping_accuracy": round(
                sum(r.get("feature_mapping_accuracy", 0.0) for r in with_sym)
                / max(1, len(with_sym)), 3),
            "avg_latency_ms": round(
                sum(r["latency_ms"] for r in rs) / len(rs), 1),
            "llm_calls": sum(r.get("llm_calls", 0) for r in rs),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rs),
            "completion_tokens": sum(r.get("completion_tokens", 0)
                                     for r in rs),
        }
    return scored_rows, {"arms": summary, "seed_counts": seed_counts}


def _files_recall(files: list[str], gold: list[str]) -> float:
    if not gold:
        return 0.0
    hit = set(files) & set(gold)
    return round(len(hit) / len(gold), 3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repos", default="chalk,zustand,express")
    ap.add_argument("--tasks", type=Path, default=TASKS_PATH)
    ap.add_argument("--arms", default="raw,d0,d1",
                    help="逗号分隔：raw,d0,d1")
    ap.add_argument("--out-dir", type=Path,
                    default=Path(__file__).resolve().parent / "results")
    ap.add_argument("--llm-base-url", default=None)
    ap.add_argument("--llm-api-key", default=None)
    ap.add_argument("--llm-model", default=None)
    args = ap.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for r in repos:
        info = check_repo(r)
        if not (info["exists"] and info["has_git"]):
            print(f"SKIP {r}: not ready {info}")
    tasks = [t for t in load_tasks(args.tasks) if t.repo in repos]

    llm = None
    if "d1" in arms:
        import os
        from src.config import LLMConfig
        from src.llm.client import LLMClient
        cfg = LLMConfig(
            base_url=args.llm_base_url or os.environ.get("LLM_BASE_URL"),
            api_key=args.llm_api_key or os.environ.get("LLM_API_KEY"),
            model=args.llm_model or os.environ.get("LLM_MODEL",
                                                   "gpt-4o-mini"))
        if not cfg.available:
            print("REFUSED: d1 臂需要 LLM（--llm-base-url/--llm-api-key/"
                  "--llm-model 或 LLM_* 环境变量）")
            return 2
        llm = LLMClient(cfg)
        print(f"llm: {cfg.base_url} model={cfg.model} "
              f"available={llm.available}")

    bench = SemanticAblation(llm=llm)
    rows, seed_counts = bench.run(tasks, repos, arms)

    scored, summary = score_rows(rows, tasks, seed_counts)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_jsonl = args.out_dir / "semantic_ablation.jsonl"
    with out_jsonl.open("w") as f:
        for r in scored:
            clean = {k: v for k, v in r.items()
                     if not k.startswith("_") and k != "candidate_details"}
            f.write(json.dumps(clean, ensure_ascii=False) + "\n")
    _write_md(scored, summary, args.out_dir / "semantic_ablation.md")
    print(f"wrote {out_jsonl} and semantic_ablation.md")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _write_md(rows: list[dict], summary: dict, path: Path) -> None:
    lines = ["# Semantic ResolveTarget Ablation (Phase 12B)", ""]
    lines.append(f"tasks: {len(rows)} rows | arms: "
                 f"{', '.join(sorted(summary.get('arms', {})))} | "
                 f"seed_counts: {summary.get('seed_counts', {})}")
    lines.append("")
    lines.append("| arm | tasks | scored | unresolved | recall@1 | recall@3 "
                 "| mapping_acc | avg_ms | llm_calls | prompt_tok | compl_tok |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for arm, s in summary.get("arms", {}).items():
        lines.append(
            f"| {arm} | {s['tasks']} | {s['scored']} "
            f"| {s['unresolved_rate']} | {s['target_recall_at_1']} "
            f"| {s['target_recall_at_3']} | {s['feature_mapping_accuracy']} "
            f"| {s['avg_latency_ms']} | {s['llm_calls']} "
            f"| {s['prompt_tokens']} | {s['completion_tokens']} |")
    lines.append("")
    lines.append("## Per-task (scored arms 并排)")
    lines.append("")
    lines.append("| task | repo | type | gold_files | "
                 + " | ".join(f"{a} top1" for a in sorted(
                     summary.get("arms", {}))) + " |")
    lines.append("|---|---|---|---|" + "---|" * len(summary.get("arms", {})))
    by_task: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], {})[r["arm"]] = r
    for tid in sorted(by_task):
        arms_r = by_task[tid]
        first = next(iter(arms_r.values()))
        cells = []
        for arm in sorted(summary.get("arms", {})):
            r = arms_r.get(arm)
            if r is None:
                cells.append("-")
            elif not r.get("candidates"):
                cells.append(f"*unresolved* (r@1={r.get('target_recall_at_1','')})")
            else:
                top = r["top"][0] if r.get("top") else {}
                cells.append(f"{top.get('name','?')} "
                             f"[{top.get('method','')}] "
                             f"r@1={r.get('target_recall_at_1','')}")
        lines.append(f"| {tid} | {first['repo']} | {first['task_type']} "
                     f"| {','.join(first.get('gold_files', [])) or '—'} "
                     f"| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("> 指标：target_recall@k = top-k 候选文件覆盖 gold 文件"
                 "比例；feature_mapping_accuracy = top-1 feature 名命中"
                 "gold 符号（有符号 gold 的任务才计入）；unresolved = "
                 "零候选走确定性退路。raw 臂 = 12A 原状（只播 Next.js "
                 "形状种子）。")
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
