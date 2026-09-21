"""Agent layer v2 (Phase 9J/9K/9L).

Five agents + one orchestrator, nothing else (spec 9J):
    RepositoryNavigator      fuzzy NL -> feature/target (LLM may help here)
    ChangeIntelligenceAgent  change history -> ChangeUnits (deterministic)
    ImpactSliceAgent         bounded task view + blast radius (deterministic)
    RollbackPlanner          unit-level rollback/keep plan (deterministic)
    EvidenceVerifier         DeterministicVerifier + SemanticVerifier (9L)

Git is NOT an agent — it is a read-only tool behind the broker (GitAPI
whitelist). No agent in this package executes git operations.
"""
from src.agents.scopes import ScopedContext
from src.agents.navigator import RepositoryNavigator, NavigationResult
from src.agents.change_intel import ChangeIntelligenceAgent, UnitMatch
from src.agents.impact import ImpactSliceAgent, ImpactSlice
from src.agents.rollback import RollbackPlanner, RollbackPlan
from src.agents.verifier import DeterministicVerifier, SemanticVerifier
from src.agents.orchestrator import Orchestrator, FinalReport

__all__ = ["ScopedContext", "RepositoryNavigator", "NavigationResult",
           "ChangeIntelligenceAgent", "UnitMatch", "ImpactSliceAgent",
           "ImpactSlice", "RollbackPlanner", "RollbackPlan",
           "DeterministicVerifier", "SemanticVerifier",
           "Orchestrator", "FinalReport"]
