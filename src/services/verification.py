"""Verification Service（Phase 13G）：执行结果的终审 Verifier。

八项检查（spec 13G）归到五个裁决面：

1. expected_change_pass —— 每个回退单元的文件在 actual.patch 里真有
   反转：单元 forward 引入的行（'+'）必须被 actual 删除（出现在 '-'
   里）。查"回退真的发生了"，不是"文件变过了"。
2. preservation_pass —— 每个保留单元无损：keep 文件没进 actual → 过；
   同文件不同 hunk（keep 与 rollback 撞文件）→ keep 单元 forward 引入
   的行不允许出现在 actual 的 '-' 里；keep 文件被动但根本不在
   expected → 硬失败。
3. scope_pass —— actual 变更文件集 ⊆ plan.target_files，且 actual 与
   proposed 内容级一致（13E 对比器复检；越界文件/额外行 = 硬失败）。
4. tests_pass —— validation_results 非空且全部 PASSED（空 = 没跑过
   测试 = 不许沾沾自喜）。
5. evidence_pass —— proposed/actual patch 在案、plan 证据 id 全部可
   回查、attempt 记录完整。

overall：VERIFIED / PARTIAL（软面缺，状态不推进）/ FAILED（硬面破 →
VERIFICATION_FAILED 终态）。Verifier 只裁决，不修复。
"""
from __future__ import annotations

from pathlib import Path

from src.errors import DataAgentError
from src.git_history.api import GitAPI
from src.maintenance.models import (ExecutionAttempt,
                                    ExecutionVerification)
from src.semgraph.schema_v2 import EdgeType, NodeType
from src.services.patch import _patch_signature


