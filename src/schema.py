"""Unified output schema — every mode emits exactly these models as JSON.

This is the contract that makes lexical/structural/structural_git/semantica
comparable in the evaluation pipeline (消融实验).

Design notes:
- TaskContext / Target / Evidence / AnalysisResult are the four shared
  abstractions requested for the future agent cluster; they live here so
  Phase 1..5 code and later agents speak the same language.
- Every claim a mode makes must carry Evidence with provenance
  (file:line, commit sha, graph query...). No evidence → don't emit it.
"""
from __future__ import annotations

import time
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from src.config import MAX_CANDIDATES, VALID_MODES, VALID_TASKS

Mode = Literal["lexical", "structural", "structural_git", "semantica"]
TaskType = Literal["locate", "impact", "rollback"]


# ---------------------------------------------------------------- evidence

class Evidence(BaseModel):
    """A single verifiable fact backing a result (provenance unit)."""
    kind: str  # lexical_match | symbol_def | call_edge | import_edge | git_log |
               # git_blame | co_change | graph_edge | ui_string | diff_hunk ...
    source: str  # "src/lib/auth.ts:42" | "a1b2c3d" | graph query id
    detail: str = ""
    snippet: str = Field(default="", max_length=600)


class Target(BaseModel):
    """The resolved subject of a task (a symbol, file, commit, ...)."""
    type: str  # function | class | component | file | commit | api_endpoint | ui_string
    name: str
    file: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    commit: Optional[str] = None
    confidence: float = 1.0
    evidence: list[Evidence] = Field(default_factory=list)


# ---------------------------------------------------------------- system metrics

class SystemMetrics(BaseModel):
    """Cross-mode comparability of cost, not just quality."""
    mode: Mode
    latency_s: float
    retrieved_context_chars: int = 0
    retrieved_context_items: int = 0
    tool_call_count: int = 0
    tool_calls: list[str] = Field(default_factory=list)
    llm_used: bool = False
    warnings: list[str] = Field(default_factory=list)


class ToolRecorder:
    """Records every retriever/parser/git call (never silent, always bounded)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context_chars = 0
        self.context_items = 0
        self.warnings: list[str] = []
        self._t0 = time.perf_counter()

    def tool(self, name: str) -> None:
        self.calls.append(name)

    def context(self, chars: int, items: int = 1) -> None:
        self.context_chars += chars
        self.context_items += items

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def metrics(self, mode: str, llm_used: bool) -> SystemMetrics:
        return SystemMetrics(
            mode=mode,  # type: ignore[arg-type]
            latency_s=round(time.perf_counter() - self._t0, 4),
            retrieved_context_chars=self.context_chars,
            retrieved_context_items=self.context_items,
            tool_call_count=len(self.calls),
            tool_calls=self.calls,
            llm_used=llm_used,
            warnings=self.warnings,
        )


# ---------------------------------------------------------------- task 1: locate

class LocatedCandidate(BaseModel):
    file: str
    symbol: Optional[str] = None
    kind: Optional[str] = None  # function | class | component | api_endpoint | ui_string | file
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    score: float = 0.0
    reason: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class LocateResult(BaseModel):
    task: Literal["locate"] = "locate"
    query_interpretation: str = ""   # what the system understood it was looking for
    candidates: list[LocatedCandidate] = Field(default_factory=list)


# ---------------------------------------------------------------- task 2: impact

class ImpactSide(BaseModel):
    """One affected entity on any impact list."""
    file: str
    symbol: Optional[str] = None
    kind: str = "function"           # function | class | component | file | api_route | test
    relation: str = ""               # caller | callee | importer | indirect | related
    via: str = ""                    # chain description, e.g. "Page -> AuthNav -> login"
    evidence: list[Evidence] = Field(default_factory=list)


class ImpactResult(BaseModel):
    task: Literal["impact"] = "impact"
    target: Optional[Target] = None
    direct_callers: list[ImpactSide] = Field(default_factory=list)
    callees: list[ImpactSide] = Field(default_factory=list)
    importing_files: list[ImpactSide] = Field(default_factory=list)
    indirectly_affected: list[ImpactSide] = Field(default_factory=list)
    related: list[ImpactSide] = Field(default_factory=list)  # api routes / tests
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- task 3: rollback

class HunkRef(BaseModel):
    """One unified-diff hunk, the atomic rollback unit."""
    file: str
    hunk_idx: int                    # index within the file's diff
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str = ""


class ChangeUnit(BaseModel):
    """A semantic slice of a commit: '修改系统标题' or '修改登录验证'."""
    unit_id: str                     # e.g. "abc123-U1"
    commit: str
    summary: str = ""                # human-readable intent guess
    semantic_label: str = ""         # title-change | auth-logic | styling | deps | ...
    files: list[str] = Field(default_factory=list)
    hunks: list[HunkRef] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)  # symbols touched (structural)
    ui_strings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    cohesion: float = 1.0            # intra-unit similarity / coupling score


class RollbackResult(BaseModel):
    task: Literal["rollback"] = "rollback"
    commit: str
    query_interpretation: str = ""
    change_units: list[ChangeUnit] = Field(default_factory=list)
    suspected_problem_changes: list[str] = Field(default_factory=list)  # unit_ids
    changes_to_rollback: list[str] = Field(default_factory=list)        # unit_ids
    changes_to_keep: list[str] = Field(default_factory=list)            # unit_ids
    affected_files: list[str] = Field(default_factory=list)
    collateral_damage_risk: float = 0.0   # 0..1, estimated
    risk_factors: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    operations_hint: list[str] = Field(default_factory=list)  # suggested (NOT executed) git ops


TaskResult = Union[LocateResult, ImpactResult, RollbackResult]


# ---------------------------------------------------------------- envelope

class TaskOutput(BaseModel):
    """The single top-level JSON every mode/task combination emits."""
    schema_version: str = "1.0"
    task: TaskType
    mode: Mode
    query: str
    repo: str
    commit: Optional[str] = None     # rollback only
    result: TaskResult
    system: SystemMetrics

    def write_json(self, path=None) -> str:
        import json
        text = json.dumps(json.loads(self.model_dump_json()), ensure_ascii=False, indent=2)
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return text


def truncate_candidates(res: LocateResult, top_k: int = MAX_CANDIDATES) -> LocateResult:
    res.candidates = res.candidates[:top_k]
    return res
