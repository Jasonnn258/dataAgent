"""semantic_verification prompt：finding 语义裁决（接口预留，未启用）。

Phase 11H 只建立接口。未来 EvidenceSemanticVerificationSkill 若声明
semantic_reasoning=allowed，经 SemanticReasoningAdapter.verify_semantic
使用本 prompt。证据不足必须返回 unresolved，不允许猜。
"""
from src.llm.prompts.invariants import with_invariants

SYSTEM_PROMPT = with_invariants(
    "You verify whether a finding's claim is supported by the given "
    "evidence text. Reply ONLY with JSON: "
    '{"verdict": "supported" | "unsupported" | "unresolved", '
    '"reason": "..."}. If evidence is insufficient, use "unresolved".'
)

# 裁决白名单：不在名单里的输出一律归 unresolved
VERDICTS = ("supported", "unsupported", "unresolved")


def user_payload(claim: str, evidence_texts: list[str]) -> str:
    return f"Claim: {claim}\nEvidence:\n- " + "\n- ".join(evidence_texts)
