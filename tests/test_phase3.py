"""Phase 3 tests: read-only git API + git enrichment layers."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.config import Settings  # noqa: E402
from src.errors import GitError  # noqa: E402
from src.git_history.api import GitAPI  # noqa: E402
from src.schema import ToolRecorder  # noqa: E402
from src.tasks.locate import run_locate  # noqa: E402
from src.tasks.impact import run_impact  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(scope="module")
def api(seeded):
    return GitAPI(FIXTURE, ToolRecorder())


def test_git_whitelist_blocks_writes(api):
    for bad in (["reset", "--hard"], ["commit", "-m", "x"], ["push"], ["checkout", "--", "f"]):
        with pytest.raises(GitError):
            api._run(bad)


def test_log_parses_all_commits(api):
    cs = api.log()
    assert len(cs) == 5
    assert cs[0].subject == "refactor: extract input format check into validate lib"
    mixed = next(c for c in cs if "reword system title" in c.subject)
    assert set(mixed.files) == {"src/app/layout.tsx", "src/app/login/page.tsx", "src/lib/auth.ts"}


def mixed_sha(api) -> str:
    return next(c.sha for c in api.log() if "reword system title" in c.subject)


def test_diff_mixed_commit_hunks(api):
    sha = mixed_sha(api)
    d = api.diff(sha)
    by_file = {}
    for h in d.hunks:
        by_file.setdefault(h.file, []).append(h)
    assert set(by_file) == {"src/app/layout.tsx", "src/app/login/page.tsx", "src/lib/auth.ts"}
    # each file has exactly one hunk with both + and - lines
    for file, hs in by_file.items():
        assert len(hs) == 1
        tags = {t for t, _ in hs[0].lines}
        assert "+" in tags and "-" in tags


def test_blame_finds_regression_line(api):
    blame = api.blame("src/lib/auth.ts")
    reg = mixed_sha(api)
    lines_from_mixed = [ln for ln, sha in blame.items() if sha == reg]
    assert lines_from_mixed == [15]  # the `>= 8` validation line


def test_co_change_pairs(api):
    pairs = api.co_change_pairs()
    assert pairs.get(("src/app/layout.tsx", "src/lib/auth.ts"), 0) >= 2  # init + mixed


# ---------------------------------------------------------------- e2e
def test_locate_structural_git_gets_git_evidence(seeded):
    s = Settings(repo=FIXTURE, mode="structural_git", task="locate", top_k=10, no_llm=True)
    out = run_locate(s, "登录验证的逻辑在哪里")
    files = [c.file for c in out.result.candidates]
    assert any("auth" in f for f in files[:6])
    assert "src/lib/auth.ts" in files, "filename-only match must still yield a candidate"
    with_git = [c for c in out.result.candidates
                if any(e.kind in ("git_log", "co_change") for e in c.evidence)]
    assert with_git, "git layer should attach evidence in structural_git mode"
    assert any(t.startswith("git:") for t in out.system.tool_calls)


def test_impact_structural_git_cochange(seeded):
    s = Settings(repo=FIXTURE, mode="structural_git", task="impact", no_llm=True)
    out = run_impact(s, "generateWithRetry")
    assert out.result.target.file == "src/lib/retry.ts"
    assert any(i.relation == "co-change" for i in out.result.indirectly_affected) or \
        any(e.kind == "git_blame" for e in out.result.target.evidence)


def test_locate_mode_requires_git(tmp_path):
    s = Settings(repo=tmp_path, mode="structural_git", task="locate")
    with pytest.raises(FileNotFoundError):
        s.validate()
