from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from predict_agent.artifacts import store_artifact
from predict_agent.cash import LedgerError, available_cash
from predict_agent.cohorts import (
    CohortIdentity,
    active_cohort,
    code_version,
    cohort_id_for,
    cohort_portfolios,
    ensure_cohort,
)
from predict_agent.db import connect
from tests.predict.fixtures import NOW

REPO = Path(__file__).resolve().parents[2]


class CohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.policy = store_artifact(self.conn, "policy", '{"price": "bounds"}', NOW)
        self.shadow = store_artifact(self.conn, "policy", '{"price": "p_mid"}', NOW)
        prompt = store_artifact(self.conn, "prompt", "Forecast without prices.", NOW)
        self.identity = CohortIdentity(
            portfolios={"primary": self.policy, "shadow_mid": self.shadow},
            prompt_hash=prompt,
            model_id="claude-model-x",
            research_settings={"tools": ["WebSearch", "WebFetch"], "daily_usd": "5"},
            scoring_version="1",
            baseline_window_seconds=1800,
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def open(self, identity: CohortIdentity, bankroll: str = "1000") -> str:
        return ensure_cohort(
            self.conn, identity, starting_bankroll=Decimal(bankroll), code_version="abc", now=NOW
        )

    def test_each_portfolio_is_funded_once_and_independently(self) -> None:
        cohort = self.open(self.identity)
        self.assertEqual(self.open(self.identity), cohort)
        portfolios = cohort_portfolios(self.conn, cohort)
        self.assertEqual(set(portfolios), {"primary", "shadow_mid"})
        for portfolio_id in portfolios.values():
            self.assertEqual(available_cash(self.conn, portfolio_id), Decimal("1000"))
        self.assertEqual(active_cohort(self.conn), cohort)
        fundings = self.conn.execute(
            "SELECT COUNT(*) FROM cash_ledger WHERE entry_type = 'FUNDING'"
        ).fetchone()[0]
        self.assertEqual(fundings, 2)

    def test_every_identity_field_changes_the_cohort(self) -> None:
        other_prompt = store_artifact(self.conn, "prompt", "Forecast v2.", NOW)
        variants = {
            "portfolios": replace(self.identity, portfolios={"primary": self.policy}),
            "prompt_hash": replace(self.identity, prompt_hash=other_prompt),
            "model_id": replace(self.identity, model_id="claude-model-y"),
            "research_settings": replace(self.identity, research_settings={"tools": []}),
            "scoring_version": replace(self.identity, scoring_version="2"),
            "baseline_window_seconds": replace(self.identity, baseline_window_seconds=600),
            "generation": replace(self.identity, generation=2),
        }
        base = cohort_id_for(self.identity)
        for field, variant in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(cohort_id_for(variant), base)

    def test_new_identity_closes_old_and_funds_independently(self) -> None:
        old = self.open(self.identity)
        new = self.open(replace(self.identity, model_id="claude-model-y"), bankroll="250")
        statuses = dict(self.conn.execute("SELECT cohort_id, status FROM cohorts").fetchall())
        self.assertEqual(statuses, {old: "CLOSED", new: "ACTIVE"})
        old_primary = cohort_portfolios(self.conn, old)["primary"]
        new_primary = cohort_portfolios(self.conn, new)["primary"]
        self.assertEqual(available_cash(self.conn, old_primary), Decimal("1000"))
        self.assertEqual(available_cash(self.conn, new_primary), Decimal("250"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertIn("COHORT_CLOSED", kinds)

    def test_reopening_a_closed_identity_is_refused_but_a_new_generation_opens(self) -> None:
        self.open(self.identity)
        self.open(replace(self.identity, model_id="claude-model-y"))
        with self.assertRaisesRegex(LedgerError, "generation"):
            self.open(self.identity)
        restarted = self.open(replace(self.identity, generation=2))
        self.assertEqual(active_cohort(self.conn), restarted)

    def test_invalid_identities_and_bankrolls_refused(self) -> None:
        bad = {
            "unknown prompt": replace(self.identity, prompt_hash="0" * 64),
            "prompt used as policy": replace(
                self.identity, portfolios={"primary": self.identity.prompt_hash}
            ),
            "no primary": replace(self.identity, portfolios={"shadow_mid": self.shadow}),
            "bad variant": replace(
                self.identity, portfolios={"primary": self.policy, "Shadow Mid": self.shadow}
            ),
            "zero window": replace(self.identity, baseline_window_seconds=0),
            "generation 0": replace(self.identity, generation=0),
        }
        for label, identity in bad.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                self.open(identity)
        for bankroll in ("0", "-5", "NaN"):
            with self.subTest(bankroll=bankroll), self.assertRaises(LedgerError):
                self.open(self.identity, bankroll=bankroll)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0], 0)

    def test_cohort_and_portfolio_rows_are_frozen(self) -> None:
        cohort = self.open(self.identity)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE cohorts SET starting_bankroll = '9' WHERE cohort_id = ?", (cohort,)
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE portfolios SET policy_hash = ?", (self.shadow,))

    def test_code_version(self) -> None:
        self.assertRegex(code_version(REPO), re.compile(r"^[0-9a-f]{40}(-dirty)?$"))
        self.assertEqual(code_version(Path(self._tmp.name)), "unknown")


if __name__ == "__main__":
    unittest.main()
