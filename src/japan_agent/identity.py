from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Persona:
    name: str
    one_line: str
    principles: tuple[str, ...]
    voice: str

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{2,48}", self.name):
            raise ValueError("persona name must be a professional 3-49 character name")
        combined = f"{self.one_line} {' '.join(self.principles)} {self.voice}".lower()
        if not re.search(r"\b(?:ai|artificial intelligence)\b", combined):
            raise ValueError("persona must disclose that it is an AI")
        if not any(term in combined for term in ("japan", "japanese")):
            raise ValueError("persona must state its Japan mandate")
        if not self.principles:
            raise ValueError("persona requires at least one principle")


def write_persona(root: Path, persona: Persona) -> tuple[Path, Path]:
    persona.validate()
    persona_path = root / "PERSONA.md"
    mandate_path = root / "MANDATE.md"
    if persona_path.exists() or mandate_path.exists():
        raise FileExistsError("first-boot identity already exists; refusing to overwrite it")
    persona_text = (
        f"# {persona.name}\n\n"
        f"> {persona.one_line}\n\n"
        "I am an AI research analyst, not a person, legal account holder, broker, or financial "
        "adviser. Bogdan owns the account and must approve every immutable trade ticket.\n\n"
        "## Principles\n\n"
        + "\n".join(f"- {item}" for item in persona.principles)
        + f"\n\n## Voice\n\n{persona.voice}\n"
    )
    mandate_text = (
        "# Mandate\n\n"
        "Research long-term, cash-only exposure to Japanese robotics, AI, semiconductors, energy "
        "transition, and connected domains through a broker-verified whitelist of UCITS ETFs and "
        "fractional cash equities.\n\n"
        "The analyst performs research and proposes fixed tickets. Deterministic code applies the "
        "risk policy. Bogdan is the legal account holder and explicitly approves or rejects every "
        "ticket. No approval means no order. There is no leverage, shorting, derivatives, "
        "third-party "
        "money, or direct broker access from the language model. Public writing is educational "
        "documentation of an experiment, not investment advice.\n"
    )
    persona_path.write_text(persona_text, encoding="utf-8")
    mandate_path.write_text(mandate_text, encoding="utf-8")
    return persona_path, mandate_path


IDENTITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "one_line": {"type": "string"},
        "principles": {"type": "array", "items": {"type": "string"}},
        "voice": {"type": "string"},
    },
    "required": ["name", "one_line", "principles", "voice"],
    "additionalProperties": False,
}


async def choose_persona_with_claude(model: str) -> Persona:
    """First-boot ritual. Claude chooses once; deterministic validation controls the files."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    except ImportError as error:
        raise RuntimeError(
            "install the 'research' extra for the first-boot identity ritual"
        ) from error
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=(
            "Choose a restrained professional identity for a transparent AI analyst. The identity "
            "must clearly say it is AI, explicitly tie itself to Japanese new-technology research, "
            "avoid human credentials, and avoid claims of investment-adviser status."
        ),
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
        output_format={"type": "json_schema", "schema": IDENTITY_SCHEMA},
    )
    value: dict[str, Any] | None = None
    async for message in query(
        prompt="Choose your durable name, one-line identity, principles, and writing voice.",
        options=options,
    ):
        if isinstance(message, ResultMessage):
            candidate = getattr(message, "structured_output", None)
            if isinstance(candidate, dict):
                value = candidate
    if value is None:
        raise RuntimeError("Claude returned no structured persona")
    principles = value.get("principles")
    if not isinstance(principles, list):
        raise ValueError("persona principles must be a list")
    persona = Persona(
        name=str(value["name"]).strip(),
        one_line=str(value["one_line"]).strip(),
        principles=tuple(str(item).strip() for item in principles if str(item).strip()),
        voice=str(value["voice"]).strip(),
    )
    persona.validate()
    return persona
