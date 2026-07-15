from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from config.paths import RuntimePaths
from mt5_bot import XM_MT5_Bot, _completed_candle_identity
from mt5_shared_utils import in_trade_window
from mt5_xm_config import MT5RuntimeConfig
from mt5_xm_gateway import MT5Gateway
from trading.execution_service import ExecutionRequest, ExecutionService


class Phase1PlatformAcceptance(unittest.TestCase):
    def _gateway_fixture(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        symbols = root / "symbols.csv"
        symbols.write_text(
            "symbol,visible,description,path,digits,point,volume_min,volume_max,volume_step,contract_size\n"
            "EURUSD,1,Euro,Forex,5,0.00001,0.01,100,0.01,100000\n"
            "FRA40Cash,1,France,Indices,1,0.1,0.1,100,0.1,1\n"
        )
        (root / "account.txt").write_text(
            "login=1\nterminal_connected=1\n"
            "broker_utc_offset_seconds=3600\n"
        )
        now = int(datetime.now(timezone.utc).timestamp())
        (root / "tick_EURUSD.txt").write_text(
            f"bid=1.10000\nask=1.10010\ntime_utc={now}\ntime_broker={now + 3600}\n"
        )
        rows = []
        for i in range(8):
            stamp = now - (7 - i) * 3600
            rows.append(f"{stamp},1.1,1.2,1.0,1.15,10,1,0.0001")
        (root / "rates_EURUSD_H1.csv").write_text(
            "time,open,high,low,close,volume,spread_points,spread_price\n"
            + "\n".join(rows)
            + "\n"
        )
        cfg = MT5RuntimeConfig()
        cfg.bridge_dir = root
        cfg.symbol_aliases = {"FRA40": "FRA40Cash"}
        cfg.forex_symbols = ["EURUSD"]
        cfg.indices_symbols = ["FRA40"]
        cfg.metals_symbols = []
        cfg.enabled_markets = "forex,indices"
        gateway = MT5Gateway(cfg)
        gateway.mode = "bridge"
        self.addCleanup(temp.cleanup)
        return root, cfg, gateway

    def test_P01_bridge_connection_contract(self):
        root, _cfg, gateway = self._gateway_fixture()
        self.assertEqual(gateway.account_info().login, 1)
        self.assertTrue(gateway.account_info().terminal_connected)

    def test_P02_tick_ingestion_contract(self):
        _root, _cfg, gateway = self._gateway_fixture()
        resolved, tick = gateway.symbol_tick("EURUSD")
        self.assertEqual(resolved, "EURUSD")
        self.assertGreater(tick.bid, 0)
        self.assertGreater(tick.ask, tick.bid)
        self.assertGreater(tick.time_utc, 0)

    def test_P03_candle_construction_contract(self):
        _root, _cfg, gateway = self._gateway_fixture()
        frame = gateway.rates("EURUSD", "H1", 8)
        self.assertEqual(list(frame.columns)[:5], ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(len(frame), 8)
        self.assertEqual(float(frame.iloc[-1]["Close"]), 1.15)

    def test_P04_timestamp_validation_contract(self):
        _root, _cfg, gateway = self._gateway_fixture()
        frame = gateway.rates("EURUSD", "H1", 8)
        times = list(frame.index)
        self.assertTrue(all(left < right for left, right in zip(times, times[1:])))
        raw = pd.Series([int(item.timestamp()) for item in times])
        normalized, metadata = gateway._normalized_bridge_times(
            raw, _root / "rates_EURUSD_H1.csv", "H1"
        )
        self.assertEqual(metadata["broker_time_offset_seconds"], 3600)
        self.assertTrue(metadata["timestamp_normalized"])
        self.assertEqual(len(normalized), len(raw))

    def test_P05_freshness_contract(self):
        now = datetime.utcnow().replace(second=0, microsecond=0)
        periods = {"H4": 14400, "H1": 3600, "M15": 900, "M5": 300, "M1": 60}
        counts = {"H4": 61, "H1": 81, "M15": 61, "M5": 41, "M1": 4}
        frames = {}
        for label, period in periods.items():
            current = int(now.timestamp()) // period * period
            index = [datetime.utcfromtimestamp(current - (counts[label] - 1 - i) * period) for i in range(counts[label])]
            frames[label] = pd.DataFrame(
                {"Open": [1.0] * counts[label], "High": [1.1] * counts[label],
                 "Low": [0.9] * counts[label], "Close": [1.05] * counts[label],
                 "Volume": [1] * counts[label]},
                index=pd.DatetimeIndex(index),
            )
        bot = object.__new__(XM_MT5_Bot)
        bot.gateway = SimpleNamespace(
            mode="bridge",
            symbol_tick=lambda _symbol: ("EURUSD", SimpleNamespace(
                time_utc=int(datetime.now(timezone.utc).timestamp()),
                time=int(datetime.now(timezone.utc).timestamp()),
                time_broker=int(datetime.now(timezone.utc).timestamp()),
                broker_utc_offset_seconds=0,
                receipt_time_utc=datetime.now(timezone.utc),
            )),
        )
        bot.intelligence = None
        ok, state, _reason, evidence = bot._replacement_data_freshness("EURUSD", frames)
        self.assertTrue(ok, (state, evidence))
        self.assertEqual(state, "FRESH")

    def test_P06_historical_loading_contract(self):
        _root, _cfg, gateway = self._gateway_fixture()
        self.assertGreaterEqual(len(gateway.rates("EURUSD", "H1", 8)), 8)

    def test_P07_session_scheduler_contract(self):
        weekday = datetime(2026, 7, 15, 12, 0)
        self.assertTrue(in_trade_window("forex", weekday))
        self.assertFalse(in_trade_window("index_cfd", weekday.replace(hour=3)))

    def test_P08_symbol_routing_contract(self):
        _root, _cfg, gateway = self._gateway_fixture()
        self.assertEqual(gateway.ensure_symbol("FRA40"), "FRA40Cash")

    def test_P09_broker_alias_validation_contract(self):
        _root, _cfg, gateway = self._gateway_fixture()
        result = gateway.validate_symbol_routing(["EURUSD", "FRA40"])
        self.assertTrue(all(row["ok"] for row in result.values()), result)

    def test_P10_persistence_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            bot = object.__new__(XM_MT5_Bot)
            bot.state_file = path
            bot.state = {"pending_setups": {"one": {"state": "WAITING"}}, "version": 1}
            import threading
            bot._state_io_lock = threading.RLock()
            bot._save_state()
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertFalse((path.with_suffix(".json.tmp")).exists())

    def test_P11_execution_transport_contract(self):
        seen = []
        service = ExecutionService(lambda request: seen.append(request) or "accepted")
        request = ExecutionRequest("EURUSD", "BUY", 0.1, 1.09, 1.12)
        self.assertEqual(service.submit(request), "accepted")
        self.assertEqual(seen[0], request)

    def test_P12_logging_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            bot = object.__new__(XM_MT5_Bot)
            bot.state_file = Path(temp) / "state.json"
            bot._audit_io_lock = __import__("threading").RLock()
            bot._audit_decision({"event": "PLATFORM_ACCEPTANCE", "decision": "PASS"})
            row = json.loads(bot._decision_audit_path().read_text().splitlines()[-1])
            self.assertEqual(row["event"], "PLATFORM_ACCEPTANCE")

    def test_P13_recovery_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            payload = {"pending_setups": {"setup-1": {"state": "WAITING_M1"}}, "closed_tickets": [7]}
            path.write_text(json.dumps(payload))
            bot = object.__new__(XM_MT5_Bot)
            bot.state_file = path
            self.assertEqual(bot._load_state(), payload)

    def test_P14_dashboard_event_source_contract(self):
        source = Path("/opt/cipherfx_mt5/dashboard/mt5_backend/main.py").read_text()
        state_store = Path("/opt/cipherfx_mt5/dashboard/backend/state_store.py").read_text()
        self.assertIn("read_signal_scanner", source + state_store)
        self.assertIn("read_status", source + state_store)
        self.assertIn("def place_market_order", source)
        self.assertIn("read-only backend: trading runtime owns broker execution", source)

    def test_P15_watchdog_contract(self):
        script = Path("/opt/cipherfx_mt5/ops/start_mt5_stack.sh")
        subprocess.run(["bash", "-n", str(script)], check=True)
        text = script.read_text()
        self.assertIn("mt5_heartbeat.json", text)
        self.assertIn("watchdog_failure_limit", text)
        self.assertIn("health_failures", text)
        bot_text = Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
        self.assertIn("def _write_heartbeat", bot_text)
        self.assertIn('self._write_heartbeat("RUNNING")', bot_text)
        self.assertIn("DEFERRED_LIVE_HOT_PATH", bot_text)
        self.assertNotIn("shadow_runtime = self.upgrade_suite.runtime_audit()", bot_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
