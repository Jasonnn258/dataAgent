"""PolicyService（Phase 11D）：版本化规则门的组合与落档。

确定性服务：跑规则集、把结果记成可审计 decision。gate 只裁决，不执行；
服从 BLOCK 是调用方（Agent）的契约。逻辑自 ContextBroker 原样迁入。
"""
from __future__ import annotations

from src.errors import DataAgentError
from src.semgraph.objects import Decision, PolicyResult


class PolicyService:
    def __init__(self, broker):
        self.broker = broker

    def check_policy(self, rule_name: str, context: dict) -> PolicyResult:
        from src.semgraph.policy import POLICY_RULES
        rule = POLICY_RULES.get(rule_name)
        if rule is None:
            raise DataAgentError(f"unknown policy rule {rule_name!r}")
        return rule.evaluate(context)

    def run_policy_gate(self, context: dict, task_id: str = "") -> PolicyResult:
        """跑完整规则集并把结果记成可审计 decision。返回触发的最重动作。"""
        span = getattr(self.broker.rec, "span", None)
        if span is None:
            return self._gate(context, task_id)
        with span("policy", "PolicyGate", "gate", task_id=task_id):
            return self._gate(context, task_id)

    def _gate(self, context: dict, task_id: str) -> PolicyResult:
        from src.config import maintenance_policy, policy_version
        from src.semgraph.policy import gate
        result = gate(context)
        # action → risk 是策略映射（11I 外置），不是代码常量
        risk = maintenance_policy()["policy"]["action_risk"][result.action.value]
        self.broker.record_decision(Decision.make(
            "policy_gate", result.action.value, task_id=task_id, risk=risk,
            decision_maker="PolicyGate",
            reason_summary=result.detail[:200],
            policy=(f"{result.rule.name} v{result.rule.version} -> "
                    f"{result.action.value}") if result.rule else "none-triggered",
            policy_version=policy_version()))
        return result
