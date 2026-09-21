"""ChangeUnitAnalysisSkill —— 词表 → ChangeUnit 匹配（Phase 11A）。

逻辑自 ChangeIntelligenceAgent.find_units 原样抽出：label/UI 文案/
符号/文件四路确定性打分 + 时近排序，每次匹配铸造 evidence、注册
finding。输出恒为单元级 —— 绝不假设 commit == change unit。

打分权重 W_* 目前与原 agent 一致硬编码；Phase 11I 移入
config/maintenance_policy.yaml。
"""
from __future__ import annotations

from src.skills.base import BaseSkill
from src.skills.registry import register
from src.skills.spec import (SKILL_PARTIAL, SKILL_SUCCESS, SkillResult,
                             SkillSpec)
from src.semgraph.objects import Evidence, EvidenceType, Finding

# 确定性权重：label 是最强信号，UI 文案次之（11I 外置到 policy config）
W_LABEL, W_UI, W_SYMBOL, W_FILE = 3.0, 2.0, 2.0, 1.0
LABEL_PARTIAL = 0.7                    # label 子串命中折扣


class ChangeUnitAnalysisSkill(BaseSkill):
    spec = SkillSpec(
        name="change_unit_analysis",
        description="match change units against a term vocabulary (deterministic)",
        required_inputs=["terms"],
        produced_outputs=["matches"],
        allowed_capabilities=["change.find_units", "repository.node",
                              "evidence.add", "evidence.finding.add"],
        evidence_requirements="每命中一条 CHANGE_UNIT evidence（provenance 指向单元自带证据）",
        preconditions=["change layer 已建（build_change_graph）"],
        success_conditions=["词表命中 ≥1 单元且逐条带证据/finding"],
        failure_conditions=["变更层未激活（partial，matches 为空）"],
    )

    def _execute(self, context: dict, broker) -> SkillResult:
        out = SkillResult(skill=self.spec.name)
        if not broker.layer_active("change"):
            out.status = SKILL_PARTIAL
            out.data = {"matches": []}
            out.warn("change layer inactive — nothing to match (G<2)")
            return out
        tl = [t.lower() for t in context["terms"] if t]
        commits = context.get("commits")
        matches: list[dict] = []
        for cu in broker.find_change_units():
            p = cu.props
            if commits and p.get("commit", "") not in commits:
                continue
            label = p.get("semantic_label", "")
            syms = p.get("symbols", [])
            short_syms = [s.rsplit("::", 1)[-1].lower() for s in syms]
            ui = " ".join(p.get("ui_strings", [])).lower()
            files = " ".join(p.get("files", [])).lower()
            score = 0.0
            for t in tl:
                if t == label.lower():
                    score += W_LABEL
                elif t in label.lower():
                    score += W_LABEL * LABEL_PARTIAL
                if t in ui:
                    score += W_UI
                if any(t == s or t in s for s in short_syms):
                    score += W_SYMBOL
                if t in files:
                    score += W_FILE
            if score <= 0:
                continue
            unit_ev = p.get("evidence_id", "")
            ev = Evidence.make(
                EvidenceType.CHANGE_UNIT, source="change-intel:match",
                target=cu.id, location=p.get("commit", "")[:12],
                payload=f"unit {p.get('unit_id')} [{label}] matches terms "
                        f"{[t for t in tl if t][:6]} (score {score})",
                provenance={"derived_from": [unit_ev]} if unit_ev else {})
            broker.add_evidence(ev)
            f = broker.add_finding(Finding.make(
                f"unit {p.get('unit_id')} [{label}] in {p.get('commit', '')[:8]} "
                f"matches the change description", "ChangeUnitAnalysisSkill",
                [ev.id]))
            matches.append({
                "unit_id": cu.id, "commit": p.get("commit", ""),
                "label": label, "date": self._commit_date(broker, p.get("commit", "")),
                "files": list(p.get("files", [])), "symbols": list(syms),
                "score": score, "finding_id": f.id, "evidence_id": unit_ev,
                "match_evidence_id": ev.id})
        # 优者在前：先分数，再时近（按 commit 日期，不按 sha 序）
        matches.sort(key=lambda m: (-m["score"], m["date"], m["commit"]))
        out.status = SKILL_SUCCESS
        out.data = {"matches": matches}
        out.evidence_ids = [m["match_evidence_id"] for m in matches]
        return out

    @staticmethod
    def _commit_date(broker, sha: str) -> str:
        node = broker.node(f"commit:{sha}")
        return node.props.get("date", "") if node else ""


register(ChangeUnitAnalysisSkill())
