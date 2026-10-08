"""A stand-in for the `claude_agent_sdk` module: models ClaudeSDKClient and related types."""

from __future__ import annotations

import asyncio
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


# Every field of claude_agent_sdk.types.ClaudeAgentOptions in SDK 0.2.163 (the version the
# `predict` extra pins), so a misspelled option fails here even without the real SDK.
OPTION_NAMES = frozenset({
    "add_dirs", "agents", "allowed_tools", "betas", "can_use_tool", "cli_path",
    "continue_conversation", "cwd", "debug_stderr", "disallowed_tools", "effort",
    "enable_file_checkpointing", "env", "extra_args", "fallback_model", "fork_session",
    "forward_subagent_text", "hooks", "include_hook_events", "include_partial_messages",
    "load_timeout_ms", "max_budget_usd", "max_buffer_size", "max_thinking_tokens",
    "max_turns", "mcp_servers", "model", "output_format", "permission_mode",
    "permission_prompt_tool_name", "plugins", "resume", "resume_drops_turn",
    "resume_session_at", "sandbox", "session_id", "session_store", "session_store_flush",
    "setting_sources", "settings", "skills", "stderr", "strict_mcp_config", "system_prompt",
    "task_budget", "thinking", "tools", "user", "verbatim_prompts",
})


def make_options(**kwargs: Any) -> ClaudeAgentOptions:
    unknown = sorted(set(kwargs) - OPTION_NAMES)
    if unknown:
        raise TypeError(f"ClaudeAgentOptions got unexpected keyword arguments {unknown}")
    return ClaudeAgentOptions(kwargs)


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


def fetch_page(
    url: str, *, code: int = 200, text: str = "page text", size: int | None = None
) -> dict[str, Any]:
    """A WebFetch result as CLI 2.1.286 returns it. HTTP errors and cross-host redirect
    notices come back in this same shape (as ordinary completions) with their status code."""
    return {
        "bytes": len(text.encode()) if size is None else size,
        "code": code,
        "codeText": "OK" if code == 200 else str(code),
        "result": text,
        "durationMs": 1,
        "url": url,
    }


@dataclass
class ToolCall:
    """One scripted tool call: run through PreToolUse, and (when allowed) PostToolUse with
    `response` as the tool result. A WebFetch call without a response gets a real-shaped
    200 page for its URL."""

    tool: str
    tool_input: dict[str, Any]
    response: Any = None
    error: str | None = None  # when set, the call fails: PostToolUseFailure fires

    def __post_init__(self) -> None:
        url = self.tool_input.get("url")
        if self.tool == "WebFetch" and self.response is None and isinstance(url, str):
            self.response = fetch_page(url)


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
    model: str | None = "claude-test"  # the init report's model field (None: absent)
    early_calls: list[ToolCall] = field(default_factory=list)
    send_result: bool = True
    hang_seconds: float = 0.0  # a session that stalls before its result
    hang_on_enter: float = 0.0  # startup (connect) stalls
    hang_on_exit: float = 0.0  # closing (disconnect) stalls


class FakeClient:
    """Models ClaudeSDKClient for testing."""

    def __init__(self, fake_sdk: FakeSdk, options: ClaudeAgentOptions) -> None:
        self.fake_sdk = fake_sdk
        self.options = options.kwargs
        self.prompt: str | None = None
        self.cwd = self.options["cwd"]

    async def __aenter__(self) -> FakeClient:
        if self.fake_sdk.script.hang_on_enter:
            await asyncio.sleep(self.fake_sdk.script.hang_on_enter)
        self.fake_sdk.options = self.options
        self.fake_sdk.cwd_existed = os.path.isdir(self.cwd)
        self.fake_sdk.cwd_entries = os.listdir(self.cwd)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self.fake_sdk.script.hang_on_exit:
            await asyncio.sleep(self.fake_sdk.script.hang_on_exit)
        self.fake_sdk.closed = True  # only once closing has completed
        self.fake_sdk.cwd_existed_at_close = os.path.isdir(self.cwd)
        return False

    async def query(self, prompt: str) -> None:
        self.prompt = prompt
        self.fake_sdk.prompt = prompt

    @staticmethod
    async def _post(hooks: Any, call: ToolCall, tool_input: dict[str, Any]) -> None:
        if call.error is not None:
            failure = {"tool_name": call.tool, "tool_input": tool_input, "error": call.error}
            await hooks["PostToolUseFailure"][0].hooks[0](failure, "toolu_1", {"signal": None})
            return
        post = {"tool_name": call.tool, "tool_input": tool_input,
                "tool_response": call.response}
        await hooks["PostToolUse"][0].hooks[0](post, "toolu_1", {"signal": None})

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
            await self._post(hooks, call, tool_input)
        if script.send_init:
            init: dict[str, Any] = {"tools": script.tools, "mcp_servers": script.mcp_servers}
            if script.model is not None:
                init["model"] = script.model
            yield SystemMessage("init", init)
        for call in script.calls:
            pre = {"tool_name": call.tool, "tool_input": call.tool_input}
            decision = await hooks["PreToolUse"][0].hooks[0](pre, "toolu_1", {"signal": None})
            self.fake_sdk.decisions.append(decision)
            output = decision["hookSpecificOutput"]
            if output["permissionDecision"] != "allow":
                continue
            tool_input = output.get("updatedInput", call.tool_input)
            self.fake_sdk.executed.append(ToolCall(call.tool, tool_input, call.response))
            await self._post(hooks, call, tool_input)
        yield AssistantMessage([TextBlock(script.text)])
        if script.hang_seconds:
            await asyncio.sleep(script.hang_seconds)
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
        module.ClaudeAgentOptions = make_options  # type: ignore[attr-defined]
        module.SystemMessage = SystemMessage  # type: ignore[attr-defined]
        module.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
        module.TextBlock = TextBlock  # type: ignore[attr-defined]
        module.ResultMessage = ResultMessage  # type: ignore[attr-defined]
        module.ClaudeSDKClient = self.client  # type: ignore[attr-defined]
        return module

    def client(self, *, options: ClaudeAgentOptions) -> FakeClient:
        return FakeClient(self, options)
