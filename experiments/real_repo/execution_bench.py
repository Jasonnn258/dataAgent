"""Phase 13 执行环 benchmark（Phase 12A execution metrics）。

在真实 repo 的**本地克隆副本**上跑完整执行链（源 repo /workspace/yjx/
real_repos 保持只读——worktree add 会写 .git/worktrees，所以执行绝不
在原件上做）：

    副本上重建图 → 分析链（rollback/compound 分支）→
    MaintenanceExecutorAgent.execute() → 沙箱事实测量

沙箱事实（不是"计划说了什么"，是"沙箱里实际改了什么"）：
- exec_status / stopped_at / verification：链走到哪、停在哪
- exec_changed_files：worktree 相对 HEAD 的实际 diff 文件面（只读 git 查询）
- 双门裁决（pre/post gate 的 action）
- 验证命令：node -e 0（no-op，让 tests 面真实通过——真实 repo 的测试
  套件需要 npm install，不适合 benchmark 默认路径；有需要再挂）

用法：
  python -m experiments.real_repo.execution_bench --repos chalk,zustand,express
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from experiments.real_repo.benchmark import _usable_terms  # noqa: E402
from experiments.real_repo.loader import repo_path  # noqa: E402
from experiments.real_repo.schema import Task, load_tasks  # noqa: E402

# 副本根目录：持久卷上的临时区（TMPDIR 纪律），每次运行重新克隆
EXEC_ROOT = Path("/workspace/yjx/tmp/rr_exec")
TASKS_PATH = Path(__file__).resolve().parent / "tasks.jsonl"

# no-op 验证命令：白名单内的 node，让 13G 的 tests 面有真实 PASSED
NOOP_VALIDATE = [["node", "-e", "0"]]


class ExecutionBench:
    """每 repo 一个克隆副本 + 副本 broker（图建一次，任务复用）。"""

    def __init__(self, commits_limit: int = 900):
        self.commits_limit = commits_limit
        self._pool: dict[str, dict] = {}

    # ------------------------------------------------------------ 副本
    def _clone(self, name: str) -> Path:
        """本地克隆（hardlink 对象，秒级）。每次运行重新克隆 = 干净现场。"""
        dst = EXEC_ROOT / name
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", str(repo_path(name)),
                        str(dst)], check=True)
        return dst

    def _entry(self, name: str) -> dict:
        if name not in self._pool:
            from src.execution import ExecutionRecorder
            from src.semgraph.change_graph import build_change_graph
            from src.semgraph.context_broker import ContextBroker
            from src.skills import SkillRuntime

            t0 = time.perf_counter()
            copy = self._clone(name)
            broker = ContextBroker(copy, ExecutionRecorder())
            build_change_graph(broker, commits_limit=self.commits_limit)
            self._pool[name] = {
                "broker": broker,
                "runtime": SkillRuntime(broker),
                "copy": copy,
                "build_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        return self._pool[name]

    # ------------------------------------------------------------ 主链
    def run_task(self, task: Task, oracle: bool = False) -> dict:
        """oracle=True：跳过分析链，直接把 gold commits 的全部单元交给
        safe_rollback —— 把"分析层找得到吗"（12B/12C 的题）与"执行环
        执行得好吗"（Phase 13 的题）分开量。oracle 行带 mode 标记。
        """
        from src.agents.executor import MaintenanceExecutorAgent

        entry = self._entry(task.repo)
        broker, rt = entry["broker"], entry["runtime"]
        t0 = time.perf_counter()
        out: dict = {"repo_copy": str(entry["copy"]),
                     "build_ms": entry["build_ms"],
                     "mode": "oracle" if oracle else "analysis"}

        # ---- 副本上的分析链（与 benchmark 的 rollback 分支同一形态；
        # resolve 失败/退路垃圾词元 → 确定性词表，状态如实记录）----
        keep_matches: list[dict] = []
        if oracle:
            # gold commits 允许 8 位短 sha：先精确、再前缀兜底
            gold_commits = set(task.gold.get("commits", []) or [])
            matches = []
            for sha in sorted(gold_commits):
                cus = broker.find_change_units(commit=sha)
                if not cus:
                    cus = [cu for cu in broker.find_change_units()
                           if cu.props.get("commit", "").startswith(sha)]
                matches.extend({"unit_id": cu.id,
                                "commit": cu.props.get("commit", ""),
                                "score": 1.0} for cu in cus)
            if not matches:
                return {**out, "exec_status": "", "stopped_at": "oracle_units",
                        "error": f"no units found for gold commits "
                                 f"{sorted(gold_commits)}",
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
        else:
            nav = rt.run("resolve_target", {"query": task.query,
                                            "task_id": task.id})
            out["resolve_status"] = nav.status
            terms_p = _usable_terms(nav.data.get("terms") if nav.ok else None,
                                    task.query)
            cu = rt.run("change_unit_analysis",
                        {"terms": terms_p, "task_id": task.id})
            if not cu.ok:
                return {**out, "exec_status": "", "stopped_at": "change_unit_analysis",
                        "error": cu.error[:200],
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
            matches = cu.data.get("matches", [])
            if task.keep_hint:
                navk = rt.run("resolve_target", {"query": task.keep_hint,
                                                 "task_id": task.id})
                out["keep_resolve_status"] = navk.status
                terms_k = _usable_terms(navk.data.get("terms") if navk.ok else None,
                                        task.keep_hint)
                cuk = rt.run("change_unit_analysis",
                             {"terms": terms_k, "task_id": task.id})
                if cuk.ok:
                    keep_matches = cuk.data.get("matches", [])
        sr = rt.run("safe_rollback", {
            "problem_matches": matches,
            "keep_matches": keep_matches,
            "affected_routes": [], "task_id": task.id})
        if not sr.ok:
            return {**out, "exec_status": "", "stopped_at": "safe_rollback",
                    "error": sr.error[:200],
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

        # ---- 执行环（13I agent 驾驶；验证命令 = no-op node）----
        agent = MaintenanceExecutorAgent(broker)
        outcome = agent.execute(sr.data, task_id=task.id,
                                validation_commands=NOOP_VALIDATE,
                                affected_routes=[])
        attempt = outcome.attempt
        out.update({
            "execution_id": outcome.execution_id,
            "exec_status": outcome.status,
            "stopped_at": outcome.stopped_at,
            "verification": outcome.verification,
            "pre_gate": outcome.pre_gate.get("action", ""),
            "post_gate": outcome.post_gate.get("action", ""),
            "notes": list(outcome.notes)[:4],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        })

        # ---- 沙箱事实：worktree 相对 HEAD 的实际改动面（只读查询）----
        changed: list[str] = []
        if attempt is not None and attempt.workspace:
            proc = subprocess.run(
                ["git", "-C", attempt.workspace, "diff", "--name-only",
                 "HEAD"], capture_output=True, text=True)
            changed = [x for x in proc.stdout.splitlines() if x.strip()]
        out["exec_changed_files"] = sorted(changed)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repos", default="chalk,zustand,express")
    ap.add_argument("--oracle", action="store_true",
                    help="跳过分析链，gold commits 直取单元（执行环对照）")
    ap.add_argument("--tasks", type=Path, default=TASKS_PATH)
    ap.add_argument("--out", type=Path, default=None,
                    help="默认 results/exec_results.jsonl（oracle 模式为 "
                         "exec_oracle_results.jsonl）")
    args = ap.parse_args()
    if args.out is None:
        args.out = (Path(__file__).resolve().parent / "results" /
                    ("exec_oracle_results.jsonl" if args.oracle
                     else "exec_results.jsonl"))

    repos = {r.strip() for r in args.repos.split(",") if r.strip()}
    tasks = [t for t in load_tasks(args.tasks)
             if t.repo in repos and t.task_type in ("rollback", "compound")]
    if args.oracle:
        # oracle 只对带 gold commits 的任务有意义
        tasks = [t for t in tasks if t.gold.get("commits")]
    from experiments.real_repo.evaluator import score_execution
    bench = ExecutionBench()
    rows = []
    for t in tasks:
        try:
            raw = bench.run_task(t, oracle=args.oracle)
        except Exception as e:   # 单任务失败不拖垮整批
            raw = {"exec_status": "", "stopped_at": "harness",
                   "error": f"{type(e).__name__}: {e}"[:300]}
        row = {"task_id": t.id, "repo": t.repo, "task_type": t.task_type,
               "gold_source": t.gold_source, **raw}
        row["exec_scores"] = score_execution(t, raw)
        rows.append(row)
        print(f"{t.id:42s} {raw.get('exec_status') or '-':18s} "
              f"stopped={raw.get('stopped_at', '-')} "
              f"changed={len(raw.get('exec_changed_files', []))}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        import json
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    print(f"\nwrote {args.out}")

    # 分析面跑过则合并重写 results.md（执行面追加在报告尾部；两种
    # 模式的执行结果都在：analysis 主链 + oracle 对照各一段）
    analysis = args.out.parent / "results.jsonl"
    if analysis.exists():
        import json as _json
        from experiments.real_repo.report import (summarize, summarize_exec,
                                                  write_markdown)

        def _load(p: Path) -> list[dict]:
            return [_json.loads(l) for l in p.open() if l.strip()] \
                if p.exists() else []

        arows = _load(analysis)
        exec_rows = _load(args.out.parent / "exec_results.jsonl")
        if not args.oracle and not exec_rows:
            exec_rows = rows          # 首跑分析模式还没落盘
        oracle_rows = _load(args.out.parent / "exec_oracle_results.jsonl")
        write_markdown(summarize(arows), arows,
                       args.out.parent / "results.md",
                       exec_rows=exec_rows,
                       exec_summary=summarize_exec(exec_rows),
                       oracle_rows=oracle_rows,
                       oracle_summary=summarize_exec(oracle_rows)
                       if oracle_rows else None)
        print(f"merged exec section into {args.out.parent / 'results.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
