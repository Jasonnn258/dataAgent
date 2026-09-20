"""Configuration for the code-agent experiment framework.

Everything is environment-driven so the same CLI runs in ablation mode
without code changes. LLM settings are optional: when unset, all tasks
fall back to deterministic heuristics and structural/Git analysis still
works (工程要求 #8).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_MODES = ("lexical", "structural", "structural_git", "semantica")
VALID_TASKS = ("locate", "impact", "rollback")

# Context budget guards (工程要求 #5: never dump a whole repo into a prompt)
MAX_SNIPPET_CHARS = 1200      # per retrieved item
MAX_CONTEXT_CHARS = 24_000    # total context handed to the LLM in one call
MAX_CANDIDATES = 20           # max candidates returned per query


@dataclass
class LLMConfig:
    base_url: str | None = field(default_factory=lambda: os.environ.get("LLM_BASE_URL"))
    api_key: str | None = field(default_factory=lambda: os.environ.get("LLM_API_KEY"))
    model: str | None = field(default_factory=lambda: os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    timeout_s: float = field(default_factory=lambda: float(os.environ.get("LLM_TIMEOUT_S", "60")))

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass
class Settings:
    repo: Path
    mode: str = "lexical"
    task: str = "locate"
    top_k: int = 10
    # cache dir for structural index / semantica graph; default inside repo parent
    index_dir: Path | None = None
    llm: LLMConfig = field(default_factory=LLMConfig)
    no_llm: bool = False  # explicit --no-llm overrides env config

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        if self.task not in VALID_TASKS:
            raise ValueError(f"task must be one of {VALID_TASKS}, got {self.task!r}")
        if not self.repo.is_dir():
            raise FileNotFoundError(f"--repo path is not a directory: {self.repo}")
        if self.mode in ("structural_git", "semantica") or self.task == "rollback":
            git_dir = self.repo / ".git"
            if not git_dir.exists():
                raise FileNotFoundError(
                    f"mode={self.mode} / task={self.task} needs a git repo, "
                    f"but {self.repo} has no .git — see experiments/fixtures/seed_fixture.py"
                )

    def llm_enabled(self) -> bool:
        return (not self.no_llm) and self.llm.available
