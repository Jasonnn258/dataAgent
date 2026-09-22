"""Agent CLI 入口（Phase 13K）。

    python -m src.agent_main --repo <path> --query "登录改坏了…" \
                       [--keep "保留系统标题"] \
                       [--execute plan|sandbox|workspace] \
                       [--validate "python -m pytest -q"]… \
                       [--approver 名字] [--i-approve]

三种模式，危险度递增，默认最安全：

- plan（默认）：Orchestrator 分析链，产出回退/保留计划 + 证据核验。
  纯只读 —— 和验收演示一样，nothing was executed。
- sandbox：在 MaintenanceExecutorAgent 驾驶下把计划在沙箱 worktree 里
  执行到 VERIFIED（验证命令跑在沙箱里），两道策略门全记录。真实仓库
  零改动；拿到的是 execution_id 和一份可审计的运行目录。
- workspace：**会改真实仓库**。前置三把钥匙一把不少：
  1. execution_policy.promote_enabled=true（配置默认 false）；
  2. 沙箱链先走到 VERIFIED 且 post gate 不 BLOCK；
  3. 命令行显式 --i-approve（explicit_approval）。
  落地后不 commit 不 push，reverse.patch 留档，撤销 = git apply -R。

实验框架 CLI（python -m src.main / experiments/*）保持原样不动。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.errors import DataAgentError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.agent_main",
        description="dataAgent agent CLI — plan / sandbox / (workspace) "
                    "with policy gates",
    )
    p.add_argument("--repo", required=True,
                   help="目标仓库路径（一切分析的根）")
    p.add_argument("--query", required=True,
                   help="问题侧自然语言描述（要回退什么）")
    p.add_argument("--keep", default="",
                   help="保留侧提示（同 commit 里哪些改动要留下）")
    p.add_argument("--execute", choices=("plan", "sandbox", "workspace"),
                   default="plan",
                   help="plan=只出计划（默认）；sandbox=沙箱执行到 "
                        "VERIFIED；workspace=晋升进真实仓库（默认被策略"
                        "关闭，且必须 --i-approve）")
    p.add_argument("--validate", action="append", default=None,
                   metavar="CMD",
                   help="沙箱验证命令（可重复；空格分隔，内部转 argv）。"
                        "不给则用仓库 .dataagent/validation_commands.json")
    p.add_argument("--approver", default="cli-user",
                   help="审批人名字（落进 human decision 的审计档）")
    p.add_argument("--i-approve", action="store_true",
                   help="workspace 模式的显式批准开关 —— 不给就不晋升")
    return p


def _analysis(broker, query: str, keep_hint: str):
    """Orchestrator 分析链（导航→情报→波及→计划→核验）。"""
    from src.agents.orchestrator import Orchestrator
    return Orchestrator(broker).run(query, keep_hint=keep_hint)


def _sandbox_execute(broker, report, validation_commands):
    """把分析出的计划交给执行 agent 跑沙箱链。"""
    from src.agents.executor import MaintenanceExecutorAgent
    agent = MaintenanceExecutorAgent(broker)
    payload = {
        "rollback_units": report.plan.rollback_units,
        "keep_units": report.plan.keep_units,
        "policy_action": (report.plan.policy_result.action.value
                          if report.plan.policy_result else ""),
    }
    routes = list(report.plan.affected_routes or [])
    return agent.execute(payload, task_id=report.task_id,
                         validation_commands=validation_commands,
                         affected_routes=routes)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"ERROR: no such repo directory: {repo}", file=sys.stderr)
        return 2
    validation_commands = ([c.split() for c in args.validate if c.strip()]
                           if args.validate else None)
    try:
        from src.schema import ToolRecorder
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker

        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        report = _analysis(broker, args.query, args.keep)
        print(report.dump())
        if args.execute == "plan":
            return 0

        outcome = _sandbox_execute(broker, report, validation_commands)
        print("\n" + "=" * 60)
        print(outcome.dump())
        if args.execute == "sandbox":
            return 0 if outcome.ok else 1

        # ---- workspace 模式：审批 + 晋升（三把钥匙在此收齐）----
        print("\n" + "=" * 60)
        if not args.i_approve:
            print("REFUSED: --execute workspace requires --i-approve "
                  "(explicit human approval); nothing was promoted")
            return 1
        if not outcome.ok:
            print("REFUSED: sandbox chain did not reach VERIFIED clean — "
                  "nothing to promote")
            return 1
        broker.approve_promotion(outcome.execution_id,
                                 approved_by=args.approver)
        attempt = broker.promote_execution(
            outcome.execution_id, explicit_approval=True,
            actor=f"cli:{args.approver}")
        print(f"promoted: {attempt.execution_id} — changes are "
              f"UNCOMMITTED in {repo}")
        print(f"reverse patch: {attempt.reverse_patch_path} "
              f"(undo with: git apply -R <it>)")
        print("commit/push 是人的动作 —— 本工具不会替你做")
        return 0
    except DataAgentError as e:
        print(f"ERROR [{type(e).__name__}]: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
