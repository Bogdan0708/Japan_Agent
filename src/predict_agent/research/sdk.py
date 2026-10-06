"""The only module that touches the Claude Agent SDK (spec §6). Imported lazily so the
core stays stdlib-only; install the `predict` extra to use it.

Isolation, all fail closed:
- `tools=["WebSearch", "WebFetch"]` is the available built-in tool set; no settings files,
  MCP servers, skills or project files are loaded; the session runs in an empty directory
  and prompts are delivered verbatim (no @path expansion).
- A PreToolUse hook decides every tool call: WebSearch runs with the blocked-domain list
  forced onto its input; WebFetch runs only for http(s) URLs off the blocked list; the
  structured-output tool is allowed; everything else is denied. `permission_mode="dontAsk"`
  denies anything the hook does not explicitly allow.
- A PostToolUse hook records every tool input and result into the transcript.
- The session's own init report must list no tool outside EXPECTED_SESSION_TOOLS and no
  MCP server, or the run is aborted before any tool executes."""

from __future__ import annotations

import asyncio
import importlib
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from .config import blocked_host

ALLOWED_TOOLS = ("WebSearch", "WebFetch")
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
# What the session may report as available. Pinned by the Plan 4 live smoke run.
EXPECTED_SESSION_TOOLS = frozenset({*ALLOWED_TOOLS, STRUCTURED_OUTPUT_TOOL})
# Belt and braces with `tools=` and the hook (spec §6): never offer these.
DENIED_TOOLS = (
    "Agent", "Bash", "BashOutput", "Edit", "ExitPlanMode", "Glob", "Grep", "KillShell",
    "MultiEdit", "NotebookEdit", "NotebookRead", "Read", "Skill", "SlashCommand", "Task",
    "TodoWrite", "Write",
)
_RESULT_ERRORS = {
    "error_max_budget_usd": "MAX_BUDGET",
    "error_max_turns": "MAX_TURNS",
    "error_max_structured_output_retries": "SCHEMA_INVALID",
}


class ResearchUnavailable(RuntimeError):
    """The Claude Agent SDK is not installed."""


@dataclass(frozen=True)
class ResearchRequest:
    system_prompt: str
    user_prompt: str
    model: str
    max_turns: int
    budget_usd: Decimal
    blocked_domains: tuple[str, ...]
    output_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ResearchOutcome:
    structured_output: Any
    transcript: tuple[dict[str, Any], ...]
    fetched_urls: frozenset[str]
    final_text: str
    cost_usd: Decimal | None
    error: str | None  # refusal code; None on success
    detail: str


def load_sdk() -> ModuleType:
    try:
        return importlib.import_module("claude_agent_sdk")
    except ModuleNotFoundError:
        raise ResearchUnavailable(
            "the Claude Agent SDK is not installed: pip install -e '.[predict]'"
        ) from None


# A plain ASCII DNS name whose last label starts with a letter (so no IP literals).
_HOSTNAME = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


# Local-network names and wildcard-DNS services that resolve to private addresses. The hook
# does no DNS resolution; names only.
_LOCAL_NAMES = frozenset({"nip.io", "sslip.io", "localtest.me", "localhost"})
_LOCAL_SUFFIXES = (
    ".local", ".internal", ".lan", ".home.arpa", ".localhost", ".localdomain",
    ".nip.io", ".sslip.io", ".localtest.me",
)


def _fetchable(url: object, blocked: tuple[str, ...]) -> str | None:
    """None when WebFetch may open `url`, else the reason it may not. The CLI parses URLs
    with WHATWG rules, so anything Python's parser could read differently is refused:
    non-ASCII, backslashes, whitespace or control characters anywhere, and userinfo or
    percent-escapes in the authority; the host must be a plain DNS name (no IP literal)."""
    if not isinstance(url, str):
        return "url is not a string"
    if not url.isascii() or "\\" in url or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return f"url has characters URL parsers disagree on: {url!r}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"only http(s) URLs may be fetched: {url!r}"
    if "@" in parts.netloc or "%" in parts.netloc:
        return f"userinfo and escapes are not allowed in the host: {url!r}"
    host = parts.hostname or ""
    if not _HOSTNAME.fullmatch(host):
        return f"host must be a plain DNS name: {url!r}"
    if blocked_host(host, blocked):
        return f"{host} is on the blocked-domain list"
    if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        return f"{host} is a local-network name"
    lowered = url.lower()
    for domain in blocked:
        if domain in lowered or domain.replace(".", "-") in lowered:
            return f"url refers to blocked domain {domain}"
    return None


