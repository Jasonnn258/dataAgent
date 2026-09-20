"""Task 3: safe git rollback analysis (read-only, never executes git mutations)."""
from __future__ import annotations

from src.config import Settings
from src.errors import DataAgentError
from src.schema import TaskOutput
from src.tasks import register


@register("rollback")
def run_rollback(settings: Settings, query: str, commit: str | None = None) -> TaskOutput:
    raise DataAgentError("rollback: implemented in Phase 4")
