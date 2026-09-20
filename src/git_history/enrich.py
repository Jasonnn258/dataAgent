"""Git-history enrichment (mode>=structural_git).

locate: commits whose message matches query terms vote for the files they
touched; file churn/recency adds softer evidence.
impact: co-change files of the target's module + last-change commit of the
target symbol (blame at definition lines).
"""
from __future__ import annotations

from pathlib import Path

from src.errors import GitError
from src.git_history.api import GitAPI
from src.schema import (Evidence, ImpactResult, LocatedCandidate, ToolRecorder)
from src.search.keywords import QueryTerms

GIT_MSG_BOOST = 4.0
GIT_COOCCUR_BOOST = 1.5
MAX_COMMITS_SCAN = 100


def _api(repo: Path, rec: ToolRecorder) -> GitAPI | None:
    api = GitAPI(repo, rec)
    if not api.is_repo():
        rec.warn(f"{repo} is not a git repository — git layer skipped")
        return None
    return api


# ---------------------------------------------------------------- locate
def enrich_locate_with_git(repo: Path, cands: list[LocatedCandidate],
                           qt: QueryTerms, rec: ToolRecorder) -> list[LocatedCandidate]:
    api = _api(repo, rec)
    if api is None:
        return cands
    terms = [t.lower() for t, _ in qt.all_search_terms()] + \
            [s.lower() for s in qt.cjk_segments + qt.cjk_subterms]

    try:
        commits = api.log(max_count=MAX_COMMITS_SCAN)
    except GitError as e:
        rec.warn(f"git log unavailable: {e}")
        return cands

    # message-matching commits vote for their touched files
    votes: dict[str, list[str]] = {}
    for c in commits:
        msg = (c.subject + " " + c.body).lower()
        matched = [t for t in terms if len(t) >= 2 and t in msg]
        if not matched:
            continue
        for f in c.files:
            if _is_code(f):
                votes.setdefault(f, []).append(f"{c.short} {c.subject[:60]} (matched {','.join(matched[:3])})")

    votes_same_commit: dict[str, set[str]] = {}
    for c in commits:
        fs = {f for f in c.files if _is_code(f)}
        for f in fs:
            votes_same_commit.setdefault(f, set()).update(fs - {f})

    for c in cands:
        if c.file in votes:
            reasons = votes[c.file]
            c.score = round(c.score + GIT_MSG_BOOST + 0.5 * (len(reasons) - 1), 3)
            c.reason += f" | git: {len(reasons)} commit(s) match query terms"
            c.evidence.append(Evidence(kind="git_log", source=reasons[0].split(" ")[0],
                                       detail=reasons[0], snippet="; ".join(reasons[:3])))
        # files that frequently change *together with* a query-voted file
        partners = votes_same_commit.get(c.file, set())
        voted_partners = partners & votes.keys()
        if voted_partners:
            c.score = round(c.score + GIT_COOCCUR_BOOST, 3)
            c.reason += f" | git co-change with {sorted(voted_partners)[:2]}"
            c.evidence.append(Evidence(kind="co_change", source=c.file,
                                       detail=f"changes together with {', '.join(sorted(voted_partners)[:3])}"))
    return cands


def _is_code(path: str) -> bool:
    return path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))


# ---------------------------------------------------------------- impact
def enrich_impact_with_git(repo: Path, res: ImpactResult, rec: ToolRecorder,
                           target_file: str | None = None,
                           target_lines: tuple[int, int] | None = None) -> ImpactResult:
    api = _api(repo, rec)
    if api is None:
        return res
    try:
        if target_file and target_lines:
            blame = api.blame(target_file, target_lines)
            if blame:
                shas = sorted(set(blame.values()))
                commits = {c.sha: c for c in api.log(max_count=MAX_COMMITS_SCAN)}
                for sha in shas[:3]:
                    c = commits.get(sha)
                    if c and res.target is not None:
                        res.target.evidence.append(Evidence(
                            kind="git_blame", source=c.short,
                            detail=f"target lines last changed by '{c.subject[:70]}'"))
        if target_file:
            pairs = api.co_change_pairs(max_count=MAX_COMMITS_SCAN)
            from src.schema import ImpactSide
            for (a, b), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
                if target_file in (a, b) and n >= 2:
                    other = b if a == target_file else a
                    res.indirectly_affected.append(ImpactSide(
                        file=other, kind="file", relation="co-change",
                        via=f"changed together {n}x with {target_file}",
                        evidence=[Evidence(kind="co_change", source=f"{a}+{b}",
                                           detail=f"co-change support {n}")]))
                    if len([x for x in res.indirectly_affected if x.relation == "co-change"]) >= 5:
                        break
    except GitError as e:
        rec.warn(f"git impact enrichment failed: {e}")
    return res
