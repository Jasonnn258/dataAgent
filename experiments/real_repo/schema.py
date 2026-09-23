"""真实 repo benchmark 的任务 schema（Phase 12A）。

纪律（spec）：
- gold_source ∈ {manual, pending}：真实 repo 的 Ground Truth 禁止
  程序自动生成；pending 只记 execution metrics，不算 accuracy。
- 五种任务类型 locate / impact / history / rollback / compound，
  gold 键按类型固定，loader 加载时强校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field

TASK_TYPES = ("locate", "impact", "history", "rollback", "compound")
GOLD_SOURCES = ("manual", "pending")

# 每种任务的 gold 必填键（compound 是 locate+history+rollback 的并集
# 再加 policy_action；允许省略某一块，省略即该块不计分）
GOLD_KEYS: dict[str, tuple[str, ...]] = {
    "locate": ("files", "symbols"),
    "impact": ("callers",),
    "history": ("commits",),
    "rollback": ("rollback_files",),
    "compound": ("files", "commits", "rollback_files", "policy_action"),
}


@dataclass
class Task:
    """一条真实 repo 基准任务（tasks.jsonl 的一行）。"""
    id: str
    repo: str                       # loader 注册表里的名字
    task_type: str
    query: str
    gold_source: str = "pending"
    gold: dict = field(default_factory=dict)
    notes: str = ""
    # 可选执行输入（非评分标准）：
    anchor: str = ""                # impact：限定符号名，如 lib/response.js::res.send
    keep_hint: str = ""             # rollback/compound：keep 侧 query
    terms: list[str] = field(default_factory=list)   # history：显式词表（默认从 query 派生）
    target_commit: str | None = None   # 12D 历史任务用；12A 恒 None
    complexity: str = ""            # 12E 路由矩阵复用：simple|medium|complex

    def to_dict(self) -> dict:
        return {"id": self.id, "repo": self.repo, "task_type": self.task_type,
                "query": self.query, "target_commit": self.target_commit,
                "gold_source": self.gold_source, "gold": self.gold,
                "notes": self.notes, "anchor": self.anchor,
                "keep_hint": self.keep_hint, "terms": self.terms,
                "complexity": self.complexity}


class TaskSchemaError(ValueError):
    """任务行不合法（id 重复 / 类型未知 / gold 键缺失）。"""


def validate_task(d: dict) -> Task:
    """把 jsonl 一行变成 Task；任何 schema 违规就地报错。"""
    for key in ("id", "repo", "task_type", "query", "gold_source", "gold"):
        if key not in d:
            raise TaskSchemaError(f"task missing key {key!r}: {d}")
    if d["task_type"] not in TASK_TYPES:
        raise TaskSchemaError(f"{d['id']}: unknown task_type {d['task_type']!r}")
    if d["gold_source"] not in GOLD_SOURCES:
        raise TaskSchemaError(
            f"{d['id']}: gold_source must be one of {GOLD_SOURCES}")
    if d["gold_source"] == "manual":
        missing = [k for k in GOLD_KEYS[d["task_type"]] if k not in d["gold"]]
        if missing:
            raise TaskSchemaError(
                f"{d['id']}: manual gold missing {missing} "
                f"for task_type {d['task_type']}")
    t = Task(
        id=d["id"], repo=d["repo"], task_type=d["task_type"],
        query=d["query"], gold_source=d["gold_source"],
        gold=d.get("gold") or {}, notes=d.get("notes", ""),
        anchor=d.get("anchor", ""), keep_hint=d.get("keep_hint", ""),
        terms=d.get("terms", []),
        target_commit=d.get("target_commit"),
        complexity=d.get("complexity", ""))
    return t


def load_tasks(path) -> list[Task]:
    """读 tasks.jsonl（逐行 JSON）；id 重复即报错。"""
    import json
    tasks: list[Task] = []
    seen: set[str] = set()
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        t = validate_task(d)
        if t.id in seen:
            raise TaskSchemaError(f"duplicate task id {t.id!r}")
        seen.add(t.id)
        tasks.append(t)
    return tasks
