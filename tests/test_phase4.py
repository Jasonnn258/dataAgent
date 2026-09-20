"""Phase 4 tests: change-unit splitting + rollback recommendation."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.config import Settings  # noqa: E402
from src.errors import DataAgentError  # noqa: E402
from src.schema import TaskOutput  # noqa: E402
from src.tasks.rollback import run_rollback  # noqa: E402

Q_MIXED = "登录逻辑昨天改坏了，需要撤销，但保留同一次提交中的标题修改"


@pytest.fixture(scope="module", autouse=True)
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


def mixed_ref() -> str:
    from src.git_history.api import GitAPI
    from src.schema import ToolRecorder
    api = GitAPI(FIXTURE, ToolRecorder())
    return next(c.sha for c in api.log() if "reword system title" in c.subject)


def validate_ref() -> str:
    from src.git_history.api import GitAPI
    from src.schema import ToolRecorder
    api = GitAPI(FIXTURE, ToolRecorder())
    return next(c.sha for c in api.log() if "format check" in c.subject)


def _run(mode: str, query: str, ref: str) -> TaskOutput:
    s = Settings(repo=FIXTURE, mode=mode, task="rollback", no_llm=True)
    return run_rollback(s, query, ref)


# ------------------------------------------------------------- mixed commit
@pytest.mark.parametrize("mode", ["lexical", "structural", "structural_git"])
def test_mixed_commit_splits_and_classifies(mode):
    out = _run(mode, Q_MIXED, mixed_ref())
    r = out.result
    assert len(r.change_units) == 2, [u.semantic_label for u in r.change_units]
    rb = [u for u in r.change_units if u.unit_id in r.changes_to_rollback]
    keep = [u for u in r.change_units if u.unit_id in r.changes_to_keep]
    assert len(rb) == 1 and rb[0].semantic_label == "auth"
    assert set(rb[0].files) == {"src/app/login/page.tsx", "src/lib/auth.ts"}
    assert len(keep) == 1 and keep[0].semantic_label == "title"
    assert keep[0].files == ["src/app/layout.tsx"]
    assert r.collateral_damage_risk == 0.0  # clean file-level separation
    assert r.affected_files == ["src/app/login/page.tsx", "src/lib/auth.ts"]


def test_rollback_output_is_analysis_only():
    out = _run("structural", Q_MIXED, mixed_ref())
    hints = out.result.operations_hint
    assert hints and "ANALYSIS ONLY" in hints[0]
    assert not any("git revert" == h.split()[0] and "--execute" in h for h in hints)


# ------------------------------------------------------------- harder case (c5)
def test_structural_binds_callgraph_for_attribution():
    """validate.ts has no auth tokens; only the call graph ties it to login."""
    out = _run("structural", "登录输入校验改坏了，撤销这次修改", validate_ref())
    rb_files = {f for u in out.result.change_units
                if u.unit_id in out.result.changes_to_rollback for f in u.files}
    assert "src/lib/validate.ts" in rb_files
    assert "src/app/api/auth/login/route.ts" in rb_files


def test_lexical_misses_unlabelled_hunk_on_c5():
    """lexical cannot see the binding — validate.ts hunk stays unmatched."""
    out = _run("lexical", "登录输入校验改坏了，撤销这次修改", validate_ref())
    rb_files = {f for u in out.result.change_units
                if u.unit_id in out.result.changes_to_rollback for f in u.files}
    assert "src/lib/validate.ts" not in rb_files  # the ablation gap, by design


# ------------------------------------------------------------- robustness
def test_rollback_requires_git_repo(tmp_path):
    s = Settings(repo=tmp_path, mode="structural_git", task="rollback")
    with pytest.raises((FileNotFoundError, DataAgentError)):
        s.validate()
        run_rollback(s, "x", "HEAD")


def test_rollback_default_head(seeded):
    s = Settings(repo=FIXTURE, mode="structural", task="rollback", no_llm=True)
    out = run_rollback(s, "撤销格式校验的修改", None)
    assert out.result.commit  # defaults to HEAD analysis
