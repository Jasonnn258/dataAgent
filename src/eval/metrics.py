"""Evaluation metrics (spec §5) computed per task type.

- locate:   file_recall_at_k / file_precision_at_k over the predicted file list
- impact:   precision / recall / F1 over affected (file, symbol?) pairs,
            file-level lenient mode by default
- rollback: precision / recall over rollback files, preservation_rate over
            keep files, collateral_damage_rate = wrongly-rolled-back good
            files / all files that should be kept  (the headline number)
- system:   latency_ms / context_chars / tool_call_count from SystemMetrics

Gold format (experiments/tasks.jsonl):
  locate   {"files": [...]}
  impact   {"affected": [{"file":..., "symbol":..., "relation":...}, ...]}
  rollback {"rollback_files": [...], "keep_files": [...]}
"""
from __future__ import annotations


def _prf(pred: set, gold: set) -> dict:
    tp = len(pred & gold)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(gold) if gold else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "tp": tp, "pred_n": len(pred), "gold_n": len(gold)}


# ---------------------------------------------------------------- locate
def locate_metrics(output, gold: dict, k: int | None = None) -> dict:
    k = k or 5
    pred_files: list[str] = []
    for c in output.result.candidates[:k]:
        if c.file not in pred_files:
            pred_files.append(c.file)
    gold_files = set(gold.get("files", []))
    pred = set(pred_files)
    tp = len(pred & gold_files)
    return {
        "k": k,
        "file_recall_at_k": round(tp / len(gold_files), 4) if gold_files else None,
        "file_precision_at_k": round(tp / len(pred), 4) if pred else None,
        "pred_files": pred_files,
        "missed": sorted(gold_files - pred),
        "spurious": sorted(pred - gold_files),
    }


# ---------------------------------------------------------------- impact
def impact_metrics(output, gold: dict, level: str = "file") -> dict:
    """level='file': lenient (unique files on any side);
    level='symbol': strict (file, symbol) pairs where symbol is known."""
    sides = []
    r = output.result
    for attr in ("direct_callers", "callees", "importing_files",
                 "indirectly_affected", "related"):
        sides.extend(getattr(r, attr, []))
    if level == "file":
        pred = {s.file for s in sides}
        gold = {(a["file"]) for a in gold.get("affected", [])}
    else:
        pred = {(s.file, s.symbol) for s in sides if s.symbol}
        gold = {(a["file"], a.get("symbol")) for a in gold.get("affected", [])
                if a.get("symbol")}
    m = _prf(pred, gold)
    m["level"] = level
    return m


# ---------------------------------------------------------------- rollback
def rollback_metrics(output, gold: dict) -> dict:
    r = output.result
    pred_rb = set(r.affected_files)
    gold_rb = set(gold.get("rollback_files", []))
    gold_keep = set(gold.get("keep_files", []))
    pred_keep = set()
    for u in r.change_units:
        if u.unit_id in r.changes_to_keep:
            pred_keep.update(u.files)
    pred_keep -= pred_rb

    m = _prf(pred_rb, gold_rb)
    m["pred_rollback_files"] = sorted(pred_rb)
    m["pred_keep_files"] = sorted(pred_keep)
    if gold_keep:
        preserved = pred_keep & gold_keep
        m["preservation_rate"] = round(len(preserved) / len(gold_keep), 4)
        wrongly_rolled = pred_rb & gold_keep
        m["collateral_damage_rate"] = round(len(wrongly_rolled) / len(gold_keep), 4)
        m["collateral_files"] = sorted(wrongly_rolled)
    else:
        m["preservation_rate"] = None
        m["collateral_damage_rate"] = None
        m["collateral_files"] = []
    m["risk_estimate"] = r.collateral_damage_risk
    m["rollback_units"] = len(r.changes_to_rollback)
    return m


# ---------------------------------------------------------------- dispatch
def compute_metrics(task: str, output, gold: dict, k: int | None = None) -> dict:
    if task == "locate":
        return locate_metrics(output, gold, k)
    if task == "impact":
        return impact_metrics(output, gold)
    if task == "rollback":
        return rollback_metrics(output, gold)
    raise ValueError(f"unknown task {task}")
