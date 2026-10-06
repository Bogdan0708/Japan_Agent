from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.cli import main
from predict_agent.config import ConfigError
from predict_agent.research.config import blocked_host, load_research_config, parse_research
from predict_agent.research.prompt import (
    SYSTEM_PROMPT,
    prompt_artifact,
    render_user_prompt,
    research_input,
)
from tests.predict.fixtures import NOW

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "predict-policy.example.json"


def example_section() -> dict[str, Any]:
    section: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))["research"]
    return section


class ParseResearchTests(unittest.TestCase):
    def test_example_config_parses(self) -> None:
        config = parse_research(example_section())
        self.assertEqual(config.model, "claude-opus-5-5")
        self.assertEqual(config.per_forecast_usd, Decimal("3.00"))
        self.assertEqual(config.daily_usd, Decimal("30.00"))
        self.assertEqual(config.max_entry_forecasts_per_day, 15)
        self.assertEqual(config.baseline_window_seconds, 1800)
        self.assertIn("polymarket.com", config.blocked_domains)
        self.assertEqual(list(config.blocked_domains), sorted(config.blocked_domains))

    def test_settings_record_freezes_identity_fields_but_not_budgets(self) -> None:
        record = parse_research(example_section()).settings_record()
        self.assertEqual(record["tools"], ["WebSearch", "WebFetch"])
        self.assertNotIn("daily_usd", record)
        self.assertNotIn("per_forecast_usd", record)
        self.assertIn("blocked_domains", record)

    def test_invalid_sections_are_refused(self) -> None:
        def broken(**changes: Any) -> dict[str, Any]:
            section = copy.deepcopy(example_section())
            for key, value in changes.items():
                if value is None:
                    del section[key]
                else:
                    section[key] = value
            return section

        cases = {
            "missing key": broken(model=None),
            "unknown key": broken(temperature="0"),
            "empty model": broken(model=" "),
            "float budget": broken(daily_usd=30.0),
            "zero budget": broken(per_forecast_usd="0"),
            "per forecast above daily": broken(per_forecast_usd="31", daily_usd="30"),
            "bool turns": broken(max_turns=True),
            "empty domains": broken(blocked_domains=[]),
            "domain with scheme": broken(blocked_domains=["https://polymarket.com"]),
            "domain with path": broken(blocked_domains=["polymarket.com/x"]),
            "empty scoring version": broken(scoring_version=""),
            "generation zero": broken(generation=0),
            "domain with trailing newline": broken(blocked_domains=["polymarket.com\n"]),
            "uppercase domain": broken(blocked_domains=["Polymarket.com"]),
            "domain with trailing dot": broken(blocked_domains=["polymarket.com."]),
            "padded domain": broken(blocked_domains=[" polymarket.com"]),
        }
        for label, section in cases.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                parse_research(section)

    def test_blocked_host_covers_subdomains_only(self) -> None:
        blocked = ("polymarket.com",)
        self.assertTrue(blocked_host("polymarket.com", blocked))
        self.assertTrue(blocked_host("gamma-api.Polymarket.com.", blocked))
        self.assertFalse(blocked_host("notpolymarket.com", blocked))
        self.assertFalse(blocked_host("polymarket.com.evil.org", blocked))


class PromptTests(unittest.TestCase):
    def render(self) -> str:
        return render_user_prompt(
            question="Will X happen by June?",
            rules_text="Resolves Yes if X happens.",
            resolution_source="",
            end_date=NOW + timedelta(days=30),
            today=NOW,
        )

    def test_rendered_prompt_contains_only_question_rules_and_dates(self) -> None:
        prompt = self.render()
        self.assertIn("Will X happen by June?", prompt)
        self.assertIn("Resolves Yes if X happens.", prompt)
        self.assertIn("(not stated)", prompt)
        self.assertIn("2026-11-04T12:00:00Z", prompt)
        self.assertIn("Today (UTC): 2026-10-05", prompt)
        for word in ("price", "odds", "volume", "liquidity", "bid", "ask"):
            self.assertNotIn(word, prompt.lower())

    def test_artifacts_are_canonical_and_stable(self) -> None:
        self.assertEqual(prompt_artifact(), prompt_artifact())
        self.assertEqual(json.loads(prompt_artifact())["system"], SYSTEM_PROMPT)
        sent = json.loads(research_input(self.render()))
        self.assertEqual(sent, {"system": SYSTEM_PROMPT, "user": self.render()})


class ConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config" / "predict-policy.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_section_is_refused_by_loader_and_doctor(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        del raw["research"]
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "no 'research' section"):
            load_research_config(self.path)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["doctor"], root=self.root)
        self.assertEqual(code, 2)
        self.assertIn("no 'research' section", err.getvalue())

    def test_doctor_reports_the_research_model(self) -> None:
        shutil.copy(EXAMPLE, self.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["doctor"], root=self.root)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("research: model claude-opus-5-5", out.getvalue())


if __name__ == "__main__":
    unittest.main()
