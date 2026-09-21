"""ChangeIntelligenceAgent（Phase 9J / 11B）：变更历史 → ChangeUnit。

把单元的 label/符号/UI 文案与查询词表匹配 —— 对变更层做确定性打分，
每次匹配挂上 evidence。绝不假设 commit == change unit：输出永远是单元
级的。11B 起打分逻辑委托给 ChangeUnitAnalysisSkill；本 agent 保留
UnitMatch 数据形态与 scope 记账。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.errors import DataAgentError
from src.skills.runtime import SkillRuntime

# 确定性权重（11B 起真源在 skill，此处再导出兼容旧引用；11I 外置）
from src.skills.change_unit_analysis import (LABEL_PARTIAL, W_FILE, W_LABEL,
                                             W_SYMBOL, W_UI)


@dataclass
class UnitMatch:
    unit_id: str                 # 图节点 id（cu:...）
    commit: str
    label: str
    date: str = ""               # commit 日期（时近排序）
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    score: float = 0.0
    finding_id: str = ""
    evidence_id: str = ""        # 单元自带的 CHANGE_UNIT evidence


def to_unit_match(m: dict) -> UnitMatch:
    """skill 的 match dict → UnitMatch（多余键忽略）。"""
    return UnitMatch(
        unit_id=m["unit_id"], commit=m["commit"], label=m["label"],
        date=m.get("date", ""), files=list(m.get("files", [])),
        symbols=list(m.get("symbols", [])), score=m.get("score", 0.0),
        finding_id=m.get("finding_id", ""),
        evidence_id=m.get("evidence_id", ""))


class ChangeIntelligenceAgent:
    ROLE = "ChangeIntelligenceAgent"
    READS = ["find_change_units", "get_change_context", "add_evidence",
             "add_finding", "node"]
    SKILLS = ["change_unit_analysis"]

    def __init__(self, broker):
        self.broker = broker
        self._runtime = SkillRuntime(broker)
        # 兼容旧引用：权重常量转发到 skill
        self.W_LABEL, self.W_UI = W_LABEL, W_UI
        self.W_SYMBOL, self.W_FILE = W_SYMBOL, W_FILE

    def find_units(self, terms: list[str], commits: list[str] | None = None,
                   scope=None) -> list[UnitMatch]:
        """匹配词表的单元，优者在前。`commits` 限定搜索范围
        （用户用 keep hint 锚定"同一个 commit"时，orchestrator 就用它
        把 commit 钉住）。"""
        self.broker.rec.tool(f"agent:{self.ROLE}:find_units")
        ctx = {"terms": list(terms), "actor": self.ROLE}
        if commits:
            ctx["commits"] = list(commits)
        r = self._runtime.run("change_unit_analysis", ctx)
        if r.status == "failed":
            raise DataAgentError(r.error or "change analysis failed")
        matches = [to_unit_match(m) for m in r.data["matches"]]
        if scope:
            scope.produced(evidence=r.evidence_ids)
            for m in matches:
                scope.produced(finding=m.finding_id)
        return matches
