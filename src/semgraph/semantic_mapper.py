"""语义特征层（Phase 9D）：Feature/Capability/Concept 节点。

Feature 有两条产生路径（spec 9D）：

A. 确定性种子 —— 基于 route/component/config/UIString 节点的规则。
   这些是事实：连同 IMPLEMENTS 边和记录种子规则的 SEMANTIC_MAPPING
   evidence 一并写入图。

B. 可选的 LLM 语义映射 —— 自然语言 query → feature/concept 候选。
   LLM 只能选择/映射已有名字，永远不能铸造确定性关系（CALLS/
   IMPORTS/...）。无证据映射保持 `status="candidate"`，绝不作为事实
   写入图。

这一层负责回答"系统标题在哪"这类模糊问题：先给出 feature 句柄
（SystemBranding），再由确定性工具解析到文件/符号 —— LLM 负责理解，
工具负责证明。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.schema import ToolRecorder
from src.search.keywords import extract_terms
from src.semgraph.objects import Evidence, EvidenceType
from src.semgraph.schema_v2 import Edge, EdgeType, GraphV2, Node, NodeType


@dataclass
class SemanticTargetCandidate:
    feature_id: str
    name: str
    related_symbols: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    mapping_method: str = ""            # seed:route | seed:component | llm | lexical
    status: str = "candidate"           # candidate | seeded
    score: float = 0.0


def _camel(*parts: str) -> str:
    out = ""
    for p in parts:
        for seg in re.split(r"[^A-Za-z0-9]+", p):
            if not seg:
                continue
            seg = re.sub(r"^\d+", "", seg)
            if not seg:
                continue
            out += seg[0].upper() + seg[1:]
    return out


class SemanticMapper:
    """构建并查询 GraphV2 的语义层。"""

    def __init__(self, graph: GraphV2, rec: ToolRecorder | None = None):
        self.g = graph
        self.rec = rec or ToolRecorder()

    # ------------------------------------------------------------ 种子（A）
    def seed_deterministic(self) -> list[Node]:
        """规则生成的 Feature 节点。是事实，带 provenance 写入。"""
        created: list[Node] = []
        for api in self.g.nodes_of_type(NodeType.API_ENDPOINT):
            route = api.props.get("route", "")
            if not route:
                continue
            segs = [s for s in route.strip("/").split("/") if s and s != "api"]
            name = _camel(*segs) or _camel(api.id)
            created += self._seed_feature(
                f"feature:{name}", name, implements=api.id,
                rule=f"seed:route {route}", kind="Feature")
        for comp in self.g.nodes_of_type(NodeType.COMPONENT):
            name = comp.props.get("name", "")
            if not name:
                continue
            created += self._seed_feature(
                f"feature:{name}", name, implements=comp.id,
                rule=f"seed:component {comp.props.get('file', '')}",
                kind="Feature")
        # Next.js 应用的根 layout 本身就是系统品牌面
        for f in self.g.nodes_of_type(NodeType.FILE):
            if f.id.endswith("app/layout.tsx"):
                created += self._seed_feature(
                    "feature:SystemBranding", "SystemBranding",
                    implements=f.id,
                    rule="seed:root-layout app/layout.tsx", kind="Feature")
        return created

    def _seed_feature(self, fid: str, name: str, implements: str,
                      rule: str, kind: str) -> list[Node]:
        existing = self.g.node(fid)
        if existing is not None:
            return []
        node = Node(fid, NodeType.FEATURE,
                    props={"name": name, "seeded_by": rule, "status": "seeded"})
        self.g.add_node(node)
        if self.g.node(implements):
            self.g.add_edge(Edge(fid, implements, EdgeType.IMPLEMENTS,
                                 props={"seeded_by": rule}))
        ev = Evidence.make(EvidenceType.SEMANTIC_MAPPING, source=rule,
                           target=fid, payload=f"feature seeded from {implements}")
        self.rec.tool(f"semantic:{rule.split()[0]}")
        node.props["evidence_id"] = ev.id
        return [node]

    # ------------------------------------------------------------ 查询
    def map_query(self, query: str, llm=None,
                  max_candidates: int = 5) -> list[SemanticTargetCandidate]:
        """query → feature 候选。先做确定性词面匹配，再可选 LLM 精化。
        无证据的候选绝不进图。"""
        qt = extract_terms(query)
        terms = {t.lower() for t, _ in qt.all_search_terms()} | \
                {s for s in qt.cjk_segments} | {s for s in qt.cjk_subterms}
        scored: list[SemanticTargetCandidate] = []

        for feat in self.g.nodes_of_type(NodeType.FEATURE):
            name = feat.props.get("name", "")
            if not name:  # 无名 feature（测试/遗留产物）永不匹配
                continue
            name_l = name.lower()
            hit = 0.0
            for t in terms:
                if not t:
                    continue
                if t in name_l or name_l in t:
                    hit = max(hit, 3.0)
                # 子词匹配：UserLogin 对 query "login"
                elif t in re.findall(r"[a-z]+|[A-Z][a-z]*", name_l.lower()):
                    hit = max(hit, 2.0)
            if hit <= 0:
                continue
            cand = SemanticTargetCandidate(
                feature_id=feat.id, name=name, mapping_method="lexical",
                status=feat.props.get("status", "candidate"), score=hit)
            cand.related_symbols = self._related_symbols(feat.id)
            ev = Evidence.make(
                EvidenceType.SEMANTIC_MAPPING, source="mapper:lexical",
                target=feat.id, payload=f"query terms {sorted(t for t in terms if t)[:5]} "
                f"match feature name '{name}'")
            cand.evidence.append(ev)
            scored.append(cand)

        # 种子命名方案配套的人工整理中文别名 —— 是数据不是逻辑
        alias = _zh_alias(query)
        for feat in self.g.nodes_of_type(NodeType.FEATURE):
            for a_name, a_terms in alias:
                if feat.props.get("name") == a_name:
                    cand = SemanticTargetCandidate(
                        feature_id=feat.id, name=a_name,
                        mapping_method="seed-alias",
                        status=feat.props.get("status", "candidate"), score=4.0)
                    cand.related_symbols = self._related_symbols(feat.id)
                    cand.evidence.append(Evidence.make(
                        EvidenceType.SEMANTIC_MAPPING, source="mapper:zh-alias",
                        target=feat.id,
                        payload=f"query matches alias terms {a_terms} of {a_name}"))
                    scored.append(cand)

        if llm is not None and llm.available:
            scored += self._llm_map(query, llm)

        scored.sort(key=lambda c: -c.score)
        return scored[:max_candidates]

    def _llm_map(self, query: str, llm) -> list[SemanticTargetCandidate]:
        """LLM 只能从已有 feature 名里挑；输出停留在 candidate 级证据
        （mapping_method='llm'），永远不是事实。

        Phase 11H：prompt 与不变量过滤迁入 SemanticReasoningAdapter，
        本方法只负责把候选包装成 SemanticTargetCandidate + 铸
        candidate 级证据；LLM 调用单独成层（EventLayer.LLM）。
        """
        from src.llm.semantic_adapter import SemanticReasoningAdapter
        feats = [{"feature_id": f.id, "name": f.props.get("name", ""),
                  "seeded_by": f.props.get("seeded_by", "")}
                 for f in self.g.nodes_of_type(NodeType.FEATURE)][:40]
        if not feats:
            return []
        adapter = SemanticReasoningAdapter(llm)
        span = getattr(self.rec, "span", None)
        try:
            if span is None:
                self.rec.tool("llm:semantic_mapping")
                picks = adapter.map_features(feats, query)
            else:
                with span("llm", "semantic_mapping", "map"):
                    picks = adapter.map_features(feats, query)
        except Exception as e:  # LLM 出问题时表现为"没有候选"，
            # 绝不让确定性路径跟着崩
            self.rec.warn(f"semantic mapper: llm mapping skipped ({e})")
            return []
        cands = []
        by_id = {f["feature_id"]: f for f in feats}
        for c in picks:
            fid = c["feature_id"]
            cand = SemanticTargetCandidate(
                feature_id=fid, name=by_id[fid]["name"], mapping_method="llm",
                status="candidate", score=1.5)
            cand.evidence.append(Evidence.make(
                EvidenceType.SEMANTIC_MAPPING, source="mapper:llm",
                target=fid, payload=f"llm reason: {c.get('reason', '')[:200]}"))
            cand.related_symbols = self._related_symbols(fid)
            cands.append(cand)
        return cands

    def _related_symbols(self, feature_id: str) -> list[str]:
        out = []
        for e in self.g.edges_from(feature_id):
            if e.type in (EdgeType.IMPLEMENTS, EdgeType.REPRESENTS,
                          EdgeType.RELATED_TO):
                out.append(e.dst)
        return out


# 人工整理的中文别名 —— 可扩展的数据表，不是硬编码分支
ZH_FEATURE_ALIASES: list[tuple[str, list[str]]] = [
    ("SystemBranding", ["标题", "系统标题", "网站名", "品牌", "title", "branding"]),
    ("AuthLogin", ["登录", "登陆", "登录验证", "账号", "auth", "login"]),
    ("AuthRegister", ["注册", "signup", "register"]),
]


def _zh_alias(query: str) -> list[tuple[str, list[str]]]:
    q = query.lower()
    return [(name, terms) for name, terms in ZH_FEATURE_ALIASES
            if any(t.lower() in q for t in terms)]


def query_vocabulary(query: str, feature_name: str) -> list[str]:
    """给变更分析用的查询词表：query 自身词元 + 命中 feature 的别名词汇。
    （Phase 11A：从 navigator._terms_for 移入，agent/skill 经 broker 调用，
    search 依赖留在 semgraph 内部。）"""
    terms = {query}
    qt = extract_terms(query)
    terms |= {t.lower() for t, _ in qt.all_search_terms()}
    terms |= set(qt.cjk_segments) | set(qt.cjk_subterms)
    terms.add(feature_name.lower())
    for name, alias_terms in ZH_FEATURE_ALIASES:
        if name == feature_name:
            terms |= {t.lower() for t in alias_terms}
    return sorted(t for t in terms if t)
