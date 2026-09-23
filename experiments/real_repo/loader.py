"""真实 repo 注册表 + broker 池（Phase 12A）。

- repo 全部本地 clone、带 .git、只读使用（git show/log/diff 只读路径；
  任何 checkout/reset/reset 类写操作被架构纪律禁止）
- 同一 repo 的图只建一次（进程内池），多任务复用
"""
from __future__ import annotations

import time
from pathlib import Path

REAL_REPOS_ROOT = Path("/workspace/yjx/real_repos")

# name → 说明。languages 只列 AST 层支持的（ts/tsx/js/jsx）。
REPO_REGISTRY: dict[str, dict] = {
    "chalk": {"path": "chalk", "lang": "js", "about": "terminal string styling"},
    "zustand": {"path": "zustand", "lang": "ts", "about": "react state management"},
    "express": {"path": "express", "lang": "js", "about": "web framework"},
}


def repo_path(name: str) -> Path:
    entry = REPO_REGISTRY.get(name)
    if entry is None:
        raise KeyError(f"unknown real repo {name!r}; known: {list(REPO_REGISTRY)}")
    return REAL_REPOS_ROOT / entry["path"]


def check_repo(name: str) -> dict:
    """repo 就绪检查（存在/带 .git/规模）。"""
    p = repo_path(name)
    return {"name": name, "exists": p.is_dir(),
            "has_git": (p / ".git").exists(), "path": str(p)}


class BrokerPool:
    """每 repo 一个 ContextBroker（图 + 变更层只建一次）。

    commits_limit：变更层扫描的最近 N 个 commit（默认与 Phase 11
    fixture 行为一致的 50）。真实 repo 的 gold commit 可能在数百个
    commit 之前 —— benchmark 用更大的窗口，这是 benchmark 配置，
    不改变 fixture baseline。
    """

    def __init__(self, commits_limit: int = 50):
        self.commits_limit = commits_limit
        self._pool: dict[str, dict] = {}

    def get(self, name: str) -> dict:
        """返回 {broker, runtime, build_ms, stats}；按需构建。"""
        if name not in self._pool:
            from src.execution import ExecutionRecorder
            from src.semgraph.change_graph import build_change_graph
            from src.semgraph.context_broker import ContextBroker
            from src.skills import SkillRuntime
            info = check_repo(name)
            if not (info["exists"] and info["has_git"]):
                raise FileNotFoundError(
                    f"real repo {name!r} not ready: {info}")
            t0 = time.perf_counter()
            broker = ContextBroker(repo_path(name), ExecutionRecorder())
            build_change_graph(broker, commits_limit=self.commits_limit)
            self._pool[name] = {
                "broker": broker,
                "runtime": SkillRuntime(broker),
                "build_ms": round((time.perf_counter() - t0) * 1000, 1),
                "stats": broker.graph.stats(),
                "commits_limit": self.commits_limit,
            }
        return self._pool[name]
