"""CLI entrypoint.

    python -m src.main --repo <path> --mode lexical|structural|structural_git|semantica \
                       --task locate|impact|rollback --query "..." [--commit SHA]

Outputs one TaskOutput JSON (schema.py) to stdout and optionally --out file.
Read-only everywhere: no git revert/reset/commit/push is ever executed here.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import Settings, VALID_MODES, VALID_TASKS
from src.errors import DataAgentError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Code location / impact / rollback experiment framework (4 ablation modes)",
    )
    p.add_argument("--repo", required=True, help="path to the target repository")
    p.add_argument("--mode", required=True, choices=VALID_MODES)
    p.add_argument("--task", required=True, choices=VALID_TASKS)
    p.add_argument("--query", required=True, help="natural-language query (or symbol name for impact)")
    p.add_argument("--commit", default=None, help="commit sha/ref for rollback task (default: HEAD)")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--out", default=None, help="also write result JSON to this file")
    p.add_argument("--no-llm", action="store_true", help="force heuristic mode even if LLM env is set")
    p.add_argument("--index-dir", default=None, help="cache dir for structural index / graph")
    return p


def run(settings: Settings, query: str, commit: str | None) -> "object":
    from src.tasks import get_runner

    settings.validate()
    runner = get_runner(settings.task)
    return runner(settings, query, commit)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings(
        repo=Path(args.repo).resolve(),
        mode=args.mode,
        task=args.task,
        top_k=args.top_k,
        index_dir=Path(args.index_dir).resolve() if args.index_dir else None,
        no_llm=args.no_llm,
    )
    try:
        output = run(settings, args.query, args.commit)
    except DataAgentError as e:
        print(f"ERROR [{type(e).__name__}]: {e}", file=sys.stderr)
        return 2
    text = output.write_json(args.out)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
