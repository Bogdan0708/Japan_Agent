from __future__ import annotations

import ast
import contextlib
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from predict_agent.cli import main
from predict_agent.http import JsonClient
from tests.predict.fakes import RoutedOpener, http_error
from tests.predict.fixtures import (
    NO_TOKEN,
    NOW,
    YES_TOKEN,
    clob_book,
    gamma_event,
    gamma_market,
    resolution_row,
)

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "predict_agent"


class IsolationTests(unittest.TestCase):
    def test_predict_agent_never_imports_japan_agent(self) -> None:
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertFalse(name.startswith("japan_agent"))

    def test_no_write_http_methods_or_signing_in_package(self) -> None:
        forbidden = ("method=\"POST\"", "method='POST'", "eth_account", "private_key", "sign_order")
        for path in PACKAGE.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, text)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install_policy(self) -> None:
        shutil.copy(
            REPO / "config" / "predict-policy.example.json",
            self.root / "config" / "predict-policy.json",
        )

    def run_cli(self, argv: list[str], client: JsonClient | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(argv, client=client, root=self.root, now_fn=lambda: NOW)
        return code, out.getvalue()

    def test_doctor_fails_closed_without_policy(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 2)
        self.assertIn("predict-policy.example.json", output)

    def test_doctor_passes_with_policy(self) -> None:
        self.install_policy()
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)

    def test_run_data_writes_report_files(self) -> None:
        self.install_policy()
        routes: dict[str, list[object]] = {
            "api/geoblock": [{"blocked": True, "country": "GB"}],
            "tag_slug=geopolitics": [{"events": []}],
            "tag_slug=economics": [{"events": []}],
            "tag_slug=politics": [{"events": [gamma_event([gamma_market()])]}],
            "/v2/resolutions": [{"data": [resolution_row()]}, {"data": [resolution_row()]}],
            "/markets?": [[gamma_market()], []],
            f"token_id={YES_TOKEN}": [clob_book()],
            f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
        }
        client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None)
        code, output = self.run_cli(["run-data"], client=client)
        self.assertEqual(code, 0, output)
        reports = sorted((self.root / "data" / "reports").iterdir())
        self.assertEqual([p.suffix for p in reports], [".json", ".md"])
        self.assertIn("eligible: 1", reports[1].read_text(encoding="utf-8"))


class RunLifecycleTests(CliTests):
    def test_failed_run_is_marked_and_excluded_from_latest(self) -> None:
        self.install_policy()
        routes: dict[str, list[object]] = {
            "api/geoblock": [{"blocked": True, "country": "GB"}],
            "tag_slug=geopolitics": [{"events": []}],
            "tag_slug=economics": [{"events": []}],
            "tag_slug=politics": [{"events": [gamma_event([gamma_market()])]}],
            "/v2/resolutions": [http_error(503)] * 4,
        }
        client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None)
        code, _ = self.run_cli(["run-data"], client=client)
        self.assertEqual(code, 4)
        conn = sqlite3.connect(self.root / "data" / "predict.sqlite3")
        try:
            status = conn.execute("SELECT status FROM runs").fetchone()[0]
            refusal = conn.execute("SELECT reason_code FROM refusals").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((status, refusal), ("FAILED", "FETCH_ERROR"))
        code, output = self.run_cli(["report", "--latest"])
        self.assertEqual(code, 2)
        self.assertIn("no completed discovery run", output)

    def test_unknown_run_report_exits_2(self) -> None:
        self.install_policy()
        code, output = self.run_cli(["report", "--run", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown run", output)


if __name__ == "__main__":
    unittest.main()
