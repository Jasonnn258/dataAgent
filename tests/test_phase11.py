"""Phase 11 测试：Skill 抽象 / 注册表 / runtime / 八个一等 Skill。

Skill 只经 ContextBroker 取能力 —— 用 fixture 上的真实行为验证，
而不是空壳 smoke。
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.errors import DataAgentError  # noqa: E402
from src.schema import ToolRecorder  # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"

SKILL_NAMES = {"resolve_target", "build_task_view", "impact_analysis",
               "change_unit_analysis", "coupling_analysis", "safe_rollback",
               "evidence_verification", "policy_check"}


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(scope="module")
def broker(seeded):
    """全层 broker + 变更层（与验收演示同配置）。"""
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    b = ContextBroker(FIXTURE, ToolRecorder())
    build_change_graph(b)
    return b


@pytest.fixture(scope="module")
def runtime(broker):
    from src.skills import SkillRuntime
    return SkillRuntime(broker)


# ================================================================ 契约
class TestSkillContracts:
    def test_registry_has_the_eight_skills(self):
        from src.skills import default_registry
        assert set(default_registry()) == SKILL_NAMES

    def test_every_spec_is_complete(self):
        from src.skills import default_registry
        for name, skill in default_registry().items():
            s = skill.spec
            assert s.name == name and s.description
            assert s.required_inputs, name
            assert s.produced_outputs, name
            assert s.allowed_capabilities, name
            assert s.preconditions and s.success_conditions, name

    def test_llm_whitelist_first_version(self):
        """11H 前置：默认全部 forbidden，唯一例外 ResolveTargetSkill。"""
        from src.skills import default_registry
        allowed = {n for n, sk in default_registry().items()
                   if sk.spec.semantic_reasoning == "allowed"}
        assert allowed == {"resolve_target"}

    def test_duplicate_registration_raises(self):
        from src.skills.base import BaseSkill
        from src.skills.registry import SKILL_REGISTRY, register
        from src.skills.spec import SkillSpec

        class DupSkill(BaseSkill):
            spec = SkillSpec(name="resolve_target", description="dup")

        with pytest.raises(DataAgentError):
            register(DupSkill())
        assert "dup" not in SKILL_NAMES  # 未污染注册表之外的形态

    def test_missing_inputs_fails_fast(self, runtime):
        r = runtime.run("coupling_analysis", {})
        assert r.status == "failed"
        assert "missing required inputs" in r.error

    def test_unknown_skill_fails(self, runtime):
        r = runtime.run("no_such_skill", {})
        assert r.status == "failed" and "unknown skill" in r.error

    def test_skill_run_leaves_tool_trail(self, broker, runtime):
        n0 = len(broker.rec.calls)
        runtime.run("coupling_analysis", {"files_a": ["src/lib/retry.ts"],
                                          "files_b": ["src/lib/ai-helpers.ts"]})
        assert f"skill:coupling_analysis:run" in broker.rec.calls[n0:]


# ================================================================ resolve_target
class TestResolveTargetSkill:
    def test_fuzzy_zh_query_maps_to_feature(self, runtime):
        r = runtime.run("resolve_target", {"query": DEMO_QUERY})
        assert r.status == "success"
        assert r.data["feature_id"] == "feature:AuthLogin"
        assert "登录" in r.data["terms"] and "auth" in r.data["terms"]
        assert r.data["finding_id"] and r.evidence_ids

    def test_unresolvable_query_fails_loud(self, seeded):
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime
        b = ContextBroker(FIXTURE, ToolRecorder(), layers={"code"})
        r = SkillRuntime(b).run("resolve_target", {"query": "完全无关 zzz"})
        assert r.status == "failed" and "cannot resolve target" in r.error


# ================================================================ change units
class TestChangeUnitAnalysisSkill:
    def test_matches_demo_auth_unit_with_evidence(self, runtime, broker):
        nav = runtime.run("resolve_target", {"query": DEMO_QUERY})
        r = runtime.run("change_unit_analysis", {"terms": nav.data["terms"]})
        assert r.status == "success"
        ids = {m["unit_id"] for m in r.data["matches"]}
        assert any("bbdc659f-U2" in i for i in ids)
        for m in r.data["matches"]:
            assert m["score"] > 0 and m["finding_id"] and m["match_evidence_id"]
            # 事实与证据同时产生（不是事后补挂）
            assert broker.get_evidence([m["match_evidence_id"]])

    def test_commit_pinning_restricts_matches(self, runtime):
        r = runtime.run("change_unit_analysis",
                        {"terms": ["auth", "login", "登录"], "commits": ["deadbeef"]})
        assert r.status == "success" and r.data["matches"] == []

    def test_change_layer_inactive_is_partial(self, seeded):
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime
        b = ContextBroker(FIXTURE, ToolRecorder(), layers={"code", "semantic"})
        r = SkillRuntime(b).run("change_unit_analysis", {"terms": ["auth"]})
        assert r.status == "partial" and r.data["matches"] == []


# ================================================================ coupling
class TestCouplingAnalysisSkill:
    def test_detects_import_coupling(self, runtime):
        r = runtime.run("coupling_analysis", {
            "files_a": ["src/lib/ai-helpers.ts"],
            "files_b": ["src/lib/retry.ts"]})
        assert r.status == "success"
        assert r.data["couplings"] == [
            "src/lib/ai-helpers.ts imports src/lib/retry.ts"]

    def test_decoupled_sets_are_empty_not_failed(self, runtime):
        r = runtime.run("coupling_analysis", {
            "files_a": ["src/app/layout.tsx"],
            "files_b": ["src/lib/auth.ts"]})
        assert r.status == "success" and r.data["couplings"] == []


# ================================================================ safe rollback
class TestSafeRollbackSkill:
    def test_demo_arbitration_and_plan(self, runtime, broker):
        prob = runtime.run("resolve_target", {"query": DEMO_QUERY})
        keep = runtime.run("resolve_target", {"query": DEMO_KEEP})
        p = runtime.run("change_unit_analysis", {"terms": prob.data["terms"]})
        k = runtime.run("change_unit_analysis", {"terms": keep.data["terms"]})
        r = runtime.run("safe_rollback", {
            "problem_matches": p.data["matches"],
            "keep_matches": k.data["matches"],
            "affected_routes": ["/api/auth/login"], "task_id": "t-11"})
        assert r.status == "success"
        assert [u["id"] for u in r.data["rollback_units"]] == ["bbdc659f-U2"]
        assert [u["id"] for u in r.data["keep_units"]] == ["bbdc659f-U1"]
        assert r.data["shared_symbols"] == [] and r.data["couplings"] == []
        assert r.data["policy_action"] == "HUMAN_REVIEW"
        assert "HUMAN_REVIEW" in r.data["recommendation"]
        assert r.data["decision_ids"], "决策必须记录在案"
        # 平局归问题侧：auth 单元不会被 keep 侧抢走
        assert any("U2" in m["unit_id"] for m in r.data["problem_matches"])

    def test_ungated_below_decision_layer(self, seeded):
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime
        b = ContextBroker(FIXTURE, ToolRecorder(),
                          layers={"code", "semantic", "change"})
        build_change_graph(b)
        rt = SkillRuntime(b)
        p = rt.run("change_unit_analysis", {"terms": ["auth", "登录"]})
        r = rt.run("safe_rollback", {
            "problem_matches": p.data["matches"], "keep_matches": []})
        assert r.data["policy_action"] == "UNGATED"
        assert "UNGATED" in r.data["recommendation"]
        assert r.data["decision_ids"] == []


# ================================================================ verification
class TestEvidenceVerificationSkill:
    def test_unit_findings_get_verified(self, runtime, broker):
        nav = runtime.run("resolve_target", {"query": DEMO_QUERY})
        p = runtime.run("change_unit_analysis", {"terms": nav.data["terms"]})
        fids = [m["finding_id"] for m in p.data["matches"]]
        r = runtime.run("evidence_verification", {"finding_ids": fids})
        assert r.status == "success" and r.data["verdicts"]
        assert any(v.status.value == "SUPPORTED" for v in r.data["verdicts"])
        verified = {f.id for f in broker.all_findings()
                    if f.status == "verified"}
        assert set(fids) <= verified

    def test_unknown_finding_warned_not_crashed(self, runtime):
        r = runtime.run("evidence_verification",
                        {"finding_ids": ["finding:nope"]})
        assert r.warnings and r.status in ("success", "partial")


# ================================================================ policy check
class TestPolicyCheckSkill:
    def test_public_api_route_human_review(self, runtime):
        r = runtime.run("policy_check", {
            "rollback_symbols": ["validateAccount"],
            "keep_symbols": ["rewordTitle"],
            "affected_routes": ["/api/auth/login"]})
        assert r.status == "success"
        assert r.data["action"] == "HUMAN_REVIEW"
        assert r.data["rule_name"] == "rollback_public_api"

    def test_ungated_without_decision_layer(self, seeded):
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime
        b = ContextBroker(FIXTURE, ToolRecorder(), layers={"code", "semantic"})
        r = SkillRuntime(b).run("policy_check", {
            "rollback_symbols": ["a"], "keep_symbols": ["b"]})
        assert r.status == "partial" and r.data["action"] == "UNGATED"


# ================================================================ views + impact
class TestViewAndImpactSkills:
    def test_build_task_view_bounded(self, runtime, broker):
        r = runtime.run("build_task_view", {
            "task_id": "tv-11", "target_ids": ["sym:src/lib/auth.ts::validateAccount"]})
        assert r.status == "success"
        s = r.data["view_stats"]
        assert 0 < s["task_graph_nodes"] < broker.graph.stats()["nodes"]

    def test_impact_analysis_finds_route_and_callers(self, runtime):
        rt = runtime.run("build_task_view", {
            "task_id": "tv-11b", "target_ids": ["sym:src/lib/auth.ts::validateAccount"]})
        assert rt.status == "success"
        r = runtime.run("impact_analysis", {
            "target_ids": ["sym:src/lib/auth.ts::validateAccount"],
            "task_id": "tv-11b"})
        assert r.status == "success"
        assert r.data["routes"] == ["/api/auth/login"]
        assert r.data["caller_ids"]
        assert r.data["finding_id"] and r.evidence_ids


# ================================================================ runtime LLM 门
class TestRuntimeLLMGating:
    def test_llm_only_injected_for_allowed_skill(self, broker):
        from src.skills import SkillRuntime

        class FakeLLM:
            available = True
            def __init__(self):
                self.calls = 0
            def chat_json(self, system, user):
                self.calls += 1
                return {"candidates": [{"feature_id": "feature:AuthLogin",
                                        "reason": "query mentions login"}]}

        llm = FakeLLM()
        rt = SkillRuntime(broker, llm=llm)
        rt.run("change_unit_analysis", {"terms": ["auth"]})
        assert llm.calls == 0, "确定性 skill 拿不到 LLM"
        rt.run("resolve_target", {"query": "怎么改账号校验"})
        assert llm.calls == 1, "白名单 skill 才注入 LLM"


# ================================================================ 11B：agents 委托 skills
class TestAgentsDelegateToSkills:
    def test_every_agent_declares_skills(self):
        from src.agents import (ChangeIntelligenceAgent, ImpactSliceAgent,
                                Orchestrator, RepositoryNavigator,
                                RollbackPlanner)
        from src.agents.verifier import (DeterministicVerifier,
                                         SemanticVerifier)
        for cls in (RepositoryNavigator, ChangeIntelligenceAgent,
                    ImpactSliceAgent, RollbackPlanner, DeterministicVerifier,
                    SemanticVerifier):
            assert cls.SKILLS, cls.ROLE
            assert cls.READS, "READS 清单保留（兼容）"

    def test_scopes_carry_skills_and_delegation_leaves_trail(self, broker):
        from src.agents import Orchestrator
        o = Orchestrator(broker)
        n0 = len(broker.rec.calls)
        r = o.run(DEMO_QUERY, keep_hint=DEMO_KEEP)
        new = broker.rec.calls[n0:]
        # scope 同时记录能力清单与 skill 清单
        assert r.scopes["RepositoryNavigator"].skills == ["resolve_target"]
        assert r.scopes["RollbackPlanner"].skills == ["safe_rollback"]
        # agent 轨迹与 skill 轨迹并存（委托真实发生）
        assert "agent:RepositoryNavigator:navigate" in new
        assert "skill:resolve_target:run" in new
        assert "skill:safe_rollback:run" in new
        # 归属沿用 agent 名：verifier 字符串与 9L 时代一致
        names = {v.verifier for v in r.verdicts}
        assert "DeterministicVerifier" in names
        assert "SemanticVerifier" in names
        assert "EvidenceVerificationSkill" not in names

    def test_navigator_no_longer_imports_search(self):
        """11G 前置：agent 模块源码不 import search/semantica。"""
        import src.agents.navigator as nav
        src_text = Path(nav.__file__).read_text()
        for banned in ("src.search", "src.semantica", "subprocess",
                       "src.git_history"):
            assert banned not in src_text

    def test_planner_arbitrate_tie_goes_to_problem(self):
        from src.agents.change_intel import UnitMatch
        from src.agents.rollback import RollbackPlanner
        m = lambda uid, score=2.0, commit="c1": UnitMatch(
            unit_id=uid, commit=commit, label="auth", score=score)
        planner = RollbackPlanner.__new__(RollbackPlanner)  # 纯函数，无需 broker
        prob, keep = planner.arbitrate(
            [m("cu:a"), m("cu:x", score=9.0, commit="c2")],
            [m("cu:a"), m("cu:b")])
        # cu:a 平局（2.0 vs 2.0）→ 归问题侧；cu:b 独占 → keep；
        # cu:x 在别的 commit 上 → 被 keep 钉住的 commit 过滤
        assert [x.unit_id for x in prob] == ["cu:a"]
        assert [x.unit_id for x in keep] == ["cu:b"]

    def test_agent_path_registers_agent_attributed_findings(self, broker):
        """agent 委托路径的 finding 归属沿用 agent 名（与 9L 时代一致）。"""
        from src.agents.navigator import RepositoryNavigator
        nav = RepositoryNavigator(broker)
        res = nav.find_target(DEMO_QUERY)
        assert res.feature_id == "feature:AuthLogin"
        produced = [f for f in broker.all_findings()
                    if f.id == res.finding_id]
        assert produced and produced[0].producer == "RepositoryNavigator"


# ================================================================ 11C：能力执法
class TestCapabilityEnforcement:
    def test_every_declared_capability_exists_on_broker(self):
        """skill 声明的 capability 必须都是 broker 真实提供的方法。"""
        from src.semgraph.context_broker import CAPABILITIES
        from src.skills import default_registry
        provided = set(CAPABILITIES.values())
        for name, skill in default_registry().items():
            dangling = [c for c in skill.spec.allowed_capabilities
                        if c not in provided and not c.endswith(".*")]
            assert not dangling, f"{name} 声明了不存在的能力: {dangling}"

    def test_runtime_enforces_declarations_fail_fast(self, broker):
        """未声明的能力调用：就地抛错，绝不静默放行。"""
        from src.skills.base import BaseSkill
        from src.skills.runtime import SkillRuntime
        from src.skills.spec import SkillSpec

        class EvilSkill(BaseSkill):
            spec = SkillSpec(name="evil_probe",
                             description="calls broker.node undeclared",
                             required_inputs=[],
                             allowed_capabilities=[])   # 什么都没声明

            def _execute(self, context, broker):
                broker.node("sym:src/lib/auth.ts::validateAccount")
                from src.skills.spec import SKILL_SUCCESS, SkillResult
                return SkillResult(skill=self.spec.name, status=SKILL_SUCCESS)

        rt = SkillRuntime(broker, registry={"evil_probe": EvilSkill()})
        r = rt.run("evil_probe", {})
        assert r.status == "failed"
        assert "undeclared capability" in r.error
        assert "repository.node" in r.error

    def test_free_introspection_needs_no_capability(self, broker):
        from src.skills.base import BaseSkill
        from src.skills.runtime import SkillRuntime
        from src.skills.spec import (SKILL_SUCCESS, SkillResult, SkillSpec)

        class ProbeSkill(BaseSkill):
            spec = SkillSpec(name="probe", required_inputs=[],
                             allowed_capabilities=[])

            def _execute(self, context, broker):
                # layer_active 是自由内省：不算能力
                layers = {n: broker.layer_active(n)
                          for n in ("code", "semantic", "change")}
                return SkillResult(skill=self.spec.name, status=SKILL_SUCCESS,
                                   data={"layers": layers})

        r = SkillRuntime(broker, registry={"probe": ProbeSkill()}).run(
            "probe", {})
        assert r.status == "success" and r.data["layers"]["semantic"] is True
        assert r.capabilities_used == []

    def test_result_records_capabilities_used(self, runtime):
        r = runtime.run("coupling_analysis", {
            "files_a": ["src/lib/ai-helpers.ts"],
            "files_b": ["src/lib/retry.ts"]})
        assert r.status == "success"
        assert r.capabilities_used == ["change.get_couplings"]

    def test_wildcard_prefix_allows_family(self):
        from src.skills.capability import capability_allowed
        assert capability_allowed("evidence.finding.add",
                                  ["evidence.finding.*"])
        assert capability_allowed("evidence.finding.set_status",
                                  ["evidence.finding.*"])
        assert not capability_allowed("evidence.add", ["evidence.finding.*"])
        assert not capability_allowed("policy.gate", [])

    def test_all_eight_skills_run_under_guard(self, runtime, broker):
        """执法开启后全部 skill 仍可完整跑通（声明=使用，无缺口）。"""
        nav = runtime.run("resolve_target", {"query": DEMO_QUERY})
        assert nav.status == "success"
        cu = runtime.run("change_unit_analysis",
                         {"terms": nav.data["terms"]})
        assert cu.status == "success"
        v = runtime.run("build_task_view", {
            "task_id": "tv-cap", "target_ids": nav.data["related_symbols"][:1]})
        assert v.ok
        imp = runtime.run("impact_analysis", {
            "target_ids": nav.data["related_symbols"][:1],
            "task_id": "tv-cap"})
        assert imp.status == "success"
        assert runtime.run("evidence_verification", {
            "finding_ids": [m["finding_id"] for m in cu.data["matches"]]}
        ).status == "success"
        assert runtime.run("policy_check", {
            "rollback_symbols": ["validateAccount"],
            "keep_symbols": ["rewordTitle"],
            "affected_routes": ["/api/auth/login"]}).status == "success"
        sr = runtime.run("safe_rollback", {
            "problem_matches": cu.data["matches"], "keep_matches": []})
        assert sr.status == "success"
        # 仲裁真源也可直接跑（纯函数，无 broker）
        from src.skills.safe_rollback import arbitrate
        assert arbitrate([], []) == ([], [])


# ================================================================ 11D：broker 内部 Service 化
class TestBrokerServices:
    def test_broker_holds_eight_services(self, broker):
        from src.services import (ChangeService, DecisionService,
                                  EvidenceService, GraphQueryService,
                                  PolicyService, ResolutionService,
                                  SemanticService, TaskViewService)
        pairs = [("graph", GraphQueryService), ("resolution", ResolutionService),
                 ("views", TaskViewService), ("change", ChangeService),
                 ("evidence", EvidenceService), ("decision", DecisionService),
                 ("policy", PolicyService), ("semantic", SemanticService)]
        for name, cls in pairs:
            svc = getattr(broker, f"_{name}_svc")
            assert isinstance(svc, cls), name

    def test_legacy_private_aliases_are_live(self, broker):
        """旧私有入口指向服务的活注册表（同一对象，不是拷贝）。"""
        assert broker._findings is broker._evidence_svc.findings
        assert broker._evidence is broker._evidence_svc.evidence
        assert broker._views is broker._views_svc.views
        assert broker.REASON_SUMMARY_CAP == broker._decision_svc.REASON_SUMMARY_CAP

    def test_delegation_preserves_evidence_minting(self, broker):
        """经 broker 门面的读取仍在读取时刻铸造 evidence（原则 5；
        同 id 事实幂等去重，重复读取不重复注册）。"""
        from src.semgraph.objects import EvidenceType
        n0 = len(broker.all_evidence())
        ctx = broker.get_target_context("sym:src/lib/auth.ts::validateAccount")
        assert ctx.evidence_ids
        ev = broker.get_evidence(ctx.evidence_ids)[0]
        assert ev.type == EvidenceType.AST
        assert ev.source == "broker:get_target_context"
        registered = {e.id for e in broker.all_evidence()}
        assert ev.id in registered
        assert len(broker.all_evidence()) in (n0, n0 + 1)

    def test_layer_gating_still_loud_after_extraction(self, seeded):
        from src.errors import DataAgentError
        from src.semgraph.context_broker import ContextBroker
        b = ContextBroker(FIXTURE, ToolRecorder(),
                          layers={"code", "semantic"})
        with pytest.raises(DataAgentError):
            b.create_task_view("t", ["sym:src/lib/auth.ts::validateAccount"])
