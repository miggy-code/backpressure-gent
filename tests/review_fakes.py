"""In-memory fakes for the reviewer: a scripted model and a sandbox that returns canned output."""

from __future__ import annotations

import json
from typing import Any

from backpressure.review.model import Reply, ToolCall, Usage
from backpressure.review.sandbox import RunResult


class FakeWorkspace:
    def __init__(self, outputs: dict[str, tuple[int, str]] | None = None, files: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}
        self.files = files or {}
        self.commands: list[str] = []

    def exec(self, command: str, timeout: int = 120) -> RunResult:
        self.commands.append(command)
        code, out = self.outputs.get(command, (0, f"ran {command}"))
        return RunResult(command, code, out, False, 0.1)

    def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def call(name: str, args: dict[str, Any] | str, call_id: str = "c1") -> ToolCall:
    return ToolCall(call_id, name, args if isinstance(args, str) else json.dumps(args))


def reply(*calls: ToolCall, content: str = "") -> Reply:
    message: dict[str, Any] = {"role": "assistant", "content": content, "reasoning_content": "thinking"}
    if calls:
        message["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]
    return Reply(message=message, tool_calls=list(calls), usage=Usage(100, 10, 50))


class ScriptedModel:
    """Returns the scripted replies in order; repeats the last one when the script runs out."""

    model = "fake"

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[str]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        self.seen.append([dict(m) for m in messages])
        self.tools_seen.append([t["function"]["name"] for t in tools])
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
