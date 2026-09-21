"""semantic_mapping prompt：自然语言 query → feature 候选。

Phase 11H 从 src/semgraph/semantic_mapper.py::_llm_map 迁入，
行为保持：LLM 只能从给定 feature 里挑，输出停留在 candidate 级。
"""
from src.llm.prompts.invariants import with_invariants

SYSTEM_PROMPT = with_invariants(
    "You map natural-language maintenance queries to software features. "
    'Reply ONLY with JSON: {"candidates": [{"feature_id": "...", '
    '"reason": "..."}]}. '
    "Pick only from the given features. Never invent call/import relations."
)

# 与迁移前的 [:3] 截断一致
MAX_CANDIDATES = 3


def user_payload(features: list[dict], query: str) -> str:
    return f"Features: {features}\nQuery: {query}"
