"""The reviewer's model behind one small seam, so the provider can change without touching the loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import openai

# What the Agentgate proxy answers (HTTP 429, {"error": <cap>}) when a run's cap is reached.
CAP_ERRORS = ("run_cap", "customer_day_cap", "max_calls")


class ModelError(RuntimeError):
    pass


class BudgetExhausted(ModelError):
    """The proxy refused the call because the run hit its USD or call cap."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text as the model wrote it; the loop parses and reports errors back


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cached_tokens + other.cached_tokens,
        )


@dataclass(frozen=True)
class Reply:
    # The assistant message exactly as it must be sent back on the next call. DeepSeek's thinking
    # mode needs `reasoning_content` returned alongside tool results, so nothing is dropped.
    message: dict[str, Any]
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = Usage()

    @property
    def content(self) -> str:
        return str(self.message.get("content") or "")


class ModelClient(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply: ...


class OpenAICompatibleClient:
    """Chat completions against the Agentgate proxy. Never streams: the proxy meters only whole replies."""

    def __init__(self, base_url: str, token: str, model: str, max_tokens: int = 16000) -> None:
        self.model = model
        self._max_tokens = max_tokens
        # One retry for transient upstream errors; a cap refusal is final, so no more than that.
        self._client = openai.OpenAI(base_url=base_url, api_key=token, max_retries=1, timeout=600)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        try:
            resp = self._client.chat.completions.create(
                model=self.model, messages=messages, tools=tools, max_tokens=self._max_tokens, stream=False
            )  # type: ignore[call-overload]
        except openai.APIStatusError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            if exc.status_code == 429 and body.get("error") in CAP_ERRORS:
                raise BudgetExhausted(str(body["error"])) from exc
            raise ModelError(f"model call failed ({exc.status_code}): {str(exc.message)[:500]}") from exc
        except openai.APIError as exc:
            raise ModelError(f"model call failed: {exc}") from exc
        if not resp.choices:
            raise ModelError("model returned no choices")
        msg = resp.choices[0].message
        calls = [ToolCall(c.id, c.function.name, c.function.arguments or "") for c in msg.tool_calls or [] if c.type == "function"]
        usage = Usage()
        if resp.usage:
            details = resp.usage.prompt_tokens_details
            cached = (details.cached_tokens if details else None) or getattr(resp.usage, "prompt_cache_hit_tokens", 0) or 0
            usage = Usage(resp.usage.prompt_tokens, resp.usage.completion_tokens, int(cached))
        return Reply(message=msg.model_dump(exclude_none=True), tool_calls=calls, usage=usage)
