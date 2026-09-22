"""Phase 11I 测试：Policy / Scoring 参数外置。

承诺：
- config/maintenance_policy.yaml 存在且版本化；缺键/缺文件回退默认
- 默认值与 11I 前硬编码逐项一致（行为不变）
- skill 权重、tie-break、action→risk 全部来自配置
- Decision 记录 policy_version（Policy A vs B 消融可对账）
- set_maintenance_policy 是进程内消融钩子，None 恢复
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(autouse=True)
def restore_policy():
    """每个测试结束后恢复文件加载，杜绝配置污染。"""
    yield
    from src.config import set_maintenance_policy
    set_maintenance_policy(None)


@pytest.fixture(scope="module")
def broker(seeded):
    from src.schema import ToolRecorder
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    b = ContextBroker(FIXTURE, ToolRecorder())
    build_change_graph(b)
    return b


# ================================================================ 配置本身
class TestPolicyConfigFile:
    def test_config_file_exists_and_versioned(self):
        from src.config import (MAINTENANCE_POLICY_PATH,
                                maintenance_policy)
        assert MAINTENANCE_POLICY_PATH.exists()
        cfg = maintenance_policy()
        assert cfg["version"], "策略配置必须版本化"
        # 文件里的 version 就是生效 version（加载器没吞掉它）
        assert f'version: "{cfg["version"]}"' in \
            MAINTENANCE_POLICY_PATH.read_text()

    def test_defaults_match_pre_11i_constants(self):
        """默认值与 11I 前硬编码逐项一致 —— 外置不改行为。"""
        from src.config import maintenance_policy
        cfg = maintenance_policy()
        assert cfg["rollback"]["scoring"] == {
            "label": 3.0, "ui": 2.0, "symbol": 2.0, "file": 1.0,
            "label_partial": 0.7}
        assert cfg["rollback"]["tie_break"]["prefer_problem_side"] is True
        assert cfg["policy"]["action_risk"] == {
            "PASS": "low", "HUMAN_REVIEW": "medium", "BLOCK": "high"}

    def test_skill_weights_come_from_config(self):
        from src.config import maintenance_policy
        from src.skills.change_unit_analysis import (LABEL_PARTIAL, W_FILE,
                                                     W_LABEL, W_SYMBOL, W_UI)
        sc = maintenance_policy()["rollback"]["scoring"]
        assert (W_LABEL, W_UI, W_SYMBOL, W_FILE, LABEL_PARTIAL) == (
            sc["label"], sc["ui"], sc["symbol"], sc["file"],
            sc["label_partial"])

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        from src.config import load_maintenance_policy
        cfg = load_maintenance_policy(tmp_path / "nope.yaml")
        assert cfg["rollback"]["scoring"]["label"] == 3.0

    def test_partial_yaml_deep_merges(self, tmp_path):
        from src.config import load_maintenance_policy
        partial = tmp_path / "policy_b.yaml"
        partial.write_text(
            'version: "B"\n'
            "rollback:\n"
            "  tie_break:\n"
            "    prefer_problem_side: false\n")
        cfg = load_maintenance_policy(partial)
        assert cfg["version"] == "B"
        assert cfg["rollback"]["tie_break"]["prefer_problem_side"] is False
        assert cfg["rollback"]["scoring"]["label"] == 3.0  # 未给的键回默认

    def test_builtin_yaml_parser_no_pyyaml_dependency(self):
        """PyYAML 缺席时的极简解析器能读懂仓库策略文件。"""
        from src.config import (_parse_simple_yaml,
                                MAINTENANCE_POLICY_PATH)
        data = _parse_simple_yaml(MAINTENANCE_POLICY_PATH.read_text())
        assert data["version"] == "13J.1"
        assert data["rollback"]["scoring"]["label"] == 3.0
        assert data["rollback"]["tie_break"]["prefer_problem_side"] is True
        assert data["policy"]["action_risk"]["BLOCK"] == "high"


# ================================================================ 版本落档
class TestDecisionPolicyVersion:
    def test_policy_gate_decision_records_version(self, broker):
        from src.config import policy_version
        n0 = len(broker._decision_svc.decisions)
        r = broker.run_policy_gate({
            "rollback_symbols": ["validateAccount"],
            "keep_symbols": ["rewordTitle"],
            "affected_routes": ["/api/auth/login"]}, task_id="t-11i")
        assert r.action.value == "HUMAN_REVIEW"
        new = list(broker._decision_svc.decisions.values())[n0:]
        assert new and all(d.policy_version == policy_version()
                           for d in new)

    def test_rollback_decisions_record_version(self, broker):
        from src.config import policy_version
        from src.skills import SkillRuntime
        rt = SkillRuntime(broker)
        prob = rt.run("resolve_target", {"query": DEMO_QUERY})
        keep = rt.run("resolve_target", {"query": DEMO_KEEP})
        p = rt.run("change_unit_analysis", {"terms": prob.data["terms"]})
        k = rt.run("change_unit_analysis", {"terms": keep.data["terms"]})
        n0 = len(broker._decision_svc.decisions)
        r = rt.run("safe_rollback", {
            "problem_matches": p.data["matches"],
            "keep_matches": k.data["matches"],
            "affected_routes": ["/api/auth/login"]})
        assert r.data["decision_ids"]
        new = list(broker._decision_svc.decisions.values())[n0:]
        assert new and all(d.policy_version == policy_version()
                           for d in new)

    def test_config_override_changes_version_and_risk(self, broker):
        """A/B 消融钩子：覆盖配置后，新决策带新版本、risk 跟着映射走。"""
        from src.config import set_maintenance_policy
        set_maintenance_policy({"version": "B-test",
                                "policy": {"action_risk": {
                                    "HUMAN_REVIEW": "high"}}})
        n0 = len(broker._decision_svc.decisions)
        broker.run_policy_gate({
            "rollback_symbols": ["validateAccount"],
            "keep_symbols": ["rewordTitle"],
            "affected_routes": ["/api/auth/login"]}, task_id="t-11i-b")
        new = list(broker._decision_svc.decisions.values())[n0:]
        assert all(d.policy_version == "B-test" for d in new)
        assert all(d.risk == "high" for d in new)


# ================================================================ tie-break 消融
class TestTieBreakAblation:
    def _tie_matches(self):
        m = lambda uid: {"unit_id": uid, "commit": "c1", "score": 2.0,
                         "label": "auth", "files": [], "finding_id": ""}
        return [m("cu:x")], [m("cu:x")]

    def test_default_tie_goes_to_problem_side(self):
        from src.skills.safe_rollback import arbitrate
        prob, keep = arbitrate(*self._tie_matches())
        assert [u["unit_id"] for u in prob] == ["cu:x"]
        assert keep == []

    def test_flipped_tie_goes_to_keep_side(self):
        from src.config import set_maintenance_policy
        from src.skills.safe_rollback import arbitrate
        set_maintenance_policy({"rollback": {"tie_break": {
            "prefer_problem_side": False}}})
        prob, keep = arbitrate(*self._tie_matches())
        assert prob == []
        assert [u["unit_id"] for u in keep] == ["cu:x"]
