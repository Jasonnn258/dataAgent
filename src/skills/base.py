"""BaseSkill —— Skill 执行体的基类（Phase 11A）。

硬边界（Phase 11G 的静态约束在此声明意图）：
Skill 绝不 import git_history/search/structural/change_units/semantica，
也绝不执行 subprocess —— 一切系统能力经 ContextBroker。
"""
from __future__ import annotations

from src.skills.spec import (SKILL_FAILED, SKILL_SUCCESS, SkillResult,
                             SkillSpec)


class BaseSkill:
    """所有 Skill 的基类。子类只需实现 _execute()。"""
    spec: SkillSpec = None             # 类属性：子类必须覆盖

    def __init__(self) -> None:
        from src.errors import DataAgentError
        if self.spec is None:
            raise DataAgentError(
                f"{type(self).__name__} must define a SkillSpec class attribute")

    @property
    def name(self) -> str:
        return self.spec.name

    def can_run(self, context: dict) -> tuple[bool, str]:
        """静态可运行检查：required_inputs 是否齐。返回 (可否运行, 原因)。"""
        missing = [k for k in self.spec.required_inputs if k not in context]
        if missing:
            return False, f"missing required inputs: {missing}"
        return True, ""

    def run(self, context: dict, broker) -> SkillResult:
        """模板方法：can_run 检查 -> _execute -> 兜底异常转 failed。"""
        ok, why = self.can_run(context)
        if not ok:
            return SkillResult(skill=self.spec.name, status=SKILL_FAILED,
                               error=why)
        try:
            return self._execute(context, broker)
        except Exception as e:  # skill 失败必须显式，绝不静默
            return SkillResult(skill=self.spec.name, status=SKILL_FAILED,
                               error=f"{type(e).__name__}: {e}")

    def _execute(self, context: dict, broker) -> SkillResult:
        raise NotImplementedError
