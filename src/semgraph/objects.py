"""一等数据对象（Phase 9F/9G/9H/9I 地基）。

Evidence / Finding / Decision / Policy 是数据，不是日志行（原则 4）：
- provenance 在事实进入系统的那一刻就存在，不是事后补挂（原则 5）
- finding 之间绝不互相覆盖；矛盾是冲突注册表里的显式边（原则 6）
- 不存隐藏 CoT；Decision.reason_summary 是面向审计的解释，不是模型
  私有推理（spec 9H）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from src.semgraph.schema_v2 import stable_id


class EvidenceType(str, Enum):
    LEXICAL = "LEXICAL"
    AST = "AST"
    CALL_PATH = "CALL_PATH"
    GIT_DIFF = "GIT_DIFF"
    GIT_BLAME = "GIT_BLAME"
    CHANGE_UNIT = "CHANGE_UNIT"
    GRAPH_PATH = "GRAPH_PATH"
    TEST = "TEST"
    SEMANTIC_MAPPING = "SEMANTIC_MAPPING"
    EXECUTION_PLAN = "EXECUTION_PLAN"


@dataclass
class Evidence:
    id: str
    type: EvidenceType
    source: str                       # 产生它的工具 id，如 "git:diff"
    target: str                       # 所属图节点 id
    location: str = ""                # file:line / sha / 节点 id
    payload: str = ""                 # 有界的人类可读事实
    producer: str = ""                # 创建它的 agent/工具
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    provenance: dict = field(default_factory=dict)  # 上游 id（证据链）

    @classmethod
    def make(cls, type: EvidenceType, source: str, target: str, **kw) -> "Evidence":
        loc = kw.get("location", "")
        eid = kw.pop("id", None) or f"evid:{stable_id(type.value, source, target, loc)}"
        return cls(id=eid, type=type, source=source, target=target, **kw)


@dataclass
class Finding:
    id: str
    statement: str                    # 单条可验证的事实断言
    evidence_ids: list[str] = field(default_factory=list)
    producer: str = ""                # agent 名
    status: str = "proposed"          # proposed | verified | unsupported | contradicted
    contradicts: list[str] = field(default_factory=list)  # 冲突的对端 finding id
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    @property
    def supported(self) -> bool:
        return bool(self.evidence_ids)

    @classmethod
    def make(cls, statement: str, producer: str, evidence_ids: list[str] | None = None,
             fid: str | None = None) -> "Finding":
        return cls(id=fid or f"finding:{stable_id(statement, producer)}",
                   statement=statement, producer=producer,
                   evidence_ids=list(evidence_ids or []))


@dataclass
class Decision:
    id: str
    category: str                     # rollback | keep | expand_context | ...
    task_id: str = ""
    target: str = ""
    outcome: str = ""                 # 决定内容
    evidence_ids: list[str] = field(default_factory=list)
    related_findings: list[str] = field(default_factory=list)
    risk: str = "low"                 # low | medium | high
    decision_maker: str = ""          # agent 或 "human:<name>"
    reason_summary: str = ""          # 面向审计，绝不是模型私有 CoT
    policy: str = ""                  # 门控时记 "rule v_version -> ACTION"
    policy_version: str = ""          # 生效的 maintenance_policy 版本（11I）
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    @classmethod
    def make(cls, category: str, outcome: str, **kw) -> "Decision":
        did = kw.pop("id", None) or f"dec:{stable_id(category, outcome, kw.get('task_id', ''))}"
        return cls(id=did, category=category, outcome=outcome, **kw)


class PolicyAction(str, Enum):
    PASS = "PASS"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BLOCK = "BLOCK"


@dataclass
class PolicyRule:
    id: str
    name: str
    version: str
    description: str
    action: PolicyAction
    trigger: str = ""                 # 人类可读的触发条件


@dataclass
class PolicyResult:
    rule: PolicyRule | None
    action: PolicyAction
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.action == PolicyAction.PASS


# ---------------------------------------------------------------- 冲突
@dataclass
class Conflict:
    """两条互不服气的 finding。双方都保留；裁决权在 verifier。"""
    finding_a: str
    finding_b: str
    topic: str = ""
    resolved: bool = False
    resolution: str = ""


# ---------------------------------------------------------------- 裁决（9L）
class VerdictStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class Verdict:
    """verifier 对单条 finding 的三档定性裁决。刻意不带数值 confidence
    —— 假精确的 "0.87" 不如三个诚实状态。"""
    finding_id: str
    status: VerdictStatus
    verifier: str
    checks: list[str] = field(default_factory=list)   # 实际做过的检查
