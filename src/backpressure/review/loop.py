"""The reviewer's tool loop: the model investigates, code validates what it submits."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from backpressure.review.model import BudgetExhausted, ModelClient, ModelError, Usage
from backpressure.review.schema import Review
from backpressure.review.tools import TOOLS, Toolbox

Validator = Callable[[Any], tuple[Review | None, list[str]]]

STEP_LIMIT_NOTE = (
    "Step limit reached. Call submit_review now with what you have verified; list everything you could not check under not_verified."
)
NUDGE = "Keep investigating with run and read_file, then finish by calling submit_review. Plain text replies are not read."


class Status(StrEnum):
    SUBMITTED = "submitted"
    STEP_LIMIT = "step_limit"
    INVALID = "invalid"  # validation failed after every allowed retry
    BUDGET = "budget"
    MODEL_ERROR = "model_error"


@dataclass
class LoopResult:
    status: Status
    review: Review | None
    messages: list[dict[str, Any]]
    steps: int
    calls: int
    usage: Usage
    errors: list[str] = field(default_factory=list)


def run_loop(
    client: ModelClient,
    toolbox: Toolbox,
    system: str,
    kickoff: str,
    validate: Validator,
    max_steps: int = 60,
    max_retries: int = 2,
    max_nudges: int = 3,
) -> LoopResult:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": kickoff}]
    steps = calls = retries = nudges = 0
    usage = Usage()
    tools = TOOLS
    last_errors: list[str] = []

    def done(status: Status, review: Review | None = None, errors: list[str] | None = None) -> LoopResult:
        return LoopResult(status, review, messages, steps, calls, usage, errors or [])

    while True:
        try:
            reply = client.complete(messages, tools)
        except BudgetExhausted as exc:
            return done(Status.BUDGET, errors=[f"model proxy cap reached: {exc}"])
        except ModelError as exc:
            return done(Status.MODEL_ERROR, errors=[str(exc)])
        calls += 1
        usage += reply.usage
        messages.append(reply.message)

        if not reply.tool_calls:
            nudges += 1
            if nudges > max_nudges:
                return done(Status.INVALID, errors=["the model stopped calling tools without submitting a review"])
            messages.append({"role": "user", "content": STEP_LIMIT_NOTE if tools is not TOOLS else NUDGE})
            continue

        for call in reply.tool_calls:
            try:
                args = json.loads(call.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as exc:
                messages.append(_tool_result(call.id, f"error: could not parse arguments as JSON ({exc}); send valid JSON"))
                continue

            if call.name == "submit_review":
                review, errors = validate(args.get("review"))
                if review is not None:
                    return done(Status.SUBMITTED, review)
                retries += 1
                last_errors = errors
                if retries > max_retries:
                    messages.append(_tool_result(call.id, "rejected:\n" + "\n".join(errors)))
                    return done(Status.INVALID, errors=errors)
                body = "\n".join(f"- {e}" for e in errors)
                messages.append(_tool_result(call.id, f"rejected, fix these and call submit_review again:\n{body}"))
                continue

            if tools is not TOOLS or steps >= max_steps:
                messages.append(_tool_result(call.id, "error: step limit reached; only submit_review is available"))
                continue
            steps += 1
            messages.append(_tool_result(call.id, toolbox.call(call.name, args)))

        if tools is TOOLS and steps >= max_steps:
            tools = [t for t in TOOLS if t["function"]["name"] == "submit_review"]
            messages.append({"role": "user", "content": STEP_LIMIT_NOTE})
        elif tools is not TOOLS and not any(c.name == "submit_review" for c in reply.tool_calls):
            return done(Status.STEP_LIMIT, errors=last_errors or ["step limit reached without a review"])


def _tool_result(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}
