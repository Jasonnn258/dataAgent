"""语义推理公共不变量（Phase 11H）。

所有语义 skill prompt 共同遵守的边界 —— LLM 是 Semantic Reasoning
Adapter，不是 Physical Tool：它可以理解/归类/裁决**输入里已有的**
候选与证据，但绝不能铸造确定性事实。
"""

SEMANTIC_INVARIANTS = """\
Invariants (non-negotiable):
- Never invent symbols, files, commits, or feature ids that are not in the input.
- Never create or imply CALLS / IMPORTS / git relations — only deterministic tools may.
- Judge only from the given candidates and evidence.
- If evidence is insufficient, answer "unresolved" instead of guessing.
- Reply with strict JSON matching the requested schema. No prose, no markdown.
"""


def with_invariants(skill_system_prompt: str) -> str:
    """把公共不变量拼到具体 skill prompt 之后（不变量永远收尾）。"""
    return f"{skill_system_prompt}\n\n{SEMANTIC_INVARIANTS}"
