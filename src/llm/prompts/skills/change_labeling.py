"""change_labeling prompt：变更单元的语义标签（接口预留，未启用）。

Phase 11H 只建立接口，没有任何确定性路径调用它 —— 不为了用 LLM
而强行启用。未来 ChangeUnitLabelSkill 若声明
semantic_reasoning=allowed，经 SemanticReasoningAdapter.label_change_unit
使用本 prompt。
"""
from src.llm.prompts.invariants import with_invariants

SYSTEM_PROMPT = with_invariants(
    "You classify a code change unit for a maintenance task. "
    'Reply ONLY with JSON: {"label": "auth" | "ui" | "config" | "docs" '
    '| "infra" | "other", "confidence": <0.0-1.0>, "reason": "..."}. '
    "Judge only from the given diff summary and hint terms."
)

# 标签白名单：不在名单里的输出一律归 other
LABELS = ("auth", "ui", "config", "docs", "infra", "other")


def user_payload(diff_summary: str, hint_terms: list[str]) -> str:
    return f"Diff summary: {diff_summary}\nHint terms: {hint_terms}"
