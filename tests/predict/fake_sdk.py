"""A stand-in for the `claude_agent_sdk` module: models ClaudeSDKClient and related types."""

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
    send_result: bool = True


class FakeClient:
    """Models ClaudeSDKClient for testing."""

    def __init__(self, fake_sdk: FakeSdk, options: ClaudeAgentOptions) -> None:
        self.fake_sdk = fake_sdk
        self.options = options.kwargs
        self.prompt: str | None = None
        self.cwd = self.options["cwd"]

    async def __aenter__(self) -> FakeClient:
        self.fake_sdk.options = self.options
        self.fake_sdk.cwd_existed = os.path.isdir(self.cwd)
        self.fake_sdk.cwd_entries = os.listdir(self.cwd)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.fake_sdk.closed = True
        self.fake_sdk.cwd_existed_at_close = os.path.isdir(self.cwd)
        return False

    async def query(self, prompt: str) -> None:
        self.prompt = prompt
        self.fake_sdk.prompt = prompt

    async def receive_response(self) -> AsyncIterator[Any]:
        hooks = self.options["hooks"]
        script = self.fake_sdk.script
        if script.raise_error is not None:
            raise script.raise_error
        # Process early_calls through hooks before init
        for call in script.early_calls:
            pre = {"tool_name": call.tool, "tool_input": call.tool_input}
            decision = await hooks["PreToolUse"][0].hooks[0](pre, "toolu_1", {"signal": None})
            self.fake_sdk.decisions.append(decision)
            output = decision["hookSpecificOutput"]
            if output["permissionDecision"] != "allow":
                continue
            tool_input = output.get("updatedInput", call.tool_input)
            self.fake_sdk.executed.append(ToolCall(call.tool, tool_input, call.response))
            post = {"tool_name": call.tool, "tool_input": tool_input,
                    "tool_response": call.response}
            await hooks["PostToolUse"][0].hooks[0](post, "toolu_1", {"signal": None})
        if script.send_init:
            yield SystemMessage(
                "init", {"tools": script.tools, "mcp_servers": script.mcp_servers}
            )
        for call in script.calls:
            pre = {"tool_name": call.tool, "tool_input": call.tool_input}
            decision = await hooks["PreToolUse"][0].hooks[0](pre, "toolu_1", {"signal": None})
            self.fake_sdk.decisions.append(decision)
            output = decision["hookSpecificOutput"]
            if output["permissionDecision"] != "allow":
                continue
            tool_input = output.get("updatedInput", call.tool_input)
            self.fake_sdk.executed.append(ToolCall(call.tool, tool_input, call.response))
            post = {"tool_name": call.tool, "tool_input": tool_input,
                    "tool_response": call.response}
            await hooks["PostToolUse"][0].hooks[0](post, "toolu_1", {"signal": None})
        yield AssistantMessage([TextBlock(script.text)])
        if script.send_result:
            yield script.result


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
        self.cwd_existed_at_close = False

    def module(self) -> types.ModuleType:
        module = types.ModuleType("claude_agent_sdk")
        module.HookMatcher = HookMatcher  # type: ignore[attr-defined]
        module.ClaudeAgentOptions = lambda **kwargs: ClaudeAgentOptions(kwargs)  # type: ignore[attr-defined]
        module.SystemMessage = SystemMessage  # type: ignore[attr-defined]
        module.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
        module.TextBlock = TextBlock  # type: ignore[attr-defined]
        module.ResultMessage = ResultMessage  # type: ignore[attr-defined]
        module.ClaudeSDKClient = self.client  # type: ignore[attr-defined]
        return module

    def client(self, *, options: ClaudeAgentOptions) -> FakeClient:
        return FakeClient(self, options)
