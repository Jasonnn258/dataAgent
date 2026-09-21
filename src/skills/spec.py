"""SkillSpec / SkillResult —— Skill 的数据契约（Phase 11A）。

Spec 是**数据**：声明输入/输出/所需 capability/前置与成败条件，
可被注册表枚举、被测试断言、被评估器消费（11J）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.errors import DataAgentError


@dataclass
class SkillSpec:
    """一份 Skill 的完整契约（数据，不是代码）。"""
    name: str                          # 唯一名，如 "change_unit_analysis"
    description: str = ""
    required_inputs: list[str] = field(default_factory=list)   # context 必须带的键
    produced_outputs: list[str] = field(default_factory=list)  # SkillResult.data 承诺的键
    allowed_capabilities: list[str] = field(default_factory=list)  # broker capability 白名单
    evidence_requirements: str = ""    # 本 skill 的产出需要什么级别的证据
    preconditions: list[str] = field(default_factory=list)     # 运行前必须为真
    success_conditions: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)
    semantic_reasoning: str = "forbidden"   # forbidden | allowed（11H：LLM 白名单）

    def __post_init__(self) -> None:
        if not self.name:
            raise DataAgentError("SkillSpec.name is required")


# SkillResult.status 的三档：确定性能力只有这三种终态
SKILL_SUCCESS = "success"
SKILL_PARTIAL = "partial"
SKILL_FAILED = "failed"


@dataclass
class SkillResult:
    """Skill 的统一返回结构（对齐 11E 的 ToolResult 形态）。"""
    skill: str                         # skill name
    status: str = SKILL_FAILED         # success | partial | failed
    data: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    trace_id: str = ""
    capabilities_used: list[str] = field(default_factory=list)  # 11C 守卫回填

    @property
    def ok(self) -> bool:
        return self.status in (SKILL_SUCCESS, SKILL_PARTIAL)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
