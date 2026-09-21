"""ResolveTargetSkill —— 模糊自然语言 → feature/符号目标（Phase 11A）。

逻辑自 RepositoryNavigator.find_target 原样抽出：
语义候选（经 broker 门面）→ 无命中时确定性符号/文件名退路。
失败的导航必须显式（failed status 带原始错误），绝不静默返回空。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_FAILED, SKILL_PARTIAL, SKILL_SUCCESS,
                             SkillResult, SkillSpec)
from src.semgraph.objects import Evidence, EvidenceType, Finding


class ResolveTargetSkill(BaseSkill):
    spec = SkillSpec(
        name="resolve_target",
        description="fuzzy NL query -> feature/symbol target + vocabulary",
        required_inputs=["query"],
        produced_outputs=["feature_id", "feature_name", "related_symbols",
                          "terms", "finding_id", "candidates"],
        allowed_capabilities=["semantic.map_candidates",
                              "semantic.query_terms",
                              "repository.resolve_target",
                              "evidence.add", "evidence.finding.add"],
        evidence_requirements="SEMANTIC_MAPPING（语义命中）或 AST（确定性退路）",
        preconditions=["semantic layer active 或 query 含精确符号/文件名"],
        success_conditions=["产出唯一 top 目标 + 词表 + finding（带证据）"],
        failure_conditions=["query 完全无法解析（G0：语义层关且无精确名）"],
        semantic_reasoning="allowed",   # 11H：第一版 LLM 白名单
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        query = context["query"]
        llm = context.get("llm")
        # actor = 负责本次调用的 Agent 名（归属沿用调用方，默认 skill 自身）
        actor = context.get("actor") or "ResolveTargetSkill"
        cands = broker.map_semantic_candidates(query, llm=llm)
        out = SkillResult(skill=self.spec.name, status=SKILL_SUCCESS)
        if not cands:
            # 没有 feature 命中 —— 确定性符号/文件名退路
            node = broker.resolve_target(query)
            ev = Evidence.make(EvidenceType.AST, source="navigator:resolve",
                               target=node.id,
                               payload=f"fallback resolution to {node.id}")
            broker.add_evidence(ev)
            f = broker.add_finding(Finding.make(
                f"query targets {node.id}", actor, [ev.id]))
            out.data = {"feature_id": node.id,
                        "feature_name": node.props.get("name", ""),
                        "related_symbols": [node.id],
                        "terms": [query], "finding_id": f.id,
                        "candidates": 0, "candidate_details": []}
            out.evidence_ids = [ev.id]
            return out
        top = cands[0]
        for ev in top.evidence:
            broker.add_evidence(ev)
        f = broker.add_finding(Finding.make(
            f"query targets feature {top.name} ({top.mapping_method})",
            actor, [ev.id for ev in top.evidence]))
        out.data = {"feature_id": top.feature_id,
                    "feature_name": top.name,
                    "related_symbols": top.related_symbols,
                    "terms": broker.build_query_terms(query, top.name),
                    "finding_id": f.id,
                    "candidates": len(cands),
                    "candidate_details": list(cands)}
        out.evidence_ids = [ev.id for ev in top.evidence]
        return out


register(ResolveTargetSkill())
