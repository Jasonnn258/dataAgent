"""Task 1: natural-language code location."""
from __future__ import annotations

from src.config import Settings
from src.errors import DataAgentError
from src.schema import TaskOutput
from src.tasks import register
from src.tasks.base import TaskRunnerBase


@register("locate")
def run_locate(settings: Settings, query: str, commit: str | None = None) -> TaskOutput:
    raise DataAgentError("locate: implemented in Phase 1 (see README dev order)")
