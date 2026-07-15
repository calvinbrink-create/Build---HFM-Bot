from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModularBoundaryTests(unittest.TestCase):
    def test_entrypoint_is_small_and_has_main_guard(self):
        source = (ROOT / "run_mt5_bot.py").read_text()
        self.assertNotIn("runpy", source)
        self.assertIn('if __name__ == "__main__":', source)
        self.assertLess(len(source.splitlines()), 80)

    def test_runtime_import_does_not_load_live_bot(self):
        code = "import sys; import trading.runtime; assert 'mt5_bot' not in sys.modules"
        completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=False)
        self.assertEqual(completed.returncode, 0)

    def test_strategy_facade_preserves_objects(self):
        import strategy_architecture_v1 as legacy
        from strategies import architecture

        self.assertIs(architecture.evaluate, legacy.evaluate)
        self.assertIs(architecture.Candle, legacy.Candle)
        self.assertIs(architecture.Frame, legacy.Frame)

    def test_paths_use_live_environment_contract(self):
        from config.paths import RuntimePaths

        paths = RuntimePaths.from_env()
        self.assertTrue(str(paths.state_file).endswith("mt5_runtime_state.json"))
        self.assertTrue(str(paths.database_file).endswith("mt5_state.db"))
        self.assertIn("cipherfx", str(paths.bridge_dir))

    def test_backend_source_has_no_order_submission_calls(self):
        source = (ROOT / "dashboard" / "mt5_backend" / "main.py").read_text()
        for token in ("gateway.place_market_order", "gateway.place_pending_order", "gateway.close_position", "gateway.modify_position", "gateway.cancel_order"):
            self.assertNotIn(token, source)

    def test_strategy_entry_has_one_runtime_submission_boundary(self):
        source = (ROOT / "mt5_bot.py").read_text()
        self.assertEqual(source.count("self.gateway.place_market_order("), 1)
        self.assertEqual(source.count("self.execution_service.submit("), 1)
        self.assertIn("def _submit_strategy_v1_order", source)

    def test_execution_service_is_broker_agnostic(self):
        source = (ROOT / "trading" / "execution_service.py").read_text()
        self.assertNotIn("MetaTrader5", source)
        self.assertNotIn("mt5.order_send", source)

    def test_dashboard_mutations_are_explicitly_read_only(self):
        source = (ROOT / "dashboard" / "mt5_backend" / "main.py").read_text()
        self.assertGreaterEqual(source.count("read-only backend: trading runtime owns broker execution"), 5)


if __name__ == "__main__":
    unittest.main()
