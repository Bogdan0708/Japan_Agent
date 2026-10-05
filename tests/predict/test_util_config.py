from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from predict_agent.config import ConfigError, Settings, load_discovery_config
from predict_agent.util import (
    canonical_json,
    from_epoch_ms,
    isoformat,
    parse_datetime,
    sha256_json,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json"


class UtilTests(unittest.TestCase):
    def test_from_epoch_ms_is_exact_utc(self) -> None:
        self.assertEqual(
            from_epoch_ms("1791210333803"),
            datetime(2026, 10, 5, 14, 25, 33, 803000, tzinfo=UTC),
        )

    def test_parse_and_format_round_trip_z_suffix(self) -> None:
        value = parse_datetime("2026-10-28T03:59:00Z")
        self.assertEqual(isoformat(value), "2026-10-28T03:59:00Z")

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(ValueError):
            isoformat(datetime(2026, 1, 1))

    def test_canonical_json_serialises_decimal_as_exact_string(self) -> None:
        self.assertEqual(canonical_json({"b": Decimal("0.10"), "a": 1}), '{"a":1,"b":"0.10"}')

    def test_sha256_json_is_key_order_independent(self) -> None:
        self.assertEqual(sha256_json({"a": 1, "b": 2}), sha256_json({"b": 2, "a": 1}))


class ConfigTests(unittest.TestCase):
    def test_example_policy_loads(self) -> None:
        config, digest = load_discovery_config(EXAMPLE)
        self.assertEqual(config.tag_categories[0], ("geopolitics", "geopolitics"))
        self.assertEqual(config.known_outcome_threshold, Decimal("0.98"))
        self.assertEqual(len(digest), 64)

    def test_missing_policy_fails_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(ConfigError, "predict-policy.example.json"),
        ):
            load_discovery_config(Path(tmp) / "predict-policy.json")

    def test_invalid_window_rejected(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["discovery"]["min_days_to_end"] = 100
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "min_days_to_end"):
                load_discovery_config(path)

    def test_empty_tags_rejected(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["discovery"]["tag_categories"] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "tag_categories"):
                load_discovery_config(path)

    def test_settings_paths(self) -> None:
        settings = Settings.from_root(Path("/x"))
        self.assertEqual(settings.database_path, Path("/x/data/predict.sqlite3"))
        self.assertEqual(settings.policy_path, Path("/x/config/predict-policy.json"))
        self.assertEqual(settings.reports_dir, Path("/x/data/reports"))


class NonFiniteConfigTests(unittest.TestCase):
    def test_nan_config_rejected(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["discovery"]["min_liquidity"] = "NaN"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "min_liquidity"):
                load_discovery_config(path)


if __name__ == "__main__":
    unittest.main()
