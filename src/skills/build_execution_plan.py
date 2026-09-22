"""BuildExecutionPlanSkill —— RollbackPlan → ExecutionPlan（Phase 13B）。

纯确定性翻译，不做任何新决策：分桶决策在 safe_rollback（仲裁 + policy
gate）已经做完，这里只把结果翻译成可执行、可验证、可审计的合同：

- expected_changes：回退单元 × 文件 → "REVERT" 合同（符号取短名）
- forbidden_changes：保留单元 × 文件 → "UNCHANGED" 合同
- base_commit + repository_snapshot：plan 生成时刻的仓库指纹，
  之后每一步执行前都要重拍对比（STALE 检测的基准）

计划外的文件修改不在合同里 —— 那是 13G Verifier 的 scope 检查
（modified ⊆ plan.target_files）负责判越界，这里不重复。
"""
from __future__ import annotations

import json

from src.errors import DataAgentError
from src.maintenance.models import (ExecutionPlan, ForbiddenChange,
                                    PlannedChange)
from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import SKILL_FAILED, SKILL_SUCCESS, SkillResult, SkillSpec
from src.semgraph.objects import Evidence, EvidenceType
from src.semgraph.schema_v2 import NodeType

# evidence payload 硬上限（计划摘要也不放大载荷）
_PLAN_PAYLOAD_CAP = 1200


def _short(qualified: str) -> str:
    return qualified.rsplit("::", 1)[-1]


def _normalize_units(payload) -> tuple[list[dict], list[dict]]:
    """兼容两种输入：safe_rollback 的 data dict（生产路径）或带
    rollback_units/keep_units 属性的对象（测试/编排器直传）。"""
    if hasattr(payload, "rollback_units"):
        src = [(getattr(payload, "rollback_units", []) or []),
               (getattr(payload, "keep_units", []) or [])]
    elif isinstance(payload, dict):
        src = [payload.get("rollback_units", []) or [],
               payload.get("keep_units", []) or []]
    else:
        raise DataAgentError(
            f"rollback_plan must be safe_rollback data or plan object, "
            f"got {type(payload).__name__}")

    def norm(raw: list) -> list[dict]:
        out: list[dict] = []
        for u in raw:
            out.append({"id": u.get("id") or u.get("unit_id", ""),
                        "label": u.get("label", ""),
                        "commit": u.get("commit", ""),
                        "files": list(u.get("files", []))})
        return out

    return norm(src[0]), norm(src[1])


def _policy_digest(payload) -> dict:
    """从 plan 里抽 policy 裁决的有界摘要（dict 或 PolicyResult 均可）。"""
    pr = None
    if isinstance(payload, dict):
        pr = payload.get("policy_result")
        action = payload.get("policy_action", "")
        detail = payload.get("policy_detail", "")
        rule = payload.get("rule_name", "")
    else:
        pr = getattr(payload, "policy_result", None)
        action = detail = rule = ""
    if pr is not None:
        action = getattr(pr, "action", None)
        action = getattr(action, "value", action) or action or ""
        detail = getattr(pr, "detail", "") or ""
        rule = getattr(pr, "rule", None)
        rule = (getattr(rule, "name", "") or "") if rule else ""
    return {"action": str(action), "detail": str(detail)[:200],
            "rule": str(rule)}


def _sanitize_commands(raw) -> list[list[str]]:
    """validation_commands 只接受 argv list（拒收 shell 字符串）。
    非法条目直接丢弃并告警，绝不猜调用方意图。"""
    out: list[list[str]] = []
    for c in raw or []:
        if (isinstance(c, list) and c
                and all(isinstance(t, str) and t.strip() for t in c)):
            out.append(list(c))
    return out


