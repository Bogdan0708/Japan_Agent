from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from importlib.util import find_spec
from unittest import mock

from predict_agent.research.schema import OUTPUT_SCHEMA
from predict_agent.research.sdk import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    EXPECTED_SESSION_TOOLS,
    STRUCTURED_OUTPUT_TOOL,
    ResearchRequest,
    ResearchUnavailable,
    _Session,
    build_options,
    load_sdk,
    run_research,
    toolset_problem,
)
from tests.predict.fake_sdk import FakeSdk, ResultMessage, Script, ToolCall

BLOCKED = ("kalshi.com", "polymarket.com")
REQUEST = ResearchRequest(
    system_prompt="system",
    user_prompt="Question: will it happen?",
    model="claude-test",
    max_turns=7,
    budget_usd=Decimal("3.00"),
    blocked_domains=BLOCKED,
    output_schema=OUTPUT_SCHEMA,
)


def run(script: Script) -> tuple[FakeSdk, object]:
    fake = FakeSdk(script)
    outcome = run_research(REQUEST, load=fake.module)
    return fake, outcome


class OptionsTests(unittest.TestCase):
    def test_session_is_isolated_to_the_research_toolset(self) -> None:
        fake, outcome = run(Script())
        options = fake.options
        self.assertEqual(options["tools"], ["WebSearch", "WebFetch"])
        self.assertEqual(options["allowed_tools"], [])
        self.assertEqual(options["setting_sources"], [])
        self.assertEqual(options["mcp_servers"], {})
        self.assertIs(options["strict_mcp_config"], True)
        self.assertEqual(options["skills"], [])
        self.assertEqual(options["permission_mode"], "dontAsk")
        self.assertIs(options["verbatim_prompts"], True)
        self.assertEqual(options["model"], "claude-test")
        self.assertEqual(options["max_turns"], 7)
        self.assertEqual(options["max_budget_usd"], 3.0)
        self.assertEqual(options["system_prompt"], "system")
        self.assertEqual(
            options["output_format"], {"type": "json_schema", "schema": OUTPUT_SCHEMA}
        )
        self.assertEqual(set(options["hooks"]), {"PreToolUse", "PostToolUse"})
        self.assertTrue(fake.cwd_existed)
        self.assertEqual(fake.cwd_entries, [])  # an empty working directory
        self.assertEqual(fake.prompt, REQUEST.user_prompt)
        self.assertEqual(options["disallowed_tools"], list(DENIED_TOOLS))
        # No overlap between allowed and denied
        self.assertTrue(
            set(ALLOWED_TOOLS).isdisjoint(set(DENIED_TOOLS)) and
            STRUCTURED_OUTPUT_TOOL not in DENIED_TOOLS
        )
        self.assertIsNone(outcome.error)  # type: ignore[attr-defined]


