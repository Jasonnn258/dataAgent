"""Phase 0 tests: CLI skeleton, settings validation, schema contract."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Settings  # noqa: E402
from src.errors import DataAgentError  # noqa: E402
from src.schema import LocateResult, TaskOutput, SystemMetrics  # noqa: E402


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "src.main", *args],
        cwd=ROOT, capture_output=True, text=True,
    )


def test_cli_help():
    r = cli("--help")
    assert r.returncode == 0 and "--mode" in r.stdout


def test_cli_rejects_bad_mode():
    r = cli("--repo", str(ROOT), "--mode", "nope", "--task", "locate", "--query", "x")
    assert r.returncode == 2


def test_settings_rejects_missing_git(tmp_path):
    with pytest.raises(FileNotFoundError):
        Settings(repo=tmp_path, mode="structural_git", task="locate").validate()


def test_settings_rejects_bad_repo():
    with pytest.raises(FileNotFoundError):
        Settings(repo=ROOT / "no_such_dir").validate()


def test_task_output_schema_roundtrip():
    out = TaskOutput(
        task="locate", mode="lexical", query="q", repo="/x",
        result=LocateResult(candidates=[]),
        system=SystemMetrics(mode="lexical", latency_s=0.1),
    )
    data = out.model_dump()
    assert data["task"] == "locate" and data["result"]["task"] == "locate"
    assert "system" in data and data["system"]["tool_call_count"] == 0
