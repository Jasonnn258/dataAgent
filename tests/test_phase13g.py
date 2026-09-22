"""Phase 13G 测试：执行终审五面裁决（fixture 复制 tmp）。"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "experiments" / "fixtures" / "fixture_repo"

from src.errors import DataAgentError                      # noqa: E402
from src.schema import ToolRecorder                        # noqa: E402

DEMO_QUERY = "登录逻辑改坏了，帮我找出问题修改，准备回退"
DEMO_KEEP = "保留同 commit 中已经改好的系统标题"
PASS_CMDS = [["python", "-c", "print('ok')"]]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    tmp = tmp_path_factory.mktemp("p13g") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


def _chain(repo, validation_commands=PASS_CMDS, task_id="t13g"):
    """完整链到 VALIDATED（ready for verify）。"""
    from src.semgraph.change_graph import build_change_graph
    from src.semgraph.context_broker import ContextBroker
    from src.skills import SkillRuntime

    broker = ContextBroker(repo, ToolRecorder())
    build_change_graph(broker)
    rt = SkillRuntime(broker)
    nav = rt.run("resolve_target", {"query": DEMO_QUERY, "task_id": task_id})
    navk = rt.run("resolve_target", {"query": DEMO_KEEP, "task_id": task_id})
    cu = rt.run("change_unit_analysis",
                {"terms": nav.data["terms"], "task_id": task_id})
    cuk = rt.run("change_unit_analysis",
                 {"terms": navk.data["terms"], "task_id": task_id})
    sr = rt.run("safe_rollback", {
        "problem_matches": cu.data["matches"],
        "keep_matches": cuk.data["matches"],
        "affected_routes": ["/api/auth/login"], "task_id": task_id})
    bp = rt.run("build_execution_plan", {
        "rollback_plan": sr.data, "task_id": task_id,
        "validation_commands": validation_commands})
    pe = rt.run("prepare_execution", {"plan": bp.data["plan"]})
    br = rt.run("build_rollback_patch", {"execution": pe.data["execution"]})
    ap = rt.run("apply_patch", {"execution": br.data["execution"]})
    assert ap.ok, ap.error
    vr = rt.run("validate_execution", {"execution": ap.data["execution"]})
    assert vr.ok, vr.error
    return broker, rt, bp.data["plan"], vr.data["execution"]


# ================================================================ 主裁决
class TestVerdicts:
    def test_canonical_verified(self, repo):
        """canonical：auth 回退 + title 保留 + 全过 → VERIFIED。"""
        broker, rt, plan, attempt = _chain(repo)
        r = rt.run("verify_execution", {"execution": attempt})
        assert r.ok, r.error
        v = r.data["verification"]
        assert v["overall"] == "VERIFIED"
        assert v["expected_change_pass"] and v["preservation_pass"]
        assert v["scope_pass"] and v["tests_pass"] and v["evidence_pass"]
        assert r.data["status"] == "VERIFIED"
        # 明细八项在案
        names = {c["check"] for c in v["checks"]}
        assert {"expected.overall", "preserve.overall", "scope.files",
                "scope.content", "tests", "evidence"} <= names

    def test_partial_without_tests(self, repo):
        """软面缺：没配验证命令 → PARTIAL，状态停在 VALIDATED。"""
        broker, rt, plan, attempt = _chain(repo, validation_commands=None,
                                           task_id="t13g-notest")
        r = rt.run("verify_execution", {"execution": attempt})
        assert r.status == "partial"
        assert r.data["verification"]["overall"] == "PARTIAL"
        assert r.data["verification"]["tests_pass"] is False
        assert r.data["status"] == "VALIDATED"   # 不推进
        # 补跑测试后重验可 VERIFIED（状态还允许 VALIDATED→VERIFIED）
        vr2 = rt.run("validate_execution", {"execution":
                                            r.data["execution"]})
        # 零命令 → 依然 VALIDATED；直接写 repo 配置再验
        cfg = repo / ".dataagent" / "validation_commands.json"
        cfg.write_text('[["python", "-c", "print(\'late\')"]]\n',
                       encoding="utf-8")
        try:
            r2 = rt.run("verify_execution", {"execution":
                                             r.data["execution"]})
            # 命令清单在 validate 时已快照进 attempt，重验不会捡起配置；
            # PARTIAL 依旧 —— 这是对的：验证结果必须来自真实执行记录
            assert r2.data["verification"]["overall"] == "PARTIAL"
        finally:
            cfg.unlink()

    def test_failed_when_forbidden_file_touched(self, repo):
        """失败用例：keep 侧文件被外部篡改 → FAILED（VERIFICATION_FAILED
        终态）。"""
        broker, rt, plan, attempt = _chain(repo, task_id="t13g-forb")
        # 沙箱里动 keep 文件（模拟 git 或人干了计划外的事），并伪造
        # actual.patch 反映这个状态
        wt = Path(attempt.workspace)
        layout = wt / "src/app/layout.tsx"
        layout.write_text(
            layout.read_text(encoding="utf-8").replace("系统", "被改坏"),
            encoding="utf-8")
        diff = subprocess.run(["git", "diff", "--no-color", "HEAD"],
                              cwd=wt, capture_output=True, text=True)
        Path(attempt.actual_patch_path).write_text(diff.stdout,
                                                   encoding="utf-8")
        r = rt.run("verify_execution", {"execution": attempt})
        assert r.status == "failed"
        v = r.data["verification"]
        assert v["overall"] == "FAILED"
        assert v["preservation_pass"] is False
        assert r.data["status"] == "VERIFICATION_FAILED"
        with pytest.raises(DataAgentError):   # 终态不可翻
            broker.verify_execution(r.data["execution"])

    def test_failed_when_out_of_plan_file(self, repo):
        """失败用例：计划外文件被改 → scope FAILED。"""
        broker, rt, plan, attempt = _chain(repo, task_id="t13g-oop")
        wt = Path(attempt.workspace)
        (wt / "README.md").write_text(
            (wt / "README.md").read_text(encoding="utf-8") + "\nrogue\n",
            encoding="utf-8")
        diff = subprocess.run(["git", "diff", "--no-color", "HEAD"],
                              cwd=wt, capture_output=True, text=True)
        Path(attempt.actual_patch_path).write_text(diff.stdout,
                                                   encoding="utf-8")
        r = rt.run("verify_execution", {"execution": attempt})
        v = r.data["verification"]
        assert v["overall"] == "FAILED"
        assert v["scope_pass"] is False

    def test_failed_when_revert_missing(self, repo):
        """失败用例：回退没真发生（actual 是空 diff）→ expected FAILED。"""
        broker, rt, plan, attempt = _chain(repo, task_id="t13g-miss")
        Path(attempt.actual_patch_path).write_text("", encoding="utf-8")
        r = rt.run("verify_execution", {"execution": attempt})
        v = r.data["verification"]
        assert v["overall"] == "FAILED"
        assert v["expected_change_pass"] is False

    def test_refuses_unvalidated_attempt(self, repo):
        broker, rt, plan, attempt = _chain(repo, task_id="t13g-ref")
        fresh = broker._workspace_svc.registry.create(plan)
        r = rt.run("verify_execution", {"execution": fresh})
        assert r.status == "failed"
        assert "VALIDATED" in r.error
