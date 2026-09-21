"""SkillRuntime —— Skill 的统一执行入口（Phase 11A）。

职责（刻意最小）：
- 按名字解析 skill
- 注入 llm（仅当 spec.semantic_reasoning == "allowed"；默认确定性路径
  拿不到 LLM，11H 的白名单在这里生效）
- 留下执行痕迹（skill:{name}:run）
- 11C 将在此校验 allowed_capabilities

不做：结果解释、重试、自动组合 —— 那些是 Agent 的职责。
"""
from __future__ import annotations

from src.skills.spec import SKILL_FAILED, SKILL_SUCCESS, SkillResult


class SkillRuntime:
    def __init__(self, broker, registry: dict | None = None, llm=None):
        from src.skills.registry import default_registry
        self.broker = broker
        self.registry = registry if registry is not None else default_registry()
        self.llm = llm

    def get(self, name: str):
        return self.registry.get(name)

    def run(self, name: str, context: dict) -> SkillResult:
        skill = self.registry.get(name)
        if skill is None:
            return SkillResult(skill=name, status=SKILL_FAILED,
                               error=f"unknown skill {name!r}")
        ctx = dict(context)  # 不污染调用方的 dict
        if self.llm is not None and skill.spec.semantic_reasoning == "allowed":
            ctx.setdefault("llm", self.llm)
        self.broker.rec.tool(f"skill:{name}:run")
        return skill.run(ctx, self.broker)
