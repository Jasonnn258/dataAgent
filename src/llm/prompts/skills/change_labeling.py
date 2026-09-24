"""change_labeling prompt：变更单元的语义标签（Phase 12C 启用）。

11H 只建接口；12C 的 ChangeUnitLabelSkill 经
SemanticReasoningAdapter.label_change_units 使用本 prompt，输出严格
JSON、白名单过滤、candidate_features 只能从给定 feature 名里挑
（不变量：LLM 只能挑已有名字，不能铸造确定性事实）。

输出永远停留在 candidate 级 —— 绝不写回单元的确定性
semantic_label/props。
"""

from src.llm.prompts.invariants import with_invariants

SYSTEM_PROMPT = with_invariants(
    "You classify code change units for a maintenance task. "
    'Reply ONLY with JSON: {"units": [{"unit_id": "...", '
    '"label": "auth" | "title" | "ui" | "deps" | "docs" | "api" | '
    '"data" | "other", '
    '"intent": "bugfix" | "feature" | "refactor" | "perf" | "docs" | '
    '"release" | "test" | "other", '
    '"candidate_features": ["..."], "reason": "..."}]}. '
    "label = change domain (aligned with the deterministic domain "
    "catalog); intent = what the change is for; candidate_features "
    "MUST be names copied from the given feature list, never invented. "
    "Judge only from the given unit summaries, hint terms and features."
)

# 标签白名单：与 C0 的 DOMAIN_SIGNALS 目录对齐（12C）
LABELS = ("auth", "title", "ui", "deps", "docs", "api", "data", "other")

# 意图白名单：commit 级变更意图分类（12C 新增）
INTENTS = ("bugfix", "feature", "refactor", "perf", "docs", "release",
           "test", "other")

# 每次调用最多标注的单元数（prompt 有界）
MAX_UNITS = 12


def user_payload(units: list[dict], hint_terms: list[str],
                 known_features: list[str]) -> str:
    """units: [{unit_id, summary, commit_subject, files}]（调用方已截断）。"""
    return (f"Units: {units}\nHint terms: {hint_terms}\n"
            f"Features: {known_features}")
