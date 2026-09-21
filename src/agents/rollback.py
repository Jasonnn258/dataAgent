"""RollbackPlanner（Phase 9J / 11B）：单元级回退/保留计划，绝不执行。

产出 RollbackPlan（数据），带着单元的 evidence 记录 rollback/keep
decision，并对计划自身的事实跑 policy gate。它从不跑 git：没有
checkout、没有 revert、没有 reset —— 计划即交付物，执行属于
HUMAN_REVIEW/PASS 之后的人类。

11B 起装配/gate/decision 全部委托给 SafeRollbackSkill（归属沿用
RollbackPlanner，decision_maker 不变）；仲裁也从这里暴露给
orchestrator（逻辑真源在 skill 模块的 arbitrate()）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.agents.change_intel import UnitMatch, to_unit_match
from src.errors import DataAgentError
from src.semgraph.objects import Decision  # noqa: F401（旧引用兼容）
from src.skills.runtime import SkillRuntime
from src.skills.safe_rollback import arbitrate as _arbitrate


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


class RollbackPlanner:
    ROLE = "RollbackPlanner"
    READS = ["find_change_units", "import_couplings", "run_policy_gate",
             "record_decision", "node"]
    SKILLS = ["safe_rollback"]

    def __init__(self, broker):
        self.broker = broker
        self._runtime = SkillRuntime(broker)

    def arbitrate(self, problem_matches: list[UnitMatch],
                  keep_matches: list[UnitMatch]):
        """keep/problem 仲裁（委托 skill 模块的纯函数）。返回
        (problem_units, keep_units_kept)，仍是 UnitMatch 形态。"""
        from dataclasses import asdict
        prob, keep = _arbitrate([asdict(m) for m in problem_matches],
                                [asdict(m) for m in keep_matches])
        return [to_unit_match(m) for m in prob], [to_unit_match(m) for m in keep]

    def plan(self, rollback_unit_ids: list[str], keep_unit_ids: list[str],
             affected_routes: list[str] | None = None, task_id: str = "",
             scope=None) -> RollbackPlan:
        self.broker.rec.tool(f"agent:{self.ROLE}:plan")
        # 旧接口收 unit id 列表：包一层最小 match（空 commit 不构成钉住，
        # 见 skill.arbitrate）—— 仲裁在此退化为直通
        wrap = lambda ids: [{"unit_id": u, "commit": "", "score": 0.0}
                            for u in ids]
        r = self._runtime.run("safe_rollback", {
            "problem_matches": wrap(rollback_unit_ids),
            "keep_matches": wrap(keep_unit_ids),
            "affected_routes": list(affected_routes or []),
            "task_id": task_id, "actor": self.ROLE})
        if r.status == "failed":
            raise DataAgentError(r.error or "rollback planning failed")
        d = r.data
        plan = RollbackPlan(
            task_id=task_id,
            rollback_units=list(d["rollback_units"]),
            keep_units=list(d["keep_units"]),
            rollback_symbols=list(d["rollback_symbols"]),
            keep_symbols=list(d["keep_symbols"]),
            shared_symbols=list(d["shared_symbols"]),
            rollback_files=list(d["rollback_files"]),
            keep_files=list(d["keep_files"]),
            couplings=list(d["couplings"]),
            affected_routes=list(affected_routes or []),
            policy_result=d.get("policy_result"),
            recommendation=d["recommendation"],
            decision_ids=list(d["decision_ids"]))
        if scope:
            for did in plan.decision_ids:
                scope.produced(decision=did)
        return plan
