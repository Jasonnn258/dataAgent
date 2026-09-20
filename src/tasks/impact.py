"""Task 2: change impact analysis."""
from __future__ import annotations

from src.config import Settings
from src.errors import DataAgentError
from src.schema import TaskOutput
from src.tasks import register


@register("impact")
def run_impact(settings: Settings, query: str, commit: str | None = None) -> TaskOutput:
    raise DataAgentError("impact: implemented in Phase 2")
