"""执行层策略门（Phase 13H）：规则触发是确定性事实，动作是策略旋钮。

分工：
- 触发检测（本模块）：纯 python 事实判断 —— 计划/尝试里有什么危险面、
  沙箱里实际发生了什么。不带版本、不配置化：事实就是事实。
- 动作档位（config/maintenance_policy.yaml execution_policy 段）：
  同一个触发在这个仓库值 BLOCK 还是 HUMAN_REVIEW，是策略决定的，
  版本随 maintenance_policy.version 落档。

两道门：
- pre_execution_gate(plan)：执行前看计划 —— 公开 API 面、认证面、
  同符号又回退又保留。
- post_execution_gate(attempt)：执行后看事实 —— 计划外文件、keep 损伤、
  源仓库漂移、贴不上、测试挂，加 pre 面的复检。

gate 只裁决不执行；服从 BLOCK 是调用方（MaintenanceExecutorAgent）的
契约。auto_push 恒为 false：PASS 也绝不自动 push。
"""
from __future__ import annotations

from src.semgraph.objects import PolicyAction, PolicyResult, PolicyRule

# 确定性词面（小写匹配）：认证/登录/凭据面
_AUTH_TOKENS = ("auth", "login", "signin", "session", "token",
                "credential", "password", "oauth", "permission")
# 公开 API 面的路由文件模式
_ROUTE_TOKENS = ("route", "api/", "controller", "endpoint")

_ACTION_ORDER = {PolicyAction.PASS: 0, PolicyAction.HUMAN_REVIEW: 1,
                 PolicyAction.BLOCK: 2}


def _matches(text: str, tokens: tuple[str, ...]) -> bool:
    t = text.lower()
    return any(tok in t for tok in tokens)


def _plan_surfaces(plan):
    """计划暴露的危险面（文件/符号/路由）。"""
    files = list(getattr(plan, "target_files", []) or [])
    symbols = list(getattr(plan, "target_symbols", []) or []) + [
        s for u in list(getattr(plan, "rollback_units", []) or [])
        + list(getattr(plan, "keep_units", []) or [])
        for s in u.get("symbols", [])]
    routes = list(getattr(plan, "affected_routes", []) or [])
    return files, symbols, routes


def _shared_symbols(plan) -> list[str]:
    rb = {s.rsplit("::", 1)[-1]
          for u in plan.rollback_units for s in u.get("symbols", [])}
    kp = {s.rsplit("::", 1)[-1]
          for u in plan.keep_units for s in u.get("symbols", [])}
    return sorted(rb & kp)


def detect_pre_triggers(plan) -> dict[str, str]:
    """计划级触发（规则名 → 触发事实描述）。"""
    files, symbols, routes = _plan_surfaces(plan)
    triggers: dict[str, str] = {}
    api_hits = ([r for r in routes if r]
                + [f for f in files if _matches(f, _ROUTE_TOKENS)])
    if api_hits:
        triggers["public_api_change"] = (
            f"plan touches public API surface: {api_hits[:5]}")
    auth_hits = [f for f in files if _matches(f, _AUTH_TOKENS)]
    auth_hits += [s for s in symbols if _matches(s, _AUTH_TOKENS)]
    if auth_hits:
        triggers["auth_change"] = (
            f"plan touches auth surface: {sorted(set(auth_hits))[:5]}")
    shared = _shared_symbols(plan)
    if shared:
        triggers["same_symbol_rollback_keep"] = (
            f"same symbol both rolled back and kept: {shared}")
    return triggers


def detect_post_triggers(attempt) -> dict[str, str]:
    """尝试级触发：沙箱里实际发生的事（硬红线）+ 计划面复检。"""
    triggers = dict(detect_pre_triggers(attempt.plan))
    status = attempt.status
    verification = attempt.verification_results[-1] \
        if attempt.verification_results else {}
    hard_detail = "; ".join(
        c.get("detail", "") for c in verification.get("checks", [])
        if not c.get("pass", True))[:200]
    if not verification.get("scope_pass", True):
        triggers["unexpected_file_change"] = (
            "changes outside plan scope detected: " + hard_detail)
    if not verification.get("preservation_pass", True):
        triggers["forbidden_keep_change"] = (
            "keep-side content damaged: " + hard_detail)
    if status == "STALE_PLAN":
        triggers["repository_state_changed"] = (
            "source repository moved since the plan snapshot")
    if status == "CONFLICT":
        triggers["apply_check_failed"] = (
            "inverse patch does not apply at the base state")
    results = attempt.validation_results or []
    if status == "TEST_FAILED" or any(
            (r or {}).get("status") not in ("PASSED", None) for r in results):
        triggers["test_failed"] = (
            f"validation commands failed: status={status}, "
            f"{sum(1 for r in results if (r or {}).get('status') != 'PASSED')}"
            f"/{len(results)} not passed")
    return triggers


def _gate(triggers: dict[str, str], action_map: dict[str, str],
          gate_name: str, version: str) -> PolicyResult:
    """触发 → 最重动作（动作档位来自策略配置）。"""
    evaluated: list[PolicyResult] = []
    for name, detail in sorted(triggers.items()):
        action_str = action_map.get(name)
        if action_str not in ("BLOCK", "HUMAN_REVIEW", "PASS"):
            continue    # 未配置的规则不出声（策略文件决定门面）
        rule = PolicyRule(
            id=f"policy:{name}", name=name, version=version,
            description=f"{gate_name} rule", action=PolicyAction(action_str),
            trigger=detail[:200])
        evaluated.append(PolicyResult(rule=rule,
                                      action=PolicyAction(action_str),
                                      detail=f"{name}: {detail}"[:300]))
    if not evaluated:
        return PolicyResult(
            rule=None, action=PolicyAction.PASS,
            detail=f"{gate_name}: no configured rule triggered")
    return max(evaluated, key=lambda r: _ACTION_ORDER[r.action])


def pre_execution_gate(plan, policy_cfg: dict) -> PolicyResult:
    from src.config import policy_version
    cfg = (policy_cfg.get("execution_policy", {})
                .get("pre_gate", {}))
    return _gate(detect_pre_triggers(plan), cfg, "execution_pre_gate",
                 policy_version())


def post_execution_gate(attempt, policy_cfg: dict) -> PolicyResult:
    from src.config import policy_version
    cfg = (policy_cfg.get("execution_policy", {})
                .get("post_gate", {}))
    return _gate(detect_post_triggers(attempt), cfg, "execution_post_gate",
                 policy_version())
