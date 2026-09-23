"""Unified, optional LLM interface over an OpenAI-compatible endpoint.

- Configured (LLM_BASE_URL + LLM_API_KEY [+ LLM_MODEL]): real calls, JSON-mode.
- Not configured: `available=False`; task runners use deterministic heuristics.
- Configured but failing: raises LLMError — quality must not silently degrade.

All prompts are bounded by config.MAX_CONTEXT_CHARS before being sent
(工程要求 #5/#6: retrieval → local context → reasoning).
"""
from __future__ import annotations

import json
import re
from typing import Any

from src.config import MAX_CONTEXT_CHARS, LLMConfig
from src.errors import LLMError


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = None
        # 12B：用量累计（llm_calls/prompt/completion tokens 实验指标）。
        # 只在配置了 LLM 的运行里增长 —— fixture 无 LLM，零漂移
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        if cfg.available:
            try:
                from openai import OpenAI
                self._client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout_s)
            except Exception as e:  # import/init failure of the SDK is fatal if configured
                raise LLMError(f"LLM configured but SDK init failed: {e}") from e

    @property
    def available(self) -> bool:
        return self._client is not None

    def chat_json(self, system: str, user: str, max_chars: int = MAX_CONTEXT_CHARS) -> dict[str, Any]:
        """One JSON-mode call. Returns parsed dict. Raises LLMError on failure."""
        if not self.available:
            raise LLMError("LLM not configured — caller must use heuristic path")
        if len(user) > max_chars:
            user = user[:max_chars] + f"\n...[truncated {len(user) - max_chars} chars]"
        try:
            resp = self._client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
        except Exception as e:
            raise LLMError(f"LLM call failed: {type(e).__name__}: {e}") from e
        u = getattr(resp, "usage", None)
        if u is not None:  # 端点没回 usage 时不虚造
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
            self.usage["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # tolerate ```json fences some endpoints emit despite json mode
            m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass
            raise LLMError(f"LLM returned non-JSON output: {raw[:300]!r}")
