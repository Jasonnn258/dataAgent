"""角色限定上下文（Phase 9K）。

Agent 之间共享的是*结构化事实* —— evidence、finding、decision、policy
结果 —— 经由 broker 的注册表（原则 3）；绝不共享彼此的中间思考。每个
agent 在一个 ScopedContext 下运行，其中写明：

  - 它可以从 broker 读什么（能力清单，按方法名）
  - 它可以生长哪个 task view（有界、带审计）
  - 它产出的 evidence/finding/decision（自己的输出轨迹）

能力清单是写进文档的契约，不是沙箱：硬边界（禁 Semantica 内部结构、
禁整图检索、禁执行 git）由 broker API 本身强制。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.semgraph.task_view import TaskGraphView


@dataclass
class ScopedContext:
    role: str
    task_id: str
    reads: list[str] = field(default_factory=list)   # broker 方法名
    task_view: TaskGraphView | None = None
    evidence_ids: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)

    def produced(self, evidence: list[str] = None, finding: str = "",
                 decision: str = "") -> None:
        if evidence:
            self.evidence_ids.extend(evidence)
        if finding:
            self.finding_ids.append(finding)
        if decision:
            self.decision_ids.append(decision)
