"""MaintenanceExecutorAgent（Phase 13I）：沙箱执行链的唯一驾驶员。

前五个 agent（导航/情报/波及/计划/校验）产出的是*计划*；这个 agent 把
已经过仲裁的 RollbackPlan 在沙箱里走到 VERIFIED：

    build_execution_plan     计划 → 可执行合同（expected/forbidden）
    policy_check(pre)        计划危险面裁决 —— BLOCK 就地停车，不建沙箱
    prepare_execution        源仓库快照核对 → 沙箱 worktree
    build_rollback_patch     确定性反向 patch + 沙箱预检
    apply_patch              贴进沙箱 + 计划外修改检查
    validate_execution       沙箱里跑验证命令（argv 白名单）
    verify_execution         五面终审
    policy_check(post)       沙箱事实裁决 —— BLOCK 永不 promote

它自己没有执行原语：不碰 git、不写文件、不 subprocess —— 一切写动作
经 skill → broker → service → 物理工具，且全部落在沙箱 worktree。硬边界
由架构测试守住（agent 禁 import service / subprocess）。

停车即停车：任何一步失败/被 BLOCK，链就地终止并如实上报终态，不重试、
不修计划、不降级。执行成功也不 promote —— promote 是 13J 的独立技能，
默认关闭，需要人类显式批准。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.maintenance.models import ExecutionPlan
from src.skills.runtime import SkillRuntime
from src.skills.spec import SKILL_SUCCESS


@dataclass
class ExecutionOutcome:
    """一次沙箱执行链的结构化结局（给人看的，不是给机器吃的）。"""
    task_id: str = ""
    execution_id: str = ""
    status: str = ""                    # 尝试终态（ExecutionStatus）
    stopped_at: str = ""                # 停在哪一步（成功 = "post_gate"）
    pre_gate: dict = field(default_factory=dict)    # {action, rule, detail}
    post_gate: dict = field(default_factory=dict)
    verification: str = ""              # VERIFIED / PARTIAL / FAILED
    notes: list[str] = field(default_factory=list)
    attempt: object | None = None       # ExecutionAttempt（审计对象）
    trace_id: str = ""                  # 全链执行树的根（13L）

    @property
    def ok(self) -> bool:
        """链走完且两道门都没 BLOCK（可能仍需 HUMAN_REVIEW）。"""
        return (self.status == "VERIFIED"
                and self.pre_gate.get("action") != "BLOCK"
                and self.post_gate.get("action") != "BLOCK")

    def dump(self) -> str:
        pre = self.pre_gate.get("action", "-")
        post = self.post_gate.get("action", "-")
        rule = self.post_gate.get("rule") or self.pre_gate.get("rule") or ""
        lines = [
            f"execution {self.execution_id or '-'} task {self.task_id}",
            f"status: {self.status or '-'} (stopped at {self.stopped_at})",
            f"gates: pre={pre} post={post}" + (f" rule={rule}" if rule else ""),
            f"verification: {self.verification or '-'}",
        ]
        for n in self.notes:
            lines.append(f"note: {n}")
        lines.append("NOTE: promote is off by default — VERIFIED is not "
                     "an approval to touch the real workspace")
        return "\n".join(lines)


class MaintenanceExecutorAgent:
    ROLE = "MaintenanceExecutor"
    # 只读内省 + 双门裁决；写动作全部经 skill（SKILLS 清单）
    READS = ["get_execution", "executions", "node",
             "pre_execution_gate", "post_execution_gate"]
    SKILLS = ["build_execution_plan", "prepare_execution",
              "build_rollback_patch", "apply_patch", "validate_execution",
              "verify_execution", "policy_check"]

    def __init__(self, broker):
        self.broker = broker
        self._runtime = SkillRuntime(broker)

    # ------------------------------------------------------------ 主链
    def execute(self, rollback_plan, task_id: str = "",
                validation_commands: list[list[str]] | None = None,
                affected_routes: list[str] | None = None,
                scope=None) -> ExecutionOutcome:
        """把仲裁后的 RollbackPlan 在沙箱里执行到 VERIFIED（或停车）。

        整条链包在一个 agent span 里（13L）：skill/policy/tool 事件全部
        挂到这棵执行树上，attempt.trace_id 记根节点 id，run 目录落
        trace.jsonl —— 谁、何时、以何身份、调了什么，整链可重建。
        """
        task_id = task_id or "task"
        span = getattr(self.broker.rec, "span", None)
        if span is None:      # 纯 ToolRecorder：照常执行，只是没树
            self.broker.rec.tool(f"agent:{self.ROLE}:execute")
            return self._run_chain(rollback_plan, task_id,
                                   validation_commands, affected_routes,
                                   scope)
        with span("agent", self.ROLE, "execute",
                  task_id=task_id) as ev:
            out = self._run_chain(rollback_plan, task_id,
                                  validation_commands, affected_routes,
                                  scope)
            out.trace_id = ev.trace_id
            ev.meta["execution_id"] = out.execution_id
            ev.meta["stopped_at"] = out.stopped_at
        # span 收口后绑定树根并落 trace.jsonl（此刻全链事件完整且根
        # 事件的耗时/状态已定稿；只在首次绑定时写，重放不覆盖）
        if out.attempt is not None and out.attempt.trace_id == "":
            self.broker.bind_trace(out.attempt.execution_id, ev.trace_id)
        return out

    def _run_chain(self, rollback_plan, task_id, validation_commands,
                   affected_routes, scope) -> ExecutionOutcome:
        self.broker.rec.tool(f"agent:{self.ROLE}:execute")
        out = ExecutionOutcome(task_id=task_id)
        gates_ctx = {"rollback_symbols": [], "keep_symbols": [],
                     "task_id": task_id}   # legacy 必填（契约），gate 模式忽略

        # 1. 计划合同（纯翻译，无新决策）。也接受已构建的 ExecutionPlan
        #    直传 —— 重放场景（CLI plan 模式的产物稍后执行）：快照还是
        #    建计划那一刻的，stale 检测在 prepare 门口接住它。
        if isinstance(rollback_plan, ExecutionPlan):
            plan = rollback_plan
        else:
            r = self._runtime.run("build_execution_plan", {
                "rollback_plan": rollback_plan, "task_id": task_id,
                "validation_commands": list(validation_commands or []),
                "affected_routes": list(affected_routes or []),
                "actor": self.ROLE})
            if r.status != SKILL_SUCCESS:
                out.stopped_at = "build_execution_plan"
                out.notes.append(r.error or "plan build failed")
                return out
            plan = r.data["plan"]
            if scope:
                scope.produced(evidence=[r.data["evidence_id"]])

        # 2. 执行前门：BLOCK 就地停车 —— 沙箱也不给建（危险面计划
        #    没过门就动手，连沙箱里的浪费都不该发生）
        g = self._runtime.run("policy_check",
                              {**gates_ctx, "gate": "execution_pre",
                               "plan": plan})
        # policy_check 返回的 data 就是门摘要
        out.pre_gate = {"action": g.data.get("action", ""),
                        "rule": g.data.get("rule_name", ""),
                        "detail": g.data.get("detail", "")[:200]}
        if not g.ok:
            out.stopped_at = "pre_gate"
            out.notes.append(g.error or "pre gate not evaluated")
            return out
        if out.pre_gate["action"] == "BLOCK":
            out.stopped_at = "pre_gate"
            out.notes.append(
                f"pre gate BLOCK ({out.pre_gate['rule']}) — refusing to "
                f"prepare; the plan never enters a sandbox")
            return out

        # 3-7. 沙箱链：prepare → patch → apply → validate → verify
        chain: list[tuple[str, str]] = [
            ("prepare_execution", "prepare"),        # STALE_PLAN
            ("build_rollback_patch", "patch"),       # CONFLICT
            ("apply_patch", "apply"),                # CONFLICT / 计划外修改
            ("validate_execution", "validate"),      # TEST_FAILED
            ("verify_execution", "verify"),          # 五面终审
        ]
        attempt = None
        for skill_name, step in chain:
            ctx = {"plan": plan, "task_id": task_id, "actor": self.ROLE} \
                if attempt is None else {"execution": attempt}
            r = self._runtime.run(skill_name, ctx)
            attempt = (r.data.get("execution") or attempt)
            if attempt is not None:
                out.execution_id = attempt.execution_id
                out.status = attempt.status
                out.attempt = attempt
            if r.status != SKILL_SUCCESS:
                out.stopped_at = skill_name
                out.notes.append(r.error or f"{skill_name} failed")
                return out
            if step == "verify":
                out.verification = str(
                    (getattr(attempt, "verification_results", None) or [{}])[-1]
                    .get("overall", ""))

        # 8. 执行后门：沙箱事实裁决（BLOCK = 永不 promote，但事实已在案）
        g = self._runtime.run("policy_check",
                              {**gates_ctx, "gate": "execution_post",
                               "execution": attempt})
        out.post_gate = {"action": g.data.get("action", ""),
                         "rule": g.data.get("rule_name", ""),
                         "detail": g.data.get("detail", "")[:200]}
        if not g.ok:
            out.stopped_at = "post_gate"
            out.notes.append(g.error or "post gate not evaluated")
            return out
        out.stopped_at = "post_gate"
        if out.post_gate["action"] == "BLOCK":
            out.notes.append(
                f"post gate BLOCK ({out.post_gate['rule']}) — execution "
                f"verified but must never be promoted")
        elif out.post_gate["action"] == "HUMAN_REVIEW":
            out.notes.append("post gate HUMAN_REVIEW — awaiting explicit "
                             "human approval before any promote")
        return out

    # ------------------------------------------------------------ 内省
    def executions(self):
        """历史尝试（只读；给 CLI/报告用）。"""
        return self.broker.executions()
