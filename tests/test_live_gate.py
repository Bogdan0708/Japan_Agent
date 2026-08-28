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

    def test_complete_human_gate_allows_environment_check(self) -> None:
        gate_path = self.root / "live-gate.json"
        gate_path.write_text(
            canonical_json(
                {
                    "approved_by": "Bogdan",
                    "reviewed_at": NOW,
                    "paper_tracking_started_at": NOW - timedelta(days=21),
                    "manual_fix_free_days": 21,
                    "no_risk_breaches": True,
                    "journal_verified": True,
                    "compliance_confirmation_reference": "T212-email-2026-08-01",
                }
            ),
            encoding="utf-8",
        )
        os.chmod(gate_path, 0o600)
        configured = replace(
            self.base,
            allow_live_trading="I_ACKNOWLEDGE_REAL_MONEY",
            written_consent_reference="T212-email-2026-08-01",
            live_gate_file=gate_path,
        )
        assert_environment_allowed(configured, NOW)


if __name__ == "__main__":
    unittest.main()
