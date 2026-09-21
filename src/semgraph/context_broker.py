"""Context Broker（Phase 9B）：agent 与 Semantica 之间的唯一一扇门。

Agent 绝不直接碰 GraphV2 内部结构、semantica.kg 对象、CodeIndex 或
GitAPI —— 一切经由这个 facade，它返回的全是项目自有的 dataclass
（spec 9B）。

为什么要这层间接：它是事实进入系统那一刻铸造 evidence 的接缝（原则
5）、给 task view 做预算的地方（原则：禁止整图检索）、也是 Phase 10
G0-G4 消融开关图层的旋钮。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.errors import DataAgentError, GraphError
from src.schema import ToolRecorder
from src.semgraph.objects import (Conflict, Decision, Evidence, EvidenceType,
                                  Finding, PolicyResult)
from src.semgraph.schema_v2 import Edge, EdgeType, GraphV2, Node, NodeType
from src.semgraph.task_view import TaskGraphView


@dataclass
class TargetContext:
    """解析到目标之后，围绕它的确定性邻域事实。"""
    target: Node
    definition: str = ""                # file:行号区间
    direct_callers: list[Node] = field(default_factory=list)
    direct_callees: list[Node] = field(default_factory=list)
    importers: list[Node] = field(default_factory=list)
    related_routes: list[Node] = field(default_factory=list)
    related_tests: list[Node] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ChangeContext:
    """时间性事实：最近触达目标的 change unit 有哪些。"""
    target: str = ""
    last_commits: list[Node] = field(default_factory=list)
    change_units: list[Node] = field(default_factory=list)   # 由 9E 填充
    evidence_ids: list[str] = field(default_factory=list)


# ------------------------------------------------------------ 能力映射（11C）
# broker 方法名 → capability 名。SkillSpec.allowed_capabilities 声明的
# 就是右侧的名字；SkillRuntime 用它做运行期执法（fail fast）。
# 未列出的方法（layer_active/stats/rec/graph…）是自由内省，不算能力。
CAPABILITIES: dict[str, str] = {
    # repository.*：代码层解析与节点读取
    "resolve_target":        "repository.resolve_target",
    "get_target_context":    "repository.get_context",
    "node":                  "repository.node",
    # graph.*：有界视图与路径
    "path":                  "graph.find_path",
    "create_task_view":      "graph.create_task_view",
    "expand_task_view":      "graph.expand_task_view",
    "get_task_view":         "graph.get_task_view",
    # change.*：变更层事实
    "get_change_context":    "change.get_context",
    "find_change_units":     "change.find_units",
    "import_couplings":      "change.get_couplings",
    # evidence.*：证据与 finding 的注册/查询/状态迁移
    "add_evidence":          "evidence.add",
    "get_evidence":          "evidence.get",
    "all_evidence":          "evidence.query",
    "evidence_about":        "evidence.query",
    "add_finding":           "evidence.finding.add",
    "all_findings":          "evidence.query",
    "unsupported_findings":  "evidence.query",
    "conflicts":             "evidence.query",
    "conflicts_involving":   "evidence.query",
    "unresolved_conflicts":  "evidence.query",
    "set_finding_status":    "evidence.finding.set_status",
    "resolve_conflict":      "evidence.finding.set_status",
    # decision.*：决策记忆
    "record_decision":       "decision.record",
    "get_precedents":        "decision.query",
    # policy.*：规则门
    "run_policy_gate":       "policy.gate",
    "check_policy":          "policy.evaluate",
    # semantic.*：语义门面（LLM 白名单在 skill spec 侧）
    "map_semantic_candidates": "semantic.map_candidates",
    "build_query_terms":     "semantic.query_terms",
}


class ContextBroker:
    # "taskview" 是伪层：它门控的是有界 task view，不是图内容
    #（Phase 10 消融里的 G3）
    ALL_LAYERS = {"code", "semantic", "change", "evidence", "decision",
                  "taskview"}

    def __init__(self, repo: Path, rec: ToolRecorder | None = None,
                 layers: set[str] | None = None):
        """layers：本次运行激活哪些层（G0..G4 消融）。None = 当前已建的
        全部。未激活的图层从 v2 投影里剪掉；未激活的能力在 agent 尝试
        使用时大声报错，绝不静默降级。"""
        from src.semgraph.enrich import get_context_graph
        self.repo = repo
        self.rec = rec or ToolRecorder()
        self._v1 = get_context_graph(repo, self.rec)
        self.graph = GraphV2.from_v1(self._v1)
        self.layers = (set(layers) | {"code"}) if layers is not None \
            else set(self.ALL_LAYERS)
        unknown = self.layers - self.ALL_LAYERS
        if unknown:
            raise DataAgentError(f"unknown layers {sorted(unknown)}")
        if layers is not None:
            self.graph = self.graph.prune_to_layers(self.layers)
        # 对象存储（evidence/decision 注册表是进程内 v1）
        self._evidence: dict[str, Evidence] = {}
        self._findings: dict[str, Finding] = {}
        self._conflicts: list[Conflict] = []
        self._decisions: dict[str, Decision] = {}
        self._views: dict[str, TaskGraphView] = {}
        self._counter = 0
        self._mapper_obj = None        # 语义层懒加载（11A：收进门面）

    def layer_active(self, name: str) -> bool:
        return name in self.layers

    # ------------------------------------------------------------ 目标
    def resolve_target(self, query: str) -> Node:
        """确定性目标解析：优先 query 里的精确符号名（camelCase 词元），
        退回文件名匹配。模糊自然语言匹配是 semantic mapper 的职责
        （9D），永远不是这个方法的。"""
        tokens = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
                  if any(c.isupper() for c in t[1:]) or "_" in t]
        for tok in tokens:
            for n in self.graph.nodes_of_type(NodeType.FUNCTION, NodeType.METHOD,
                                              NodeType.CLASS, NodeType.COMPONENT):
                if n.props.get("name") == tok:
                    return n
        # 文件名退路
        for n in self.graph.nodes_of_type(NodeType.FILE):
            name = n.id.split("/")[-1]
            if name in query or name.rsplit(".", 1)[0] in query:
                return n
        raise DataAgentError(f"cannot resolve target from query: {query!r}")

    def get_target_context(self, target_id: str) -> TargetContext:
        node = self.graph.node(target_id)
        if node is None:
            raise GraphError(f"unknown target node {target_id}")
        ctx = TargetContext(target=node)
        g = self.graph

        def is_def(n: Node) -> bool:
            return n.type in (NodeType.FUNCTION, NodeType.METHOD, NodeType.CLASS,
                              NodeType.COMPONENT)

        # caller：scope -CALLS-> callname -REFERENCES-> sym
        for callname in g.neighbors(target_id, rel_types={EdgeType.REFERENCES},
                                    direction="in"):
            for scope in g.neighbors(callname.id, rel_types={EdgeType.CALLS},
                                     direction="in"):
                ctx.direct_callers.append(scope)
        # callee：sym -REFERENCES-> callname <-CALLS- scope（要的是该符号
        # scope 可达的定义）
        scope_id = f"scope:{target_id[4:]}" if target_id.startswith("sym:") else None
        if scope_id and g.node(scope_id):
            for cn in g.neighbors(scope_id, rel_types={EdgeType.CALLS}, direction="out"):
                for d in g.neighbors(cn.id, rel_types={EdgeType.REFERENCES},
                                     direction="out"):
                    if d.id != target_id and is_def(d):
                        ctx.direct_callees.append(d)
        fid = f"file:{node.props.get('file', '')}" if node.props.get("file") else None
        if fid and g.node(fid):
            ctx.importers = g.neighbors(fid, rel_types={EdgeType.IMPORTS},
                                        direction="in")
            for api in g.neighbors(fid, rel_types={EdgeType.DEFINES}, direction="out"):
                if api.type == NodeType.API_ENDPOINT:
                    ctx.related_routes.append(api)
            ctx.definition = node.props.get("file", "")
        # 触达目标的路由：从每个 caller 沿调用链向上走（v1
        # api_routes_reaching 语义，确定性）。逻辑上一步 = callname:{caller
        # 短名} <-CALLS- 各 scope。深度封顶：这是路由发现，不是完整传递
        # 闭包。
        seen_routes: set[str] = {r.id for r in ctx.related_routes}
        seen_scopes: set[str] = set()
        frontier = [c.id for c in ctx.direct_callers]
        for _ in range(3):
            nxt: list[str] = []
            for sid in frontier:
                if sid in seen_scopes:
                    continue
                seen_scopes.add(sid)
                snode = g.node(sid)
                sfile = snode.props.get("file") if snode else None
                if sfile:
                    for api in g.neighbors(f"file:{sfile}",
                                           rel_types={EdgeType.DEFINES},
                                           direction="out"):
                        if api.type == NodeType.API_ENDPOINT and api.id not in seen_routes:
                            seen_routes.add(api.id)
                            ctx.related_routes.append(api)
                short = sid.split("::")[-1]
                for upper in g.neighbors(f"callname:{short}",
                                         rel_types={EdgeType.CALLS},
                                         direction="in"):
                    if upper.id not in seen_scopes:
                        nxt.append(upper.id)
            frontier = nxt
            if not frontier:
                break
        # 读取时铸造 evidence（原则 5）
        ev = Evidence.make(type=EvidenceType.AST,
                           source="broker:get_target_context", target=target_id,
                           location=ctx.definition,
                           payload=f"{len(ctx.direct_callers)} callers, "
                                   f"{len(ctx.direct_callees)} callees, "
                                   f"{len(ctx.importers)} importers")
        self.add_evidence(ev)
        ctx.evidence_ids.append(ev.id)
        return ctx

    # ------------------------------------------------------------ 视图
    def create_task_view(self, task_id: str, target_ids: list[str],
                         relations: set[EdgeType] | None = None) -> TaskGraphView:
        if not self.layer_active("taskview"):
            raise DataAgentError(
                "task views are disabled (taskview layer inactive, G<3)")
        view = TaskGraphView.select(self.graph, task_id, target_ids,
                                    rel_types=relations, trigger="init")
        self._views[task_id] = view
        return view

    def expand_task_view(self, task_id: str, seeds: list[str],
                         relations: set[EdgeType] | None = None,
                         depth: int = 1, trigger: str = "") -> TaskGraphView:
        view = self._views.get(task_id)
        if view is None:
            raise DataAgentError(f"no task view {task_id!r} — create it first")
        view.expand(self.graph, seeds, relations=relations, depth=depth,
                    trigger=trigger)
        return view

    def get_task_view(self, task_id: str) -> TaskGraphView:
        return self._views[task_id]

    # ------------------------------------------------------------ 变更
    def get_change_context(self, target_id: str) -> ChangeContext:
        """时间性上下文：最近触达目标的 ChangeUnit 与 Commit，新的在前，
        HEAD 关联显式可查。回退规划从这里拿单元起步 —— 绝不从"那个
        commit"起步（spec 9E：commit != change unit）。"""
        ctx = ChangeContext(target=target_id)
        g = self.graph
        fid = self._file_of(target_id)
        if not fid:
            return ctx
        units = [cu for cu in g.neighbors(fid, rel_types={EdgeType.MODIFIES},
                                          direction="in")
                 if cu.type == NodeType.CHANGE_UNIT]
        units.sort(key=self._commit_date_of, reverse=True)
        ctx.change_units = units[:10]
        commits = [c for c in g.neighbors(fid, rel_types={EdgeType.CHANGED_BY},
                                          direction="out")
                   if c.type == NodeType.COMMIT]
        commits += [c for c in g.neighbors(fid, rel_types={EdgeType.MODIFIES},
                                           direction="in")
                    if c.type == NodeType.COMMIT]
        dedup: dict[str, Node] = {c.id: c for c in commits}
        ctx.last_commits = sorted(dedup.values(),
                                  key=self._commit_date_of, reverse=True)[:10]
        # 读取时铸造 provenance（原则 5）
        ev = Evidence.make(
            EvidenceType.CHANGE_UNIT, source="broker:get_change_context",
            target=target_id, location=fid,
            payload=f"{len(ctx.change_units)} change unit(s), "
                    f"{len(ctx.last_commits)} commit(s) touch {fid}")
        self.add_evidence(ev)
        ctx.evidence_ids.append(ev.id)
        return ctx

    def _commit_date_of(self, node: Node) -> str:
        if node.type == NodeType.CHANGE_UNIT:
            cn = self.graph.node(f"commit:{node.props.get('commit', '')}")
            return cn.props.get("date", "") if cn else ""
        return node.props.get("date", "")

    def _file_of(self, node_id: str) -> str | None:
        n = self.graph.node(node_id)
        if n is None:
            return None
        if n.type == NodeType.FILE:
            return node_id
        f = n.props.get("file")
        return f"file:{f}" if f else None

    # ------------------------------------------------------------ evidence
    def add_evidence(self, ev: Evidence) -> Evidence:
        existing = self._evidence.get(ev.id)
        if existing:  # 同 id 不同内容是 bug
            if (existing.type, existing.target, existing.location) != \
                    (ev.type, ev.target, ev.location):
                raise GraphError(f"evidence id collision with different facts: {ev.id}")
            return existing
        self._evidence[ev.id] = ev
        props = {"type": ev.type.value, "source": ev.source,
                 "target": ev.target, "location": ev.location,
                 "payload": ev.payload[:300], "producer": ev.producer,
                 "timestamp": ev.timestamp}
        if ev.provenance:  # 上游 evidence id —— 审计链（9F）
            props["provenance"] = dict(list(ev.provenance.items())[:5])
        self.graph.add_node(Node(ev.id, NodeType.EVIDENCE, props=props))
        if self.graph.node(ev.target):
            self.graph.add_edge(Edge(ev.target, ev.id, EdgeType.SUPPORTED_BY,
                                     props={"role": "about"}))
        return ev

    def get_evidence(self, ids: list[str]) -> list[Evidence]:
        return [self._evidence[i] for i in ids if i in self._evidence]

    def all_evidence(self) -> list[Evidence]:
        return list(self._evidence.values())

    # ------------------------------------------------------------ finding
    def add_finding(self, finding: Finding) -> Finding:
        """注册 finding。绝不覆盖；与既有 finding 的矛盾登记成冲突交给
        verifier（原则 6）。"""
        fid = finding.id
        if fid in self._findings:
            return self._findings[fid]
        self._findings[fid] = finding
        self.graph.add_node(Node(fid, NodeType.FINDING,
                                 props={"statement": finding.statement,
                                        "producer": finding.producer,
                                        "status": finding.status}))
        for eid in finding.evidence_ids:
            if self._evidence.get(eid):
                self.graph.add_edge(Edge(fid, eid, EdgeType.SUPPORTED_BY))
        # 显式冲突检测（同主题、断言不合）
        self._register_conflicts(finding)
        return finding

    def _register_conflicts(self, finding: Finding) -> None:
        fkey = _topic_symbols(finding.statement)
        for other in self._findings.values():
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
                self._conflicts.append(c)
                finding.contradicts.append(other.id)
                other.contradicts.append(finding.id)
                self.graph.add_edge(Edge(finding.id, other.id,
                                         EdgeType.CONTRADICTS,
                                         props={"topic": topic}))

    def all_findings(self) -> list[Finding]:
        return list(self._findings.values())

    @property
    def conflicts(self) -> list[Conflict]:
        return list(self._conflicts)

    # ------------------------------------------------------------ 校验（9F/9G）
    def evidence_about(self, target_id: str,
                       ev_type: EvidenceType | None = None) -> list[Evidence]:
        """关于某个图节点已注册的全部 evidence（可按类型过滤）。"""
        out = [e for e in self._evidence.values() if e.target == target_id]
        if ev_type is not None:
            out = [e for e in out if e.type == ev_type]
        return sorted(out, key=lambda e: e.timestamp)

    def unresolved_conflicts(self) -> list[Conflict]:
        return [c for c in self._conflicts if not c.resolved]

    def conflicts_involving(self, finding_id: str) -> list[Conflict]:
        return [c for c in self._conflicts
                if finding_id in (c.finding_a, c.finding_b)]

    def unsupported_findings(self) -> list[Finding]:
        """引用了从未注册的 evidence id 的 finding —— 它们的
        SUPPORTED_BY 边指向空气。喂给 policy gate。"""
        return [f for f in self._findings.values()
                if f.evidence_ids and not all(e in self._evidence
                                              for e in f.evidence_ids)]

    def set_finding_status(self, finding_id: str, status: str,
                           verifier: str = "") -> Finding:
        """带守卫的状态迁移（9F/9G）：
        - verified 要求证据已注册且无未解决冲突
        - verifier 名字落到图节点上（审计，不是 CoT）"""
        f = self._findings.get(finding_id)
        if f is None:
            raise DataAgentError(f"unknown finding {finding_id!r}")
        if status not in ("proposed", "verified", "unsupported", "contradicted"):
            raise DataAgentError(f"invalid finding status {status!r}")
        if status == "verified":
            if not f.evidence_ids:
                raise DataAgentError(
                    f"cannot verify {finding_id!r}: no evidence cited — "
                    "a finding without evidence is unsupported, never verified")
            missing = [e for e in f.evidence_ids if e not in self._evidence]
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
        node = self.graph.node(finding_id)
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
        if conflict not in self._conflicts:
            raise DataAgentError("unknown conflict — not registered by this broker")
        conflict.resolved = True
        conflict.resolution = resolution
        if winner:
            loser = next(fid for fid in (conflict.finding_a, conflict.finding_b)
                         if fid != winner)
            self.set_finding_status(loser, "contradicted", verifier=resolver)
        self.record_decision(Decision.make(
            "conflict_resolution", resolution, target=conflict.topic,
            related_findings=[conflict.finding_a, conflict.finding_b],
            risk="medium", decision_maker=resolver,
            reason_summary=f"winner={winner or 'none'}"))
        return conflict

    # ------------------------------------------------------------ 决策（9H）
    # 审计文本的硬上限：decision 只存 reason 摘要，绝不存模型隐藏思维链
    #（spec 9H）
    REASON_SUMMARY_CAP = 500

    def record_decision(self, d: Decision) -> Decision:
        if len(d.reason_summary) > self.REASON_SUMMARY_CAP:
            self.rec.warn(
                f"decision {d.id}: reason_summary {len(d.reason_summary)} chars "
                f"capped to {self.REASON_SUMMARY_CAP} — hidden CoT is not stored")
            d.reason_summary = d.reason_summary[:self.REASON_SUMMARY_CAP - 1] + "…"
        self._decisions[d.id] = d
        self.graph.add_node(Node(d.id, NodeType.DECISION, props={
            "category": d.category, "outcome": d.outcome, "task_id": d.task_id,
            "risk": d.risk, "decision_maker": d.decision_maker,
            "reason_summary": d.reason_summary, "policy": d.policy}))
        for eid in d.evidence_ids:
            if self._evidence.get(eid):
                self.graph.add_edge(Edge(d.id, eid, EdgeType.SUPPORTED_BY))
        for fid in d.related_findings:
            if self._findings.get(fid):
                self.graph.add_edge(Edge(d.id, fid, EdgeType.DERIVED_FROM))
        return d

    def get_precedents(self, category: str = "", target: str = "",
                       query: str = "") -> list[Decision]:
        """决策记忆查询：按 category、按目标节点、和/或按自由文本 query
        （query 的符号必须出现在 decision 的文本块里）。"""
        qsyms = _topic_symbols(query) if query else frozenset()
        out = []
        for d in self._decisions.values():
            if category and d.category != category:
                continue
            if target and target not in (d.target or ""):
                continue
            if qsyms:
                blob = f"{d.outcome} {d.reason_summary} {d.target}"
                if not (qsyms & _topic_symbols(blob)):
                    continue
            out.append(d)
        return sorted(out, key=lambda d: d.timestamp)

    # ------------------------------------------------------------ 策略（9I）
    def check_policy(self, rule_name: str, context: dict) -> PolicyResult:
        from src.semgraph.policy import POLICY_RULES
        rule = POLICY_RULES.get(rule_name)
        if rule is None:
            raise DataAgentError(f"unknown policy rule {rule_name!r}")
        return rule.evaluate(context)

    def run_policy_gate(self, context: dict, task_id: str = "") -> PolicyResult:
        """跑完整规则集并把结果记成可审计 decision。返回触发的最重动作。
        服从 BLOCK 是 orchestrator 的契约 —— gate 只裁决，不执行。"""
        from src.semgraph.policy import gate
        result = gate(context)
        risk = {"PASS": "low", "HUMAN_REVIEW": "medium",
                "BLOCK": "high"}[result.action.value]
        self.record_decision(Decision.make(
            "policy_gate", result.action.value, task_id=task_id, risk=risk,
            decision_maker="PolicyGate",
            reason_summary=result.detail[:200],
            policy=(f"{result.rule.name} v{result.rule.version} -> "
                    f"{result.action.value}") if result.rule else "none-triggered"))
        return result

    # ------------------------------------------------------------ 语义门面（11A）
    def _mapper(self):
        """SemanticMapper 懒加载 + 确定性播种（幂等）。语义层未激活时
        返回 None —— 调用方据此走确定性退路。"""
        if self._mapper_obj is None and self.layer_active("semantic"):
            from src.semgraph.semantic_mapper import SemanticMapper
            self._mapper_obj = SemanticMapper(self.graph, self.rec)
            self._mapper_obj.seed_deterministic()
        return self._mapper_obj

    def map_semantic_candidates(self, query: str, llm=None) -> list:
        """模糊 query → feature 候选（agent/skill 不再自己构造 mapper）。"""
        mapper = self._mapper()
        return mapper.map_query(query, llm=llm) if mapper is not None else []

    def build_query_terms(self, query: str, feature_name: str) -> list[str]:
        """变更分析词表（query 词元 + feature 别名）。"""
        from src.semgraph.semantic_mapper import query_vocabulary
        return query_vocabulary(query, feature_name)

    # ------------------------------------------------------------ 工具
    def node(self, node_id: str) -> Node | None:
        return self.graph.node(node_id)

    def find_change_units(self, label: str = "", file: str = "",
                          commit: str = "") -> list[Node]:
        """给 change-intelligence agent 的 ChangeUnit 查询（file 是对
        单元文件的子串匹配）。"""
        out = []
        for cu in self.graph.nodes_of_type(NodeType.CHANGE_UNIT):
            p = cu.props
            if label and p.get("semantic_label", "") != label:
                continue
            if commit and p.get("commit", "") != commit:
                continue
            if file and not any(file in f for f in p.get("files", [])):
                continue
            out.append(cu)
        return out

    def import_couplings(self, files_a: list[str],
                         files_b: list[str]) -> list[str]:
        """两个文件集合之间的直接 IMPORTS 边，双向都查 —— 回退规划的
        附带损伤信号。"""
        a = {f"file:{f}" for f in files_a}
        b = {f"file:{f}" for f in files_b}
        out = []
        for fa in a:
            for e in self.graph.edges_from(fa):
                if e.type == EdgeType.IMPORTS and e.dst in b and e.dst != fa:
                    out.append(f"{fa[len('file:'):]} imports {e.dst[len('file:'):]}")
        for fb in b:
            for e in self.graph.edges_from(fb):
                if e.type == EdgeType.IMPORTS and e.dst in a and e.dst != fb:
                    out.append(f"{fb[len('file:'):]} imports {e.dst[len('file:'):]}")
        return sorted(set(out))

    def path(self, a: str, b: str) -> list[str] | None:
        """经 v1 PathFinder 查多跳关联路径（broker 负责保持同步）。"""
        self.graph.sync_back_to_v1(self._v1)
        return self._v1.path(a, b)

    def stats(self) -> dict:
        return {"graph": self.graph.stats(),
                "evidence": len(self._evidence),
                "findings": len(self._findings),
                "conflicts": len(self._conflicts),
                "decisions": len(self._decisions),
                "views": {t: v.stats() for t, v in self._views.items()}}


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