class VerificationService:
    """执行核验（broker 委托至此；只读 attempt 与补丁事实）。"""

    def __init__(self, broker) -> None:
        self.broker = broker
        self.repo = broker.repo
        self.rec = broker.rec
        self._git: GitAPI | None = None

    @property
    def git(self) -> GitAPI:
        if self._git is None:
            self._git = GitAPI(self.repo, self.rec)
        return self._git

    # ------------------------------------------------------------ 事实采集
    def _unit_forward_lines(self, units: list[dict]) -> dict[str, dict]:
        """单元 → {file: {"added": [...], "removed": [...]}}（forward 行）。

        added = 该单元 forward diff 的 '+' 行（它引入的内容）；
        removed = '-' 行（它删掉的内容）。回退语义：added 必须被 actual
        删除；keep 语义：added 必须原样保留。
        """
        g = self.broker.graph
        out: dict[str, dict] = {}
        per_commit: dict[str, dict[tuple[str, int], object]] = {}
        for u in units:
            node = g.node(u["id"]) or g.node(f"cu:{u['id']}")
            if node is None or node.type != NodeType.CHANGE_UNIT:
                continue    # 图上没有就按"无行级事实"处理，靠文件级兜底
            commit = node.props.get("commit", "")
            if commit and commit not in per_commit:
                table: dict[tuple[str, int], object] = {}
                for h in self.git.diff(commit).hunks:
                    table.setdefault((h.file, h.idx), h)
                per_commit[commit] = table
            hunk_ids = {hn.props["file"]: int(hn.props["hunk_idx"])
                        for hn in g.neighbors(
                            node.id, {EdgeType.CONTAINS_CHANGE},
                            direction="out")
                        if hn.type == NodeType.HUNK}
            for file, idx in hunk_ids.items():
                h = per_commit.get(commit, {}).get((file, idx))
                if h is None:
                    continue
                bucket = out.setdefault(u["id"], {}) \
                              .setdefault(file, {"added": [], "removed": []})
                bucket["added"] += [t for tag, t in h.lines if tag == "+"]
                bucket["removed"] += [t for tag, t in h.lines if tag == "-"]
        return out

    # ------------------------------------------------------------ 裁决
    def verify(self, attempt: ExecutionAttempt) -> tuple[ExecutionAttempt,
                                                         ExecutionVerification]:
        ws = self.broker._workspace_svc
        if attempt.status != "VALIDATED":
            raise DataAgentError(
                f"verify_execution needs a VALIDATED attempt, got "
                f"{attempt.status}")
        plan = attempt.plan
        checks: list[dict] = []

        proposed = Path(attempt.patch_path)
        actual = Path(attempt.actual_patch_path)
        if not proposed.is_file() or not actual.is_file():
            raise DataAgentError(
                "attempt lacks proposed/actual patch — cannot verify")
        p_sig = _patch_signature(proposed.read_text(encoding="utf-8"))
        a_sig = _patch_signature(actual.read_text(encoding="utf-8"))

        # ---- 1. expected_change_pass：回退单元被真反转 ----
        rb_lines = self._unit_forward_lines(plan.rollback_units)
        expected_ok = True
        for u in plan.rollback_units:
            per_file = rb_lines.get(u["id"], {})
            for f in u["files"]:
                if f not in a_sig:
                    expected_ok = False
                    checks.append({"check": "expected.change",
                                   "pass": False,
                                   "detail": f"{u['id']} file not "
                                             f"reverted: {f}"})
                    continue
                added = per_file.get(f, {}).get("added", [])
                removed_by_actual = {t for tag, t in a_sig[f] if tag == "-"}
                missing = [ln for ln in added
                           if ln not in removed_by_actual]
                if missing:
                    expected_ok = False
                    checks.append({
                        "check": "expected.invert", "pass": False,
                        "detail": f"{u['id']} introduced lines still "
                                  f"present in {f}: "
                                  f"{missing[:2]}"})
        checks.append({"check": "expected.overall", "pass": expected_ok,
                       "detail": f"{len(plan.expected_changes)} "
                                 f"expected change(s)"})

        # ---- 2. preservation_pass：keep 侧无损 ----
        keep_ok = True
        rb_files = {f for u in plan.rollback_units for f in u["files"]}
        kp_lines = self._unit_forward_lines(plan.keep_units)
        for u in plan.keep_units:
            for f in u["files"]:
                if f not in a_sig:
                    continue    # 文件根本没动：最强保留
                if f not in rb_files:
                    keep_ok = False   # 只属于 keep 的文件被动了：硬伤
                    checks.append({"check": "preserve.forbidden_file",
                                   "pass": False,
                                   "detail": f"keep-only file changed: "
                                             f"{f} ({u['id']})"})
                    continue
                # 同文件不同 hunk：keep 引入的行不允许被 actual 删掉
                added = kp_lines.get(u["id"], {}).get(f, {}).get("added", [])
                removed_by_actual = {t for tag, t in a_sig[f] if tag == "-"}
                damaged = [ln for ln in added
                           if ln in removed_by_actual]
                if damaged:
                    keep_ok = False
                    checks.append({
                        "check": "preserve.keep_hunk", "pass": False,
                        "detail": f"keep unit {u['id']} lines removed "
                                  f"from {f}: {damaged[:2]}"})
        checks.append({"check": "preserve.overall", "pass": keep_ok,
                       "detail": f"{len(plan.forbidden_changes)} "
                                 f"forbidden change(s)"})

        # ---- 3. scope_pass：改动 ⊆ 计划 + actual==proposed ----
        from src.services.patch import compare_patches
        out_of_plan = sorted(set(a_sig) - set(plan.target_files))
        drift = compare_patches(proposed, actual)
        scope_ok = not out_of_plan and not drift
        checks.append({"check": "scope.files", "pass": not out_of_plan,
                       "detail": f"out-of-plan: {out_of_plan}"})
        checks.append({"check": "scope.content", "pass": not drift,
                       "detail": "; ".join(drift)[:200] or
                                 "actual == proposed"})

        # ---- 4. tests_pass：命令跑了且全过 ----
        results = attempt.validation_results or []
        tests_ok = bool(results) and all(
            r.get("status") == "PASSED" for r in results)
        statuses = [r.get("status") for r in results]
        checks.append({"check": "tests", "pass": tests_ok,
                       "detail": f"{len(results)} result(s), "
                                 f"statuses={statuses}"})

        # ---- 5. evidence_pass：链条完整可回看 ----
        found = self.broker.get_evidence(list(plan.evidence_ids)) \
            if plan.evidence_ids else []
        evidence_ok = (bool(plan.evidence_ids)
                       and len(found) == len(plan.evidence_ids)
                       and bool(attempt.patch_path)
                       and bool(attempt.actual_patch_path)
                       and plan.repository_snapshot is not None)
        checks.append({"check": "evidence", "pass": evidence_ok,
                       "detail": f"{len(found)}/{len(plan.evidence_ids)} "
                                 f"resolvable; patches on disk"})

        hard_ok = expected_ok and keep_ok and scope_ok
        soft_ok = tests_ok and evidence_ok
        overall = ("VERIFIED" if hard_ok and soft_ok
                   else "PARTIAL" if hard_ok else "FAILED")
        verdict = ExecutionVerification(
            expected_change_pass=expected_ok,
            preservation_pass=keep_ok,
            scope_pass=scope_ok,
            tests_pass=tests_ok,
            evidence_pass=evidence_ok,
            overall=overall,
            checks=checks)

        # ---- 状态推进：只有 VERIFIED 才进 VERIFIED；FAILED 落终态；
        # ---- PARTIAL 停在 VALIDATED（补齐软面后可重验） ----
        if overall == "VERIFIED":
            ws.registry.update_status(attempt.execution_id, "VERIFIED",
                                      note="all verification faces passed")
        elif overall == "FAILED":
            ws.registry.update_status(
                attempt.execution_id, "VERIFICATION_FAILED",
                note="; ".join(c["detail"] for c in checks
                               if not c["pass"])[:300])
        attempt = ws.registry.attach(
            attempt.execution_id,
            verification_results=[verdict.to_dict()])
        ws._dump_attempt(attempt)
        self.rec.tool("verification:run")
        return attempt, verdict
