"""RepositoryNavigator（Phase 9J / 11B）：模糊自然语言 → feature 目标。

唯一允许 LLM 参与的 agent —— 即便在这里，LLM 也只能在已有 feature 名
字里挑（SemanticMapper 契约）；每一次挑选都停在 candidate 级，直到确
定性工具给出佐证。导航失败要大声报错：DataAgentError，绝不静默返回空
结果。

11B 起，匹配逻辑全部委托给 ResolveTargetSkill（经 SkillRuntime）；
语义候选/词表经 ContextBroker 语义门面获取 —— agent 不再自己构造
SemanticMapper，也不再直连 search。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.errors import DataAgentError
from src.semgraph.semantic_mapper import SemanticTargetCandidate
from src.skills.runtime import SkillRuntime


@dataclass
class NavigationResult:
    feature_id: str = ""
    feature_name: str = ""
    related_symbols: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)     # 给 CI 的查询词表
    finding_id: str = ""
    candidates: list[SemanticTargetCandidate] = field(default_factory=list)


class RepositoryNavigator:
    ROLE = "RepositoryNavigator"
    # 旧能力清单（兼容保留）；11B 起实际执行走 SKILLS
    READS = ["map_query", "resolve_target", "add_evidence", "add_finding"]
    SKILLS = ["resolve_target"]

    def __init__(self, broker, llm=None):
        self.broker = broker
        self.llm = llm
        self._runtime = SkillRuntime(broker, llm=llm)

    def find_target(self, query: str, scope=None) -> NavigationResult:
        self.broker.rec.tool(f"agent:{self.ROLE}:navigate")
        r = self._runtime.run("resolve_target",
                              {"query": query, "actor": self.ROLE})
        if r.status == "failed":
            # skill 层吞异常转 failed；agent 边界把失败重新变大声
            raise DataAgentError(r.error or "navigation failed")
        if scope:
            scope.produced(evidence=r.evidence_ids,
                           finding=r.data["finding_id"])
        return NavigationResult(
            feature_id=r.data["feature_id"],
            feature_name=r.data["feature_name"],
            related_symbols=r.data["related_symbols"],
            terms=r.data["terms"], finding_id=r.data["finding_id"],
            candidates=r.data["candidate_details"])
