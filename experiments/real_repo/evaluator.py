"""真实 repo 评分器（Phase 12A）。

规则：
- gold_source=pending：scored=False，不算 accuracy（只留 execution
  metrics，由 report 展示）
- 匹配一律带容差：文件相对路径精确；符号/限定名子串双向；commit
  8 位短 sha 前缀
- 每个 metric 都带 0-1 归一（collateral 除外，越小越好）
"""
from __future__ import annotations

from experiments.real_repo.schema import Task


def _sym_match(pred: str, gold: str) -> bool:
    """符号匹配容差：限定名与短名任一方向子串即命中。"""
    p = pred.split("::")[-1] if "::" in pred else pred
    g = gold.split("::")[-1] if "::" in gold else gold
    return gold in pred or pred in gold or p == g


def _set_prf(pred: list[str], gold: list[str], match=None) -> tuple[float, float]:
    """集合 precision/recall（match=None 时精确匹配）。"""
    if not gold:
        return (1.0 if not pred else 0.0, 1.0)
    if not pred:
        return (0.0, 0.0)
    match = match or (lambda p, g: p == g)
    hits = sum(1 for g in gold if any(match(p, g) for p in pred))
    prec = round(hits / len(pred), 3)
    rec = round(hits / len(gold), 3)
    return prec, rec


def _files_prf(pred_files: list[str], gold_files: list[str]) -> dict:
    p, r = _set_prf(pred_files, gold_files)
    return {"file_precision": p, "file_recall": r}


def score_task(task: Task, raw: dict) -> dict:
    """按任务类型评分；pending → scored=False。"""
    if task.gold_source != "manual":
        return {"scored": False,
                "reason": f"gold_source={task.gold_source}"}
    if raw.get("status") == "error":
        return {"scored": True, "errored": True,
                "all_zero": True, "note": raw.get("error", "")[:200]}
    t = task.task_type
    gold = task.gold
    tgt = raw.get("targets", {})
    out: dict = {"scored": True}

    if t == "locate":
        files = tgt.get("files", [])
        out.update(_files_prf(files, gold.get("files", [])))
        gold_syms = gold.get("symbols", [])
        hit_sym = any(_sym_match(p, g)
                      for p in tgt.get("related_symbols", [])
                      for g in gold_syms) if gold_syms else None
        out["symbol_hit"] = hit_sym
        out["unresolved"] = tgt.get("feature_id") is None

    elif t == "impact":
        p, r = _set_prf(tgt.get("callers", []), gold.get("callers", []),
                        match=_sym_match)
        out["caller_precision"], out["caller_recall"] = p, r
        gold_routes = gold.get("routes", [])
        if gold_routes:
            rp, rr = _set_prf(tgt.get("routes", []), gold_routes)
            out["route_precision"], out["route_recall"] = rp, rr

    elif t == "history":
        pred_commits = tgt.get("commits", [])
        gold_commits = gold.get("commits", [])
        if gold_commits:
            hit = sum(1 for g in gold_commits
                      if any(pc.startswith(g[:8]) or g.startswith(pc[:8])
                             for pc in pred_commits))
            out["commit_recall"] = round(hit / len(gold_commits), 3)

    elif t == "rollback":
        out.update(_score_rollback(tgt, gold))

    elif t == "compound":
        out.update(_files_prf(tgt.get("rollback_files", []),
                              gold.get("files", [])))
        gold_commits = gold.get("commits", [])
        if gold_commits:
            pred_commits = tgt.get("commits", [])
            hit = sum(1 for g in gold_commits
                      if any(pc.startswith(g[:8]) or g.startswith(pc[:8])
                             for pc in pred_commits))
            out["commit_recall"] = round(hit / len(gold_commits), 3)
        out.update(_score_rollback(tgt, gold))
        if gold.get("policy_action"):
            out["policy_action_match"] = int(
                tgt.get("policy_action") == gold["policy_action"])
    return out


def _score_rollback(tgt: dict, gold: dict) -> dict:
    rb = set(tgt.get("rollback_files", []))
    kp = set(tgt.get("keep_files", []))
    grb = set(gold.get("rollback_files", []))
    gkp = set(gold.get("keep_files", []))
    out: dict = {}
    if grb:
        out["rollback_precision"] = round(len(rb & grb) / len(rb), 3) \
            if rb else 0.0
        out["rollback_recall"] = round(len(rb & grb) / len(grb), 3)
    if gkp:
        # preservation：gold keep 文件没被误回退的比例
        out["preservation_rate"] = round(
            1 - len((rb & gkp)) / len(gkp), 3)
        out["collateral_damage"] = len(rb & gkp)
    return out


# ------------------------------------------------------------ 执行环（12A）
def score_execution(task: Task, raw: dict) -> dict:
    """执行环评分：沙箱里**实际**改了什么 vs gold（Phase 13 集成）。

    与分析面 _score_rollback 的区别：这里评的是 worktree 的真实 diff
    文件面，不是计划声明的文件面 —— 计划说得好不算数，贴出来的才算。
    """
    if task.gold_source != "manual":
        return {"scored": False,
                "reason": f"gold_source={task.gold_source}"}
    gold = task.gold
    changed = set(raw.get("exec_changed_files", []))
    out: dict = {
        "scored": True,
        # apply 成功（链走到终审/门）与全绿（VERIFIED）分开记：
        # no-op 验证命令下 VERIFIED 只证明流程通，不代表测试通过
        "exec_applied": int(raw.get("stopped_at", "")
                            in ("verify_execution", "post_gate")),
        "exec_verified": int(raw.get("exec_status") == "VERIFIED"),
    }
    grb = gold.get("rollback_files", [])
    if grb:
        hits = len(changed & set(grb))
        out["exec_precision"] = round(hits / len(changed), 3) \
            if changed else 0.0
        out["exec_recall"] = round(hits / len(grb), 3)
    gkp = gold.get("keep_files", [])
    if gkp:
        dmg = len(changed & set(gkp))
        out["exec_preservation"] = round(1 - dmg / len(gkp), 3)
        out["exec_collateral"] = dmg
    return out
