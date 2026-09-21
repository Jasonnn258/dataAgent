"""EvidenceService（Phase 11D）：evidence / finding / 冲突注册表。

确定性服务：事实注册、主题冲突检测、带守卫的状态迁移。逻辑自
ContextBroker 原样迁入 —— 行为、id、错误消息全部不变。图节点/边的
写入经 broker.graph（broker 保持对外 API 兼容）。
"""
from __future__ import annotations

import re

from src.errors import DataAgentError, GraphError
from src.semgraph.objects import Conflict, Decision, Evidence, EvidenceType, Finding
from src.semgraph.schema_v2 import Edge, EdgeType, Node, NodeType


class EvidenceService:
    def __init__(self, broker):
        self.broker = broker
        self.evidence: dict[str, Evidence] = {}
        self.findings: dict[str, Finding] = {}
        self.conflicts: list[Conflict] = []

    # ------------------------------------------------------------ evidence
    def add_evidence(self, ev: Evidence) -> Evidence:
        existing = self.evidence.get(ev.id)
        if existing:  # 同 id 不同内容是 bug
            if (existing.type, existing.target, existing.location) != \
                    (ev.type, ev.target, ev.location):
                raise GraphError(f"evidence id collision with different facts: {ev.id}")
            return existing
        self.evidence[ev.id] = ev
        g = self.broker.graph
        props = {"type": ev.type.value, "source": ev.source,
                 "target": ev.target, "location": ev.location,
                 "payload": ev.payload[:300], "producer": ev.producer,
                 "timestamp": ev.timestamp}
        if ev.provenance:  # 上游 evidence id —— 审计链（9F）
            props["provenance"] = dict(list(ev.provenance.items())[:5])
        g.add_node(Node(ev.id, NodeType.EVIDENCE, props=props))
        if g.node(ev.target):
            g.add_edge(Edge(ev.target, ev.id, EdgeType.SUPPORTED_BY,
                            props={"role": "about"}))
        return ev

    def get_evidence(self, ids: list[str]) -> list[Evidence]:
        return [self.evidence[i] for i in ids if i in self.evidence]

    def all_evidence(self) -> list[Evidence]:
        return list(self.evidence.values())

    # ------------------------------------------------------------ finding
    def add_finding(self, finding: Finding) -> Finding:
        """注册 finding。绝不覆盖；与既有 finding 的矛盾登记成冲突交给
        verifier（原则 6）。"""
        fid = finding.id
        if fid in self.findings:
            return self.findings[fid]
        self.findings[fid] = finding
        self.broker.graph.add_node(Node(fid, NodeType.FINDING,
                                        props={"statement": finding.statement,
                                               "producer": finding.producer,
                                               "status": finding.status}))
        for eid in finding.evidence_ids:
            if self.evidence.get(eid):
                self.broker.graph.add_edge(Edge(fid, eid, EdgeType.SUPPORTED_BY))
        # 显式冲突检测（同主题、断言不合）
        self._register_conflicts(finding)
        return finding

    def _register_conflicts(self, finding: Finding) -> None:
        fkey = _topic_symbols(finding.statement)
        for other in self.findings.values():
            if other.id == finding.id or other.id in finding.contradicts:
                continue
            okey = _topic_symbols(other.statement)
            # 主体符号相同、谓词不合 => 冲突。只共享样板词（'auth' 这类
            # label、'candidate' 这类词）不算争端 —— 必须共享一个具体
            # 标识符。
            shared = {s for s in fkey & okey if _is_specific(s)}
            if shared and fkey != okey:
                topic = " ".join(sorted(shared))
                c = Conflict(finding_a=other.id, finding_b=finding.id,
                             topic=topic)
                self.conflicts.append(c)
                finding.contradicts.append(other.id)
                other.contradicts.append(finding.id)
                self.broker.graph.add_edge(Edge(finding.id, other.id,
                                                EdgeType.CONTRADICTS,
                                                props={"topic": topic}))

    def all_findings(self) -> list[Finding]:
        return list(self.findings.values())

    @property
    def open_conflicts(self) -> list[Conflict]:
        return list(self.conflicts)

    # ------------------------------------------------------------ 查询
    def evidence_about(self, target_id: str,
                       ev_type: EvidenceType | None = None) -> list[Evidence]:
        """关于某个图节点已注册的全部 evidence（可按类型过滤）。"""
        out = [e for e in self.evidence.values() if e.target == target_id]
        if ev_type is not None:
            out = [e for e in out if e.type == ev_type]
        return sorted(out, key=lambda e: e.timestamp)

    def unresolved_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if not c.resolved]

    def conflicts_involving(self, finding_id: str) -> list[Conflict]:
        return [c for c in self.conflicts
                if finding_id in (c.finding_a, c.finding_b)]

    def unsupported_findings(self) -> list[Finding]:
        """引用了从未注册的 evidence id 的 finding —— 它们的
        SUPPORTED_BY 边指向空气。喂给 policy gate。"""
        return [f for f in self.findings.values()
                if f.evidence_ids and not all(e in self.evidence
                                              for e in f.evidence_ids)]

    # ------------------------------------------------------------ 状态迁移
    def set_finding_status(self, finding_id: str, status: str,
                           verifier: str = "") -> Finding:
        """带守卫的状态迁移（9F/9G）：
        - verified 要求证据已注册且无未解决冲突
        - verifier 名字落到图节点上（审计，不是 CoT）"""
        f = self.findings.get(finding_id)
        if f is None:
            raise DataAgentError(f"unknown finding {finding_id!r}")
        if status not in ("proposed", "verified", "unsupported", "contradicted"):
            raise DataAgentError(f"invalid finding status {status!r}")
        if status == "verified":
            if not f.evidence_ids:
                raise DataAgentError(
                    f"cannot verify {finding_id!r}: no evidence cited — "
                    "a finding without evidence is unsupported, never verified")
            missing = [e for e in f.evidence_ids if e not in self.evidence]
            if missing:
                raise DataAgentError(
                    f"cannot verify {finding_id!r}: unregistered evidence {missing}")
            open_c = [c for c in self.conflicts_involving(finding_id)
                      if not c.resolved]
            if open_c:
                raise DataAgentError(
                    f"cannot verify {finding_id!r}: {len(open_c)} unresolved "
                    "conflict(s) — resolve the conflict first")
        f.status = status
        node = self.broker.graph.node(finding_id)
        if node is not None:
            node.props["status"] = status
            if verifier:
                node.props[f"{status}_by"] = verifier
        return f

    def resolve_conflict(self, conflict: Conflict, resolution: str,
                         winner: str | None = None,
                         resolver: str = "verifier") -> Conflict:
        """冲突的裁决权在 verifier。两条 finding 都留在注册表里；输家标
        contradicted，裁决本身记成 Decision（审计轨迹，衔接 9H）。"""
        if conflict not in self.conflicts:
            raise DataAgentError("unknown conflict — not registered by this broker")
        conflict.resolved = True
        conflict.resolution = resolution
        if winner:
            loser = next(fid for fid in (conflict.finding_a, conflict.finding_b)
                         if fid != winner)
            self.set_finding_status(loser, "contradicted", verifier=resolver)
        self.broker.record_decision(Decision.make(
            "conflict_resolution", resolution, target=conflict.topic,
            related_findings=[conflict.finding_a, conflict.finding_b],
            risk="medium", decision_maker=resolver,
            reason_summary=f"winner={winner or 'none'}"))
        return conflict


