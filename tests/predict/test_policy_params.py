from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.cli import main
from predict_agent.config import ConfigError
from predict_agent.policy_params import (
    load_policy_config,
    parse_policy,
    policy_from_artifact,
    variant_policies,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "predict-policy.example.json"


def example_section() -> dict[str, Any]:
    section: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))["policy"]
    return section


class ParsePolicyTests(unittest.TestCase):
    def test_example_config_parses_to_the_spec_defaults(self) -> None:
        params = parse_policy(example_section(), "bounds")
        self.assertEqual(params.starting_bankroll, Decimal("1000"))
        self.assertEqual(params.min_edge, Decimal("0.05"))
        self.assertEqual(params.min_confidence, "medium")
        self.assertEqual(params.kelly_fraction, Decimal("0.25"))
        self.assertEqual(
            (params.cap_market, params.cap_event, params.cap_category, params.cap_total_open),
            (Decimal("0.02"), Decimal("0.05"), Decimal("0.15"), Decimal("0.50")),
        )
        self.assertEqual(params.max_slippage, Decimal("0.02"))
        self.assertEqual((params.min_hours_to_close, params.max_book_age_seconds), (48, 120))

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
            "missing key": broken(min_edge=None),
            "unknown key": broken(leverage="2"),
            "float not string": broken(min_edge=0.05),
            "nan": broken(min_edge="NaN"),
            "edge 1": broken(min_edge="1"),
            "kelly above 1": broken(kelly_fraction="1.5"),
            "zero bankroll": broken(starting_bankroll="0"),
            "bad confidence": broken(min_confidence="certain"),
            "list confidence": broken(min_confidence=["high"]),
            "dict confidence": broken(min_confidence={"high": 1}),
            "caps missing one": broken(caps={"market": "0.02", "event": "0.05",
                                             "category": "0.15"}),
            "cap zero": broken(caps={"market": "0", "event": "0.05", "category": "0.15",
                                     "total_open": "0.5"}),
            "bool hours": broken(min_hours_to_close=True),
            "negative slippage": broken(max_slippage="-0.01"),
        }
        for label, section in cases.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                parse_policy(section, "bounds")
        with self.assertRaises(ConfigError):
            parse_policy(example_section(), "median")


class ArtifactTests(unittest.TestCase):
    def test_variants_differ_only_in_probability_source_and_round_trip(self) -> None:
        params = parse_policy(example_section(), "bounds")
        policies = variant_policies(params)
        self.assertEqual(set(policies), {"primary", "shadow_mid"})
        primary = policy_from_artifact(policies["primary"])
        shadow = policy_from_artifact(policies["shadow_mid"])
        self.assertEqual((primary.probability, shadow.probability), ("bounds", "mid"))
        self.assertEqual(primary, params)
        primary_raw = json.loads(policies["primary"])
        shadow_raw = json.loads(policies["shadow_mid"])
        del primary_raw["probability"], shadow_raw["probability"]
        self.assertEqual(primary_raw, shadow_raw)

    def test_corrupt_artifact_is_refused(self) -> None:
        for content in ("not json", "[]", '{"probability": "bounds"}'):
            with self.subTest(content=content), self.assertRaises(ConfigError):
                policy_from_artifact(content)


class ConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config" / "predict-policy.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, raw: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(raw), encoding="utf-8")

    def test_loads_the_policy_section_as_the_primary_policy(self) -> None:
        shutil.copy(EXAMPLE, self.path)
        self.assertEqual(load_policy_config(self.path).probability, "bounds")

    def test_missing_file_section_or_probability_override_is_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "is missing"):
            load_policy_config(self.path)
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.write({"discovery": raw["discovery"]})
        with self.assertRaisesRegex(ConfigError, "no 'policy' section"):
            load_policy_config(self.path)
        raw["policy"]["probability"] = "mid"
        self.write(raw)
        with self.assertRaisesRegex(ConfigError, "per portfolio variant"):
            load_policy_config(self.path)

    def test_doctor_fails_closed_without_a_policy_section(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.write({"discovery": raw["discovery"]})
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["doctor"], root=self.root)
        self.assertEqual(code, 2)
        self.assertIn("no 'policy' section", err.getvalue())


if __name__ == "__main__":
    unittest.main()
