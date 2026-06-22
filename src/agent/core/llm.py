"""Provider-agnostic LLM client with auto-fallback.

Two ops used by this project: structured (forced-tool) completion for the
classifier, and plain text completion for the drafter. Anthropic is the
primary; Google Gemini is an automatic fallback so a lapsed ANTHROPIC_API_KEY
degrades the agent to a different model rather than a hard outage.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from enum import StrEnum
from functools import lru_cache

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Provider-agnostic error. Both providers wrap their native SDK errors."""


class Tier(StrEnum):
    FAST = "fast"
    DEEP = "deep"


class Provider(ABC):
    name: str

    @abstractmethod
    def structured_completion(
        self,
        *,
        tier: Tier,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict,
    ) -> dict: ...

    @abstractmethod
    def text_completion(
        self,
        *,
        tier: Tier,
        system: str,
        user: str,
        max_tokens: int,
    ) -> str: ...


# ── Anthropic ──────────────────────────────────────────────────────────────────

_ANTHROPIC_MODELS = {
    Tier.FAST: "claude-haiku-4-5-20251001",
    Tier.DEEP: "claude-sonnet-4-6",
}


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def structured_completion(
        self,
        *,
        tier: Tier,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict,
    ) -> dict:
        from anthropic import APIError

        try:
            response = self._client.messages.create(
                model=_ANTHROPIC_MODELS[tier],
                max_tokens=400,
                system=system,
                tools=[
                    {
                        "name": tool_name,
                        "description": tool_description,
                        "input_schema": tool_schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user}],
            )
        except APIError as exc:
            raise LLMError(f"anthropic structured call failed: {exc}") from exc

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return dict(block.input)
        raise LLMError(f"anthropic did not call tool {tool_name!r}: {response.content!r}")

    def text_completion(
        self,
        *,
        tier: Tier,
        system: str,
        user: str,
        max_tokens: int,
    ) -> str:
        from anthropic import APIError

        try:
            response = self._client.messages.create(
                model=_ANTHROPIC_MODELS[tier],
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except APIError as exc:
            raise LLMError(f"anthropic text call failed: {exc}") from exc

        if not response.content or not hasattr(response.content[0], "text"):
            raise LLMError(f"anthropic returned no text content: {response.content!r}")
        return response.content[0].text


# ── Gemini ─────────────────────────────────────────────────────────────────────

_GEMINI_MODEL = "gemini-2.5-flash"

# Free-tier Gemini is rate-limited (a handful of requests/min). On a 429 the API
# returns a suggested retryDelay; honour it (capped) for a bounded number of
# attempts so a short burst — backlog drain, FAST→DEEP escalation — rides over
# the per-minute window instead of failing straight through to Needs Review.
_GEMINI_MAX_RETRIES = 2
_GEMINI_RETRY_CAP_S = 35.0
_GEMINI_DEFAULT_RETRY_S = 20.0

# Gemini's response_schema is an OpenAPI-3 subset. JSON-Schema keywords it
# rejects on numbers (minimum/maximum) are stripped before sending — they're
# advisory in the original Anthropic schema anyway and the model still receives
# the description.
_GEMINI_SCHEMA_DROP_KEYS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}


def _gemini_retry_delay(exc: Exception) -> float:
    """Seconds to wait before retrying a 429, parsed from the server's hint."""
    m = re.search(r"retry(?:Delay'?:?\s*'?|\s+in\s+)(\d+(?:\.\d+)?)s", str(exc))
    delay = float(m.group(1)) if m else _GEMINI_DEFAULT_RETRY_S
    return min(delay, _GEMINI_RETRY_CAP_S)


def _strip_for_gemini(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in _GEMINI_SCHEMA_DROP_KEYS}
    if "properties" in out and isinstance(out["properties"], dict):
        out["properties"] = {k: _strip_for_gemini(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _strip_for_gemini(out["items"])
    return out


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self) -> None:
        from google import genai

        self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    def _generate(self, *, what: str, user: str, config) -> str:
        """Call generate_content, retrying on 429, and return the response text."""
        from google.genai import errors

        for attempt in range(_GEMINI_MAX_RETRIES + 1):
            try:
                response = self._client.models.generate_content(
                    model=_GEMINI_MODEL, contents=user, config=config
                )
                break
            except errors.ClientError as exc:
                if exc.code == 429 and attempt < _GEMINI_MAX_RETRIES:
                    delay = _gemini_retry_delay(exc)
                    logger.warning(
                        "[llm] gemini %s rate-limited (429) — retrying in %.0fs "
                        "(attempt %d/%d)",
                        what,
                        delay,
                        attempt + 1,
                        _GEMINI_MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                raise LLMError(f"gemini {what} call failed: {exc}") from exc
            except errors.APIError as exc:
                raise LLMError(f"gemini {what} call failed: {exc}") from exc

        text = response.text
        if not text:
            raise LLMError(f"gemini returned empty response: {response!r}")
        return text

    def structured_completion(
        self,
        *,
        tier: Tier,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict,
    ) -> dict:
        from google.genai import types

        text = self._generate(
            what="structured",
            user=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=_strip_for_gemini(tool_schema),
                max_output_tokens=400,
                # Flash is a thinking model and thinking tokens count against
                # max_output_tokens — left on, they exhaust the budget before
                # any JSON is emitted (output truncates to '{"'). Classification
                # doesn't need reasoning tokens, so disable thinking.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"gemini returned non-JSON: {text!r}") from exc

    def text_completion(
        self,
        *,
        tier: Tier,
        system: str,
        user: str,
        max_tokens: int,
    ) -> str:
        from google.genai import types

        return self._generate(
            what="text",
            user=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                # Disable thinking — see structured_completion. Drafts are short
                # and the thinking budget would otherwise eat the token cap.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )


# ── Fallback composer ──────────────────────────────────────────────────────────


class FallbackProvider(Provider):
    name = "fallback"

    def __init__(self, primary: Provider, fallback: Provider) -> None:
        self.primary = primary
        self.fallback = fallback

    def _try(self, method: str, **kwargs):
        try:
            result = getattr(self.primary, method)(**kwargs)
            logger.info("[llm] %s served by %s", method, self.primary.name)
            return result
        except LLMError as exc:
            logger.warning(
                "[llm] %s primary=%s failed (%s) — falling back to %s",
                method,
                self.primary.name,
                exc,
                self.fallback.name,
            )
            result = getattr(self.fallback, method)(**kwargs)
            logger.info("[llm] %s served by %s (fallback)", method, self.fallback.name)
            return result

    def structured_completion(self, **kwargs) -> dict:
        return self._try("structured_completion", **kwargs)

    def text_completion(self, **kwargs) -> str:
        return self._try("text_completion", **kwargs)


# ── Factory ────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_provider() -> Provider:
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))

    if has_anthropic and has_gemini:
        logger.info("[llm] provider: anthropic (primary) + gemini (fallback)")
        return FallbackProvider(AnthropicProvider(), GeminiProvider())
    if has_anthropic:
        logger.info("[llm] provider: anthropic (no gemini fallback configured)")
        return AnthropicProvider()
    if has_gemini:
        logger.info("[llm] provider: gemini only (no ANTHROPIC_API_KEY set)")
        return GeminiProvider()
    raise LLMError("No LLM credentials found — set ANTHROPIC_API_KEY and/or GEMINI_API_KEY")


__all__ = [
    "LLMError",
    "Tier",
    "Provider",
    "AnthropicProvider",
    "GeminiProvider",
    "FallbackProvider",
    "get_provider",
]
