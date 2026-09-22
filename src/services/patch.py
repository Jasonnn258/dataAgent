"""PatchService（Phase 13D）：确定性反向 patch 构建。

算法（无 LLM，可复算）：
1. 从图上收集回退单元的 HUNK 节点（file + hunk_idx + 所属 commit）
   —— **keep 单元构造性排除**：它们的 hunk 根本不进集合，不是"事后
   从 patch 里删掉"。这是"只回退问题修改、保留正确修改"的机制保证。
2. 每个单元用 GitAPI.diff(unit_commit) 取回 hunk 原文，逐行反转：
   '+'→'-'、'-'→'+'、上下文行不动；头换成
   `@@ -new_start,new_lines +old_start,old_lines @@`。
3. 同文件 hunk 按 new_start（当前状态行号）升序拼接成 unified diff。

纪律：
- 不偷改 patch：hunk 内容逐字来自 git diff 解析结果，绝不修剪/重排/
  补上下文
- 第一版不用 --3way：apply --check 不过就是 CONFLICT，不自动消解
- 落盘 proposed.patch + PatchArtifact（sha256 身份证），先在沙箱
  worktree 里 apply --check 预检，失败 → CONFLICT 终态
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from src.errors import DataAgentError, GitError
from src.git_history.api import GitAPI, Hunk
from src.maintenance.models import ExecutionAttempt, PatchArtifact
from src.semgraph.objects import Evidence, EvidenceType
from src.semgraph.schema_v2 import EdgeType, NodeType

# 反向 hunk 最多允许的行数（防超大补丁刷爆内存）
_HUNK_LINE_CAP = 5_000
# evidence payload 硬上限
_PAYLOAD_CAP = 300


class PatchService:
    """反向 patch 构建 + 落盘 + 预检（broker 内部委托至此）。"""

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

    # ------------------------------------------------------------ hunk 收集
    def _hunk_refs(self, units: list[dict]) -> list[dict]:
        """回退单元 → [{commit, file, hunk_idx, unit_id}]（图上事实）。"""
        g = self.broker.graph
        refs: list[dict] = []
        for u in units:
            uid = u["id"]
            node = g.node(uid) or g.node(f"cu:{uid}")
            if node is None or node.type != NodeType.CHANGE_UNIT:
                raise DataAgentError(
                    f"rollback unit not on graph: {uid} — cannot collect "
                    "hunks deterministically")
            # cu 的 CONTAINS_CHANGE 出边同时连着 hunk；入边是 commit，
            # 只取 HUNK 类型（注意 neighbors 必须用解析出的节点 id）
            hunks = g.neighbors(node.id, {EdgeType.CONTAINS_CHANGE},
                                direction="out")
            hunk_nodes = [n for n in hunks if n.type == NodeType.HUNK]
            if not hunk_nodes:
                raise DataAgentError(
                    f"rollback unit {uid} has no hunks on graph")
            for hn in hunk_nodes:
                refs.append({"unit_id": node.props.get("unit_id", uid),
                             "commit": node.props.get("commit", ""),
                             "file": hn.props["file"],
                             "hunk_idx": int(hn.props["hunk_idx"])})
        return refs

    # ------------------------------------------------------------ 反转
    @staticmethod
    def _reverse_hunk(h: Hunk) -> list[str]:
        """一个前向 hunk → 反向 diff 文本行（含 @@ 头，不含文件头）。"""
        if len(h.lines) > _HUNK_LINE_CAP:
            raise DataAgentError(
                f"hunk too large to reverse: {h.file}#{h.idx} "
                f"({len(h.lines)} lines)")
        # 前向：-old +new；反向：-new +old（上下文不动）
        header = (f"@@ -{h.new_start},{h.new_lines} "
                  f"+{h.old_start},{h.old_lines} @@")
        flip = {" ": " ", "+": "-", "-": "+"}
        return [header] + [flip[tag] + text for tag, text in h.lines]

    def _file_section(self, file: str, hunks: list[Hunk]) -> list[str]:
        """单文件的反向 diff 段（含 diff --git / --- / +++ 文件头）。"""
        hunks = sorted(hunks, key=lambda h: (h.new_start, h.idx))
        lines: list[str] = [f"diff --git a/{file} b/{file}"]
        added = all(h.old_lines == 0 for h in hunks)   # 前向新增 → 反向删除
        deleted = all(h.new_lines == 0 for h in hunks)  # 前向删除 → 反向重建
        lines.append("--- /dev/null" if deleted else f"--- a/{file}")
        lines.append("+++ /dev/null" if added else f"+++ b/{file}")
        for h in hunks:
            lines += self._reverse_hunk(h)
        return lines

    def build_inverse_patch(self, plan) -> tuple[str, dict]:
        """plan → (patch 文本, 构建元数据)。纯函数式，不落盘不执行。"""
        refs = self._hunk_refs(plan.rollback_units)
        # (commit, file, hunk_idx) → 去重后按 commit 取 hunk 原文
        by_commit: dict[str, dict[str, dict[int, Hunk]]] = {}
        seen: set[tuple[str, str, int]] = set()
        for r in refs:
            key = (r["commit"], r["file"], r["hunk_idx"])
            if key in seen:
                continue
            seen.add(key)
            if r["commit"] not in by_commit:
                diff = self.git.diff(r["commit"])
                by_commit[r["commit"]] = {}
                for h in diff.hunks:
                    by_commit[r["commit"]].setdefault(
                        (h.file, h.idx), h)
            table = by_commit[r["commit"]]
            h = table.get((r["file"], r["hunk_idx"]))
            if h is None:
                raise DataAgentError(
                    f"hunk vanished in git diff: {r['file']}#"
                    f"{r['hunk_idx']} @ {r['commit'][:8]}")
        # 按文件聚合（跨 commit 的同文件 hunk 合到一段）
        per_file: dict[str, list[tuple[int, Hunk]]] = {}
        unit_ids: dict[str, set[str]] = {}
        for r in refs:
            h = by_commit[r["commit"]][(r["file"], r["hunk_idx"])]
            per_file.setdefault(r["file"], []).append((h.new_start, h))
            unit_ids.setdefault(r["file"], set()).add(r["unit_id"])
        sections: list[list[str]] = []
        for file in sorted(per_file):
            hunks = [h for _, h in sorted(per_file[file],
                                          key=lambda t: (t[0], t[1].idx))]
            sections.append(self._file_section(file, hunks))
        patch_text = "\n".join(ln for sec in sections for ln in sec) + "\n"
        meta = {
            "affected_files": sorted(per_file),
            "source_change_units": sorted({r["unit_id"] for r in refs}),
            "hunks_total": sum(len(v) for v in per_file.values()),
            "unit_map": {f: sorted(v) for f, v in unit_ids.items()},
        }
        return patch_text, meta

    # ------------------------------------------------------------ 编排
    def build(self, attempt: ExecutionAttempt) -> tuple[ExecutionAttempt,
                                                        PatchArtifact | None]:
        """为 PREPARED 尝试构建 proposed.patch 并在沙箱预检。

        成功 → PATCH_BUILT；apply --check 失败 → CONFLICT（终态，不偷改
        patch、不试 --3way）。任何图/git 前置缺失 → DataAgentError。
        """
        ws = self.broker._workspace_svc
        if attempt.status != "PREPARED":
            raise DataAgentError(
                f"build_rollback_patch needs a PREPARED attempt, got "
                f"{attempt.status} (prepare_execution first)")
        if not attempt.workspace:
            raise DataAgentError("attempt has no sandbox worktree")

        patch_text, meta = self.build_inverse_patch(attempt.plan)
        run_dir = ws.runs_root / attempt.execution_id
        patch_path = run_dir / "proposed.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        sha = hashlib.sha256(patch_path.read_bytes()).hexdigest()

        artifact = PatchArtifact(
            path=str(patch_path), base_commit=attempt.plan.base_commit,
            affected_files=meta["affected_files"],
            affected_symbols=sorted({
                s.rsplit("::", 1)[-1]
                for u in attempt.plan.rollback_units
                for s in u.get("symbols", [])}),
            source_change_units=meta["source_change_units"],
            sha256=sha, hunks_total=meta["hunks_total"],
            evidence_ids=list(attempt.plan.evidence_ids))

        # ---- 沙箱预检：base 状态上必须干净可应用 ----
        try:
            ws.sandbox.run(["apply", "--check", str(patch_path)],
                           cwd=Path(attempt.workspace))
        except GitError as e:   # 预检失败是业务终态，不是崩溃
            ws.registry.update_status(
                attempt.execution_id, "CONFLICT",
                note=f"apply --check failed: {e}"[:300])
            # patch 已落盘：登记路径留审计现场（失败的 patch 也不许消失）
            ws.registry.attach(attempt.execution_id,
                               patch_path=str(patch_path))
            ws._dump_attempt(ws.registry.get(attempt.execution_id))
            return ws.registry.get(attempt.execution_id), None

        # ---- EXECUTION_PATCH evidence（审计锚点）----
        evidence = Evidence.make(
            type=EvidenceType.EXECUTION_PATCH, source="execution:patch",
            target=meta["source_change_units"][0]
            if meta["source_change_units"] else attempt.task_id,
            location=sha[:12],
            payload=(f"inverse patch for {meta['affected_files']} "
                     f"({meta['hunks_total']} hunks, keep-side excluded "
                     f"constructively)")[:_PAYLOAD_CAP],
            producer="PatchService")
        self.broker.add_evidence(evidence)
        artifact.evidence_ids.append(evidence.id)

        attempt = ws.registry.update_status(attempt.execution_id,
                                            "PATCH_BUILT",
                                            note=f"proposed.patch {sha[:12]}")
        attempt = ws.registry.attach(attempt.execution_id,
                                     patch_path=str(patch_path))
        ws._dump_attempt(attempt)
        self.rec.tool("patch:build")
        return attempt, artifact
