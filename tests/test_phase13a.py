"""Phase 13A：执行层数据模型与状态机。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from src.errors import DataAgentError
from src.maintenance.models import (ExecutionAttempt, ExecutionPlan,
                                    ExecutionStatus, ForbiddenChange,
                                    PlannedChange, RepositorySnapshot)
from src.maintenance.state import AttemptRegistry, transition_allowed


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        task_id="task-1", repo="/tmp/r", base_commit="bbdc659f",
        rollback_units=[{"id": "cu:bbdc659f-U2", "label": "auth",
                         "commit": "bbdc659f", "files": ["src/lib/auth.ts"]}],
        keep_units=[{"id": "cu:bbdc659f-U1", "label": "title",
                     "commit": "bbdc659f", "files": ["src/app/layout.tsx"]}],
        target_files=["src/lib/auth.ts", "src/app/layout.tsx"],
        target_symbols=["validateAccount"],
        expected_changes=[PlannedChange(
            file="src/lib/auth.ts", symbols=["validateAccount"],
            change="REVERT", source_unit="cu:bbdc659f-U2")],
        forbidden_changes=[ForbiddenChange(
            file="src/app/layout.tsx", symbols=[],
            source_unit="cu:bbdc659f-U1")],
        validation_commands=[["pytest", "-q"]],
        risk="medium", policy_result={"action": "HUMAN_REVIEW"},
        evidence_ids=["evid:x"],
        repository_snapshot=RepositorySnapshot(
            head="8edc826", working_tree_clean=True,
            target_file_hashes={"src/lib/auth.ts": "abc"}))


# ---------------------------------------------------------------- 状态机
def test_success_chain_allowed():
    chain = [ExecutionStatus.PLANNED, ExecutionStatus.PREPARED,
             ExecutionStatus.PATCH_BUILT, ExecutionStatus.APPLY_CHECKED,
             ExecutionStatus.APPLIED_SANDBOX, ExecutionStatus.VALIDATED,
             ExecutionStatus.VERIFIED, ExecutionStatus.READY_TO_PROMOTE,
             ExecutionStatus.PROMOTED]
    for cur, nxt in zip(chain, chain[1:]):
        assert transition_allowed(cur, nxt), f"{cur} -> {nxt} 应合法"


def test_backward_and_skip_transitions_rejected():
    for cur, nxt in [(ExecutionStatus.PREPARED, ExecutionStatus.APPLIED_SANDBOX),
                     (ExecutionStatus.VERIFIED, ExecutionStatus.PREPARED),
                     (ExecutionStatus.PLANNED, ExecutionStatus.PATCH_BUILT),
                     (ExecutionStatus.READY_TO_PROMOTE, ExecutionStatus.VERIFIED)]:
        assert not transition_allowed(cur, nxt)


def test_failure_states_are_terminal():
    for failed in (ExecutionStatus.CONFLICT, ExecutionStatus.TEST_FAILED,
                   ExecutionStatus.VERIFICATION_FAILED,
                   ExecutionStatus.STALE_PLAN, ExecutionStatus.POLICY_BLOCKED,
                   ExecutionStatus.PROMOTION_FAILED):
        for nxt in (ExecutionStatus.PREPARED, ExecutionStatus.PATCH_BUILT,
                    ExecutionStatus.VERIFIED):
            assert not transition_allowed(failed, nxt), \
                f"{failed} 是终态，不能迁移到 {nxt}"


# ---------------------------------------------------------------- 注册表
def test_registry_create_update_attach():
    reg = AttemptRegistry()
    a = reg.create(_plan())
    assert a.status == "PLANNED"
    assert a.execution_id.startswith("exec-0001-")
    reg.update_status(a.execution_id, ExecutionStatus.PREPARED, note="worktree ok")
    assert reg.get(a.execution_id).status == "PREPARED"
    assert reg.get(a.execution_id).notes == ["worktree ok"]
    reg.attach(a.execution_id, workspace="/tmp/w", patch_path="/tmp/p")
    assert reg.get(a.execution_id).patch_path == "/tmp/p"
    with pytest.raises(DataAgentError):   # 重复 id
        reg.create(_plan(), execution_id=a.execution_id)
    with pytest.raises(DataAgentError):   # 非法迁移
        reg.update_status(a.execution_id, ExecutionStatus.VERIFIED)
    with pytest.raises(DataAgentError):   # 未知字段
        reg.attach(a.execution_id, no_such_field=1)


# ---------------------------------------------------------------- 序列化
def test_attempt_json_roundtrip():
    a = ExecutionAttempt(execution_id="exec-1", plan=_plan(),
                         status=ExecutionStatus.PREPARED.value,
                         workspace="/tmp/w")
    b = ExecutionAttempt.from_json(a.to_json())
    assert b.execution_id == "exec-1"
    assert b.plan is not None
    assert b.plan.repository_snapshot is not None
    assert b.plan.repository_snapshot.target_file_hashes == {"src/lib/auth.ts": "abc"}
    assert b.plan.expected_changes[0].symbols == ["validateAccount"]
    assert b.plan.forbidden_changes[0].file == "src/app/layout.tsx"
    json.loads(a.to_json())   # 本身就是合法 JSON


def test_snapshot_fingerprint_changes_with_content():
    s1 = RepositorySnapshot(head="a", working_tree_clean=True,
                            target_file_hashes={"f": "1"})
    s2 = RepositorySnapshot(head="a", working_tree_clean=True,
                            target_file_hashes={"f": "1"})
    s3 = RepositorySnapshot(head="b", working_tree_clean=True,
                            target_file_hashes={"f": "1"})
    s4 = RepositorySnapshot(head="a", working_tree_clean=False,
                            target_file_hashes={"f": "1"})
    assert s1.fingerprint() == s2.fingerprint()
    assert s1.fingerprint() != s3.fingerprint()
    assert s1.fingerprint() != s4.fingerprint()


def test_plan_summary_is_bounded():
    s = _plan().summary()
    assert s["base_commit"] == "bbdc659f"[:12]
    assert s["policy_action"] == "HUMAN_REVIEW"
    assert any("REVERT" in e for e in s["expected"])
    assert any("UNCHANGED" in f for f in s["forbidden"])
