"""Phase 11J 测试：Skill-level evaluation。

目标：Agent 失败时，能判断到底哪个 Skill 失败 —— 每个技能一行
（status/latency/broker_calls/tool_calls/llm_calls/evidence_count）
加 fixture 已知答案上的正确性指标。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"

REQUIRED_FIELDS = ("skill", "status", "latency_ms", "broker_calls",
                   "physical_tool_calls", "llm_calls", "evidence_count")


@pytest.fixture(scope="module")
def seeded():
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    return FIXTURE


@pytest.fixture(scope="module")
def report(seeded):
    from experiments.skill_eval import run_skill_eval
    return run_skill_eval(FIXTURE)


class TestSkillEvalRows:
    def test_all_eight_skills_covered(self, report):
        skills = {r["skill"] for r in report["rows"]}
        assert skills == {"resolve_target", "build_task_view",
                          "impact_analysis", "change_unit_analysis",
                          "coupling_analysis", "safe_rollback",
                          "evidence_verification", "policy_check"}

    def test_every_row_complete_and_green(self, report):
        assert len(report["rows"]) == 10   # 双侧导航/单元匹配各跑两次
        for r in report["rows"]:
            for f in REQUIRED_FIELDS:
                assert f in r, (r["skill"], f)
            assert r["status"] == "success"
            assert r["latency_ms"] >= 0 and r["broker_calls"] >= 0

    def test_deterministic_run_zero_llm_calls(self, report):
        assert sum(r["llm_calls"] for r in report["rows"]) == 0
        assert report["summary"]["llm_calls_total"] == 0

    def test_report_is_json_serializable(self, report):
        text = json.dumps(report, ensure_ascii=False)
        assert json.loads(text) == report


class TestSkillEvalCorrectness:
    def test_resolve_target_hits_ground_truth(self, report):
        navs = [r for r in report["rows"] if r["skill"] == "resolve_target"]
        assert [m["target_correct"] for m in
                (r["metrics"] for r in navs)] == [1, 1]

    def test_change_unit_matching_correct(self, report):
        cus = [r for r in report["rows"]
               if r["skill"] == "change_unit_analysis"]
        assert all(m["unit_match_correct"] == 1
                   for m in (r["metrics"] for r in cus))

    def test_coupling_and_impact_prf(self, report):
        by = {r["skill"]: r["metrics"] for r in report["rows"]}
        assert by["coupling_analysis"]["precision"] == 1.0
        assert by["coupling_analysis"]["recall"] == 1.0
        assert by["impact_analysis"]["routes_precision"] == 1.0
        assert by["impact_analysis"]["routes_recall"] == 1.0
        assert by["impact_analysis"]["callers_found"] >= 1

    def test_safe_rollback_metrics(self, report):
        m = next(r["metrics"] for r in report["rows"]
                 if r["skill"] == "safe_rollback")
        assert m["rollback_precision"] == 1.0
        assert m["preservation_rate"] == 1
        assert m["collateral_damage"] == 0

    def test_verification_and_policy(self, report):
        by = {r["skill"]: r["metrics"] for r in report["rows"]}
        assert by["evidence_verification"]["supported_fraction"] == 1.0
        assert by["policy_check"]["action_correct"] == 1
        assert by["policy_check"]["action"] == "HUMAN_REVIEW"


class TestSkillFailureLocalization:
    def test_unresolvable_query_localizes_failure(self, seeded):
        """目标：Agent 失败时定位到具体 skill —— 断掉的环节记 failed，
        下游技能照常评估（链条不断）。"""
        from experiments.skill_eval import run_skill_eval
        r = run_skill_eval(FIXTURE, query="完全无关 zzz",
                           keep_hint="完全无关 zzz")
        by_skill = {}
        for row in r["rows"]:
            by_skill.setdefault(row["skill"], []).append(row["status"])
        assert by_skill["resolve_target"] == ["failed", "failed"]
        assert by_skill["change_unit_analysis"] == ["failed", "failed"]
        assert r["summary"]["failed"] == 4
        # 下游照常被评估：不是整条链一断全断
        assert "success" in by_skill["coupling_analysis"]
        assert "success" in by_skill["policy_check"]