# 停用词表：finding 主题符号提取时排除（动词/虚词/模板词/十六进制 sha
# 字样 —— 共享一个 commit hash 是上下文，不是主题）
_TOPIC_STOP_WORDS = frozenset({
    "affects", "affect", "impacts", "impact", "only", "the", "and",
    "in", "on", "to", "of", "is", "are", "was", "route", "routes",
    "api", "via", "not", "unit", "units", "label", "feature",
    "query", "commit", "change", "matches", "description",
    "targets", "files", "symbols", "seed", "alias", "candidate",
    "modifying", "callers", "caller", "same", "exact", "claim",
    "here", "bare", "without", "evidence",
})

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_HEX_RE = re.compile(r"[0-9a-f]{6,}")


def _topic_symbols(statement: str) -> frozenset[str]:
    """一条 finding 的主题符号集合（subject）。两条 finding 的 subject
    集合相交但不相同时视为冲突 —— 如 'X affects A' vs 'X only affects
    B'。动词/停用词与 sha 形态的十六进制词元被排除。"""
    syms = _TOKEN_RE.findall(statement)
    return frozenset(s for s in syms
                     if s not in _TOPIC_STOP_WORDS
                     and not _HEX_RE.fullmatch(s))


def _is_specific(token: str) -> bool:
    """identifier 形态：camelCase/snake_case/含数字/CJK。纯小写英文词
    （'auth'、'navbar'、'candidate'）是词汇不是主体 —— 单共享这么一个
    词构不成争端主张。"""
    return (any(c.isupper() for c in token) or "_" in token
            or any(c.isdigit() for c in token)
            or any(ord(c) > 0x2E80 for c in token))
