from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from japan_agent.execute.service import ExecutionBlocked, assert_environment_allowed
from japan_agent.models import canonical_json

from .helpers import NOW, settings


class LiveGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.base = settings(self.root, environment="live")
        self.base.ensure_directories()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_live_is_blocked_without_independent_gates(self) -> None:
        with self.assertRaises(ExecutionBlocked):
            assert_environment_allowed(self.base, NOW)

    def write_gate(self, mode: int = 0o600, **overrides) -> Path:
        gate: dict = {
            "approved_by": "Bogdan",
            "reviewed_at": NOW,
            "paper_tracking_started_at": NOW - timedelta(days=21),
            "manual_fix_free_days": 21,
            "no_risk_breaches": True,
            "journal_verified": True,
            "compliance_confirmation_reference": "T212-email-2026-08-01",
        }
        gate.update(overrides)
        path = self.root / "live-gate.json"
        path.write_text(canonical_json(gate), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def configured(self, gate_path: Path, **overrides):
        base = replace(
            self.base,
            allow_live_trading="I_ACKNOWLEDGE_REAL_MONEY",
            written_consent_reference="T212-email-2026-08-01",
            live_gate_file=gate_path,
        )
        return replace(base, **overrides) if overrides else base

    def test_complete_human_gate_allows_environment_check(self) -> None:
        assert_environment_allowed(self.configured(self.write_gate()), NOW)

    def test_missing_gate_file_blocks(self) -> None:
        with self.assertRaisesRegex(ExecutionBlocked, "missing"):
            assert_environment_allowed(self.configured(self.root / "absent.json"), NOW)

    def test_malformed_gate_file_blocks(self) -> None:
        for label, content in (("not json", "{broken"), ("missing keys", "{}")):
            with self.subTest(label):
                path = self.root / "live-gate.json"
                path.write_text(content, encoding="utf-8")
                os.chmod(path, 0o600)
                with self.assertRaisesRegex(ExecutionBlocked, "malformed"):
                    assert_environment_allowed(self.configured(path), NOW)

    def test_each_gate_condition_blocks_alone(self) -> None:
        cases = [
            ("missing acknowledgement", {}, {"allow_live_trading": None}, 0o600, "acknowledgement"),
            ("missing consent ref", {}, {"written_consent_reference": None}, 0o600, "consent"),
            ("group-accessible file", {}, {}, 0o640, "not be accessible"),
            ("no approver", {"approved_by": "  "}, {}, 0o600, "no human approver"),
            (
                "consent mismatch",
                {"compliance_confirmation_reference": "OTHER-REF"},
                {},
                0o600,
                "mismatch",
            ),
            ("short clean streak", {"manual_fix_free_days": 13}, {}, 0o600, "shorter than 14"),
            ("clean days exceed elapsed", {"manual_fix_free_days": 40}, {}, 0o600, "not support"),
            (
                "short elapsed window",
                {
                    "paper_tracking_started_at": NOW - timedelta(days=10),
                    "manual_fix_free_days": 14,
                },
                {},
                0o600,
                "not support",
            ),
            ("risk breaches", {"no_risk_breaches": False}, {}, 0o600, "risk and journal"),
            ("journal unverified", {"journal_verified": False}, {}, 0o600, "risk and journal"),
            ("future review", {"reviewed_at": NOW + timedelta(days=1)}, {}, 0o600, "future"),
        ]
        for label, gate_overrides, settings_overrides, mode, regex in cases:
            with self.subTest(label):
                gate_path = self.write_gate(mode, **gate_overrides)
                configured = self.configured(gate_path, **settings_overrides)
                with self.assertRaisesRegex(ExecutionBlocked, regex):
                    assert_environment_allowed(configured, NOW)


if __name__ == "__main__":
    unittest.main()
