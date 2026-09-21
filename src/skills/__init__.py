"""Skill 层（Phase 11A）：可复用能力的正式抽象。

Agent = 谁负责；Skill = 如何完成一个可复用能力。Skill 只经
ContextBroker 取能力，绝不直接碰 git/AST/search/semantica。

调用链（Phase 11 最终形态）：
    Agent → Skill → ContextBroker → Deterministic Service → Physical Tool
    → Evidence
"""
from src.skills.spec import (SKILL_FAILED, SKILL_PARTIAL, SKILL_SUCCESS,
                             SkillResult, SkillSpec)
from src.skills.base import BaseSkill
from src.skills.capability import CapabilityGuard, capability_allowed
from src.skills.registry import SKILL_REGISTRY, default_registry, get_skill, register
from src.skills.runtime import SkillRuntime

from src.skills.resolve_target import ResolveTargetSkill
from src.skills.build_task_view import BuildTaskViewSkill
from src.skills.impact_analysis import ImpactAnalysisSkill
from src.skills.change_unit_analysis import ChangeUnitAnalysisSkill
from src.skills.coupling_analysis import CouplingAnalysisSkill
from src.skills.safe_rollback import SafeRollbackSkill
from src.skills.evidence_verification import EvidenceVerificationSkill
from src.skills.policy_check import PolicyCheckSkill

__all__ = ["SKILL_FAILED", "SKILL_PARTIAL", "SKILL_SUCCESS", "SkillResult",
           "SkillSpec", "BaseSkill", "CapabilityGuard", "capability_allowed",
           "SKILL_REGISTRY", "default_registry",
           "get_skill", "register", "SkillRuntime",
           "ResolveTargetSkill", "BuildTaskViewSkill", "ImpactAnalysisSkill",
           "ChangeUnitAnalysisSkill", "CouplingAnalysisSkill",
           "SafeRollbackSkill", "EvidenceVerificationSkill", "PolicyCheckSkill"]