class HookTests(unittest.TestCase):
    def test_web_search_always_runs_with_the_blocked_domain_list(self) -> None:
        fake, outcome = run(Script(calls=[
            ToolCall("WebSearch", {"query": "q", "allowed_domains": ["polymarket.com"]},
                     {"results": ["snippet"]}),
            ToolCall("WebSearch", {"query": "q2", "blocked_domains": []}, {"results": []}),
        ]))
        for executed in fake.executed:
            self.assertEqual(executed.tool_input["blocked_domains"], list(BLOCKED))
            self.assertNotIn("allowed_domains", executed.tool_input)

    def test_web_fetch_is_denied_for_blocked_hosts_and_non_http_urls(self) -> None:
        urls = {
            "https://polymarket.com/event/x": False,
            "https://www.kalshi.com/markets": False,
            "file:///etc/passwd": False,
            "ftp://example.org/data.csv": False,
            "https://www.reuters.com/world/a": True,
            "http://example.org/b": True,
            "https://polymarket.com\\@good.com/": False,
            "https://polymarket%2Ecom/": False,
            "https://ｐｏｌｙｍａｒｋｅｔ.com/": False,
            "https://polymarket。com/": False,
            "https://good.com@polymarket.com/": False,
            "https://1.2.3.4/": False,
            "https://[::1]/": False,
            "https://www.reuters.com/a%20b?x=1#f": True,
            "https://medium.com/@writer/post": True,
            "https://web.archive.org/web/2026/https://polymarket.com/event/x": False,
            "https://polymarket-com.translate.goog/event/x": False,
            "https://r.jina.ai/https://polymarket.com/x": False,
            "https://example.com/?u=POLYMARKET.COM": False,
            "http://router.local/": False,
            "http://metadata.google.internal/x": False,
            "http://192-168-1-1.nip.io/": False,
            "http://localhost/": False,
            "http://nas.lan/": False,
            "http://printer.home.arpa/": False,
            "http://a.sslip.io/": False,
            "http://x.localtest.me/": False,
            "https://www.internal-medicine.org/": True,
        }
        fake, outcome = run(Script(calls=[
            ToolCall("WebFetch", {"url": url, "prompt": "p"}, "page") for url in urls
        ]))
        decisions = [d["hookSpecificOutput"]["permissionDecision"] for d in fake.decisions]
        self.assertEqual(decisions, ["allow" if ok else "deny" for ok in urls.values()])
        self.assertEqual(
            outcome.fetched_urls,  # type: ignore[attr-defined]
            frozenset(url for url, ok in urls.items() if ok),
        )

    def test_any_other_tool_is_denied_and_recorded(self) -> None:
        fake, outcome = run(Script(calls=[ToolCall("Bash", {"command": "ls"}, "x")]))
        self.assertEqual(fake.executed, [])
        denied = [e for e in outcome.transcript if e["event"] == "denied"]  # type: ignore[attr-defined]
        self.assertEqual([e["tool"] for e in denied], ["Bash"])

    def test_transcript_keeps_every_result_including_uncited_snippets(self) -> None:
        snippet = {"results": [{"title": "T", "content": "Polymarket traders give it 62%"}]}
        fake, outcome = run(Script(calls=[ToolCall("WebSearch", {"query": "q"}, snippet)]))
        results = [e for e in outcome.transcript if e["event"] == "result"]  # type: ignore[attr-defined]
        self.assertEqual(results[0]["output"], snippet)

    def test_structured_output_tool_is_allowed_but_not_transcribed(self) -> None:
        fake, outcome = run(Script(calls=[ToolCall("StructuredOutput", {"a": 1}, "ok")]))
        self.assertEqual(len(fake.executed), 1)
        self.assertEqual(outcome.transcript, ())  # type: ignore[attr-defined]

    def test_hook_decision_shape(self) -> None:
        import asyncio

        session = _Session(BLOCKED)
        session.verified = True
        decision = asyncio.run(session.pre_tool_use(
            {"tool_name": "WebFetch", "tool_input": {"url": "https://polymarket.com"}},
            None, {"signal": None},
        ))
        output = decision["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("blocked", output["permissionDecisionReason"])

    def test_no_init_report_fails_closed(self) -> None:
        fake, outcome = run(Script(send_init=False))
        self.assertEqual(outcome.error, "TOOLSET_MISMATCH")  # type: ignore[attr-defined]
        self.assertIsNone(outcome.structured_output)  # type: ignore[attr-defined]

    def test_tool_call_before_init_is_denied(self) -> None:
        fake, outcome = run(Script(early_calls=[
            ToolCall("WebSearch", {"query": "q"}, {})
        ]))
        self.assertEqual(fake.decisions[0]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(fake.executed, [])
        self.assertIsNone(outcome.error)  # type: ignore[attr-defined]

    def test_malformed_hook_inputs_fail_closed(self) -> None:
        import asyncio

        session = _Session(BLOCKED)
        session.verified = True
        # pre_tool_use with malformed tool_input (not a dict)
        decision = asyncio.run(session.pre_tool_use(
            {"tool_name": "WebFetch", "tool_input": "not a dict"},
            None, {"signal": None},
        ))
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
        # post_tool_use with malformed tool_input
        result = asyncio.run(session.post_tool_use(
            {"tool_name": "WebFetch", "tool_input": "not a dict", "tool_response": "x"},
            None, {"signal": None},
        ))
        self.assertEqual(result, {})
        self.assertIs(session.hook_error, True)


class SelfCheckTests(unittest.TestCase):
    def test_extra_tool_or_mcp_server_aborts_before_any_tool_runs(self) -> None:
        for script in (
            Script(tools=["WebSearch", "WebFetch", "Bash"],
                   calls=[ToolCall("WebSearch", {"query": "q"}, {})]),
            Script(mcp_servers=[{"name": "x"}], calls=[ToolCall("WebSearch", {"query": "q"}, {})]),
            Script(tools=["WebFetch"], calls=[ToolCall("WebFetch", {"url": "https://a.org"}, "")]),
        ):
            with self.subTest(tools=script.tools, mcp=script.mcp_servers):
                fake, outcome = run(script)
                self.assertEqual(outcome.error, "TOOLSET_MISMATCH")  # type: ignore[attr-defined]
                self.assertEqual(fake.decisions, [])
                self.assertIsNone(outcome.structured_output)  # type: ignore[attr-defined]
                self.assertTrue(fake.closed)
                self.assertTrue(fake.cwd_existed_at_close)
                # Detail should be the real problem, not "session sent no init report"
                self.assertTrue(
                    "unexpected tools" in outcome.detail or "MCP" in outcome.detail  # type: ignore[attr-defined]
                )

    def test_session_is_closed_after_a_normal_run(self) -> None:
        fake, outcome = run(Script())
        self.assertTrue(fake.closed)
        self.assertTrue(fake.cwd_existed_at_close)
        self.assertIsNone(outcome.error)  # type: ignore[attr-defined]

    def test_verified_session_without_a_result_fails_closed(self) -> None:
        fake, outcome = run(Script(send_result=False))
        self.assertEqual(outcome.error, "NO_RESULT")  # type: ignore[attr-defined]
        self.assertIsNone(outcome.structured_output)  # type: ignore[attr-defined]
        self.assertTrue(fake.closed)
        self.assertTrue(fake.cwd_existed_at_close)

    def test_expected_toolset_passes(self) -> None:
        self.assertIsNone(toolset_problem({"tools": sorted(EXPECTED_SESSION_TOOLS),
                                           "mcp_servers": []}))
        self.assertIsNone(toolset_problem({"tools": ["WebSearch", "WebFetch"],
                                           "mcp_servers": []}))
        self.assertIsNotNone(toolset_problem({}))


class ResultTests(unittest.TestCase):
    def test_success_returns_output_text_and_exact_cost(self) -> None:
        output = {"abstain": True}
        fake, outcome = run(Script(text="final words",
                                   result=ResultMessage("success", total_cost_usd=0.1234,
                                                        structured_output=output)))
        self.assertEqual(outcome.structured_output, output)  # type: ignore[attr-defined]
        self.assertEqual(outcome.cost_usd, Decimal("0.1234"))  # type: ignore[attr-defined]
        self.assertEqual(outcome.final_text, "final words")  # type: ignore[attr-defined]
        self.assertIsNone(outcome.error)  # type: ignore[attr-defined]

    def test_failure_subtypes_map_to_codes(self) -> None:
        cases = {
            "error_max_budget_usd": "MAX_BUDGET",
            "error_max_turns": "MAX_TURNS",
            "error_max_structured_output_retries": "SCHEMA_INVALID",
            "error_during_execution": "SDK_ERROR",
        }
        for subtype, code in cases.items():
            with self.subTest(subtype):
                _, outcome = run(Script(result=ResultMessage(subtype, is_error=True,
                                                             total_cost_usd=1.5)))
                self.assertEqual(outcome.error, code)  # type: ignore[attr-defined]
                self.assertEqual(outcome.cost_usd, Decimal("1.5"))  # type: ignore[attr-defined]

    def test_success_without_output_and_missing_cost(self) -> None:
        _, outcome = run(Script(result=ResultMessage("success", total_cost_usd=None)))
        self.assertEqual(outcome.error, "NO_RESULT")  # type: ignore[attr-defined]
        self.assertIsNone(outcome.cost_usd)  # type: ignore[attr-defined]

    def test_sdk_exception_is_an_error_code_with_the_type_name_only(self) -> None:
        _, outcome = run(Script(raise_error=RuntimeError("secret token sk-123 leaked")))
        self.assertEqual(outcome.error, "SDK_ERROR")  # type: ignore[attr-defined]
        self.assertEqual(outcome.detail, "RuntimeError")  # type: ignore[attr-defined]


class AvailabilityTests(unittest.TestCase):
    def test_missing_sdk_raises_research_unavailable(self) -> None:
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}), \
                self.assertRaisesRegex(ResearchUnavailable, "pip install"):
            load_sdk()

    @unittest.skipUnless(find_spec("claude_agent_sdk"), "Claude Agent SDK not installed")
    def test_real_sdk_accepts_every_option_name(self) -> None:
        sdk = load_sdk()
        self.assertTrue(hasattr(sdk, "ClaudeSDKClient"))
        options = build_options(sdk, REQUEST, _Session(BLOCKED), "/tmp")
        self.assertEqual(options.tools, ["WebSearch", "WebFetch"])
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertEqual(options.setting_sources, [])
        self.assertEqual(options.skills, [])
        self.assertEqual(options.allowed_tools, [])
        self.assertEqual(options.disallowed_tools, list(DENIED_TOOLS))
        self.assertTrue(options.strict_mcp_config)
        self.assertTrue(options.verbatim_prompts)
        # Verify hooks are HookMatcher instances
        for hook_list in (options.hooks["PreToolUse"], options.hooks["PostToolUse"]):
            for matcher in hook_list:
                self.assertIsInstance(matcher, sdk.HookMatcher)


if __name__ == "__main__":
    unittest.main()
