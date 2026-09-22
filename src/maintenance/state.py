"""执行状态机（Phase 13A）：状态迁移合法性的唯一真源。

规则：
- 只能沿成功链或转失败态前进，绝不后退（VERIFIED 不能回到 PREPARED）
- 失败态是终态：CONFLICT 之后不存在"再试一次就好"的静默路径 ——
  重试 = 新的 ExecutionAttempt，旧的留档
- STALE_PLAN / POLICY_BLOCKED 在每个阶段都可达（计划和现实随时可能
  分岔；门在任何时候都可以拦）
"""
from __future__ import annotations

import time

from src.errors import DataAgentError
from src.maintenance.models import ExecutionAttempt, ExecutionPlan, ExecutionStatus

# 成功链（spec 13A 的顺序）
TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.PLANNED: frozenset({
        ExecutionStatus.PREPARED, ExecutionStatus.STALE_PLAN,
        ExecutionStatus.POLICY_BLOCKED}),
    ExecutionStatus.PREPARED: frozenset({
        ExecutionStatus.PATCH_BUILT, ExecutionStatus.CONFLICT,
        ExecutionStatus.STALE_PLAN, ExecutionStatus.POLICY_BLOCKED}),
    ExecutionStatus.PATCH_BUILT: frozenset({
        ExecutionStatus.APPLY_CHECKED, ExecutionStatus.CONFLICT,
        ExecutionStatus.STALE_PLAN, ExecutionStatus.POLICY_BLOCKED}),
    ExecutionStatus.APPLY_CHECKED: frozenset({
        ExecutionStatus.APPLIED_SANDBOX, ExecutionStatus.CONFLICT,
        ExecutionStatus.STALE_PLAN, ExecutionStatus.POLICY_BLOCKED}),
    ExecutionStatus.APPLIED_SANDBOX: frozenset({
        ExecutionStatus.VALIDATED, ExecutionStatus.TEST_FAILED,
        ExecutionStatus.VERIFICATION_FAILED, ExecutionStatus.STALE_PLAN,
        ExecutionStatus.POLICY_BLOCKED}),
    ExecutionStatus.VALIDATED: frozenset({
        ExecutionStatus.VERIFIED, ExecutionStatus.TEST_FAILED,
        ExecutionStatus.VERIFICATION_FAILED, ExecutionStatus.STALE_PLAN,
        ExecutionStatus.POLICY_BLOCKED}),
    ExecutionStatus.VERIFIED: frozenset({
        ExecutionStatus.READY_TO_PROMOTE, ExecutionStatus.STALE_PLAN,
        ExecutionStatus.POLICY_BLOCKED}),
    ExecutionStatus.READY_TO_PROMOTE: frozenset({
        ExecutionStatus.PROMOTED, ExecutionStatus.PROMOTION_FAILED,
        ExecutionStatus.STALE_PLAN, ExecutionStatus.POLICY_BLOCKED}),
    # 终态：成功链尽头与全部失败态都没有出边
    ExecutionStatus.PROMOTED: frozenset(),
}


def transition_allowed(current: ExecutionStatus,
                       nxt: ExecutionStatus) -> bool:
    return nxt in TRANSITIONS.get(current, frozenset())


def _coerce(status) -> ExecutionStatus:
    if isinstance(status, ExecutionStatus):
        return status
    try:
        return ExecutionStatus(status)
    except ValueError:
        raise DataAgentError(f"unknown execution status {status!r}") from None


class AttemptRegistry:
    """ExecutionAttempt 注册表（进程内；attempt.json 落盘由 WorkspaceService 负责）。"""

    def __init__(self) -> None:
        self._attempts: dict[str, ExecutionAttempt] = {}

    def create(self, plan: ExecutionPlan,
               execution_id: str = "") -> ExecutionAttempt:
        """登记一次新尝试（PLANNED）。execution_id 缺省自动生成。"""
        eid = execution_id or self._next_id(plan)
        if eid in self._attempts:
            raise DataAgentError(f"duplicate execution_id {eid!r}")
        attempt = ExecutionAttempt(execution_id=eid, task_id=plan.task_id,
                                   plan=plan,
                                   status=ExecutionStatus.PLANNED.value)
        self._attempts[eid] = attempt
        return attempt

    def _next_id(self, plan: ExecutionPlan) -> str:
        n = len(self._attempts) + 1
        base = plan.base_commit[:8] or "nocommit"
        return f"exec-{n:04d}-{base}"

    def get(self, execution_id: str) -> ExecutionAttempt:
        attempt = self._attempts.get(execution_id)
        if attempt is None:
            raise DataAgentError(f"unknown execution attempt {execution_id!r}")
        return attempt

    def update_status(self, execution_id: str, new_status,
                      note: str = "") -> ExecutionAttempt:
        """带守卫的状态迁移：非法迁移就地大声报错。"""
        attempt = self.get(execution_id)
        cur = _coerce(attempt.status)
        nxt = _coerce(new_status)
        if not transition_allowed(cur, nxt):
            raise DataAgentError(
                f"illegal execution transition {execution_id}: "
                f"{cur.value} -> {nxt.value}")
        attempt.status = nxt.value
        attempt.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if note:
            attempt.notes.append(note[:300])   # 有界
        return attempt

    def attach(self, execution_id: str, **fields) -> ExecutionAttempt:
        """填 attempt 的产物字段（workspace/patch_path/...）。状态不动。"""
        attempt = self.get(execution_id)
        for k, v in fields.items():
            if not hasattr(attempt, k):
                raise DataAgentError(f"ExecutionAttempt has no field {k!r}")
            setattr(attempt, k, v)
        attempt.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return attempt

    def all_attempts(self) -> list[ExecutionAttempt]:
        return list(self._attempts.values())
