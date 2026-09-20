"""Task 3: safe git rollback analysis. READ-ONLY — analysis and suggestions
only; no revert/reset/checkout is ever executed (工程要求 #9).

Decision pipeline (deterministic; optional LLM refinement):
  query → problem clause / keep clause (split on 保留/保持/但)
        → domain matching against ChangeUnit labels & evidence
        → rollback units / keep units / risk assessment / git operations hint
"""
from __future__ import annotations

from pathlib import Path

from src.change_units.units import (DOMAIN_SIGNALS, characterize_hunks,
                                    cluster_into_units)
from src.config import Settings
from src.errors import DataAgentError, GitError
from src.git_history.api import GitAPI
from src.schema import (Evidence, RollbackResult, TaskOutput)
from src.search.keywords import extract_terms
from src.tasks import register
from src.tasks.base import TaskRunnerBase

KEEP_MARKERS = ["保留", "保持", "不动", "留下", "不影响", "keep"]


@register("rollback")
def run_rollback(settings: Settings, query: str, commit: str | None = None) -> TaskOutput:
    return RollbackRunner(settings).run(query, commit or "HEAD")


class RollbackRunner(TaskRunnerBase):
    task = "rollback"

    def run(self, query: str, ref: str) -> TaskOutput:
        api = GitAPI(self.repo, self.rec)
        if not api.is_repo():
            raise DataAgentError(f"rollback needs a git repo; {self.repo} is not one")
        try:
            sha = api.resolve(ref)
            diff = api.diff(sha)
        except GitError as e:
            raise DataAgentError(f"cannot analyze commit {ref!r}: {e}") from e

        idx = None
        if self.structural_enabled():
            from src.structural.enrich import build_index
            idx = build_index(self.repo, self.rec)

        infos = characterize_hunks(diff, idx, self.rec)
        units = cluster_into_units(sha, infos, idx, self.rec)
        commit_subject = api.log(max_count=10)
        subj = next((c.subject for c in commit_subject if c.sha == sha), "")

        problem_units, keep_units, unmatched = self._classify(query, units)
        risk, factors = self._collateral_risk(problem_units, keep_units, unmatched, idx)

        result = RollbackResult(
            commit=sha,
            query_interpretation=self._interp,
            change_units=units,
            suspected_problem_changes=[u.unit_id for u in problem_units],
            changes_to_rollback=[u.unit_id for u in problem_units],
            changes_to_keep=[u.unit_id for u in keep_units + unmatched],
            affected_files=sorted({f for u in problem_units for f in u.files}),
            collateral_damage_risk=risk,
            risk_factors=factors,
            evidence=self._decision_evidence(problem_units, keep_units),
            operations_hint=self._operations_hint(problem_units, keep_units, sha),
        )
        result.query_interpretation += f" | commit[{sha[:8]} {subj[:60]}]"

        if self.graph_enabled():
            from src.semgraph.enrich import enrich_rollback_with_graph
            enrich_rollback_with_graph(self.repo, result, self.rec)

        return TaskOutput(
            task="rollback", mode=self.s.mode, query=query, repo=str(self.repo),
            commit=sha, result=result,
            system=self.rec.metrics(self.s.mode, llm_used=False),
        )

    # ------------------------------------------------------------ intent
    _interp: str = ""

    def _classify(self, query: str, units):
        problem_text, keep_text = self._split_query(query)
        self._interp = (f"rollback-domain[{self._domains_of(problem_text)}] "
                        f"keep-domain[{self._domains_of(keep_text) or 'unspecified'}]")
        problem_domains = self._domains_of(problem_text)
        keep_domains = self._domains_of(keep_text)

        problem, keep, unmatched = [], [], []
        for u in units:
            udoms = self._unit_domains(u)
            if problem_domains & udoms:
                problem.append(u)
            elif keep_domains & udoms:
                keep.append(u)
            elif not problem_domains:
                unmatched.append(u)  # no explicit problem -> nothing matched; stay safe
            else:
                unmatched.append(u)
        return problem, keep, unmatched

    def _split_query(self, query: str) -> tuple[str, str]:
        """Split into (problem_clause, keep_clause) on 保留-type markers."""
        best = None
        for m in KEEP_MARKERS:
            i = query.find(m)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, m)
        if best is None:
            return query, ""
        i, m = best
        return query[:i], query[i + len(m):]

    @staticmethod
    def _domains_of(text: str) -> set[str]:
        if not text.strip():
            return set()
        qt = extract_terms(text)
        blob = " ".join([text] + [t for t, _ in qt.all_search_terms()]).lower()
        doms = set()
        for dom, triggers in DOMAIN_SIGNALS.items():
            for trig in triggers:
                if trig.lower() in blob:
                    doms.add(dom)
                    break
        return doms

    def _unit_domains(self, u) -> set[str]:
        doms = {u.semantic_label} if u.semantic_label != "mixed" else set()
        blob = " ".join([*u.files, *u.symbols, *u.ui_strings,
                         *(e.snippet for e in u.evidence)]).lower()
        for dom, triggers in DOMAIN_SIGNALS.items():
            if any(t in blob for t in triggers):
                doms.add(dom)
        return doms

    # ------------------------------------------------------------ risk
    def _collateral_risk(self, problem_units, keep_units, unmatched, idx) -> tuple[float, list[str]]:
        risk, factors = 0.0, []
        pf = {f for u in problem_units for f in u.files}
        kf = {f for u in keep_units + unmatched for f in u.files}
        shared = pf & kf
        if shared:
            risk += 0.35
            factors.append(f"same file holds rollback and keep changes -> hunk-level surgery needed: {sorted(shared)}")
        if idx is not None:
            for u in problem_units:
                for q in u.symbols:
                    name = q.split("::")[-1].split(".")[0]
                    for sym in idx.symbols_by_name.get(name, []):
                        for e in idx.callers_of(sym):
                            if e.caller_file in kf:
                                risk += 0.15
                                factors.append(f"kept files call rolled-back symbol {name} ({e.caller_file}:{e.line})")
                                break
        if not problem_units:
            risk = min(1.0, risk + 0.2)
            factors.append("no change unit matched the problem description — recommendation is uncertain")
        if unmatched and problem_units:
            factors.append(f"{len(unmatched)} unit(s) matched neither clause; kept by default (conservative)")
        return round(min(risk, 1.0), 3), factors

    def _decision_evidence(self, problem_units, keep_units) -> list[Evidence]:
        evs = []
        for u in problem_units:
            evs.append(Evidence(kind="rollback_match", source=u.unit_id,
                                detail=f"label={u.semantic_label}; {u.summary[:120]}",
                                snippet=(u.evidence[0].snippet if u.evidence else "")[:300]))
        for u in keep_units:
            evs.append(Evidence(kind="keep_match", source=u.unit_id,
                                detail=f"label={u.semantic_label}; {u.summary[:120]}"))
        return evs[:10]

    def _operations_hint(self, problem_units, keep_units, sha: str) -> list[str]:
        """Suggested (NEVER executed) git workflows."""
        if not problem_units:
            return ["(no rollback recommendation — inspect units manually)"]
        pf = sorted({f for u in problem_units for f in u.files})
        whole_files_clean = not ({f for u in keep_units for f in u.files} &
                                 {f for u in problem_units for f in u.files})
        hints = [f"# ANALYSIS ONLY — nothing below was executed. commit={sha[:10]}"]
        if whole_files_clean:
            hints.append(f"git revert --no-commit {sha[:10]}   # then unstage keep-files:")
            keep_files = sorted({f for u in keep_units for f in u.files})
            for f in keep_files:
                hints.append(f"git restore --staged --worktree {f}")
            hints.append(f"# rollback payload = {', '.join(pf)}")
        else:
            hints.append("hunk-level surgery required (rollback and keep share a file):")
            hints.append(f"git revert --no-commit {sha[:10]} && git reset   # start from full revert")
            hints.append("git checkout -p HEAD~1 -- <shared file>   # interactively re-apply keep hunks")
        hints.append("run tests after rollback; verify with git diff --staged")
        return hints
