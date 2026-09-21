"""Policy Gate (Phase 9I): lightweight structured rules with versions.

A project-internal adapter — deliberately NOT a wrapper around Semantica's
PolicyEngine API (spec allows this). Rules are data objects; evaluation is
deterministic python over an explicit context dict produced by the caller
(verifier / planner). No rule fires silently: every PolicyResult names the
rule and version that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.semgraph.objects import PolicyAction, PolicyResult, PolicyRule


@dataclass
class SimpleRule(PolicyRule):
    """Rule whose trigger is evaluated by a python callable over a context
    dict. The callable must be pure and side-effect free."""
    check_fn: object = None       # (dict) -> (triggered: bool, detail: str)

    def evaluate(self, context: dict) -> PolicyResult:
        assert callable(self.check_fn), f"rule {self.name} has no check_fn"
        triggered, detail = self.check_fn(context)
        if not triggered:
            return PolicyResult(rule=None, action=PolicyAction.PASS,
                                detail=f"rule {self.name} not triggered")
        return PolicyResult(
            rule=self, action=self.action,
            detail=detail or f"{self.name} v{self.version}: {self.trigger}")


def _shared_symbol(context: dict) -> tuple[bool, str]:
    rb = set(context.get("rollback_symbols", []))
    keep = set(context.get("keep_symbols", []))
    shared = rb & keep
    return bool(shared), (f"rollback/keep share symbols: {sorted(shared)}"
                          if shared else "")


def _public_api(context: dict) -> tuple[bool, str]:
    routes = [r for r in context.get("affected_routes", []) if r]
    return bool(routes), (f"affected API routes: {routes}" if routes else "")


def _db_migration(context: dict) -> tuple[bool, str]:
    files = context.get("changed_files", [])
    hits = [f for f in files if "migration" in f.lower() or f.endswith(".sql")]
    return bool(hits), f"migration-like files changed: {hits}" if hits else ""


def _unsupported(context: dict) -> tuple[bool, str]:
    n = context.get("unsupported_findings", 0)
    return bool(n), f"{n} finding(s) without evidence" if n else ""


def _invalid_path(context: dict) -> tuple[bool, str]:
    n = context.get("invalid_graph_paths", 0)
    return bool(n), f"{n} graph path(s) failed verification" if n else ""


def _test_failure(context: dict) -> tuple[bool, str]:
    n = context.get("unresolved_test_failures", 0)
    return bool(n), f"{n} unresolved test failure(s)" if n else ""


def _mk(name: str, action: PolicyAction, trigger: str, fn) -> SimpleRule:
    return SimpleRule(
        id=f"policy:{name}", name=name, version="1.0.0",
        description=f"{name}: {trigger}", action=action, trigger=trigger,
        check_fn=fn)


# registry — the initial rule set from the spec
POLICY_RULES: dict[str, SimpleRule] = {
    "rollback_keep_same_symbol": _mk(
        "rollback_keep_same_symbol", PolicyAction.HUMAN_REVIEW,
        "rollback and keep sets share a symbol/function", _shared_symbol),
    "rollback_public_api": _mk(
        "rollback_public_api", PolicyAction.HUMAN_REVIEW,
        "rollback touches public API routes (HIGH_RISK)", _public_api),
    "db_migration_change": _mk(
        "db_migration_change", PolicyAction.HUMAN_REVIEW,
        "change involves DB migration files", _db_migration),
    "unsupported_finding": _mk(
        "unsupported_finding", PolicyAction.BLOCK,
        "core finding has no supporting evidence", _unsupported),
    "invalid_graph_path": _mk(
        "invalid_graph_path", PolicyAction.BLOCK,
        "a graph path cited as evidence is invalid", _invalid_path),
    "unresolved_test_failure": _mk(
        "unresolved_test_failure", PolicyAction.BLOCK,
        "test failures relevant to the decision are unresolved", _test_failure),
}

_ACTION_ORDER = {PolicyAction.PASS: 0, PolicyAction.HUMAN_REVIEW: 1,
                 PolicyAction.BLOCK: 2}


def evaluate_all(context: dict) -> list[PolicyResult]:
    """Every triggered rule (untriggered rules stay silent — 'checked and
    clean' is recorded once by the gate, not per rule)."""
    return [r for r in (rule.evaluate(context)
                        for rule in POLICY_RULES.values()) if not r.passed]


def gate(context: dict) -> PolicyResult:
    """Most severe triggered action across the whole rule set. The caller
    (orchestrator) must honor BLOCK — the gate itself never executes."""
    triggered = evaluate_all(context)
    if not triggered:
        return PolicyResult(
            rule=None, action=PolicyAction.PASS,
            detail=f"clean: {len(POLICY_RULES)} policy rules checked, none triggered")
    return max(triggered, key=lambda r: _ACTION_ORDER[r.action])
