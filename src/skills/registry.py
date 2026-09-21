"""Skill 注册表（Phase 11A）。

名字 → Skill 实例的单点映射。Agent 的 SKILLS 清单引用这里的名字；
runtime 按名字解析。注册发生在 import 时（无副作用，纯声明）。
"""
from __future__ import annotations

from src.errors import DataAgentError
from src.skills.base import BaseSkill

SKILL_REGISTRY: dict[str, BaseSkill] = {}


def register(skill: BaseSkill) -> BaseSkill:
    """登记一个 skill 实例；重名是 bug，直接抛。"""
    if skill.name in SKILL_REGISTRY:
        raise DataAgentError(f"duplicate skill registration: {skill.name}")
    SKILL_REGISTRY[skill.name] = skill
    return skill


def get_skill(name: str) -> BaseSkill:
    skill = SKILL_REGISTRY.get(name)
    if skill is None:
        raise DataAgentError(
            f"unknown skill {name!r} — registered: {sorted(SKILL_REGISTRY)}")
    return skill


def default_registry() -> dict[str, BaseSkill]:
    """导入全部 skill 模块（触发注册）后返回注册表副本。"""
    import src.skills.resolve_target          # noqa: F401
    import src.skills.build_task_view         # noqa: F401
    import src.skills.impact_analysis         # noqa: F401
    import src.skills.change_unit_analysis    # noqa: F401
    import src.skills.coupling_analysis       # noqa: F401
    import src.skills.safe_rollback           # noqa: F401
    import src.skills.evidence_verification   # noqa: F401
    import src.skills.policy_check            # noqa: F401
    return dict(SKILL_REGISTRY)