class _Session:
    """Hook state for one research run."""

    def __init__(self, blocked: tuple[str, ...]) -> None:
        self.blocked = blocked
        self.transcript: list[dict[str, Any]] = []
        self.fetched: set[str] = set()
        self.verified = False
        self.hook_error = False

    @staticmethod
    def _decision(decision: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        output: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            **extra,
        }
        if reason:
            output["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": output}

    async def pre_tool_use(
        self, input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        try:
            name = input_data.get("tool_name")
            tool_input = dict(input_data.get("tool_input") or {})
            # Fail closed until init is verified
            if not self.verified:
                reason = "session toolset not verified yet"
                self.transcript.append(
                    {"event": "denied", "tool": str(name), "input": tool_input, "reason": reason}
                )
                return self._decision("deny", reason)
            if name == "WebSearch":
                tool_input.pop("allowed_domains", None)
                tool_input["blocked_domains"] = list(self.blocked)
                self.transcript.append({"event": "call", "tool": name, "input": tool_input})
                return self._decision("allow", updatedInput=tool_input)
            if name == "WebFetch":
                fetch_reason = _fetchable(tool_input.get("url"), self.blocked)
                if fetch_reason is None:
                    self.transcript.append({"event": "call", "tool": name, "input": tool_input})
                    return self._decision("allow")
                self.transcript.append(
                    {"event": "denied", "tool": name, "input": tool_input, "reason": fetch_reason}
                )
                return self._decision("deny", fetch_reason)
            if name == STRUCTURED_OUTPUT_TOOL:
                return self._decision("allow")
            reason = f"tool {name!r} is not available for research"
            self.transcript.append(
                {"event": "denied", "tool": str(name), "input": tool_input, "reason": reason}
            )
            return self._decision("deny", reason)
        except Exception:  # noqa: BLE001
            reason = "hook could not read the tool call"
            self.transcript.append(
                {"event": "denied", "tool": str(input_data.get("tool_name", "unknown")),
                 "input": input_data.get("tool_input") or {}, "reason": reason}
            )
            return self._decision("deny", reason)

    async def post_tool_use(
        self, input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        try:
            name = input_data.get("tool_name")
            if name == STRUCTURED_OUTPUT_TOOL:
                return {}
            tool_input = dict(input_data.get("tool_input") or {})
            self.transcript.append(
                {
                    "event": "result",
                    "tool": str(name),
                    "input": tool_input,
                    "output": input_data.get("tool_response"),
                }
            )
            if name == "WebFetch" and isinstance(tool_input.get("url"), str):
                self.fetched.add(tool_input["url"])
            return {}
        except Exception:  # noqa: BLE001
            self.hook_error = True
            return {}


def build_options(sdk: ModuleType, request: ResearchRequest, session: _Session, cwd: str) -> Any:
    hook = sdk.HookMatcher
    return sdk.ClaudeAgentOptions(
        tools=list(ALLOWED_TOOLS),
        allowed_tools=[],
        disallowed_tools=list(DENIED_TOOLS),
        system_prompt=request.system_prompt,
        mcp_servers={},
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        permission_mode="dontAsk",
        max_turns=request.max_turns,
        # The SDK takes this cap as a float; it is an operational stop, not ledger money.
        max_budget_usd=float(request.budget_usd),
        model=request.model,
        output_format={"type": "json_schema", "schema": dict(request.output_schema)},
        cwd=cwd,
        verbatim_prompts=True,
        hooks={
            "PreToolUse": [hook(matcher=None, hooks=[session.pre_tool_use])],
            "PostToolUse": [hook(matcher=None, hooks=[session.post_tool_use])],
        },
    )


def toolset_problem(init_data: Mapping[str, Any]) -> str | None:
    """None when the session's init report shows exactly the research toolset."""
    tools = init_data.get("tools")
    if not isinstance(tools, list):
        return "session reported no tool list"
    extra = sorted(set(map(str, tools)) - EXPECTED_SESSION_TOOLS)
    missing = sorted(set(ALLOWED_TOOLS) - set(map(str, tools)))
    if extra or missing:
        return f"unexpected tools {extra}, missing tools {missing}"
    if init_data.get("mcp_servers"):
        return "session reported MCP servers"
    return None


def _cost(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


async def _run(sdk: ModuleType, request: ResearchRequest) -> ResearchOutcome:
    session = _Session(request.blocked_domains)
    text: list[str] = []
    structured: Any = None
    cost: Decimal | None = None
    error: str | None = None
    detail = ""
    with tempfile.TemporaryDirectory(prefix="predict-research-") as cwd:
        options = build_options(sdk, request, session, cwd)
        try:
            async with sdk.ClaudeSDKClient(options=options) as client:
                await client.query(request.user_prompt)
                async for message in client.receive_response():
                    if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                        problem = toolset_problem(message.data)
                        if problem is not None:
                            error, detail = "TOOLSET_MISMATCH", problem
                            break
                        session.verified = True
                    elif isinstance(message, sdk.AssistantMessage):
                        text.extend(
                            block.text for block in message.content
                            if isinstance(block, sdk.TextBlock)
                        )
                    elif isinstance(message, sdk.ResultMessage):
                        cost = _cost(message.total_cost_usd)
                        # Only process result if we have verified the toolset
                        if session.verified:
                            if message.subtype == "success" and not message.is_error:
                                structured = message.structured_output
                                error, detail = (None, "") if structured is not None else (
                                    "NO_RESULT", "the session returned no structured output"
                                )
                            else:
                                error = _RESULT_ERRORS.get(message.subtype, "SDK_ERROR")
                                detail = f"session ended with {message.subtype}"
            if error is None and not session.verified:
                error, detail = "TOOLSET_MISMATCH", "the session sent no init report"
            elif error is None and session.hook_error:
                error, detail = "HOOK_ERROR", "a tool result could not be recorded"
            elif error is None and structured is None:
                error, detail = "NO_RESULT", "the session ended without a result"
        except Exception as caught:  # noqa: BLE001 — any SDK failure fails this attempt
            error, detail = "SDK_ERROR", type(caught).__name__
    return ResearchOutcome(
        structured_output=structured,
        transcript=tuple(session.transcript),
        fetched_urls=frozenset(session.fetched),
        final_text="\n".join(text),
        cost_usd=cost,
        error=error,
        detail=detail,
    )


def run_research(
    request: ResearchRequest, *, load: Callable[[], ModuleType] = load_sdk
) -> ResearchOutcome:
    """Run one forecast session. Raises ResearchUnavailable when the SDK is missing;
    every other failure is returned as an outcome with an error code."""
    return asyncio.run(_run(load(), request))
