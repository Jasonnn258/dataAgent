"""ChangeUnitLabelSkill —— 变更单元语义标签（Phase 12C）。

对给定 ChangeUnit 批量产出 LLM 语义标签（label 域 / intent 意图 /
candidate_features 候选 feature），三条硬约束：

1. 输出永远停留在 candidate 级：每单元一条 SEMANTIC_MAPPING
   evidence（payload 带 status=candidate），绝不写回单元的确定性
   semantic_label / props —— C0 的确定性标签仍是唯一事实；
2. candidate_features 只能从图里已有的 feature 名里挑（adapter 层
   过滤幻觉 + 本层只喂图内名字，双重图验证）；
3. LLM 不可用/失败 => 明确 degraded 留痕（labels 为空），调用方的
   确定性路径照常工作，绝不静默吞。

12C 消融（C0 vs C1）里 C1 臂的消费方式在 harness 层（ablation
runner）：用 graph-verified feature 重打分，不改本 skill 行为。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_FAILED, SKILL_PARTIAL, SKILL_SUCCESS,
                             SkillResult, SkillSpec)
from src.semgraph.objects import Evidence, EvidenceType
from src.semgraph.schema_v2 import NodeType


def _node(broker, uid: str):
    """unit id 双形态容错：props 里的 unit_id 无前缀，图节点 id 带 cu: 前缀。

    两种写法都查一遍（与 benchmark._resolve_anchor 的前缀补齐同构），
    避免调用方传错形态时单元整批被静默丢弃。
    """
    return broker.node(uid) if uid.startswith("cu:") else (
        broker.node(uid) or broker.node(f"cu:{uid}"))


class ChangeUnitLabelSkill(BaseSkill):
    spec = SkillSpec(
        name="change_unit_label",
        description="batch LLM semantic labels for change units "
                    "(candidate-level only)",
        required_inputs=["unit_ids"],
        produced_outputs=["labels", "llm_used"],
        allowed_capabilities=["repository.node", "evidence.add"],
        evidence_requirements="每单元一条 SEMANTIC_MAPPING candidate evidence",
        preconditions=["变更层已建（cu: 节点存在）；LLM 可用"],
        success_conditions=["≥1 单元拿到白名单内 label/intent + 图验证 "
                            "candidate_features + candidate evidence"],
        failure_conditions=["LLM 不可用（degraded，labels 空）"],
        semantic_reasoning="allowed",   # 12C：第二个 LLM 白名单接线点
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        out = SkillResult(skill=self.spec.name)
        llm = context.get("llm")
        hint_terms = [t for t in context.get("hint_terms", []) if t]

        # 组装单元载荷（只读图内事实：props 是确定性 characterize 的产物）
        units: list[dict] = []
        for uid in context["unit_ids"][:12]:
            node = _node(broker, uid)
            if node is None or node.props.get("commit") is None:
                continue
            subj = ""
            c = broker.node(f"commit:{node.props['commit']}")
            if c is not None:
                subj = c.props.get("subject", "")
            units.append({
                "unit_id": uid,
                "summary": node.props.get("summary", "")[:300],
                "commit_subject": subj[:120],
                "label_c0": node.props.get("semantic_label", ""),
                "files": list(node.props.get("files", []))[:6],
            })
        if not units:
            out.status = SKILL_PARTIAL
            out.data = {"labels": [], "llm_used": False}
            out.warn("no change units found — labels empty")
            return out

        if llm is None or not llm.available:
            out.status = SKILL_PARTIAL
            out.data = {"labels": [], "llm_used": False}
            out.warn("llm unavailable — deterministic labels untouched")
            return out

        # 喂给 LLM 的 feature 名单来自图（12B 通用播种后是源码符号名）
        feats = [f.props.get("name", "") for f in
                 broker.graph.nodes_of_type(NodeType.FEATURE)
                 if f.props.get("name")][:120]
        from src.llm.semantic_adapter import SemanticReasoningAdapter
        try:
            labels = SemanticReasoningAdapter(llm).label_change_units(
                units, hint_terms, feats)
        except Exception as e:   # LLM 失败 => degraded，不拖垮确定性链
            out.status = SKILL_PARTIAL
            out.data = {"labels": [], "llm_used": False, "error": str(e)[:200]}
            out.warn(f"llm labeling failed ({e}) — labels empty")
            return out

        # candidate 级证据（绝不写回单元 props）
        for item in labels:
            node = _node(broker, item["unit_id"])
            if node is None:
                continue
            ev = Evidence.make(
                EvidenceType.SEMANTIC_MAPPING,
                source="cu-label:llm",
                target=item["unit_id"],
                payload=f"[candidate] label={item['label']} "
                        f"intent={item['intent']} "
                        f"features={item['candidate_features']} "
                        f"reason={item['reason'][:120]}")
            broker.add_evidence(ev)
            item["evidence_id"] = ev.id
        out.status = SKILL_SUCCESS if labels else SKILL_PARTIAL
        out.data = {"labels": labels, "llm_used": True,
                    "unit_count": len(units)}
        out.evidence_ids = [i["evidence_id"] for i in labels]
        if not labels:
            out.warn("llm returned no valid unit labels")
        return out


register(ChangeUnitLabelSkill())
