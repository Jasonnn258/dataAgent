"""Orchestrator（Phase 9J）：唯一的协调者，自己不带逻辑。

"X 改坏了，回退它但保留同 commit 里的 Y" 的管线：

    RepositoryNavigator    query → feature + 词表（LLM 可选）
    ChangeIntelligence     词表 → ChangeUnit；keep hint 钉住 commit
    ImpactSlice            问题单元的符号 → 有界波及面
    RollbackPlanner        单元拆分 → 计划 + policy gate + decision
    Verifiers              finding → SUPPORTED / PARTIALLY / UNSUPPORTED

每一步都走 ContextBroker；这里没有任何代码碰 git（读取发生在变更层，
执行从头到尾不存在）。每个 agent 在自己的 ScopedContext 下跑（9K）；
报告携带完整审计轨迹 —— evidence、finding、verdict、decision、policy
结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.agents.change_intel import ChangeIntelligenceAgent, UnitMatch
from src.agents.impact import ImpactSliceAgent, ImpactSlice
from src.agents.navigator import RepositoryNavigator, NavigationResult
from src.agents.rollback import RollbackPlanner, RollbackPlan
from src.agents.scopes import ScopedContext
from src.agents.verifier import DeterministicVerifier, SemanticVerifier
from src.semgraph.objects import Verdict


@dataclass
class FinalReport:
    query: str = ""
    keep_hint: str = ""
    task_id: str = ""
    navigation: NavigationResult | None = None
    problem_units: list[UnitMatch] = field(default_factory=list)
    keep_units: list[UnitMatch] = field(default_factory=list)
    slice: ImpactSlice | None = None
    plan: RollbackPlan | None = None
    verdicts: list[Verdict] = field(default_factory=list)
    scopes: dict[str, ScopedContext] = field(default_factory=dict)

    def dump(self) -> str:
        """有界的人类可读报告（验收演示的输出）。"""
        nav = self.navigation
        lines = [f"# rollback analysis — {self.query!r}",
                 f"keep hint: {self.keep_hint!r}" if self.keep_hint else "",
                 f"target feature: {nav.feature_name if nav else '?'} "
                 f"({nav.feature_id if nav else '?'})", ""]
        lines.append("problem units:")
        for u in self.problem_units:
            lines.append(f"  - {u.unit_id.split(':')[-1]} [{u.label}] "
                         f"commit {u.commit[:8]} files={u.files}")
        lines.append("keep units:")
        for u in self.keep_units:
            lines.append(f"  - {u.unit_id.split(':')[-1]} [{u.label}] "
                         f"commit {u.commit[:8]} files={u.files}")
        if self.slice:
            lines.append(f"impact: routes {self.slice.routes} "
                         f"callers {len(self.slice.caller_ids)} "
                         f"view {self.slice.stats()}")
        if self.plan:
            lines.append("")
            lines.append(self.plan.dump())
        lines.append("")
        for v in self.verdicts:
            lines.append(f"verdict {v.finding_id.split(':')[-1]}: "
                         f"{v.status.value} ({v.verifier})")
        lines.append(f"evidence registered: "
                     f"{sum(len(s.evidence_ids) for s in self.scopes.values())}"
                     if self.scopes else "")
        lines.append("NOTE: nothing was executed — this is a plan, not an action")
        return "\n".join(lines)


class Orchestrator:
    def __init__(self, broker, llm=None):
        self.broker = broker
        self.llm = llm
        self.navigator = RepositoryNavigator(broker, llm)
        self.change_intel = ChangeIntelligenceAgent(broker)
        self.impact = ImpactSliceAgent(broker)
        self.planner = RollbackPlanner(broker)
        self.det_verifier = DeterministicVerifier(broker)
        self.sem_verifier = SemanticVerifier(broker)
        self._n_tasks = 0

    def run(self, query: str, keep_hint: str = "") -> FinalReport:
        self._n_tasks += 1
        task_id = f"task-{self._n_tasks}"
        scopes: dict[str, ScopedContext] = {
            a.ROLE: ScopedContext(role=a.ROLE, task_id=task_id,
                                  reads=a.READS, skills=a.SKILLS)
            for a in (self.navigator, self.change_intel, self.impact,
                      self.planner, self.det_verifier, self.sem_verifier)}
        report = FinalReport(query=query, keep_hint=keep_hint, task_id=task_id,
                             scopes=scopes)

        # 1. 导航：模糊 query → feature + 词表
        nav = self.navigator.find_target(query, scope=scopes[self.navigator.ROLE])
        report.navigation = nav

        # 2. 变更情报：匹配词表的单元；仲裁委托 planner（逻辑真源在
        #    SafeRollbackSkill）：同时命中两套词表的单元归给打分更高的
        #    那套 —— 平局归问题方（嫌疑犯 stays 嫌疑犯）；keep hint 锚定
        #    commit，把问题搜索钉在用户点名的 commit 上。
        keep_nav = (self.navigator.find_target(
            keep_hint, scope=scopes[self.navigator.ROLE]) if keep_hint else None)
        prob_all = self.change_intel.find_units(
            nav.terms, scope=scopes[self.change_intel.ROLE])
        keep_all = (self.change_intel.find_units(
            keep_nav.terms, scope=scopes[self.change_intel.ROLE])
            if keep_nav else [])
        problem_units, keep_units = self.planner.arbitrate(prob_all, keep_all)
        report.problem_units = problem_units
        report.keep_units = keep_units

        # 3. 问题单元符号上的波及面切片（代码层里存在的定义；每次扩展
        #    都带审计）
        targets = []
        for u in problem_units:
            for s in u.symbols:
                node = self.broker.node(f"sym:{s}")
                if node is not None:
                    targets.append(node.id)
        if not targets and nav.related_symbols:
            targets = [t for t in nav.related_symbols if self.broker.node(t)]
        if targets:
            report.slice = self.impact.slice_impact(
                targets, task_id, scope=scopes[self.impact.ROLE])

        # 4. 回退计划 + policy gate + decision（永远不执行）
        report.plan = self.planner.plan(
            [u.unit_id for u in problem_units],
            [u.unit_id for u in keep_units],
            affected_routes=report.slice.routes if report.slice else [],
            task_id=task_id, scope=scopes[self.planner.ROLE])

        # 5. 校验本任务产出的每条 finding（去重：同一 finding 可能被两轮
        #    find_units 注册）。校验本身是一种能力：仅 G4（evidence 层）。
        if not self.broker.layer_active("evidence"):
            report.verdicts = []
            return report
        task_findings = list(dict.fromkeys(
            fid for s in scopes.values() for fid in s.finding_ids))
        for fid in task_findings:
            f = next((x for x in self.broker.all_findings() if x.id == fid),
                     None)
            if f is None:
                continue
            v1 = self.det_verifier.verify(f)
            report.verdicts.append(v1)
            v2 = self.sem_verifier.verify(f)
            if v2 is not None:
                report.verdicts.append(v2)
        return report
