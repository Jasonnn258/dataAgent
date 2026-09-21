"""ExecutionRecorder v2（Phase 11F）：可重建调用树的事件记录器。

ToolRecorder 只有一条平铺轨迹（rec.calls）；本模块在其上加一层
**结构化执行事件**：每次调用记 {trace_id, parent_trace_id, layer,
actor, action, status, duration_ms, evidence_ids, warnings}，parent
链可重建完整调用树（谁在何时以何种身份调了什么、产出了哪些证据）。

兼容承诺（零改动迁移）：
  - ExecutionRecorder 继承 ToolRecorder —— 现有 rec.tool/context/warn/
    metrics 调用点行为不变（.calls 列表照旧，g_ablation 的 tool_calls
    列不变）
  - rec.tool(name) 自动归类成事件：agent:* → AGENT 层、skill:* → SKILL
    层、其余 → TOOL 层，无需改任何调用点
  - 需要嵌套/耗时/证据关联时才用 span()（新代码路径）

有界原则：事件不存入参/返回载荷，绝不存模型隐藏思维链；error 与
warning 截断存储。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

from src.schema import ToolRecorder


class EventLayer(str, Enum):
    """执行事件的层：与 Phase 11 架构的分层一一对应。"""
    TOOL = "tool"          # 物理工具（git/AST/search/…）
    SERVICE = "service"    # 确定性服务
    BROKER = "broker"      # 能力门面
    SKILL = "skill"        # 可复用能力
    AGENT = "agent"        # 负责人
    LLM = "llm"            # 语义推理适配器（11H）
    POLICY = "policy"      # 规则门裁决


@dataclass
class ExecutionEvent:
    """一条执行事件（审计单位，不是日志行）。"""
    trace_id: str
    parent_trace_id: str = ""      # 空 = 树根
    task_id: str = ""
    layer: str = ""                # EventLayer.value
    actor: str = ""                # 谁在执行（agent/skill/service 名）
    action: str = ""               # 做了什么
    status: str = "ok"             # ok | failed | running
    duration_ms: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _classify_tool_name(name: str) -> tuple[str, str, str]:
    """把既有 rec.tool 轨迹名归类成 (layer, actor, action)。"""
    parts = name.split(":")
    if parts[0] == "agent" and len(parts) >= 3:
        return EventLayer.AGENT.value, parts[1], ":".join(parts[2:])
    if parts[0] == "skill" and len(parts) >= 3:
        return EventLayer.SKILL.value, parts[1], ":".join(parts[2:])
    return EventLayer.TOOL.value, parts[0], parts[-1]


class ExecutionRecorder(ToolRecorder):
    """ToolRecorder 的结构化超集：同一调用面，多一棵可重建的执行树。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[ExecutionEvent] = []
        self._by_trace: dict[str, ExecutionEvent] = {}
        self._stack: list[str] = []
        self._n = 0

    # ------------------------------------------------------------ 兼容面
    def tool(self, name: str) -> None:
        super().tool(name)          # .calls 轨迹照旧（旧指标不破坏）
        layer, actor, action = _classify_tool_name(name)
        self._emit(layer, actor, action)

    def warn(self, msg: str) -> None:
        super().warn(msg)
        if self._stack:             # 挂到当前 span（有界：每事件至多 8 条）
            ev = self._by_trace.get(self._stack[-1])
            if ev is not None and len(ev.warnings) < 8:
                ev.warnings.append(msg[:200])

    # ------------------------------------------------------------ 事件
    def _next_trace(self) -> str:
        self._n += 1
        return f"tr-{self._n}"

    def _emit(self, layer: str, actor: str, action: str,
              status: str = "ok", task_id: str = "",
              **meta) -> ExecutionEvent:
        ev = ExecutionEvent(
            trace_id=self._next_trace(),
            parent_trace_id=self._stack[-1] if self._stack else "",
            task_id=task_id,
            layer=layer, actor=actor, action=action,
            status=status, meta=meta)
        self.events.append(ev)
        self._by_trace[ev.trace_id] = ev
        return ev

    @contextmanager
    def span(self, layer: str, actor: str, action: str,
             task_id: str = "", **meta):
        """带耗时/父子关系的执行区间。异常 => status=failed 后原样上抛。"""
        ev = self._emit(layer, actor, action, status="running",
                        task_id=task_id, **meta)
        self._stack.append(ev.trace_id)
        t0 = time.perf_counter()
        try:
            yield ev
            ev.status = "ok"
        except Exception as e:
            ev.status = "failed"
            ev.meta["error"] = f"{type(e).__name__}: {e}"[:200]  # 有界
            raise
        finally:
            ev.duration_ms = round((time.perf_counter() - t0) * 1000, 3)
            self._stack.pop()

    # ------------------------------------------------------------ 视图
    def events_of(self, layer: str) -> list[ExecutionEvent]:
        return [e for e in self.events if e.layer == layer]

    def tree(self) -> dict:
        """按 parent 链重建嵌套树 {**event, children: [...]}。"""
        nodes = {e.trace_id: {"event": e, "children": []}
                 for e in self.events}
        roots: list[dict] = []
        for e in self.events:
            node = nodes[e.trace_id]
            parent = nodes.get(e.parent_trace_id)
            (parent["children"] if parent else roots).append(node)
        return {"roots": roots}

    def summary(self) -> dict:
        """有界摘要（进 SystemMetrics/报告；不放大载荷）。"""
        by_layer: dict[str, int] = {}
        failed = 0
        for e in self.events:
            by_layer[e.layer] = by_layer.get(e.layer, 0) + 1
            if e.status == "failed":
                failed += 1
        return {"events": len(self.events), "by_layer": by_layer,
                "failed": failed}
