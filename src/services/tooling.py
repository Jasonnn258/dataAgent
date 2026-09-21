"""call_tool —— 物理工具调用的统一包装（Phase 11E）。

Service 层调用可能失败的底层（v1 PathFinder / SemanticMapper / 之后的
LLM 适配器）时经此包装：异常转 ToolResult.failure、耗时入 meta。
守卫原则不因形态改变：失败必须显式（unwrap 大声报错），降级必须留痕
（degraded=True + note）。
"""
from __future__ import annotations

import time

from src.schema import ToolResult


def call_tool(tool: str, fn, *args, **kwargs) -> ToolResult:
    """执行一次工具调用并包成 ToolResult。fn 抛异常 => failure（不吞）。"""
    t0 = time.perf_counter()
    try:
        value = fn(*args, **kwargs)
    except Exception as e:  # 工具边界：失败显式化，交给调用方裁决
        return ToolResult.failure(tool, f"{type(e).__name__}: {e}",
                                  ms=round((time.perf_counter() - t0) * 1000, 3))
    return ToolResult.success(tool, value,
                              ms=round((time.perf_counter() - t0) * 1000, 3))
