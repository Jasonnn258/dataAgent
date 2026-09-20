"""Task runner registry: task name -> runner(settings, query, commit).

All runners return a schema.TaskOutput. Mode ablation is handled inside each
runner by activating retriever layers progressively:

    lexical        -> lexical retriever only
    structural     -> + symbol/call/import index
    structural_git -> + git history (log/blame/co-change)
    semantica      -> + context-graph queries over the same data
"""
from __future__ import annotations

from typing import Callable

from src.errors import DataAgentError

Runner = Callable[..., "object"]

_RUNNERS: dict[str, Runner] = {}


def register(task: str):
    def deco(fn: Runner) -> Runner:
        _RUNNERS[task] = fn
        return fn
    return deco


def get_runner(task: str) -> Runner:
    try:
        return _RUNNERS[task]
    except KeyError:
        raise DataAgentError(f"no runner registered for task {task!r}") from None


def _load_all() -> None:
    # import for side-effect of @register decorators
    from src.tasks import locate, impact, rollback  # noqa: F401


_load_all()
