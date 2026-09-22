"""Agent 层 v2（Phase 9J/9K/9L + 13I）。

六个 agent + 一个 orchestrator（spec 9J，13I 增补执行 agent）：
    RepositoryNavigator      模糊 NL → feature/目标（LLM 只能在这帮忙）
    ChangeIntelligenceAgent  变更历史 → ChangeUnit（确定性）
    ImpactSliceAgent         有界 task view + 波及面（确定性）
    RollbackPlanner          单元级回退/保留计划（确定性）
    EvidenceVerifier         DeterministicVerifier + SemanticVerifier（9L）
    MaintenanceExecutorAgent 沙箱执行链驾驶员（13I，只写沙箱）

Git 不是 agent —— 它是 broker 背后的只读工具（GitAPI 白名单）；沙箱写
git 只在 src/maintenance/sandbox_git（唯一出口，13C）。本包内任何 agent
都不直接执行 git 操作。
"""
from src.agents.scopes import ScopedContext
from src.agents.navigator import RepositoryNavigator, NavigationResult
from src.agents.change_intel import ChangeIntelligenceAgent, UnitMatch
from src.agents.impact import ImpactSliceAgent, ImpactSlice
from src.agents.rollback import RollbackPlanner, RollbackPlan
from src.agents.verifier import DeterministicVerifier, SemanticVerifier
from src.agents.executor import MaintenanceExecutorAgent, ExecutionOutcome
from src.agents.orchestrator import Orchestrator, FinalReport

__all__ = ["ScopedContext", "RepositoryNavigator", "NavigationResult",
           "ChangeIntelligenceAgent", "UnitMatch", "ImpactSliceAgent",
           "ImpactSlice", "RollbackPlanner", "RollbackPlan",
           "DeterministicVerifier", "SemanticVerifier",
           "MaintenanceExecutorAgent", "ExecutionOutcome",
           "Orchestrator", "FinalReport"]
