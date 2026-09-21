"""Orchestrator (Phase 9J): the only coordinator, no logic of its own.

Pipeline for "X broke, roll it back but keep Y from the same commit":

    RepositoryNavigator    query -> feature + terms (LLM optional)
    ChangeIntelligence     terms -> ChangeUnits; keep hint pins the commit
    ImpactSlice            problem units' symbols -> bounded blast radius
    RollbackPlanner        unit split -> plan + policy gate + decisions
    Verifiers              findings -> SUPPORTED / PARTIALLY / UNSUPPORTED

Every step goes through the ContextBroker; nothing here touches git
(reads happen in the change layer, execution never happens at all).
Each agent runs under its own ScopedContext (9K); the report carries the
audit trail — evidence, findings, verdicts, decisions, policy result.
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
from src.semgraph.schema_v2 import NodeType


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
        """Bounded human-readable report (the acceptance-demo output)."""
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
        return "\n".join(ln for ln in lines if ln is not None)


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
            a.ROLE: ScopedContext(role=a.ROLE, task_id=task_id, reads=a.READS)
            for a in (self.navigator, self.change_intel, self.impact,
                      self.planner, self.det_verifier, self.sem_verifier)}
        report = FinalReport(query=query, keep_hint=keep_hint, task_id=task_id,
                             scopes=scopes)

        # 1. navigate: fuzzy query -> feature + vocabulary
        nav = self.navigator.find_target(query, scope=scopes[self.navigator.ROLE])
        report.navigation = nav

        # 2. change intelligence: units matching the vocabularies. A unit
        #    matching BOTH (e.g. an auth unit whose UI copy contains 系统)
        #    goes to whichever vocabulary scored it higher — ties to the
        #    problem side (a suspect stays suspect). The keep hint then
        #    anchors the commit: "keep Y from the same commit" pins the
        #    problem search to the commits the user named.
        keep_nav = (self.navigator.find_target(
            keep_hint, scope=scopes[self.navigator.ROLE]) if keep_hint else None)
        prob_all = self.change_intel.find_units(
            nav.terms, scope=scopes[self.change_intel.ROLE])
        keep_all = (self.change_intel.find_units(
            keep_nav.terms, scope=scopes[self.change_intel.ROLE])
            if keep_nav else [])
        score_p = {m.unit_id: m for m in prob_all}
        score_k = {m.unit_id: m for m in keep_all}
        keep_units = [m for uid, m in score_k.items()
                      if uid not in score_p or score_k[uid].score > score_p[uid].score]
        keep_ids = {m.unit_id for m in keep_units}
        pin_commits = sorted({m.commit for m in keep_units}) or None
        problem_units = [m for m in prob_all
                         if m.unit_id not in keep_ids
                         and (pin_commits is None or m.commit in pin_commits)]
        report.problem_units = problem_units
        report.keep_units = keep_units

        # 3. impact slice over the problem units' symbols (definitions that
        #    exist in the code layer; each expansion is audited)
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

        # 4. rollback plan + policy gate + decisions (no execution, ever)
        report.plan = self.planner.plan(
            [u.unit_id for u in problem_units],
            [u.unit_id for u in keep_units],
            affected_routes=report.slice.routes if report.slice else [],
            task_id=task_id, scope=scopes[self.planner.ROLE])

        # 5. verify every finding this task produced (deduped: a finding can
        #    be registered from two find_units passes). Verification itself
        #    is a capability: G4 (evidence layer) only.
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
