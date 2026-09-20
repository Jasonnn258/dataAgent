"""Shared plumbing for task runners: retrieval bundle + LLM/heuristic switch.

Target Mode only (输入明确目标 → 找 target → 拉局部 context → 分析).
The RetrievalBundle is the seam where future agents (CodeLocatorAgent,
GitHistoryAgent, ...) will plug in — each contributes context items, never
raw whole-repo dumps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.config import LLMConfig, Settings
from src.llm.client import LLMClient
from src.schema import ToolRecorder


@dataclass
class ContextItem:
    """One bounded piece of retrieved context (snippet with provenance)."""
    kind: str          # lexical_match | symbol_def | call_edge | git_blame | ...
    source: str        # file:line / commit sha / graph node id
    text: str
    meta: dict = field(default_factory=dict)


class RetrievalBundle:
    def __init__(self) -> None:
        self.items: list[ContextItem] = []

    def add(self, item: ContextItem, rec: ToolRecorder, cap_chars: int) -> None:
        if len(item.text) > cap_chars:
            item.text = item.text[:cap_chars] + "...[truncated]"
        self.items.append(item)
        rec.context(len(item.text), 1)

    def dump(self, cap_chars: int) -> str:
        """Render bundle as bounded text for LLM prompting."""
        parts, total = [], 0
        for it in self.items:
            chunk = f"[{it.kind}] {it.source}\n{it.text}"
            if total + len(chunk) > cap_chars:
                parts.append("...[context budget exhausted]")
                break
            parts.append(chunk)
            total += len(chunk)
        return "\n\n".join(parts)


class TaskRunnerBase:
    task: str = ""

    def __init__(self, settings: Settings):
        self.s = settings
        self.rec = ToolRecorder()
        self.repo: Path = settings.repo
        # --no-llm forces an explicitly-unconfigured client (heuristic mode)
        self.llm = LLMClient(LLMConfig()) if settings.no_llm else LLMClient(settings.llm)
        self.bundle = RetrievalBundle()

    # -- context layers, activated by mode --------------------------------
    def lexical_enabled(self) -> bool:
        return True  # every mode may fall back to lexical matching

    def structural_enabled(self) -> bool:
        return self.s.mode in ("structural", "structural_git", "semantica")

    def git_enabled(self) -> bool:
        return self.s.mode in ("structural_git", "semantica")

    def graph_enabled(self) -> bool:
        return self.s.mode == "semantica"
