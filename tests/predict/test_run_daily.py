from __future__ import annotations

import contextlib
import fcntl
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from predict_agent.cli import main
from predict_agent.db import connect
from predict_agent.http import JsonClient
from tests.predict.fakes import RoutedOpener, http_error
from tests.predict.fixtures import CONDITION_ID, NOW, gamma_event, gamma_market, resolution_row
from tests.predict.test_collect import events_routes
from tests.predict.test_research_run import Clock, FakeRunner, outcome
from tests.predict.trade_fixtures import seed_discovery, seed_tradeable_market

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "predict-daily.sh"
STEPS = ("_data", "_settle", "_research", "_trade", "_report_performance")
MARKETS = [CONDITION_ID, "0x" + "a" * 64]


class RunDailyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        shutil.copy(REPO / "config" / "predict-policy.example.json",
                    self.root / "config" / "predict-policy.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_daily(self, **codes: Any) -> tuple[int, str, list[str]]:
        """Run `run-daily` with every step replaced by a recorder returning codes[name]
        (default 0); a code that is an exception is raised instead."""
        calls: list[str] = []

        def recorder(name: str) -> Any:
            def step(*args: Any, **kwargs: Any) -> int:
                calls.append(name)
                result = codes.get(name, 0)
                if isinstance(result, Exception):
                    raise result
                return int(result)
            return step

        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name in STEPS:
                stack.enter_context(mock.patch(f"predict_agent.cli.{name}", recorder(name)))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            code = main(["run-daily"], root=self.root, now_fn=lambda: NOW)
        return code, out.getvalue(), calls

    def test_every_step_runs_in_order(self) -> None:
        code, output, calls = self.run_daily()
        self.assertEqual(code, 0, output)
        self.assertEqual(calls, list(STEPS))
        self.assertIn("run-daily: all steps succeeded", output)

    def test_a_failed_data_run_skips_research_but_not_the_offline_steps(self) -> None:
        code, output, calls = self.run_daily(_data=4)
        self.assertEqual(code, 6)
        self.assertEqual(calls, ["_data", "_settle", "_trade", "_report_performance"])
        self.assertIn("research skipped: today's data run failed", output)
        self.assertIn("failed: data (4), research (skipped)", output)

    def test_an_exception_in_one_step_does_not_stop_the_others(self) -> None:
        code, output, calls = self.run_daily(_research=RuntimeError("boom"))
        self.assertEqual(code, 6)
        self.assertEqual(calls, list(STEPS))
        self.assertIn("run-daily: research failed: RuntimeError: boom", output)
        self.assertIn("failed: research (1)", output)

    def test_a_refused_research_step_is_reported(self) -> None:
        code, output, _ = self.run_daily(_research=2)  # e.g. the SDK is not installed
        self.assertEqual(code, 6)
        self.assertIn("failed: research (2)", output)

    def test_a_second_run_daily_is_refused_while_the_lock_is_held(self) -> None:
        lock = self.root / "data" / "predict.sqlite3.daily.lock"
        lock.parent.mkdir(parents=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, output, calls = self.run_daily()
        finally:
            os.close(fd)
        self.assertEqual((code, calls), (2, []))
        self.assertIn("another run-daily is in progress", output)
        self.assertEqual(self.run_daily()[0], 0)  # released: the next run works

    def test_offline_steps_run_for_real_on_an_empty_database(self) -> None:
        out = io.StringIO()
        with (
            mock.patch("predict_agent.cli._data", return_value=0),
            mock.patch("predict_agent.cli._research", return_value=0),
            contextlib.redirect_stdout(out),
        ):
            code = main(["run-daily"], root=self.root, now_fn=lambda: NOW)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue((self.root / "data" / "reports"
                         / "performance-20261005T120000Z.md").exists())
        self.assertEqual(list((self.root / "data" / "reports").glob("*.tmp")), [])


class RealResearchStepTests(unittest.TestCase):
    def test_broken_research_fails_the_daily_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            shutil.copy(REPO / "config" / "predict-policy.example.json",
                        root / "config" / "predict-policy.json")
            conn = connect(root / "data" / "predict.sqlite3")
            try:
                for condition_id in MARKETS:
                    seed_tradeable_market(conn, condition_id, end_date=NOW + timedelta(days=20))
                seed_discovery(conn, MARKETS, at=NOW - timedelta(hours=1))
            finally:
                conn.close()
            bad = outcome(error="SDK_ERROR", cost_usd=None, structured_output=None)
            client = JsonClient(opener=RoutedOpener({"/book": []}), sleep=lambda _: None,
                                jitter=lambda: 0.0)
            out = io.StringIO()
            with (
                mock.patch("predict_agent.cli._data", return_value=0),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(out),
            ):
                code = main(["run-daily"], root=root, client=client, now_fn=Clock(NOW),
                            runner=FakeRunner(bad, bad))
            self.assertEqual(code, 6, out.getvalue())
            self.assertIn("research had operational failures: SDK_ERROR 2", out.getvalue())
            self.assertIn("failed: research (5)", out.getvalue())


class BookOutageDailyTests(unittest.TestCase):
    def test_a_book_outage_fails_the_data_run_and_skips_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            shutil.copy(REPO / "config" / "predict-policy.example.json",
                        root / "config" / "predict-policy.json")
            routes = events_routes({"politics": [gamma_event([gamma_market()])]},
                                   [resolution_row()])
            routes["/v2/resolutions"].append({"data": [resolution_row()]})
            routes["/geoblock"] = [{"blocked": True, "country": "GB"}]
            routes["/markets?"] = [[gamma_market()], []]
            errors = [http_error(503) for _ in range(50)]
            for error in errors:
                self.addCleanup(error.close)  # unused ones would leak their temp file
            routes["/book"] = list(errors)
            client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None,
                                jitter=lambda: 0.0)
            runner = FakeRunner()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = main(["run-daily"], root=root, client=client, runner=runner,
                            now_fn=Clock(NOW))
            self.assertEqual(code, 6, out.getvalue())
            self.assertEqual(runner.requests, [])
            self.assertIn("books unavailable:", out.getvalue())
            self.assertIn("research (skipped)", out.getvalue())
            conn = connect(root / "data" / "predict.sqlite3")
            try:
                self.assertGreater(conn.execute(
                    "SELECT COUNT(*) FROM resolution_observations").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM refusals WHERE reason_code = 'BOOKS_UNAVAILABLE'"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT status FROM runs WHERE command = 'run-data'").fetchone()[0],
                    "FAILED")
            finally:
                conn.close()


class ScriptTests(unittest.TestCase):
    def test_wrapper_is_valid_bash_and_locks_times_out_and_runs_run_daily(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
        text = SCRIPT.read_text(encoding="utf-8")
        for needle in ("set -euo pipefail", "flock -n 9", "timeout ",
                       "predict_agent.cli run-daily", "PYTHONUNBUFFERED", "command -v flock"):
            self.assertIn(needle, text)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