class BuildExecutionPlanSkill(BaseSkill):
    spec = SkillSpec(
        name="build_execution_plan",
        description=("translate an arbitrated rollback plan into a "
                     "verifiable ExecutionPlan (deterministic, no git)"),
        required_inputs=["rollback_plan"],
        produced_outputs=["plan", "execution_plan", "evidence_id"],
        allowed_capabilities=["repository.node", "evidence.add",
                              "execution.snapshot"],
        evidence_requirements="EXECUTION_PLAN evidence（计划摘要 + 仓库快照指纹）",
        preconditions=["rollback_plan 来自 safe_rollback（单元 id 是图节点）"],
        success_conditions=["expected/forbidden 合同齐 + base_commit + 快照指纹"],
        failure_conditions=["回退侧为空（无可执行内容）",
                            "rollback_plan 形态不对"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        payload = context["rollback_plan"]
        task_id = context.get("task_id", "") or "task"
        actor = context.get("actor") or "BuildExecutionPlanSkill"
        out = SkillResult(skill=self.spec.name, status=SKILL_SUCCESS)

        rollback_units, keep_units = _normalize_units(payload)
        if not rollback_units:
            out.status = SKILL_FAILED
            out.error = ("rollback side is empty — nothing to execute; "
                         "re-run safe_rollback before planning execution")
            return out

        # ---- 1. 图上补全单元详情（symbols / evidence；图没有就显式留空）----
        def enrich(units: list[dict]) -> list[dict]:
            enriched: list[dict] = []
            for u in units:
                # safe_rollback 落的 id 是 props.unit_id（无前缀），
                # 图节点 id 是 "cu:<unit_id>" —— 两个候选都试
                node = None
                for cand in (u["id"], f"cu:{u['id']}"):
                    found = broker.node(cand)
                    if found is not None and \
                            found.type == NodeType.CHANGE_UNIT:
                        node = found
                        break
                if node is not None:
                    p = node.props
                    u = {**u,
                         "symbols": list(p.get("symbols", [])),
                         "semantic_label": p.get("semantic_label",
                                                 u["label"]),
                         "evidence_id": p.get("evidence_id", "")}
                    if u["evidence_id"] and u["evidence_id"] not in ev:
                        ev.append(u["evidence_id"])
                else:
                    out.warn(f"unit not on graph: {u['id']} — "
                             "plan keeps it, verification will rely on "
                             "files only")
                    u = {**u, "symbols": [],
                         "semantic_label": u["label"], "evidence_id": ""}
                enriched.append(u)
            return enriched

        ev: list[str] = []
        rollback_units = enrich(rollback_units)
        keep_units = enrich(keep_units)

        # ---- 2. 仓库快照（stale 检测基准；只读）----
        target_files = sorted({f for u in rollback_units + keep_units
                               for f in u["files"]})
        snapshot = broker.execution_snapshot(target_files)

        # ---- 3. 合同装配：expected（回退侧）/ forbidden（保留侧）----
        expected_changes: list[PlannedChange] = []
        forbidden_changes: list[ForbiddenChange] = []
        for u in rollback_units:
            for f in u["files"]:
                syms = sorted({_short(s) for s in u["symbols"]
                               if s.startswith(f + "::")})
                expected_changes.append(PlannedChange(
                    file=f, symbols=syms, change="REVERT",
                    source_unit=u["id"]))
        for u in keep_units:
            for f in u["files"]:
                syms = sorted({_short(s) for s in u["symbols"]
                               if s.startswith(f + "::")})
                forbidden_changes.append(ForbiddenChange(
                    file=f, symbols=syms, source_unit=u["id"]))
        target_symbols = sorted({_short(s) for u in rollback_units + keep_units
                                 for s in u["symbols"]})

        # ---- 4. policy 摘要 + 风险档位（裁决已在 safe_rollback 完成）----
        policy_result = _policy_digest(payload)
        from src.config import maintenance_policy
        action_risk = maintenance_policy()["policy"]["action_risk"]
        risk = action_risk.get(policy_result["action"], "medium")

        commands = _sanitize_commands(context.get("validation_commands"))
        dropped = len(list(context.get("validation_commands") or [])) \
            - len(commands)
        if dropped:
            out.warn(f"dropped {dropped} malformed validation command(s); "
                     "only argv lists are accepted")

        plan = ExecutionPlan(
            task_id=task_id, repo=str(broker.repo),
            base_commit=snapshot.head,
            rollback_units=rollback_units, keep_units=keep_units,
            target_files=target_files, target_symbols=target_symbols,
            expected_changes=expected_changes,
            forbidden_changes=forbidden_changes,
            validation_commands=commands, risk=risk,
            affected_routes=[r for r in
                             (context.get("affected_routes") or []) if r],
            policy_result=policy_result, evidence_ids=ev,
            repository_snapshot=snapshot)

        # ---- 5. EXECUTION_PLAN evidence（审计锚点）----
        summary_text = json.dumps(plan.summary(), ensure_ascii=False)
        evidence = Evidence.make(
            type=EvidenceType.EXECUTION_PLAN, source="execution:plan",
            target=rollback_units[0]["id"] or task_id,
            location=plan.base_commit[:12],
            payload=summary_text[:_PLAN_PAYLOAD_CAP], producer=actor)
        broker.add_evidence(evidence)
        ev.append(evidence.id)
        plan.evidence_ids = ev

        out.data = {"plan": plan, "execution_plan": plan.summary(),
                    "evidence_id": evidence.id}
        out.evidence_ids = list(ev)
        return out


register(BuildExecutionPlanSkill())
