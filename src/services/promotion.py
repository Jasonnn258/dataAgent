"""PromotionService（Phase 13J）：晋升 = 唯一允许修改真实 workspace 的路径。

三把钥匙缺一不可，少一把都不落地：
1. **审批态**：attempt 必须已 READY_TO_PROMOTE（approve() 把 VERIFIED
   推上来：复跑 post gate，BLOCK 拒批；同时冻结 verified.patch ——
   逐字节拷贝 proposed.patch，之后只认这份字节，禁止重新生成）。
2. **显式批准**：promote() 的 explicit_approval 必须严格为 True。
3. **策略开关**：execution_policy.promote_enabled 默认 false；auto_push
   必须为 false（本版本根本不存在 push 代码路径）。

落地时仍不放心世界：重拍源仓库快照对比计划（STALE_PLAN 禁止 apply ——
计划过期连预检都不做），apply --check 过了才 apply；落地后立即抓
reverse.patch（源仓库此刻的真实 diff，回滚它就是 `git apply -R`）。

默认不 commit 不 push：改动留在工作树里给人看。PROMOTED 不是"完事"，
是"交给人接管"。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.errors import DataAgentError
from src.maintenance.models import ExecutionAttempt
from src.semgraph.objects import Decision, Evidence, EvidenceType


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PromotionService:
    """审批（approve）与晋升（promote）的生命周期管理。"""

    def __init__(self, broker) -> None:
        self.broker = broker
        # workspace 服务持有注册表 / runs 目录 / sandbox git
        self._ws = broker._workspace_svc

    # ------------------------------------------------------------ 审批
    def approve(self, execution_id: str, approved_by: str = "human") \
            -> ExecutionAttempt:
        """VERIFIED → READY_TO_PROMOTE：复跑 post gate + 冻结 verified.patch。

        - post gate BLOCK → POLICY_BLOCKED 终态（被红线拦下的执行没有
          资格进入审批）；
        - verified.patch = proposed.patch 的逐字节拷贝（哈希对账）——
          之后 promote 只认这份字节，重新生成的 patch 一律不算数。
        """
        attempt = self._ws.get_execution(execution_id)
        if attempt.status != "VERIFIED":
            raise DataAgentError(
                f"cannot approve {execution_id}: status is "
                f"{attempt.status}, not VERIFIED")
        # 审批前复跑执行后门（用此刻的事实，不信验证时的旧结论）
        gate = self.broker.post_execution_gate(attempt,
                                               task_id=attempt.task_id)
        if gate.action.value == "BLOCK":
            self._ws.registry.update_status(
                execution_id, "POLICY_BLOCKED",
                note=f"approval refused — post gate BLOCK "
                     f"({gate.rule.name if gate.rule else '?'})")
            self._ws._dump_attempt(self._ws.registry.get(execution_id))
            raise DataAgentError(
                f"approval refused: post gate BLOCK "
                f"({gate.rule.name if gate.rule else 'unknown'}: "
                f"{gate.detail[:200]})")

        # 冻结 verified.patch（字节级拷贝 + 哈希对账）
        proposed = Path(attempt.patch_path or "")
        if not proposed.is_file():
            raise DataAgentError(
                f"cannot approve {execution_id}: proposed.patch missing "
                f"({attempt.patch_path!r})")
        run_dir = self._ws.runs_root / execution_id
        verified = run_dir / "verified.patch"
        verified.write_bytes(proposed.read_bytes())
        if _sha256_file(verified) != _sha256_file(proposed):
            raise DataAgentError("verified.patch copy failed hash check")

        self._ws.registry.update_status(execution_id, "READY_TO_PROMOTE",
                                        note=f"approved by {approved_by}")
        attempt = self._ws.registry.attach(execution_id,
                                           verified_patch_path=str(verified))
        self.broker.record_decision(Decision.make(
            "promote_approval", "READY_TO_PROMOTE",
            task_id=attempt.task_id, target=execution_id, risk="high",
            decision_maker=f"human:{approved_by}",
            reason_summary=("post gate re-checked: "
                            + gate.action.value)[:200],
            policy=(f"{gate.rule.name} v{gate.rule.version} -> "
                    f"{gate.action.value}") if gate.rule else "none-triggered"))
        self._ws._dump_attempt(attempt)
        return attempt

    # ------------------------------------------------------------ 晋升
    def promote(self, execution_id: str, explicit_approval=None,
                actor: str = "PromotePatchSkill") -> ExecutionAttempt:
        """把冻结的 verified.patch 贴进真实仓库（不 commit、不 push）。"""
        from src.config import maintenance_policy, policy_version
        cfg = maintenance_policy()["execution_policy"]
        if not cfg.get("promote_enabled", False):
            raise DataAgentError(
                "promote is disabled by policy "
                "(execution_policy.promote_enabled=false)")
        if cfg.get("auto_push", True):
            raise DataAgentError(
                "execution_policy.auto_push must stay false — this "
                "version has no push path at all")
        if explicit_approval is not True:
            raise DataAgentError(
                "promote requires explicit_approval=True (a human said so)")
        attempt = self._ws.get_execution(execution_id)
        if attempt.status != "READY_TO_PROMOTE":
            raise DataAgentError(
                f"cannot promote {execution_id}: status is "
                f"{attempt.status} — approval (approve_promotion) "
                f"must come first")

        # ---- 1. verified.patch 字节对账（禁止重新生成/偷换）----
        verified = Path(attempt.verified_patch_path or "")
        if not verified.is_file():
            raise DataAgentError(
                f"cannot promote {execution_id}: verified.patch missing")
        proposed = Path(attempt.patch_path or "")
        if not proposed.is_file() \
                or _sha256_file(verified) != _sha256_file(proposed):
            raise DataAgentError(
                "verified.patch does not match the sandbox-verified bytes "
                "(sha256 mismatch) — refusing to promote")

        plan = attempt.plan
        # ---- 2. stale 复查：贴之前源仓库必须还是计划时刻的样子 ----
        try:
            self._ws.assert_unchanged(plan.repository_snapshot,
                                      plan.target_files)
        except DataAgentError as e:
            self._ws.registry.update_status(execution_id, "STALE_PLAN",
                                            note=str(e)[:300])
            self._ws._dump_attempt(self._ws.registry.get(execution_id))
            raise DataAgentError(
                f"stale plan — promote refused: {e}") from e

        # ---- 3. 预检 → 落地（唯一真实仓库写出口）----
        sandbox = self._ws.sandbox
        try:
            sandbox.run_source(["apply", "--check", str(verified)])
            sandbox.run_source(["apply", str(verified)])
        except DataAgentError as e:
            self._ws.registry.update_status(execution_id, "PROMOTION_FAILED",
                                            note=str(e)[:300])
            self._ws._dump_attempt(self._ws.registry.get(execution_id))
            raise DataAgentError(f"promote failed: {e}") from e

        # ---- 4. reverse.patch：落地后的真实 diff（撤销 = git apply -R）----
        run_dir = self._ws.runs_root / execution_id
        reverse = run_dir / "reverse.patch"
        reverse.write_text(sandbox.run_source(
            ["diff", "--no-color", "HEAD"]), encoding="utf-8")

        self._ws.registry.update_status(
            execution_id, "PROMOTED",
            note=f"promoted by {actor}; changes left uncommitted "
                 f"(no commit, no push)")
        attempt = self._ws.registry.attach(execution_id,
                                           reverse_patch_path=str(reverse))

        # ---- 5. 审计：Decision + PROMOTION evidence ----
        self.broker.record_decision(Decision.make(
            "promote", "PROMOTED", task_id=attempt.task_id,
            target=execution_id, risk="high", decision_maker=actor,
            reason_summary=("verified.patch applied to source workspace; "
                            "uncommitted; reverse.patch captured")[:200],
            policy_version=policy_version()))
        payload = json.dumps({
            "execution_id": execution_id,
            "verified_sha256": _sha256_file(verified),
            "reverse_sha256": _sha256_file(reverse),
            "target_files": plan.target_files,
            "committed": False, "pushed": False}, ensure_ascii=False)
        evidence = Evidence.make(
            type=EvidenceType.PROMOTION, source=f"promote:{execution_id}",
            target=",".join(plan.target_files[:3]),
            location=plan.base_commit[:12], payload=payload[:800],
            producer=actor)
        self.broker.add_evidence(evidence)
        self._ws._dump_attempt(attempt)
        return attempt
