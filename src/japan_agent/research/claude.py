from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..models import ResearchDecision, canonical_json


class ResearchUnavailable(RuntimeError):
    pass


DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "instrument": {"type": "string"},
        "target_weight": {"type": "number"},
        "confidence": {"type": "number"},
        "invalidation_condition": {"type": "string"},
    },
    "required": [
        "thesis",
        "evidence",
        "action",
        "instrument",
        "target_weight",
        "confidence",
        "invalidation_condition",
    ],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are a transparent AI research analyst covering Japanese new-technology
themes: robotics, AI, semiconductors, energy transition, and connected domains. You do research
only. You cannot place, schedule, modify, or communicate orders to a broker. Use only the supplied
timestamped snapshot and whitelist. Distinguish sourced facts from inference. Prefer HOLD when data
is missing, stale, conflicting, or conviction is weak. Never invent a price, filing, disclosure,
portfolio value, or citation. Your output is untrusted until deterministic risk checks and a named
human approval both pass."""


@dataclass(frozen=True)
class AgentSdkResearcher:
    model: str

    async def decide(self, snapshot_bundle: dict[str, Any]) -> ResearchDecision:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
        except ImportError as error:
            raise ResearchUnavailable(
                "install the 'research' extra to use Claude Agent SDK"
            ) from error

        prompt = (
            "Assess the supplied research snapshot and return exactly one structured decision. "
            "The instrument must be an exact ticker from whitelist_tickers. A HOLD may use the "
            "most relevant reviewed ticker. Snapshot:\n" + canonical_json(snapshot_bundle)
        )
        structured: dict[str, Any] | None = None
        fallback_result: str | None = None
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=SYSTEM_PROMPT,
            allowed_tools=[],
            max_turns=1,
            setting_sources=[],
            output_format={"type": "json_schema", "schema": DECISION_SCHEMA},
        )
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    candidate = getattr(message, "structured_output", None)
                    if isinstance(candidate, dict):
                        structured = candidate
                    result = getattr(message, "result", None)
                    if isinstance(result, str):
                        fallback_result = result
        except Exception as error:
            raise ResearchUnavailable("Claude Agent SDK research run failed") from error
        if structured is None and fallback_result:
            try:
                candidate = json.loads(fallback_result)
            except json.JSONDecodeError as error:
                raise ResearchUnavailable("research output was not structured JSON") from error
            if isinstance(candidate, dict):
                structured = candidate
        if structured is None:
            raise ResearchUnavailable("research run returned no structured decision")
        return ResearchDecision.from_dict(structured)
