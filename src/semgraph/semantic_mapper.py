"""Semantic Feature Layer (Phase 9D): Feature/Capability/Concept nodes.

Two ways features come into existence (spec 9D):

A. deterministic seeds — rules over route/component/config/UIString nodes.
   These are FACTS: written into the graph with IMPLEMENTS edges and
   SEMANTIC_MAPPING evidence recording the seeding rule.

B. optional LLM semantic mapping — natural-language query -> feature/
   concept candidates. The LLM may ONLY pick/map names; it can never mint
   deterministic relations (CALLS/IMPORTS/...). Unevidenced mappings stay
   `status="candidate"` and are NOT written into the graph as facts.

This layer exists to answer fuzzy questions like "系统标题在哪" with a
feature handle (SystemBranding) that then resolves deterministically to
files/symbols — LLM understands, tools prove.
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
    """Builds and queries the semantic layer of a GraphV2."""

    def __init__(self, graph: GraphV2, rec: ToolRecorder | None = None):
        self.g = graph
        self.rec = rec or ToolRecorder()

    # ------------------------------------------------------------ seeds (A)
    def seed_deterministic(self) -> list[Node]:
        """Rule-based Feature nodes. Facts, written with provenance."""
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
        # root layout of a Next.js app *is* the system branding surface
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

    # ------------------------------------------------------------ query
    def map_query(self, query: str, llm=None,
                  max_candidates: int = 5) -> list[SemanticTargetCandidate]:
        """Query -> feature candidates. Deterministic lexical match first;
        optional LLM refinement second. Candidates without evidence never
        enter the graph."""
        qt = extract_terms(query)
        terms = {t.lower() for t, _ in qt.all_search_terms()} | \
                {s for s in qt.cjk_segments} | {s for s in qt.cjk_subterms}
        scored: list[SemanticTargetCandidate] = []

        for feat in self.g.nodes_of_type(NodeType.FEATURE):
            name = feat.props.get("name", "")
            if not name:  # unnamed features (test/legacy artifacts) never match
                continue
            name_l = name.lower()
            hit = 0.0
            for t in terms:
                if not t:
                    continue
                if t in name_l or name_l in t:
                    hit = max(hit, 3.0)
                # subtoken match: UserLogin vs query "login"
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

        # curated zh aliases for the seeded naming scheme — data, not logic
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
        """LLM picks from EXISTING feature names only; output stays
        candidate-grade evidence (mapping_method='llm'), never a fact."""
        feats = [{"feature_id": f.id, "name": f.props.get("name", ""),
                  "seeded_by": f.props.get("seeded_by", "")}
                 for f in self.g.nodes_of_type(NodeType.FEATURE)][:40]
        if not feats:
            return []
        try:
            out = llm.chat_json(
                system="You map natural-language maintenance queries to "
                       "software features. Reply ONLY with JSON: "
                       '{"candidates": [{"feature_id": "...", "reason": "..."}]}. '
                       "Pick only from the given features. Never invent "
                       "call/import relations.",
                user=f"Features: {feats}\nQuery: {query}")
        except Exception as e:  # LLM problems surface as candidate absence,
            # not as a crash of the deterministic path
            self.rec.warn(f"semantic mapper: llm mapping skipped ({e})")
            return []
        cands = []
        by_id = {f["feature_id"]: f for f in feats}
        for c in (out or {}).get("candidates", [])[:3]:
            fid = c.get("feature_id")
            if fid not in by_id:
                continue  # hallucinated ids are dropped, not trusted
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


# curated zh aliases — extensible data table, not hard-coded branching
ZH_FEATURE_ALIASES: list[tuple[str, list[str]]] = [
    ("SystemBranding", ["标题", "系统标题", "网站名", "品牌", "title", "branding"]),
    ("AuthLogin", ["登录", "登陆", "登录验证", "账号", "auth", "login"]),
    ("AuthRegister", ["注册", "signup", "register"]),
]


def _zh_alias(query: str) -> list[tuple[str, list[str]]]:
    q = query.lower()
    return [(name, terms) for name, terms in ZH_FEATURE_ALIASES
            if any(t.lower() in q for t in terms)]
