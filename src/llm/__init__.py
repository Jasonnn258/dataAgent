"""LLM 层（Phase 11H）：

- client.py —— 可选的 OpenAI 兼容客户端（未配置 => available=False）
- semantic_adapter.py —— SemanticReasoningAdapter，语义推理唯一入口
- prompts/ —— prompt 集中地（公共不变量 + 按语义 skill 组织）

LLM 不是 Physical Tool：确定性 Physical Tool Layer 不 import 本包，
只有语义路径经由 adapter 间接使用。
"""
from src.llm.semantic_adapter import SemanticReasoningAdapter

__all__ = ["SemanticReasoningAdapter"]
