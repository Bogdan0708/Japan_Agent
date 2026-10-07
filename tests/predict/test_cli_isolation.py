from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
RESEARCH_MAY_IMPORT = {"config", "util"}  # predict_agent modules the research package may use


def research_internal_imports(source: str) -> list[str]:
    """predict_agent modules a research source file imports (relative or absolute), plus
    any `importlib.import_module("predict_agent...")` target."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level >= 2 or module == "predict_agent" or module.startswith("predict_agent."):
                base = module.removeprefix("predict_agent").lstrip(".")
                if base:
                    found.append(base.split(".")[0])
                else:
                    found += [alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            found += [
                alias.name.removeprefix("predict_agent.").split(".")[0]
                for alias in node.names
                if alias.name == "predict_agent" or alias.name.startswith("predict_agent.")
            ]
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("predict_agent")
        ):
            found.append(node.args[0].value)
    return found


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

    def test_research_direct_imports_are_in_allow_list(self) -> None:
        # Spec §6/§8: the research layer imports nothing that touches money, policy,
        # paper trading or market data, so no code path can hand Claude a price.
        # Direct imports must be in RESEARCH_MAY_IMPORT.
        for path in (PACKAGE / "research").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for module in research_internal_imports(source):
                with self.subTest(file=path.name, module=module):
                    self.assertIn(module, RESEARCH_MAY_IMPORT)

    def test_research_modules_load_no_money_or_market_code(self) -> None:
        # Transitive check: in a fresh interpreter, import all research modules and verify
        # no forbidden modules are loaded.
        code = """
import sys
import json
from predict_agent.research.config import parse_research
from predict_agent.research.prompt import render_user_prompt
from predict_agent.research.schema import parse_output
from predict_agent.research.exposure import scan
from predict_agent.research.sdk import ResearchOutcome, ResearchRequest
loaded = sorted(name for name in sys.modules if name.startswith("predict_agent"))
print(json.dumps(loaded))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            capture_output=True,
            text=True,
            check=True,
        )
        loaded = json.loads(result.stdout)
        allowed = {
            "predict_agent",
            "predict_agent.config",
            "predict_agent.util",
            "predict_agent.research",
        }
        for module in loaded:
            with self.subTest(module=module):
                self.assertTrue(
                    module in allowed or module.startswith("predict_agent.research."),
                    f"Module {module} should not be loaded by research",
                )

    def test_research_import_helper_catches_every_form(self) -> None:
        # Test that research_internal_imports catches violations and allows correct imports.
        violations = [
            "from predict_agent import paper",
            "from ..policy import decide",
            "from predict_agent.policy import decide",
            "import predict_agent.paper",
            "from .. import cash",
            "from ..cli import main",
            "importlib.import_module('predict_agent.paper')",
        ]
        for source in violations:
            with self.subTest(source=source):
                found = research_internal_imports(source)
                self.assertTrue(
                    any(m not in RESEARCH_MAY_IMPORT for m in found),
                    f"Should have caught a forbidden import in: {source}",
                )
        allowed = [
            "from ..config import ConfigError",
            "from ..util import isoformat",
        ]
        for source in allowed:
            with self.subTest(source=source):
                found = research_internal_imports(source)
                self.assertTrue(
                    all(m in RESEARCH_MAY_IMPORT for m in found),
                    f"Should only find allowed imports in: {source}",
                )

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
