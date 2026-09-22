"""Phase 13J 测试：PromotePatchSkill —— 唯一真实仓库写路径。

核心承诺（每条都对应一个测试）：
- 默认关闭：promote_enabled=false 时谁都 promote 不了
- 三把钥匙：READY_TO_PROMOTE + explicit_approval is True + 开关
- verified.patch 冻结：字节对账，哈希不对不落地
- STALE_PLAN 禁止 apply：审批后源仓库动了 → 拒绝且终态留档
- 默认不 commit 不 push：PROMOTED 之后工作树有改动、无新提交
- reverse.patch：落地后留档，git apply -R 能撤销
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
PASS_CMDS = [["python", "-c", "print('ok')"]]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    if not (FIXTURE / ".git").exists():
        subprocess.run([sys.executable, str(ROOT / "experiments" / "fixtures" / "seed_fixture.py")],
                       check=True, cwd=ROOT)
    import shutil
    tmp = tmp_path_factory.mktemp("p13j") / "fixture_repo"
    shutil.copytree(FIXTURE, tmp, symlinks=True)
    return tmp


def _verified(repo, task_id):
    """跑到 VERIFIED 并审批，返回 (broker, rt, attempt)。"""
    from src.agents.executor import MaintenanceExecutorAgent

    broker, rt, plan_data = _rollback_plan(repo, task_id)
    agent = MaintenanceExecutorAgent(broker)
    out = agent.execute(plan_data, task_id=task_id,
                        validation_commands=PASS_CMDS,
                        affected_routes=["/api/auth/login"])
    assert out.status == "VERIFIED", out.dump()
    return broker, rt, out.attempt


def _rollback_plan(repo, task_id):
    from src.schema import ToolRecorder
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
    assert sr.ok, sr.error
    return broker, rt, sr.data


def _enable_promote():
    from src.config import set_maintenance_policy
    set_maintenance_policy({"execution_policy": {"promote_enabled": True}})


@pytest.fixture(autouse=True)
def _restore_policy():
    yield
    from src.config import set_maintenance_policy
    set_maintenance_policy(None)


# ================================================================ 默认关闭
class TestDisabledByDefault:
    def test_promote_off_without_config(self, repo):
        """默认 promote_enabled=false：即使全链绿 + 审批过，也不落地。"""
        from src.errors import DataAgentError
        from src.services.promotion import PromotionService

        broker, rt, attempt = _verified(repo, "t13j-off")
        broker.approve_promotion(attempt.execution_id, "alice")
        svc = PromotionService(broker)
        with pytest.raises(DataAgentError, match="disabled by policy"):
            svc.promote(attempt.execution_id, explicit_approval=True)
        # 源仓库没动
        assert (repo / "src" / "lib" / "auth.ts").read_text().count(">= 8")

    def test_config_default_records_switch(self):
        from src.config import maintenance_policy
        ep = maintenance_policy()["execution_policy"]
        assert ep["promote_enabled"] is False
        assert ep["auto_push"] is False


# ================================================================ 三把钥匙
class TestThreeKeys:
    def test_no_approval_no_promote(self, repo):
        """VERIFIED 但没审批 → promote 拒绝（提示先 approve）。"""
        from src.errors import DataAgentError

        _enable_promote()
        broker, rt, attempt = _verified(repo, "t13j-noappr")
        with pytest.raises(DataAgentError, match="approval .*must come first"):
            broker.promote_execution(attempt.execution_id,
                                     explicit_approval=True)

    def test_no_explicit_approval_no_promote(self, repo):
        """审批过但 explicit_approval 不是布尔 True → 拒绝。"""
        from src.errors import DataAgentError

        _enable_promote()
        broker, rt, attempt = _verified(repo, "t13j-nokey")
        broker.approve_promotion(attempt.execution_id, "alice")
        for bad in (None, False, "true", 1):
            with pytest.raises(DataAgentError,
                               match="explicit_approval"):
                broker.promote_execution(attempt.execution_id,
                                         explicit_approval=bad)

    def test_skill_requires_explicit_approval(self, repo):
        from src.skills import SkillRuntime

        broker, rt, attempt = _verified(repo, "t13j-skill")
        broker.approve_promotion(attempt.execution_id, "alice")
        _enable_promote()
        r = SkillRuntime(broker).run("promote_patch", {
            "execution": attempt, "explicit_approval": "true"})
        assert r.status == "failed"
        assert "explicit_approval" in r.error
        assert (repo / "src" / "lib" / "auth.ts").read_text().count(">= 8")


# ================================================================ 审批
class TestApprove:
    def test_approve_freezes_verified_patch(self, repo):
        broker, rt, attempt = _verified(repo, "t13j-freeze")
        assert attempt.verified_patch_path == ""
        approved = broker.approve_promotion(attempt.execution_id, "alice")
        assert approved.status == "READY_TO_PROMOTE"
        verified = Path(approved.verified_patch_path)
        assert verified.is_file()
        import hashlib
        assert hashlib.sha256(verified.read_bytes()).hexdigest() == \
            hashlib.sha256(Path(approved.patch_path).read_bytes()).hexdigest()
        # 决策落档：人是决策者
        decs = [d for d in broker.get_precedents()
                if d.category == "promote_approval"]
        assert decs and decs[-1].decision_maker == "human:alice"

    def test_approve_only_from_verified(self, repo):
        from src.errors import DataAgentError

        broker, rt, attempt = _verified(repo, "t13j-onlyonce")
        broker.approve_promotion(attempt.execution_id, "alice")
        with pytest.raises(DataAgentError, match="not VERIFIED"):
            broker.approve_promotion(attempt.execution_id, "alice")

    def test_approve_blocked_by_post_gate(self, repo):
        """post gate BLOCK 的执行没有资格进审批（POLICY_BLOCKED 终态）。"""
        from src.config import set_maintenance_policy
        from src.errors import DataAgentError

        broker, rt, attempt = _verified(repo, "t13j-blocked")
        # 把 auth 面拧到 BLOCK（事实没变，策略变严）
        set_maintenance_policy({"execution_policy": {
            "promote_enabled": True,
            "post_gate": {"auth_change": "BLOCK",
                          "public_api_change": "BLOCK"}}})
        with pytest.raises(DataAgentError, match="post gate BLOCK"):
            broker.approve_promotion(attempt.execution_id, "alice")
        assert broker.get_execution(
            attempt.execution_id).status == "POLICY_BLOCKED"


# ================================================================ 落地
class TestPromote:
    def test_full_promote_uncommitted(self, repo):
        """全钥匙齐 → 落地：工作树有改动、无新提交、reverse.patch 留档。"""
        import hashlib

        _enable_promote()
        broker, rt, attempt = _verified(repo, "t13j-go")
        broker.approve_promotion(attempt.execution_id, "alice")
        from src.skills import SkillRuntime
        r = SkillRuntime(broker).run("promote_patch", {
            "execution": broker.get_execution(attempt.execution_id),
            "explicit_approval": True, "actor": "test:promote"})
        assert r.ok, r.error
        assert r.data["status"] == "PROMOTED"
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        # 1. 真实仓库确实回退了（未提交）
        assert ">= 4" in (repo / "src" / "lib" / "auth.ts").read_text()
        # 2. keep 侧没动
        assert ">= 4" not in (repo / "src" / "app" / "layout.tsx"
                              ).read_text()
        # 3. 没有 commit / push：HEAD 不变、改动悬在工作树
        assert head == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                capture_output=True, text=True).stdout
        assert "src/lib/auth.ts" in status
        # 4. reverse.patch 存在且真能撤销
        reverse = Path(r.data["reverse_patch"])
        assert reverse.is_file() and reverse.read_text().strip()
        subprocess.run(["git", "apply", "-R", str(reverse)], cwd=repo,
                       check=True)
        assert ">= 8" in (repo / "src" / "lib" / "auth.ts").read_text()
        # 5. PROMOTION evidence 落档
        evs = [e for e in broker.all_evidence()
               if e.type.value == "PROMOTION"]
        assert evs and '"pushed": false' in evs[-1].payload

    def test_stale_refused_after_approval(self, repo):
        """审批后源仓库动了 → STALE_PLAN，连预检都不做。"""
        import subprocess as sp
        from src.errors import DataAgentError

        _enable_promote()
        broker, rt, attempt = _verified(repo, "t13j-stale")
        broker.approve_promotion(attempt.execution_id, "alice")
        # 在计划关心之外的文件上落一笔提交（HEAD 移动 → 计划过期）
        sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "LATE.md").write_text("late\n")
        sp.run(["git", "add", "LATE.md"], cwd=repo, check=True)
        sp.run(["git", "commit", "-qm", "late"], cwd=repo, check=True)
        try:
            with pytest.raises(DataAgentError, match="stale"):
                broker.promote_execution(attempt.execution_id,
                                         explicit_approval=True)
            assert broker.get_execution(
                attempt.execution_id).status == "STALE_PLAN"
            # 真实仓库没动
            assert (repo / "src" / "lib" / "auth.ts"
                    ).read_text().count(">= 8")
        finally:
            sp.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo,
                   check=True)
            sp.run(["git", "clean", "-fdq"], cwd=repo, check=True)

    def test_verified_patch_tamper_refused(self, repo):
        """verified.patch 被改动（与 proposed 哈希不合）→ 拒绝落地。"""
        from src.errors import DataAgentError

        _enable_promote()
        broker, rt, attempt = _verified(repo, "t13j-tamper")
        approved = broker.approve_promotion(attempt.execution_id, "alice")
        # 审批后偷改 verified.patch（模拟落盘文件被动过）
        verified = Path(approved.verified_patch_path)
        verified.write_bytes(verified.read_bytes() + b"\n# tampered\n")
        with pytest.raises(DataAgentError, match="sha256 mismatch"):
            broker.promote_execution(attempt.execution_id,
                                     explicit_approval=True)
        assert (repo / "src" / "lib" / "auth.ts").read_text().count(">= 8")

    def test_auto_push_true_refused(self, repo):
        """auto_push 必须恒为 false：配置拧成 true 直接拒绝执行 promote。"""
        from src.config import set_maintenance_policy
        from src.errors import DataAgentError

        set_maintenance_policy({"execution_policy": {
            "promote_enabled": True, "auto_push": True}})
        broker, rt, attempt = _verified(repo, "t13j-push")
        broker.approve_promotion(attempt.execution_id, "alice")
        with pytest.raises(DataAgentError, match="auto_push"):
            broker.promote_execution(attempt.execution_id,
                                     explicit_approval=True)


# ================================================================ 物理出口
class TestSourceExit:
    def test_run_source_shape_locked(self, repo):
        """run_source 只认三种 argv 形态，其余构造性拒绝。"""
        from src.errors import GitError

        broker, rt, _ = _rollback_plan(repo, "t13j-shape")
        sandbox = broker._workspace_svc.sandbox
        for bad in (["apply", "--reverse", "x.patch"],
                    ["checkout", "--", "main"],
                    ["push", "origin", "main"],
                    ["commit", "-m", "x"],
                    ["reset", "--hard"],
                    ["diff", "--name-only"]):
            with pytest.raises(GitError):
                sandbox.run_source(bad)

    def test_run_source_diff_shape_allowed(self, repo):
        """diff --no-color HEAD 是合法形态（reverse.patch 的素材）。"""
        broker, rt, _ = _rollback_plan(repo, "t13j-diff")
        sandbox = broker._workspace_svc.sandbox
        out = sandbox.run_source(["diff", "--no-color", "HEAD"])
        assert isinstance(out, str)   # 干净树 → 空输出也是 str
