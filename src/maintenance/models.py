"""执行层数据模型（Phase 13A）。

分析/规划产出的 RollbackPlan 在这里被翻译成可执行、可验证、可审计的
ExecutionPlan；执行过程的每一步结构化产物（snapshot / patch / validation /
verification）都落在 ExecutionAttempt 字段里 —— **不只返回 bool**。

关键设计：
- repository_snapshot 是 stale 检测的基准：plan 生成时刻的仓库状态指纹
  （HEAD / 工作树是否干净 / 目标文件哈希）。执行前重拍一次对比，不一致
  => STALE_PLAN，绝不停留在旧计划上继续执行。
- expected_changes / forbidden_changes 是 Verifier 的合同：什么必须变化、
  什么绝对不能变化。计划外的任何文件修改都算越界（scope 检查）。
- validation_commands 只接受 argv list（["pytest", "-q"]），绝不接受 shell
  字符串；来源是 repo config 或调用方显式传入，不是 LLM 生成。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum

from src.semgraph.schema_v2 import stable_id


# ---------------------------------------------------------------- 计划侧

@dataclass
class PlannedChange:
    """expected_changes 元素：这个文件/符号必须发生的变化。"""
    file: str
    symbols: list[str] = field(default_factory=list)  # 短名（file::name → name）
    change: str = "REVERT"                             # 第一版只有 REVERT
    source_unit: str = ""                              # 来源 cu: 节点 id

    def describe(self) -> str:
        sym = f"::{', '.join(self.symbols)}" if self.symbols else ""
        return f"{self.file}{sym} <- {self.change} ({self.source_unit})"


@dataclass
class ForbiddenChange:
    """forbidden_changes 元素：必须保持不变的东西（keep 侧合同）。"""
    file: str
    symbols: list[str] = field(default_factory=list)
    must: str = "UNCHANGED"
    source_unit: str = ""

    def describe(self) -> str:
        sym = f"::{', '.join(self.symbols)}" if self.symbols else ""
        return f"{self.file}{sym} == {self.must} ({self.source_unit})"


@dataclass
class RepositorySnapshot:
    """仓库状态指纹（spec 13A 最小集 + porcelain 有界快照）。

    stale 检测用：prepare 阶段重拍一次，与 plan 里这份逐项对比。
    tracked_state 是 `git status --porcelain` 的排序快照（截断到 200 行）
    —— 它同时覆盖 staged 与 unstaged 的 tracked 变更。
    """
    head: str = ""
    working_tree_clean: bool = True
    target_file_hashes: dict[str, str] = field(default_factory=dict)
    tracked_state: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """整份指纹的确定性 id（对比两个快照是否一致）。"""
        return stable_id(self.head, str(self.working_tree_clean),
                         json.dumps(self.target_file_hashes, sort_keys=True),
                         json.dumps(sorted(self.tracked_state)))


# ---------------------------------------------------------------- 状态

class ExecutionStatus(str, Enum):
    """执行状态机（迁移合法性见 state.py）。"""
    PLANNED = "PLANNED"
    PREPARED = "PREPARED"
    PATCH_BUILT = "PATCH_BUILT"
    APPLY_CHECKED = "APPLY_CHECKED"
    APPLIED_SANDBOX = "APPLIED_SANDBOX"
    VALIDATED = "VALIDATED"
    VERIFIED = "VERIFIED"
    READY_TO_PROMOTE = "READY_TO_PROMOTE"
    PROMOTED = "PROMOTED"
    # ---- 失败态（终态，不可翻回成功态） ----
    CONFLICT = "CONFLICT"
    TEST_FAILED = "TEST_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    STALE_PLAN = "STALE_PLAN"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    PROMOTION_FAILED = "PROMOTION_FAILED"


FAILURE_STATES: frozenset[ExecutionStatus] = frozenset({
    ExecutionStatus.CONFLICT, ExecutionStatus.TEST_FAILED,
    ExecutionStatus.VERIFICATION_FAILED, ExecutionStatus.STALE_PLAN,
    ExecutionStatus.POLICY_BLOCKED, ExecutionStatus.PROMOTION_FAILED,
})


# ---------------------------------------------------------------- 计划

@dataclass
class ExecutionPlan:
    """结构化执行计划（Phase 13B 由 RollbackPlan 翻译而来）。

    base_commit 是 plan 生成时刻的 HEAD —— sandbox worktree 检出到它，
    promote 前要重新核对它没变。
    """
    task_id: str = ""
    repo: str = ""
    base_commit: str = ""
    rollback_units: list[dict] = field(default_factory=list)   # {id,label,commit,files,symbols}
    keep_units: list[dict] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    target_symbols: list[str] = field(default_factory=list)
    expected_changes: list[PlannedChange] = field(default_factory=list)
    forbidden_changes: list[ForbiddenChange] = field(default_factory=list)
    validation_commands: list[list[str]] = field(default_factory=list)  # argv list
    risk: str = "medium"
    policy_result: dict = field(default_factory=dict)   # pre-execution 裁决
    evidence_ids: list[str] = field(default_factory=list)
    repository_snapshot: RepositorySnapshot | None = None
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def summary(self) -> dict:
        """有界摘要（报告/审计用，不放大载荷）。"""
        return {
            "task_id": self.task_id, "base_commit": self.base_commit[:12],
            "rollback_units": [u.get("id", "") for u in self.rollback_units],
            "keep_units": [u.get("id", "") for u in self.keep_units],
            "expected": [c.describe() for c in self.expected_changes],
            "forbidden": [c.describe() for c in self.forbidden_changes],
            "validation_commands": self.validation_commands,
            "risk": self.risk, "policy_action": self.policy_result.get("action", ""),
            "snapshot_head": (self.repository_snapshot.head[:12]
                              if self.repository_snapshot else ""),
        }


# ---------------------------------------------------------------- 补丁

@dataclass
class PatchArtifact:
    """一次补丁构建的结构化产物（13D：proposed.patch 的身份证）。

    sha256 是落盘 patch 文件的哈希 —— 之后任何环节（apply / promote）
    拿到的 patch 都必须能对上它，对不上就是"patch 被动过"。
    """
    path: str                              # proposed.patch 绝对路径
    base_commit: str = ""                  # 构建基准（plan.base_commit）
    affected_files: list[str] = field(default_factory=list)
    affected_symbols: list[str] = field(default_factory=list)
    source_change_units: list[str] = field(default_factory=list)  # cu id
    sha256: str = ""
    hunks_total: int = 0                   # 补丁里 hunk 总数
    evidence_ids: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"path": self.path, "base_commit": self.base_commit[:12],
                "affected_files": self.affected_files,
                "affected_symbols": self.affected_symbols,
                "source_change_units": self.source_change_units,
                "sha256": self.sha256[:12], "hunks_total": self.hunks_total}


# ---------------------------------------------------------------- 验证

@dataclass
class ValidationResult:
    """一条验证命令的结构化结果（13F；不是 bool）。

    status ∈ PASSED | FAILED | TIMEOUT | ERROR | BLOCKED：
    - BLOCKED 是可执行文件策略拒绝（没跑过，exit_code 为 None）
    - TIMEOUT/ERROR 是运行环境问题，与 FAILED（命令自己判失败）分开
    """
    command: list[str] = field(default_factory=list)   # argv 原样
    exit_code: int | None = None
    duration: float = 0.0                              # 秒
    stdout_summary: str = ""                           # 有界摘要
    stderr_summary: str = ""
    status: str = "NOT_RUN"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 3)
        d["stdout_summary"] = self.stdout_summary[:2000]
        d["stderr_summary"] = self.stderr_summary[:2000]
        return d


# ---------------------------------------------------------------- 核验

@dataclass
class ExecutionVerification:
    """执行核验的结构化裁决（13G；Verdict，不是日志）。

    五个面 + overall：
    - expected_change_pass：合同里"必须变"的都真变了（回退单元被反转）
    - preservation_pass：合同里"绝不能变"的都原样（keep 侧无损）
    - scope_pass：改动 ⊆ 计划（无越界文件/额外内容）
    - tests_pass：验证命令全过且至少跑了一条
    - evidence_pass：patch/证据/快照链完整可回看

    overall：VERIFIED（全过）/ PARTIAL（硬面过、软面缺：没跑测试、
    证据不全）/ FAILED（硬面破：forbidden 被动、越界、回退缺失）。
    PARTIAL 不推进状态 —— 没 evidence/没测试就不许往 promote 走。
    """
    expected_change_pass: bool = False
    preservation_pass: bool = False
    scope_pass: bool = False
    tests_pass: bool = False
    evidence_pass: bool = False
    overall: str = "FAILED"
    checks: list[dict] = field(default_factory=list)   # 逐项明细

    def to_dict(self) -> dict:
        return {"expected_change_pass": self.expected_change_pass,
                "preservation_pass": self.preservation_pass,
                "scope_pass": self.scope_pass,
                "tests_pass": self.tests_pass,
                "evidence_pass": self.evidence_pass,
                "overall": self.overall, "checks": self.checks}


# ---------------------------------------------------------------- 尝试

@dataclass
class ExecutionAttempt:
    """一次执行尝试的全量记录（审计单位，不是日志行）。"""
    execution_id: str
    task_id: str = ""
    plan: ExecutionPlan | None = None
    status: str = ExecutionStatus.PLANNED.value
    workspace: str = ""                 # sandbox worktree 绝对路径
    patch_path: str = ""                # proposed.patch
    actual_patch_path: str = ""         # actual.patch（沙箱 apply 后的真实 diff，13E）
    verified_patch_path: str = ""       # sandbox 验证通过的 patch（13J 用）
    reverse_patch_path: str = ""        # 显式撤销用（13J 生成）
    validation_results: list[dict] = field(default_factory=list)   # 13F 填充
    verification_results: list[dict] = field(default_factory=list) # 13G 填充
    evidence_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)
    trace_id: str = ""                  # 关联 ExecutionRecorder 事件
    notes: list[str] = field(default_factory=list)   # 状态迁移附注（有界）
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def to_json(self) -> str:
        """attempt 落盘形态（.dataagent/runs/<id>/attempt.json）。"""
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False, indent=1, default=str)

    @classmethod
    def from_json(cls, text: str) -> "ExecutionAttempt":
        d = json.loads(text)
        plan = d.pop("plan", None)
        snap = (plan or {}).pop("repository_snapshot", None)
        attempt = cls(**d)
        if plan is not None:
            plan["repository_snapshot"] = (
                RepositorySnapshot(**snap) if snap else None)
            plan["expected_changes"] = [
                PlannedChange(**c) for c in plan.get("expected_changes", [])]
            plan["forbidden_changes"] = [
                ForbiddenChange(**c) for c in plan.get("forbidden_changes", [])]
            attempt.plan = ExecutionPlan(**plan)
        return attempt
