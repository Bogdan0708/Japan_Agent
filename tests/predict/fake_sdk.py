"""A stand-in for the `claude_agent_sdk` module: the same names research.sdk uses, and a
`query` that replays a scripted session through the real hooks. No network, no CLI."""

from __future__ import annotations

import os
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookMatcher:
    matcher: str | None = None
    hooks: list[Any] = field(default_factory=list)


@dataclass
class ClaudeAgentOptions:
    kwargs: dict[str, Any]


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "fake"


@dataclass
class ResultMessage:
    subtype: str
    is_error: bool = False
    total_cost_usd: float | None = None
    structured_output: Any = None


@dataclass
class ToolCall:
    """One scripted tool call: run through PreToolUse, and (when allowed) PostToolUse with
    `response` as the tool result."""

    tool: str
    tool_input: dict[str, Any]
    response: Any = None


@dataclass
class Script:
    tools: list[str] = field(
        default_factory=lambda: ["WebSearch", "WebFetch", "StructuredOutput"]
    )
    mcp_servers: list[Any] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    text: str = "Done."
    result: ResultMessage = field(
        default_factory=lambda: ResultMessage(
            "success", total_cost_usd=0.42, structured_output={"ok": True}
        )
    )
    raise_error: Exception | None = None
    send_init: bool = True
    early_calls: list[ToolCall] = field(default_factory=list)


class FakeSdk:
    """Builds the fake module and records what the adapter did with it."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.options: dict[str, Any] = {}
        self.prompt: str | None = None
        self.decisions: list[dict[str, Any]] = []  # PreToolUse outputs, in order
        self.executed: list[ToolCall] = []  # calls that passed PreToolUse
        self.cwd_existed = False
        self.cwd_entries: list[str] = []
        self.closed = False

    def module(self) -> types.ModuleType:
        module = types.ModuleType("claude_agent_sdk")
        module.HookMatcher = HookMatcher  # type: ignore[attr-defined]
        module.ClaudeAgentOptions = lambda **kwargs: ClaudeAgentOptions(kwargs)  # type: ignore[attr-defined]
        module.SystemMessage = SystemMessage  # type: ignore[attr-defined]
        module.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
        module.TextBlock = TextBlock  # type: ignore[attr-defined]
        module.ResultMessage = ResultMessage  # type: ignore[attr-defined]
        module.query = self.query  # type: ignore[attr-defined]
        return module

    async def query(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        try:
            self.prompt = prompt
            self.options = options.kwargs
            cwd = self.options["cwd"]
            self.cwd_existed = os.path.isdir(cwd)
            self.cwd_entries = os.listdir(cwd)
            if self.script.raise_error is not None:
                raise self.script.raise_error
            hooks = self.options["hooks"]
            # Process early_calls through hooks before init
            for call in self.script.early_calls:
                pre = {"tool_name": call.tool, "tool_input": call.tool_input}
                decision = await hooks["PreToolUse"][0].hooks[0](pre, "toolu_1", {"signal": None})
                self.decisions.append(decision)
                output = decision["hookSpecificOutput"]
                if output["permissionDecision"] != "allow":
                    continue
                tool_input = output.get("updatedInput", call.tool_input)
                self.executed.append(ToolCall(call.tool, tool_input, call.response))
                post = {"tool_name": call.tool, "tool_input": tool_input,
                        "tool_response": call.response}
                await hooks["PostToolUse"][0].hooks[0](post, "toolu_1", {"signal": None})
            if self.script.send_init:
                yield SystemMessage(
                    "init", {"tools": self.script.tools, "mcp_servers": self.script.mcp_servers}
                )
            for call in self.script.calls:
                pre = {"tool_name": call.tool, "tool_input": call.tool_input}
                decision = await hooks["PreToolUse"][0].hooks[0](pre, "toolu_1", {"signal": None})
                self.decisions.append(decision)
                output = decision["hookSpecificOutput"]
                if output["permissionDecision"] != "allow":
                    continue
                tool_input = output.get("updatedInput", call.tool_input)
                self.executed.append(ToolCall(call.tool, tool_input, call.response))
                post = {"tool_name": call.tool, "tool_input": tool_input,
                        "tool_response": call.response}
                await hooks["PostToolUse"][0].hooks[0](post, "toolu_1", {"signal": None})
            yield AssistantMessage([TextBlock(self.script.text)])
            yield self.script.result
        finally:
            self.closed = True
