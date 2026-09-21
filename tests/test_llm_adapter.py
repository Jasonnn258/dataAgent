"""Phase 11H 测试：LLM Semantic Reasoning Adapter。

核心承诺：
- 确定性 fixture 上 LLM 调用数为 0（默认路径不碰 LLM）
- 只有 semantic_reasoning=allowed 的 skill（resolve_target）能触发 LLM
- prompt 迁出 mapper 后行为不变：幻觉 id 丢弃、candidate 级、
  异常退化为"没有候选"
- 预留接口（label/verify）在 LLM 缺位时确定性退路，不抛错
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.schema import ToolRecorder  # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"


def _walk(nodes):
    """深度优先走执行树，产出每个节点。"""
    for n in nodes:
        yield n
        yield from _walk(n["children"])


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(scope="module")
def broker(seeded):
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    b = ContextBroker(FIXTURE, ToolRecorder())
    build_change_graph(b)
    return b


class FakeLLM:
    """计数型假 LLM：记录每次调用，返回可配置的候选。"""
    available = True

    def __init__(self, out=None, raise_exc=None):
        self.calls = 0
        self._out = out or {"candidates": [
            {"feature_id": "feature:AuthLogin", "reason": "query mentions login"}]}
        self._raise = raise_exc

    def chat_json(self, system, user):
        self.calls += 1
        if self._raise:
            raise self._raise
        return self._out


# ================================================================ adapter 本体
class TestAdapterInvariants:
    def test_hallucinated_ids_dropped(self):
        from src.llm import SemanticReasoningAdapter
        llm = FakeLLM(out={"candidates": [
            {"feature_id": "feature:AuthLogin", "reason": "real"},
            {"feature_id": "feature:Ghost", "reason": "hallucination"}]})
        feats = [{"feature_id": "feature:AuthLogin", "name": "AuthLogin"}]
        picks = SemanticReasoningAdapter(llm).map_features(feats, DEMO_QUERY)
        assert [p["feature_id"] for p in picks] == ["feature:AuthLogin"]

    def test_unavailable_returns_empty_without_calling(self):
        from src.llm import SemanticReasoningAdapter

        class Dead:
            available = False
            def chat_json(self, *a, **kw):  # pragma: no cover - 不该被调
                raise AssertionError("dead llm must not be called")
        llm = Dead()
        assert SemanticReasoningAdapter(llm).map_features(
            [{"feature_id": "x"}], "q") == []

    def test_reserved_interfaces_degrade_deterministically(self):
        from src.llm import SemanticReasoningAdapter

        class Dead:
            available = False
        ad = SemanticReasoningAdapter(Dead())
        label = ad.label_change_unit("rename title", ["title"])
        assert label["label"] == "other" and label["fallback"] is True
        verdict = ad.verify_semantic("claim", ["evidence text"])
        assert verdict["verdict"] == "unresolved" and verdict["fallback"]

    def test_label_and_verdict_whitelists_enforced(self):
        from src.llm import SemanticReasoningAdapter
        llm = FakeLLM(out={"label": "finance", "confidence": 9,
                           "reason": "junk"})
        ad = SemanticReasoningAdapter(llm)
        assert ad.label_change_unit("d", ["t"])["label"] == "other"
        assert ad.label_change_unit("d", ["t"])["confidence"] == 1.0  # 越界截断
        llm2 = FakeLLM(out={"verdict": "definitely", "reason": "junk"})
        assert SemanticReasoningAdapter(llm2).verify_semantic(
            "claim", ["ev"])["verdict"] == "unresolved"


# ================================================================ prompt 迁移
class TestPrompts:
    def test_every_prompt_carries_invariants(self):
        from src.llm.prompts import invariants
        from src.llm.prompts.skills import (change_labeling,
                                            semantic_mapping,
                                            semantic_verification)
        for prompt in (semantic_mapping.SYSTEM_PROMPT,
                       change_labeling.SYSTEM_PROMPT,
                       semantic_verification.SYSTEM_PROMPT):
            assert invariants.SEMANTIC_INVARIANTS in prompt
            assert "JSON" in prompt  # 严格 schema 要求在正文里

    def test_mapping_prompt_migrated_out_of_mapper(self):
        """prompt 文本不再散落在 mapper 源码里（迁入 prompts/）。"""
        text = (ROOT / "src" / "semgraph" / "semantic_mapper.py").read_text()
        assert "Reply ONLY with JSON" not in text
        assert "You map natural-language" not in text

    def test_mapper_behavior_unchanged_via_adapter(self, broker):
        mapper = broker._semantic_svc.mapper()   # 服务路径播种（幂等）
        cands = mapper.map_query("怎么改账号校验", llm=FakeLLM())
        hit = [c for c in cands if c.feature_id == "feature:AuthLogin"
               and c.mapping_method == "llm"]
        assert hit and hit[0].status == "candidate"

    def test_llm_failure_degrades_to_no_candidates(self, broker):
        mapper = broker._semantic_svc.mapper()
        llm = FakeLLM(raise_exc=RuntimeError("endpoint down"))
        cands = mapper.map_query("怎么改账号校验", llm=llm)
        assert all(c.mapping_method != "llm" for c in cands)
        assert broker.rec.warnings  # 退化留痕


# ================================================================ spec 点名测试
class TestLLMGating:
    def test_deterministic_workflow_zero_llm_calls(self, seeded):
        """回归点 7：确定性 fixture 上完整工作流 LLM 调用数 = 0。"""
        from src.execution import ExecutionRecorder
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.agents import Orchestrator
        b = ContextBroker(FIXTURE, ExecutionRecorder())
        build_change_graph(b)
        o = Orchestrator(b)   # 不注入 llm：默认全确定性
        r = o.run(DEMO_QUERY, keep_hint=DEMO_KEEP)
        assert len(r.dump()) > 1000   # 工作流真的跑完了（G4 基线 1282 chars）
        assert not [e for e in b.rec.events if e.layer == "llm"]
        assert not [c for c in b.rec.calls if c.startswith("llm:")]

    def test_only_whitelisted_skills_can_call_llm(self, broker):
        """回归点 8：注入 llm 后，八个 skill 里只有 resolve_target
        触发 LLM，且恰好一次。"""
        from src.skills import SkillRuntime
        llm = FakeLLM()
        rt = SkillRuntime(broker, llm=llm)
        nav = rt.run("resolve_target", {"query": "怎么改账号校验"})
        assert nav.status == "success" and llm.calls == 1
        cu = rt.run("change_unit_analysis", {"terms": nav.data["terms"]})
        assert cu.status == "success"
        rt.run("evidence_verification",
               {"finding_ids": [m["finding_id"] for m in cu.data["matches"]]})
        rt.run("policy_check", {"rollback_symbols": ["validateAccount"],
                                "keep_symbols": ["rewordTitle"],
                                "affected_routes": ["/api/auth/login"]})
        rt.run("build_task_view", {"task_id": "tv-h",
                                   "target_ids": nav.data["related_symbols"][:1]})
        assert llm.calls == 1, "非白名单 skill 一个 LLM 调用都不能发生"

    def test_llm_event_gets_own_layer(self, seeded):
        """LLM 调用单独成层（EventLayer.LLM），确定性路径不产生该层。"""
        from src.execution import ExecutionRecorder
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.agents import Orchestrator
        b = ContextBroker(FIXTURE, ExecutionRecorder())
        build_change_graph(b)
        n0 = len(b.rec.events)
        Orchestrator(b, llm=FakeLLM()).run(DEMO_QUERY, keep_hint=DEMO_KEEP)
        llm_events = [e for e in b.rec.events[n0:] if e.layer == "llm"]
        assert llm_events and all(e.actor == "semantic_mapping"
                                  for e in llm_events)
        # LLM span 嵌在 resolve_target skill span 之下
        tree = b.rec.tree()
        parents = [n for n in _walk(tree["roots"])
                   if n["event"].layer == "llm"]
        assert parents and parents[0]["event"].parent_trace_id
