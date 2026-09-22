"""PolicyCheckSkill —— 结构化规则门（Phase 11A）。

逻辑自 RollbackPlanner.plan 的 gate 段抽出：组装 gate 上下文、跑完整
规则集、把结果记成可审计 decision。gate 只裁决，不执行；服从 BLOCK
是调用方（Agent）的契约。decision 层未激活时显式 UNGATED。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_PARTIAL, SKILL_SUCCESS, SkillResult,
                             SkillSpec)


class PolicyCheckSkill(BaseSkill):
    spec = SkillSpec(
        name="policy_check",
        description=("run the versioned policy gate over a structured "
                     "context, or the execution pre/post gates (13H)"),
        required_inputs=["rollback_symbols", "keep_symbols"],
        produced_outputs=["action", "detail", "rule_name", "policy"],
        allowed_capabilities=["policy.gate", "evidence.query"],
        evidence_requirements="上下文字实来自图/evidence 注册表（unsupported_findings 计数）",
        preconditions=["decision 层激活（否则 partial + UNGATED）"],
        success_conditions=["返回最重触发动作用作 decision 记录在案"],
        failure_conditions=["gate=execution_* 但没给 plan/execution"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        out = SkillResult(skill=self.spec.name)
        if not broker.layer_active("decision"):
            out.status = SKILL_PARTIAL
            out.data = {"action": "UNGATED", "detail": "",
                        "rule_name": "", "policy": ""}
            out.warn("decision layer inactive — policy not evaluated (ungated)")
            return out

        # ---- 执行层双门（13H）：gate=execution_pre / execution_post。
        # ---- rollback/keep symbols 是 legacy 门的历史必填（契约不变），
        # ---- gate 模式下不参与求值，但调用方仍须显式给出（防误调用）。
        gate_mode = context.get("gate") or ""
        if gate_mode == "execution_pre":
            plan = context.get("plan")
            if plan is None:
                out.status = SKILL_PARTIAL
                out.error = "gate=execution_pre requires a plan"
                return out
            return self._emit(broker.pre_execution_gate(
                plan, task_id=context.get("task_id", "")), out)
        if gate_mode == "execution_post":
            attempt = context.get("execution")
            if attempt is None:
                out.status = SKILL_PARTIAL
                out.error = "gate=execution_post requires an execution"
                return out
            return self._emit(broker.post_execution_gate(
                attempt, task_id=context.get("task_id", "")), out)

        gate_ctx = {
            "rollback_symbols": list(context["rollback_symbols"]),
            "keep_symbols": list(context["keep_symbols"]),
            "changed_files": list(context.get("changed_files") or []),
            "affected_routes": list(context.get("affected_routes") or []),
            "unsupported_findings": len(broker.unsupported_findings()),
        }
        result = broker.run_policy_gate(gate_ctx, task_id=context.get("task_id", ""))
        return self._emit(result, out)

    @staticmethod
    def _emit(result, out: SkillResult) -> SkillResult:
        rule = result.rule
        out.status = SKILL_SUCCESS
        out.data = {
            "action": result.action.value,
            "detail": result.detail,
            "rule_name": rule.name if rule else "",
            "policy": (f"{rule.name} v{rule.version} -> {result.action.value}"
                       if rule else "none-triggered"),
        }
        return out


register(PolicyCheckSkill())
