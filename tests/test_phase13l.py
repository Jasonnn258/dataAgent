"""Phase 13L 测试：执行轨迹（全链可重建，且构造上不含隐藏 CoT）。

承诺：
- 一次沙箱执行 = 一棵以 agent span 为根的执行树，skill/policy/tool
  事件全部挂树上（parent 链从任何叶子能走回根）
- run 目录落 trace.jsonl；attempt.trace_id 是树根
- 事件字段是固定白名单：没有入参载荷、没有模型输出、没有 CoT ——
  结构上不存在放这些东西的位置
- 纯 ToolRecorder（无 span）时链照常工作，只是没树（兼容降级）
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"
PASS_CMDS = [["python", "-c", "print('ok')"]]

# 事件字段的固定白名单（多了就是有人往轨迹里塞了不该塞的东西）
_EVENT_KEYS = {"trace_id", "parent", "task_id", "layer", "actor", "action",
               "status", "duration_ms", "evidence_ids", "warnings", "meta"}
_FORBIDDEN_KEY_PARTS = ("cot", "prompt", "thinking", "reasoning",
                        "completion", "message", "output_text", "input")


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable,
                        str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(scope="module")
def chain(seeded, tmp_path_factory):
    """跑一次完整沙箱链，留 (broker, outcome)。"""
    from src.agents.executor import MaintenanceExecutorAgent
    from src.execution import ExecutionRecorder
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    from src.skills import SkillRuntime

    repo = tmp_path_factory.mktemp("p13l") / "fixture_repo"
    shutil.copytree(seeded, repo, symlinks=True)
    broker = ContextBroker(repo, ExecutionRecorder())
    build_change_graph(broker)
    rt = SkillRuntime(broker)
    nav = rt.run("resolve_target", {"query": DEMO_QUERY, "task_id": "t13l"})
    navk = rt.run("resolve_target", {"query": DEMO_KEEP, "task_id": "t13l"})
    cu = rt.run("change_unit_analysis",
                {"terms": nav.data["terms"], "task_id": "t13l"})
    cuk = rt.run("change_unit_analysis",
                 {"terms": navk.data["terms"], "task_id": "t13l"})
    sr = rt.run("safe_rollback", {
        "problem_matches": cu.data["matches"],
        "keep_matches": cuk.data["matches"],
        "affected_routes": ["/api/auth/login"], "task_id": "t13l"})
    out = MaintenanceExecutorAgent(broker).execute(
        sr.data, task_id="t13l", validation_commands=PASS_CMDS,
        affected_routes=["/api/auth/login"])
    assert out.status == "VERIFIED", out.dump()
    return broker, out


# ================================================================ 全链树
class TestFullChain:
    def test_agent_span_is_root(self, chain):
        broker, out = chain
        assert out.trace_id.startswith("tr-")
        rec = broker.rec
        events = rec.subtree(out.trace_id)
        layers = {e.layer for e in events}
        # agent → skill → policy/tool 全挂在同一棵树上
        assert "agent" in layers and "skill" in layers
        assert "policy" in layers and "tool" in layers
        actors = {e.actor for e in events}
        assert "MaintenanceExecutor" in actors
        for s in ("build_execution_plan", "prepare_execution",
                  "build_rollback_patch", "apply_patch",
                  "validate_execution", "verify_execution", "policy_check"):
            assert s in actors, s

    def test_parent_chain_reaches_root(self, chain):
        """从任何叶子沿 parent 链都能走回 agent 根（树无孤岛）。"""
        broker, out = chain
        events = broker.rec.subtree(out.trace_id)
        by_id = {e.trace_id: e for e in events}
        for e in events:
            cur = e
            hops = 0
            while cur.parent_trace_id:
                cur = by_id[cur.parent_trace_id]
                hops += 1
                assert hops < 100, "parent 环"
        assert events[0].trace_id == out.trace_id   # 根在最前

    def test_attempt_trace_id_matches(self, chain):
        broker, out = chain
        attempt = broker.get_execution(out.execution_id)
        assert attempt.trace_id == out.trace_id

    def test_trace_jsonl_in_run_dir(self, chain):
        broker, out = chain
        path = broker._workspace_svc.runs_root / out.execution_id \
            / "trace.jsonl"
        assert path.is_file()
        lines = [json.loads(x) for x in
                 path.read_text().splitlines() if x.strip()]
        assert lines and lines[0]["trace_id"] == out.trace_id
        assert lines[0]["layer"] == "agent"
        assert {x["layer"] for x in lines} >= {"agent", "skill", "tool"}


# ================================================================ 无 CoT
class TestNoHiddenCoT:
    def test_event_keys_are_whitelisted(self, chain):
        broker, out = chain
        for e in broker.rec.subtree(out.trace_id):
            d = broker.rec.event_json(e)
            extra = set(d) - _EVENT_KEYS
            assert not extra, f"轨迹出现白名单外字段: {extra}"

    def test_no_prompt_or_cot_anywhere(self, chain):
        """字段名与值里都不出现 prompt/CoT 类内容（结构保证 + 巡检）。"""
        broker, out = chain
        blob = broker.rec.trace_jsonl(out.trace_id)
        low = blob.lower()
        for part in _FORBIDDEN_KEY_PARTS:
            assert part not in low, f"轨迹含 {part!r}"

    def test_meta_only_structured_facts(self, chain):
        broker, out = chain
        for e in broker.rec.subtree(out.trace_id):
            for k, v in e.meta.items():
                assert isinstance(v, (str, int, float, bool)), \
                    f"{e.actor}.{k} 的 meta 不是标量事实: {type(v)}"


# ================================================================ 降级兼容
class TestDegradeToToolRecorder:
    def test_plain_recorder_still_works(self, seeded, tmp_path):
        """没有 span 的旧记录器：链照常跑到 VERIFIED，只是没树。"""
        from src.agents.executor import MaintenanceExecutorAgent
        from src.schema import ToolRecorder
        from src.semgraph.change_graph import build_change_graph
        from src.semgraph.context_broker import ContextBroker
        from src.skills import SkillRuntime

        repo = tmp_path / "fixture_repo"
        shutil.copytree(seeded, repo, symlinks=True)
        broker = ContextBroker(repo, ToolRecorder())
        build_change_graph(broker)
        rt = SkillRuntime(broker)
        nav = rt.run("resolve_target", {"query": DEMO_QUERY})
        navk = rt.run("resolve_target", {"query": DEMO_KEEP})
        cu = rt.run("change_unit_analysis", {"terms": nav.data["terms"]})
        cuk = rt.run("change_unit_analysis", {"terms": navk.data["terms"]})
        sr = rt.run("safe_rollback", {
            "problem_matches": cu.data["matches"],
            "keep_matches": cuk.data["matches"],
            "affected_routes": ["/api/auth/login"]})
        out = MaintenanceExecutorAgent(broker).execute(
            sr.data, validation_commands=PASS_CMDS,
            affected_routes=["/api/auth/login"])
        assert out.status == "VERIFIED"
        assert out.trace_id == ""      # 没树，就诚实地说没有
        # 平铺轨迹仍在（旧指标不破坏）
        assert any(c.startswith("agent:MaintenanceExecutor")
                   for c in broker.rec.calls)
