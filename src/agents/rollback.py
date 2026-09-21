"""RollbackPlanner（Phase 9J）：单元级回退/保留计划，绝不执行。

产出 RollbackPlan（数据），带着单元的 evidence 记录 rollback/keep
decision，并对计划自身的事实跑 policy gate。它从不跑 git：没有
checkout、没有 revert、没有 reset —— 计划即交付物，执行属于
HUMAN_REVIEW/PASS 之后的人类。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.objects import Decision
from src.semgraph.schema_v2 import NodeType


@dataclass
class RollbackPlan:
    task_id: str = ""
    rollback_units: list[dict] = field(default_factory=list)  # {id,label,commit,files}
    keep_units: list[dict] = field(default_factory=list)
    rollback_symbols: list[str] = field(default_factory=list)  # 短名
    keep_symbols: list[str] = field(default_factory=list)
    shared_symbols: list[str] = field(default_factory=list)
    rollback_files: list[str] = field(default_factory=list)
    keep_files: list[str] = field(default_factory=list)
    couplings: list[str] = field(default_factory=list)         # import 边
    affected_routes: list[str] = field(default_factory=list)
    policy_result: object | None = None                        # PolicyResult
    recommendation: str = ""
    decision_ids: list[str] = field(default_factory=list)

    def dump(self) -> str:
        r = self.policy_result.action.value if self.policy_result else "UNKNOWN"
        lines = [
            f"rollback: {[(u['id'], u['label']) for u in self.rollback_units]}",
            f"keep:    {[(u['id'], u['label']) for u in self.keep_units]}",
            f"shared symbols: {self.shared_symbols or 'none'}",
            f"import couplings: {self.couplings or 'none'}",
            f"affected routes: {self.affected_routes or 'none'}",
            f"policy: {r}",
            f"recommendation: {self.recommendation}",
        ]
        return "\n".join(lines)


def _short(qualified: str) -> str:
    return qualified.rsplit("::", 1)[-1]


class RollbackPlanner:
    ROLE = "RollbackPlanner"
    READS = ["find_change_units", "import_couplings", "run_policy_gate",
             "record_decision", "node"]

    def __init__(self, broker):
        self.broker = broker

    def plan(self, rollback_unit_ids: list[str], keep_unit_ids: list[str],
             affected_routes: list[str] | None = None, task_id: str = "",
             scope=None) -> RollbackPlan:
        self.broker.rec.tool(f"agent:{self.ROLE}:plan")
        plan = RollbackPlan(task_id=task_id,
                            affected_routes=list(affected_routes or []))
        ev_ids: list[str] = []
        for uid, bucket in [(u, plan.rollback_units) for u in rollback_unit_ids] + \
                           [(u, plan.keep_units) for u in keep_unit_ids]:
            node = self.broker.node(uid)
            if node is None or node.type != NodeType.CHANGE_UNIT:
                continue
            p = node.props
            bucket.append({"id": p.get("unit_id", uid), "label":
                           p.get("semantic_label", ""), "commit": p.get("commit", ""),
                           "files": list(p.get("files", []))})
            if p.get("evidence_id"):
                ev_ids.append(p["evidence_id"])
        plan.rollback_files = sorted({f for u in plan.rollback_units
                                      for f in u["files"]})
        plan.keep_files = sorted({f for u in plan.keep_units for f in u["files"]})
        rb_syms, keep_syms = set(), set()
        for uid, sink in [(u, rb_syms) for u in rollback_unit_ids] + \
                         [(u, keep_syms) for u in keep_unit_ids]:
            node = self.broker.node(uid)
            if node is not None:
                sink |= {_short(s) for s in node.props.get("symbols", [])}
        plan.rollback_symbols = sorted(rb_syms)
        plan.keep_symbols = sorted(keep_syms)
        plan.shared_symbols = sorted(rb_syms & keep_syms)
        plan.couplings = self.broker.import_couplings(plan.rollback_files,
                                                      plan.keep_files)
        # 对计划自身的事实跑 policy gate（G4：decision 层激活）
        if self.broker.layer_active("decision"):
            gate_ctx = {
                "rollback_symbols": plan.rollback_symbols,
                "keep_symbols": plan.keep_symbols,
                "changed_files": plan.rollback_files + plan.keep_files,
                "affected_routes": plan.affected_routes,
                "unsupported_findings": len(self.broker.unsupported_findings()),
            }
            plan.policy_result = self.broker.run_policy_gate(gate_ctx,
                                                             task_id=task_id)
            action = plan.policy_result.action.value
        else:
            action = "UNGATED"
        if action == "BLOCK":
            plan.recommendation = (
                "STOP: policy gate blocked this plan "
                f"({plan.policy_result.rule.name if plan.policy_result.rule else '?'}) "
                "— fix the blocking condition before any rollback")
        elif plan.shared_symbols or plan.couplings:
            plan.recommendation = (
                "HUMAN_REVIEW: rollback and keep sets are coupled "
                f"(shared={plan.shared_symbols}, imports={len(plan.couplings)}) "
                "— partial rollback needs a human decision")
        elif action == "HUMAN_REVIEW":
            plan.recommendation = (
                "HUMAN_REVIEW: " +
                (plan.policy_result.detail[:160] if plan.policy_result else "") +
                " — plan is ready but needs sign-off")
        elif action == "UNGATED":
            plan.recommendation = (
                "UNGATED (no policy layer at this ablation level): "
                "structural split only — no policy review was performed")
        else:
            plan.recommendation = (
                "PASS: rollback/keep sets are decoupled — partial rollback "
                "of the listed units is safe to prepare (execution stays manual)")
        # decision：我们提议什么、为什么（审计摘要，不是 CoT）
        if not self.broker.layer_active("decision"):
            return plan          # 该消融级没有决策记忆
        for unit, outcome in [(u, "rollback") for u in plan.rollback_units] + \
                            [(u, "keep") for u in plan.keep_units]:
            d = self.broker.record_decision(Decision.make(
                outcome, f"{outcome} unit {unit['id']} [{unit['label']}] "
                         f"from {unit['commit'][:8]}",
                task_id=task_id, target=",".join(unit["files"][:3]),
                risk={"BLOCK": "high"}.get(action, "medium"),
                decision_maker=self.ROLE, evidence_ids=ev_ids,
                reason_summary=f"policy={action}; shared={plan.shared_symbols or 'none'}; "
                               f"couplings={len(plan.couplings)}"))
            plan.decision_ids.append(d.id)
            if scope:
                scope.produced(decision=d.id)
        return plan
