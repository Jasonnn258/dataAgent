"""SafeRollbackSkill —— 单元级回退/保留计划 + 仲裁（Phase 11A）。

两段逻辑原样抽出：
1. keep/problem 仲裁 —— 自 Orchestrator.run 的仲裁段：同时命中两套
   词表的单元归打分更高者，平局归问题侧（嫌疑犯 stays 嫌疑犯）；
   keep 命中把问题搜索钉在用户点名的 commit 上。
2. 计划装配 + policy gate + 决策记录 —— 自 RollbackPlanner.plan。

绝不执行 git：计划即交付物。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_SUCCESS, SkillResult, SkillSpec)
from src.semgraph.objects import Decision
from src.semgraph.schema_v2 import NodeType


def _short(qualified: str) -> str:
    return qualified.rsplit("::", 1)[-1]


def _tie_prefer_problem_side() -> bool:
    """平局策略（11I 外置）：true = 嫌疑犯 stays 嫌疑犯。每次现读，
    支持 A/B 消融。"""
    from src.config import maintenance_policy
    return bool(maintenance_policy()["rollback"]["tie_break"]
                ["prefer_problem_side"])


def arbitrate(problem_matches: list[dict], keep_matches: list[dict]):
    """keep/problem 仲裁（纯数据运算，无 broker 访问）。

    同时命中两套词表的单元归打分更高者；平局去向由策略配置
    rollback.tie_break.prefer_problem_side 决定（默认问题侧：嫌疑犯
    stays 嫌疑犯）。keep 命中把问题搜索钉在用户点名的 commit 上。
    skill 与 orchestrator（经 RollbackPlanner.arbitrate）共用这一份
    逻辑。
    """
    prefer_problem = _tie_prefer_problem_side()
    score_p = {m["unit_id"]: m for m in problem_matches}
    score_k = {m["unit_id"]: m for m in keep_matches}
    keep_units = [m for uid, m in score_k.items()
                  if uid not in score_p
                  or (score_k[uid]["score"] > score_p[uid]["score"]
                      if prefer_problem
                      else score_k[uid]["score"] >= score_p[uid]["score"])]
    keep_ids = {m["unit_id"] for m in keep_units}
    # 只有真实的 commit 才构成钉住；空 commit（如按 unit id 直传的计划
    # 路径）不限制问题侧
    pin_commits = sorted({m["commit"] for m in keep_units
                          if m.get("commit")}) or None
    problem_units = [m for m in problem_matches
                     if m["unit_id"] not in keep_ids
                     and (pin_commits is None or m["commit"] in pin_commits)]
    return problem_units, keep_units


class SafeRollbackSkill(BaseSkill):
    spec = SkillSpec(
        name="safe_rollback",
        description="arbitrate keep/problem units and assemble a gated rollback plan",
        required_inputs=["problem_matches", "keep_matches"],
        produced_outputs=["rollback_units", "keep_units", "problem_matches",
                          "keep_matches_kept", "rollback_files", "keep_files",
                          "rollback_symbols", "keep_symbols", "shared_symbols",
                          "couplings", "policy_action", "recommendation",
                          "decision_ids", "policy_result"],
        allowed_capabilities=["repository.node", "change.get_couplings",
                              "policy.gate", "decision.record",
                              "evidence.query"],
        evidence_requirements="每个入桶单元带其 CHANGE_UNIT evidence（经 ev_ids 引用）",
        preconditions=["matches 来自 change_unit_analysis（unit_id 是图节点 id）"],
        success_conditions=["两组单元确定、耦合已查、policy 已裁决（或显式 UNGATED）"],
        failure_conditions=["输入 unit_id 不是 ChangeUnit 节点（该单元被跳过并告警）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        prob_all = list(context["problem_matches"])
        keep_all = list(context["keep_matches"])
        affected_routes = list(context.get("affected_routes") or [])
        task_id = context.get("task_id", "")
        actor = context.get("actor") or "SafeRollbackSkill"
        out = SkillResult(skill=self.spec.name, status=SKILL_SUCCESS)

        # ---- 1. 仲裁（同模块 arbitrate()）----
        problem_units, keep_units = arbitrate(prob_all, keep_all)

        # ---- 2. 计划装配（自 planner.plan 原样移植）----
        rollback_unit_ids = [m["unit_id"] for m in problem_units]
        keep_unit_ids = [m["unit_id"] for m in keep_units]
        rollback_units: list[dict] = []
        keep_units_b: list[dict] = []
        ev_ids: list[str] = []
        for uid, bucket in [(u, rollback_units) for u in rollback_unit_ids] + \
                           [(u, keep_units_b) for u in keep_unit_ids]:
            node = broker.node(uid)
            if node is None or node.type != NodeType.CHANGE_UNIT:
                out.warn(f"not a ChangeUnit node: {uid} — skipped")
                continue
            p = node.props
            bucket.append({"id": p.get("unit_id", uid),
                           "label": p.get("semantic_label", ""),
                           "commit": p.get("commit", ""),
                           "files": list(p.get("files", []))})
            if p.get("evidence_id"):
                ev_ids.append(p["evidence_id"])
        rollback_files = sorted({f for u in rollback_units for f in u["files"]})
        keep_files = sorted({f for u in keep_units_b for f in u["files"]})
        rb_syms, keep_syms = set(), set()
        for uid, sink in [(u, rb_syms) for u in rollback_unit_ids] + \
                         [(u, keep_syms) for u in keep_unit_ids]:
            node = broker.node(uid)
            if node is not None:
                sink |= {_short(s) for s in node.props.get("symbols", [])}
        rollback_symbols = sorted(rb_syms)
        keep_symbols = sorted(keep_syms)
        shared_symbols = sorted(rb_syms & keep_syms)
        couplings = broker.import_couplings(rollback_files, keep_files)

        # ---- 3. policy gate（decision 层未激活时显式 UNGATED）----
        if broker.layer_active("decision"):
            gate_ctx = {
                "rollback_symbols": rollback_symbols,
                "keep_symbols": keep_symbols,
                "changed_files": rollback_files + keep_files,
                "affected_routes": affected_routes,
                "unsupported_findings": len(broker.unsupported_findings()),
            }
            policy_result = broker.run_policy_gate(gate_ctx, task_id=task_id)
            action = policy_result.action.value
            policy_detail = policy_result.detail
            rule_name = policy_result.rule.name if policy_result.rule else ""
        else:
            action = "UNGATED"
            policy_detail = ""
            rule_name = ""

        # ---- 4. recommendation 分支（与 planner 一致）----
        if action == "BLOCK":
            recommendation = (
                "STOP: policy gate blocked this plan "
                f"({rule_name or '?'}) "
                "— fix the blocking condition before any rollback")
        elif shared_symbols or couplings:
            recommendation = (
                "HUMAN_REVIEW: rollback and keep sets are coupled "
                f"(shared={shared_symbols}, imports={len(couplings)}) "
                "— partial rollback needs a human decision")
        elif action == "HUMAN_REVIEW":
            recommendation = (
                "HUMAN_REVIEW: " + policy_detail[:160] +
                " — plan is ready but needs sign-off")
        elif action == "UNGATED":
            recommendation = (
                "UNGATED (no policy layer at this ablation level): "
                "structural split only — no policy review was performed")
        else:
            recommendation = (
                "PASS: rollback/keep sets are decoupled — partial rollback "
                "of the listed units is safe to prepare (execution stays manual)")

        # ---- 5. 决策记录（审计摘要，不是 CoT）----
        decision_ids: list[str] = []
        if broker.layer_active("decision"):
            from src.config import maintenance_policy, policy_version
            # 单元级风险随裁决档位走（11I：action→risk 外置，缺档回退 medium）
            action_risk = maintenance_policy()["policy"]["action_risk"]
            for unit, outcome in [(u, "rollback") for u in rollback_units] + \
                                [(u, "keep") for u in keep_units_b]:
                d = broker.record_decision(Decision.make(
                    outcome, f"{outcome} unit {unit['id']} [{unit['label']}] "
                             f"from {unit['commit'][:8]}",
                    task_id=task_id, target=",".join(unit["files"][:3]),
                    risk=action_risk.get(action, "medium"),
                    decision_maker=actor, evidence_ids=ev_ids,
                    reason_summary=f"policy={action}; "
                                   f"shared={shared_symbols or 'none'}; "
                                   f"couplings={len(couplings)}",
                    policy_version=policy_version()))
                decision_ids.append(d.id)

        out.data = {
            "problem_matches": problem_units,
            "keep_matches_kept": keep_units,
            "rollback_units": rollback_units, "keep_units": keep_units_b,
            "rollback_files": rollback_files, "keep_files": keep_files,
            "rollback_symbols": rollback_symbols, "keep_symbols": keep_symbols,
            "shared_symbols": shared_symbols, "couplings": couplings,
            "policy_action": action, "policy_detail": policy_detail,
            "rule_name": rule_name, "recommendation": recommendation,
            "decision_ids": decision_ids, "evidence_ids": ev_ids,
            "policy_result": policy_result if broker.layer_active("decision")
            else None,
        }
        out.evidence_ids = ev_ids
        return out


register(SafeRollbackSkill())
