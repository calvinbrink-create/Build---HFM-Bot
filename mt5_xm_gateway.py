from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from mt5_xm_config import MT5RuntimeConfig


@dataclass
class MT5PositionView:
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    time: datetime


@dataclass
class MT5OrderView:
    ticket: int
    symbol: str
    order_type: str
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    state: str
    time_setup: datetime


@dataclass
class MT5SymbolSpec:
    symbol: str
    visible: bool = False
    description: str = ""
    path: str = ""
    digits: int = 5
    point: float = 0.00001
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    contract_size: float = 100000.0
    tick_value: float = 0.0
    tick_value_profit: float = 0.0
    tick_value_loss: float = 0.0
    tick_size: float = 0.00001
    stops_level: int = 0
    trade_mode: int = 0
    filling_mode: int = 0
    currency_profit: str = "USD"


class _BridgeResult(SimpleNamespace):
    pass


class MT5Gateway:
    def __init__(self, config: MT5RuntimeConfig):
        self.config = config
        self.mt5 = None
        self.mode = "bridge"
        self._last_account_payload: dict[str, str] = {}
        self._broker_offset_cache = 0

    def connect(self) -> None:
        if self.config.execution_mode in {"native", "auto"}:
            try:
                import MetaTrader5 as mt5  # type: ignore
            except Exception:
                mt5 = None
            if mt5 is not None:
                self._connect_native(mt5)
                self.mode = "native"
                return
        self._connect_bridge()
        self.mode = "bridge"

    def _connect_native(self, mt5) -> None:
        kwargs = {}
        if self.config.terminal_path:
            kwargs["path"] = self.config.terminal_path
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if self.config.login:
            ok = mt5.login(self.config.login, password=self.config.password, server=self.config.server)
            if not ok:
                raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        self.mt5 = mt5

    def _connect_bridge(self) -> None:
        self.config.bridge_dir.mkdir(parents=True, exist_ok=True)
        self.config.commands_dir.mkdir(parents=True, exist_ok=True)
        self.config.results_dir.mkdir(parents=True, exist_ok=True)
        self._write_bridge_symbol_universe()
        deadline = time.time() + 90
        while time.time() < deadline:
            if (self.config.bridge_dir / "account.txt").exists():
                return
            time.sleep(2)
        raise RuntimeError(f"MT5 bridge did not become ready in {self.config.bridge_dir}")

    def _write_bridge_symbol_universe(self) -> None:
        symbols = [self.config.resolved_symbol(sym) for sym in self.config.all_symbols()]
        if not symbols:
            return
        text = "\n".join(symbols) + "\n"
        (self.config.bridge_dir / "symbols.txt").write_text(text)
        try:
            manifest = json.dumps({"generated_at": datetime.utcnow().isoformat() + "Z", "symbols": symbols}, indent=2)
            paths = [self.config.bridge_dir / "visible_symbols.json", Path(__file__).resolve().parent / "mt5_bridge" / "visible_symbols.json"]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(manifest)
        except Exception:
            pass

    def broker_utc_offset_seconds(self) -> int:
        if self.mode == "native":
            return 0
        payload = self._read_key_values(self.config.bridge_dir / "account.txt")
        raw = payload.get("broker_utc_offset_seconds")
        try:
            value = int(float(raw)) if raw not in (None, "") else 0
        except Exception:
            value = 0
        if abs(value) <= 14 * 3600 and value != 0:
            self._broker_offset_cache = value
        return int(self._broker_offset_cache)

    def _bridge_utc_epoch(self, payload, utc_key: str, broker_key: str, legacy_key: str = "") -> int:
        try:
            value = int(float(payload.get(utc_key, 0) or 0))
            if value > 0:
                return value
        except Exception:
            pass
        raw_key = broker_key if payload.get(broker_key) not in (None, "") else legacy_key
        try:
            broker_epoch = int(float(payload.get(raw_key, 0) or 0))
        except Exception:
            broker_epoch = 0
        if broker_epoch <= 0:
            return 0
        offset = self.broker_utc_offset_seconds()
        if not offset and broker_epoch - int(time.time()) > 120:
            inferred_hours = int(round((broker_epoch - time.time()) / 3600.0))
            if 0 < abs(inferred_hours) <= 14:
                offset = inferred_hours * 3600
                self._broker_offset_cache = offset
        return broker_epoch - offset

    def shutdown(self) -> None:
        if self.mt5 is not None:
            try:
                self.mt5.shutdown()
            except Exception:
                pass

    def ensure_symbol(self, symbol: str) -> str:
        if self.mode == "native":
            assert self.mt5 is not None
            resolved = self.config.resolved_symbol(symbol)
            info = self.mt5.symbol_info(resolved)
            if info is None:
                raise RuntimeError(f"MT5 symbol not found: {resolved}")
            if not info.visible:
                if not self.mt5.symbol_select(resolved, True):
                    raise RuntimeError(f"MT5 symbol_select failed for {resolved}: {self.mt5.last_error()}")
            return resolved
        return self.config.resolved_symbol(symbol)

    def account_info(self):
        if self.mode == "native":
            assert self.mt5 is not None
            return self.mt5.account_info()
        payload = self._read_key_values(self.config.bridge_dir / "account.txt")
        if (not payload or int(payload.get("login", "0") or 0) <= 0) and self._last_account_payload:
            payload = dict(self._last_account_payload)
        elif int(payload.get("login", "0") or 0) > 0:
            self._last_account_payload = dict(payload)
        login = int(payload.get("login", "0") or 0)
        balance = float(payload.get("balance", "0") or 0.0)
        equity = float(payload.get("equity", "0") or 0.0)
        leverage = int(payload.get("leverage", "0") or 0)
        if login == int(self.config.login or 0) and balance <= 0 and equity <= 0 and leverage <= 0 and self.config.demo_balance > 0:
            balance = self.config.demo_balance
            equity = self.config.demo_balance
        return SimpleNamespace(
            login=login,
            equity=equity,
            balance=balance,
            profit=float(payload.get("profit", "0") or 0.0),
            leverage=leverage,
            server=payload.get("server", ""),
            name=payload.get("name", ""),
            currency=payload.get("currency", ""),
            margin=float(payload.get("margin", "0") or 0.0),
            free_margin=float(payload.get("free_margin", "0") or 0.0),
            terminal_connected=payload.get("terminal_connected", "1") in {"1", "true", "True"},
            trade_allowed=payload.get("trade_allowed", "1") in {"1", "true", "True"},
            account_trade_allowed=payload.get("account_trade_allowed", "1") in {"1", "true", "True"},
            broker_time_epoch=int(float(payload.get("broker_time_epoch", "0") or 0)),
            utc_time_epoch=int(float(payload.get("utc_time_epoch", "0") or 0)),
            broker_utc_offset_seconds=self.broker_utc_offset_seconds(),
            receipt_time_utc=datetime.now(timezone.utc),
        )

    def rates(self, symbol: str, timeframe, count: int) -> pd.DataFrame:
        label = self._timeframe_label(timeframe)
        if self.mode == "native":
            assert self.mt5 is not None
            resolved = self.ensure_symbol(symbol)
            rows = self.mt5.copy_rates_from_pos(resolved, self._native_timeframe(label), 0, count)
            if rows is None or len(rows) == 0:
                raise RuntimeError(f"No MT5 rates returned for {resolved}")
            df = pd.DataFrame(rows)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "tick_volume": "Volume",
                    "spread": "SpreadPoints",
                }
            )
            cols = ["Open", "High", "Low", "Close", "Volume"]
            if "SpreadPoints" in df.columns:
                df["SpreadPoints"] = pd.to_numeric(df["SpreadPoints"], errors="coerce").fillna(0.0)
                cols.append("SpreadPoints")
            return df.set_index("time")[cols]
        resolved = self.ensure_symbol(symbol)
        path = self.config.bridge_dir / f"rates_{resolved}_{label}.csv"
        if not path.exists():
            raise RuntimeError(f"Missing bridge rates file for {resolved}")
        df = self._read_bridge_csv(path)
        if df.empty:
            raise RuntimeError(f"Bridge rates file is empty for {resolved}")
        raw_time = pd.to_numeric(df["time"], errors="coerce")
        time_index, bridge_meta = self._normalized_bridge_times(raw_time, path, label)
        df["time"] = time_index
        df = df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
                "spread": "SpreadPoints",
                "spread_points": "SpreadPoints",
                "spread_price": "Spread",
            }
        )
        cols = ["Open", "High", "Low", "Close", "Volume"]
        for optional in ("SpreadPoints", "Spread"):
            if optional in df.columns:
                df[optional] = pd.to_numeric(df[optional], errors="coerce").fillna(0.0)
                cols.append(optional)
        result = df.set_index("time")[cols].tail(count)
        result.attrs["bridge_rates"] = bridge_meta
        return result

    def symbol_tick(self, symbol: str):
        if self.mode == "native":
            assert self.mt5 is not None
            resolved = self.ensure_symbol(symbol)
            tick = self.mt5.symbol_info_tick(resolved)
            if tick is None:
                raise RuntimeError(f"No MT5 tick for {resolved}")
            return resolved, tick
        resolved = self.ensure_symbol(symbol)
        payload = self._read_key_values(self.config.bridge_dir / f"tick_{resolved}.txt")
        time_utc = self._bridge_utc_epoch(payload, "time_utc", "time_broker", "time")
        return resolved, SimpleNamespace(
            bid=float(payload.get("bid", "0") or 0.0),
            ask=float(payload.get("ask", "0") or 0.0),
            last=float(payload.get("last", "0") or 0.0),
            time=time_utc,
            time_utc=time_utc,
            time_broker=int(float(payload.get("time_broker", payload.get("time", "0")) or 0)),
            broker_utc_offset_seconds=self.broker_utc_offset_seconds(),
            receipt_time_utc=datetime.now(timezone.utc),
        )

    def order_calc_profit(self, symbol: str, direction: str, volume: float, open_price: float, close_price: float) -> float:
        resolved = self.ensure_symbol(symbol)
        side = str(direction or "").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid direction for OrderCalcProfit: {direction}")
        if self.mode == "native":
            assert self.mt5 is not None
            order_type = self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL
            value = self.mt5.order_calc_profit(order_type, resolved, float(volume), float(open_price), float(close_price))
            if value is None:
                raise RuntimeError(f"MT5 order_calc_profit failed: {self.mt5.last_error()}")
            return float(value)
        request_id = f"{time.time_ns()}_calc_profit_{resolved}"
        self._write_command(request_id, {
            "action": "ORDER_CALC_PROFIT",
            "symbol": resolved,
            "direction": side,
            "volume": f"{float(volume):.8f}",
            "open_price": f"{float(open_price):.10f}",
            "close_price": f"{float(close_price):.10f}",
        })
        return float(self._await_result(request_id).value)

    def order_calc_margin(self, symbol: str, direction: str, volume: float, open_price: float) -> float:
        resolved = self.ensure_symbol(symbol)
        side = str(direction or "").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid direction for OrderCalcMargin: {direction}")
        if self.mode == "native":
            assert self.mt5 is not None
            order_type = self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL
            value = self.mt5.order_calc_margin(order_type, resolved, float(volume), float(open_price))
            if value is None:
                raise RuntimeError(f"MT5 order_calc_margin failed: {self.mt5.last_error()}")
            return float(value)
        request_id = f"{time.time_ns()}_calc_margin_{resolved}"
        self._write_command(request_id, {
            "action": "ORDER_CALC_MARGIN",
            "symbol": resolved,
            "direction": side,
            "volume": f"{float(volume):.8f}",
            "open_price": f"{float(open_price):.10f}",
        })
        return float(self._await_result(request_id).value)

    def order_calc_trade_geometry(
        self,
        symbol: str,
        direction: str,
        volume: float,
        entry: float,
        sl: float,
        tp: float,
    ) -> dict[str, float | str]:
        resolved = self.ensure_symbol(symbol)
        side = str(direction or "").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid direction for broker geometry: {direction}")
        if min(float(volume), float(entry), float(sl), float(tp)) <= 0:
            raise ValueError("broker geometry requires positive volume, entry, SL, and TP")
        if self.mode == "native":
            sl_value = self.order_calc_profit(resolved, side, volume, entry, sl)
            tp_value = self.order_calc_profit(resolved, side, volume, entry, tp)
            margin_value = self.order_calc_margin(resolved, side, volume, entry)
            return {
                "sl_loss_usd": abs(float(sl_value)),
                "tp_profit_usd": float(tp_value),
                "margin_usd": float(margin_value),
                "currency": "",
            }

        batch_id = time.time_ns()
        requests = {
            "sl": f"{batch_id}_geometry_sl_{resolved}",
            "tp": f"{batch_id}_geometry_tp_{resolved}",
            "margin": f"{batch_id}_geometry_margin_{resolved}",
        }
        common = {
            "symbol": resolved,
            "direction": side,
            "volume": f"{float(volume):.8f}",
            "open_price": f"{float(entry):.10f}",
        }
        self._write_command(requests["sl"], {**common, "action": "ORDER_CALC_PROFIT", "close_price": f"{float(sl):.10f}"})
        self._write_command(requests["tp"], {**common, "action": "ORDER_CALC_PROFIT", "close_price": f"{float(tp):.10f}"})
        self._write_command(requests["margin"], {**common, "action": "ORDER_CALC_MARGIN"})
        sl_result = self._await_result(requests["sl"])
        tp_result = self._await_result(requests["tp"])
        margin_result = self._await_result(requests["margin"])
        return {
            "sl_loss_usd": abs(float(sl_result.value)),
            "tp_profit_usd": float(tp_result.value),
            "margin_usd": float(margin_result.value),
            "currency": sl_result.currency or tp_result.currency or margin_result.currency,
        }

    def positions(self) -> list[MT5PositionView]:
        if self.mode == "native":
            assert self.mt5 is not None
            positions = self.mt5.positions_get() or []
            return [
                MT5PositionView(
                    ticket=int(p.ticket),
                    symbol=str(p.symbol),
                    direction="BUY" if int(p.type) == self.mt5.POSITION_TYPE_BUY else "SELL",
                    volume=float(p.volume),
                    price_open=float(p.price_open),
                    price_current=float(p.price_current),
                    sl=float(p.sl or 0.0),
                    tp=float(p.tp or 0.0),
                    profit=float(p.profit or 0.0),
                    time=datetime.fromtimestamp(int(p.time), tz=timezone.utc),
                )
                for p in positions
            ]
        path = self.config.bridge_dir / "positions.csv"
        if not path.exists():
            return []
        df = self._read_bridge_csv(path)
        if df.empty:
            return []
        positions: list[MT5PositionView] = []
        for _, row in df.iterrows():
            positions.append(
                MT5PositionView(
                    ticket=int(row.get("ticket", 0) or 0),
                    symbol=str(row.get("symbol", "")),
                    direction=str(row.get("direction", "BUY")).upper(),
                    volume=float(row.get("volume", 0.0) or 0.0),
                    price_open=float(row.get("price_open", 0.0) or 0.0),
                    price_current=float(row.get("price_current", 0.0) or 0.0),
                    sl=float(row.get("sl", 0.0) or 0.0),
                    tp=float(row.get("tp", 0.0) or 0.0),
                    profit=float(row.get("profit", 0.0) or 0.0),
                    time=datetime.fromtimestamp(self._bridge_utc_epoch(row, "time_utc", "time_broker", "time"), tz=timezone.utc),
                )
            )
        return positions

    def orders(self) -> list[MT5OrderView]:
        if self.mode == "native":
            assert self.mt5 is not None
            orders = self.mt5.orders_get() or []
            views: list[MT5OrderView] = []
            for order in orders:
                order_type = self._native_order_type_name(int(order.type))
                side = "BUY" if "BUY" in order_type else "SELL"
                views.append(
                    MT5OrderView(
                        ticket=int(order.ticket),
                        symbol=str(order.symbol),
                        order_type=order_type,
                        side=side,
                        volume=float(order.volume_current or order.volume_initial or 0.0),
                        price_open=float(order.price_open or 0.0),
                        sl=float(order.sl or 0.0),
                        tp=float(order.tp or 0.0),
                        state=self._native_order_state_name(int(order.state)),
                        time_setup=datetime.fromtimestamp(int(order.time_setup), tz=timezone.utc),
                    )
                )
            return views
        path = self.config.bridge_dir / "orders.csv"
        if not path.exists():
            return []
        df = self._read_bridge_csv(path)
        if df.empty:
            return []
        orders: list[MT5OrderView] = []
        for _, row in df.iterrows():
            orders.append(
                MT5OrderView(
                    ticket=int(row.get("ticket", 0) or 0),
                    symbol=str(row.get("symbol", "")),
                    order_type=str(row.get("order_type", "")),
                    side=str(row.get("side", "")),
                    volume=float(row.get("volume", 0.0) or 0.0),
                    price_open=float(row.get("price_open", 0.0) or 0.0),
                    sl=float(row.get("sl", 0.0) or 0.0),
                    tp=float(row.get("tp", 0.0) or 0.0),
                    state=str(row.get("state", "")),
                    time_setup=datetime.fromtimestamp(self._bridge_utc_epoch(row, "time_setup_utc", "time_setup_broker", "time_setup"), tz=timezone.utc),
                )
            )
        return orders

    def place_market_order(self, symbol: str, direction: str, volume: float, sl: float, tp: float, comment: str):
        spec = self.symbol_info(symbol)
        normalized_volume = self.normalize_volume(spec, volume)
        if normalized_volume <= 0:
            raise RuntimeError(
                f"Volume {volume} is below broker minimum/step for {spec.symbol} "
                f"(min={spec.volume_min}, step={spec.volume_step})"
            )
        sl = self.normalize_price(spec, sl)
        tp = self.normalize_price(spec, tp)
        if self.mode == "native":
            assert self.mt5 is not None
            resolved, tick = self.symbol_tick(symbol)
            order_type = self.mt5.ORDER_TYPE_BUY if direction == "BUY" else self.mt5.ORDER_TYPE_SELL
            price = float(tick.ask if direction == "BUY" else tick.bid)
            request = {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": resolved,
                "volume": float(normalized_volume),
                "type": order_type,
                "price": price,
                "sl": float(sl),
                "tp": float(tp),
                "deviation": int(self.config.slippage_points),
                "magic": int(self.config.magic),
                "comment": comment[:31],
                "type_time": self.mt5.ORDER_TIME_GTC,
            }
            result = self._send_native_order(request)
            if result is None:
                raise RuntimeError(f"MT5 order_send returned None: {self.mt5.last_error()}")
            return resolved, price, result
        resolved, tick = self.symbol_tick(symbol)
        request_id = f"{int(time.time() * 1000)}_{resolved}"
        self._write_command(
            request_id,
            {
                "action": "OPEN",
                "symbol": resolved,
                "direction": direction.upper(),
                "volume": f"{float(normalized_volume):.8f}",
                "sl": f"{float(sl):.8f}",
                "tp": f"{float(tp):.8f}",
                "comment": comment[:31],
                "magic": str(int(self.config.magic)),
            },
        )
        result = self._await_result(request_id)
        return resolved, float(result.price or (tick.ask if direction == "BUY" else tick.bid)), result

    def place_pending_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        price: float,
        sl: float,
        tp: float,
        pending_type: str,
        comment: str,
    ):
        spec = self.symbol_info(symbol)
        normalized_volume = self.normalize_volume(spec, volume)
        if normalized_volume <= 0:
            raise RuntimeError(
                f"Volume {volume} is below broker minimum/step for {spec.symbol} "
                f"(min={spec.volume_min}, step={spec.volume_step})"
            )
        price = self.normalize_price(spec, price)
        sl = self.normalize_price(spec, sl)
        tp = self.normalize_price(spec, tp)
        if self.mode == "native":
            assert self.mt5 is not None
            resolved = self.ensure_symbol(symbol)
            request = {
                "action": self.mt5.TRADE_ACTION_PENDING,
                "symbol": resolved,
                "volume": float(normalized_volume),
                "type": self._native_pending_order_type(direction, pending_type),
                "price": float(price),
                "sl": float(sl),
                "tp": float(tp),
                "deviation": int(self.config.slippage_points),
                "magic": int(self.config.magic),
                "comment": comment[:31],
                "type_time": self.mt5.ORDER_TIME_GTC,
            }
            request["type_filling"] = self.mt5.ORDER_FILLING_RETURN
            result = self.mt5.order_send(request)
            if result is None:
                raise RuntimeError(f"MT5 pending order_send returned None: {self.mt5.last_error()}")
            return resolved, result
        resolved = self.ensure_symbol(symbol)
        request_id = f"{int(time.time() * 1000)}_pending_{resolved}"
        self._write_command(
            request_id,
            {
                "action": "PENDING",
                "symbol": resolved,
                "direction": direction.upper(),
                "pending_type": pending_type.upper(),
                "volume": f"{float(normalized_volume):.8f}",
                "price": f"{float(price):.8f}",
                "sl": f"{float(sl):.8f}",
                "tp": f"{float(tp):.8f}",
                "comment": comment[:31],
                "magic": str(int(self.config.magic)),
            },
        )
        return resolved, self._await_result(request_id)

    def close_position(self, position: MT5PositionView):
        if self.mode == "native":
            assert self.mt5 is not None
            resolved, tick = self.symbol_tick(position.symbol)
            direction = "SELL" if position.direction == "BUY" else "BUY"
            order_type = self.mt5.ORDER_TYPE_SELL if direction == "SELL" else self.mt5.ORDER_TYPE_BUY
            price = float(tick.bid if direction == "SELL" else tick.ask)
            request = {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "position": int(position.ticket),
                "symbol": resolved,
                "volume": float(position.volume),
                "type": order_type,
                "price": price,
                "deviation": int(self.config.slippage_points),
                "magic": int(self.config.magic),
                "comment": "cipherfx-close",
                "type_time": self.mt5.ORDER_TIME_GTC,
            }
            result = self._send_native_order(request)
            if result is None:
                raise RuntimeError(f"MT5 close order_send returned None: {self.mt5.last_error()}")
            return result
        request_id = f"{int(time.time() * 1000)}_close_{position.ticket}"
        self._write_command(
            request_id,
            {
                "action": "CLOSE",
                "ticket": str(int(position.ticket)),
                "symbol": position.symbol,
                "volume": f"{float(position.volume):.8f}",
                "magic": str(int(self.config.magic)),
            },
        )
        return self._await_result(request_id)

    def modify_position(self, ticket: int, symbol: str, sl: float, tp: float):
        if self.mode == "native":
            assert self.mt5 is not None
            resolved = self.ensure_symbol(symbol)
            request = {
                "action": self.mt5.TRADE_ACTION_SLTP,
                "position": int(ticket),
                "symbol": resolved,
                "sl": float(sl),
                "tp": float(tp),
                "magic": int(self.config.magic),
            }
            result = self.mt5.order_send(request)
            if result is None:
                raise RuntimeError(f"MT5 modify position returned None: {self.mt5.last_error()}")
            return result
        request_id = f"{int(time.time() * 1000)}_modify_{ticket}"
        self._write_command(
            request_id,
            {
                "action": "MODIFY_POSITION",
                "ticket": str(int(ticket)),
                "symbol": symbol,
                "sl": f"{float(sl):.8f}",
                "tp": f"{float(tp):.8f}",
                "magic": str(int(self.config.magic)),
            },
        )
        return self._await_result(request_id)

    def cancel_order(self, ticket: int):
        if self.mode == "native":
            assert self.mt5 is not None
            request = {
                "action": self.mt5.TRADE_ACTION_REMOVE,
                "order": int(ticket),
                "magic": int(self.config.magic),
            }
            result = self.mt5.order_send(request)
            if result is None:
                raise RuntimeError(f"MT5 cancel order returned None: {self.mt5.last_error()}")
            return result
        request_id = f"{int(time.time() * 1000)}_cancel_{ticket}"
        self._write_command(
            request_id,
            {
                "action": "CANCEL_ORDER",
                "ticket": str(int(ticket)),
                "magic": str(int(self.config.magic)),
            },
        )
        return self._await_result(request_id)

    def set_symbol_visibility(self, symbol: str, enabled: bool):
        if self.mode == "native":
            assert self.mt5 is not None
            resolved = self.config.resolved_symbol(symbol)
            if not self.mt5.symbol_select(resolved, bool(enabled)):
                raise RuntimeError(f"MT5 symbol_select failed for {resolved}: {self.mt5.last_error()}")
            return resolved
        resolved = self.config.resolved_symbol(symbol)
        request_id = f"{int(time.time() * 1000)}_symbol_{resolved}"
        self._write_command(
            request_id,
            {
                "action": "SYMBOL_ENABLE",
                "symbol": resolved,
                "enabled": "1" if enabled else "0",
            },
        )
        self._await_result(request_id)
        return resolved

    def all_symbols(self) -> list[dict]:
        if self.mode == "native":
            assert self.mt5 is not None
            result = []
            for info in self.mt5.symbols_get() or []:
                result.append(
                    {
                        "symbol": str(info.name),
                        "visible": bool(info.visible),
                        "description": str(getattr(info, "description", "") or ""),
                        "path": str(getattr(info, "path", "") or ""),
                    }
                )
            return result
        path = self.config.bridge_dir / "symbols.csv"
        if not path.exists():
            return []
        df = self._read_bridge_csv(path)
        if df.empty:
            return []
        return [
            {
                "symbol": str(row.get("symbol", "")),
                "visible": str(row.get("visible", "0")).strip() in {"1", "true", "True"},
                "description": str(row.get("description", "")),
                "path": str(row.get("path", "")),
            }
            for _, row in df.iterrows()
            if str(row.get("symbol", "")).strip()
        ]

    def symbol_info(self, symbol: str) -> MT5SymbolSpec:
        resolved = self.ensure_symbol(symbol)
        if self.mode == "native":
            assert self.mt5 is not None
            info = self.mt5.symbol_info(resolved)
            if info is None:
                raise RuntimeError(f"MT5 symbol info not found: {resolved}")
            return MT5SymbolSpec(
                symbol=resolved,
                visible=bool(getattr(info, "visible", False)),
                description=str(getattr(info, "description", "") or ""),
                path=str(getattr(info, "path", "") or ""),
                digits=int(getattr(info, "digits", 5) or 5),
                point=float(getattr(info, "point", 0.00001) or 0.00001),
                volume_min=float(getattr(info, "volume_min", 0.01) or 0.01),
                volume_max=float(getattr(info, "volume_max", 100.0) or 100.0),
                volume_step=float(getattr(info, "volume_step", 0.01) or 0.01),
                contract_size=float(getattr(info, "trade_contract_size", 100000.0) or 100000.0),
                tick_value=float(getattr(info, "trade_tick_value", 0.0) or 0.0),
                tick_value_profit=float(getattr(info, "trade_tick_value_profit", 0.0) or 0.0),
                tick_value_loss=float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0),
                tick_size=float(getattr(info, "trade_tick_size", 0.0) or getattr(info, "point", 0.00001) or 0.00001),
                stops_level=int(getattr(info, "trade_stops_level", 0) or 0),
                trade_mode=int(getattr(info, "trade_mode", 0) or 0),
                filling_mode=int(getattr(info, "filling_mode", 0) or 0),
                currency_profit=str(getattr(info, "currency_profit", "") or ""),
            )
        rows = self._bridge_symbol_map()
        row = rows.get(resolved.upper(), {})
        return MT5SymbolSpec(
            symbol=resolved,
            visible=str(row.get("visible", "0")).strip() in {"1", "true", "True"},
            description=str(row.get("description", "")),
            path=str(row.get("path", "")),
            digits=int(float(row.get("digits", self._default_digits(resolved)) or self._default_digits(resolved))),
            point=float(row.get("point", self._default_point(resolved)) or self._default_point(resolved)),
            volume_min=float(row.get("volume_min", "0.01") or 0.01),
            volume_max=float(row.get("volume_max", "100.0") or 100.0),
            volume_step=float(row.get("volume_step", "0.01") or 0.01),
            contract_size=float(row.get("contract_size", self._default_contract_size(resolved)) or self._default_contract_size(resolved)),
            tick_value=float(row.get("tick_value", row.get("tick_value_loss", "0")) or 0.0),
            tick_value_profit=float(row.get("tick_value_profit", "0") or 0.0),
            tick_value_loss=float(row.get("tick_value_loss", "0") or 0.0),
            tick_size=float(row.get("tick_size", row.get("point", self._default_point(resolved))) or self._default_point(resolved)),
            stops_level=int(float(row.get("stops_level", "0") or 0)),
            trade_mode=int(float(row.get("trade_mode", "0") or 0)),
            filling_mode=int(float(row.get("filling_mode", "0") or 0)),
            currency_profit=str(row.get("currency_profit", "")),
        )

    def _bridge_deals_path_for_read(self) -> Path:
        path = self.config.bridge_dir / "deals.csv"
        tmp_path = Path(str(path) + ".tmp")
        try:
            tmp_newer = tmp_path.exists() and (not path.exists() or tmp_path.stat().st_mtime > path.stat().st_mtime)
        except OSError:
            tmp_newer = False
        if tmp_newer:
            print(
                f"[MT5_BRIDGE_DEALS_WARN] deals.csv.tmp newer than deals.csv; reading tmp fallback and not trusting stale final path={path}",
                flush=True,
            )
            return tmp_path
        return path

    def deals_history_path(self) -> Path:
        return self._bridge_deals_path_for_read()

    def deals_for_position(self, position_ticket: int):
        if self.mode == "native":
            assert self.mt5 is not None
            start = datetime(2024, 1, 1)
            end = datetime.utcnow()
            deals = self.mt5.history_deals_get(start, end) or []
            return [d for d in deals if int(getattr(d, "position_id", 0)) == int(position_ticket)]
        path = self._bridge_deals_path_for_read()
        if not path.exists():
            return []
        try:
            df = self._read_bridge_csv(path)
        except Exception as exc:
            print(f"[MT5_BRIDGE_DEALS_WARN] failed to read {path}: {str(exc)[:180]}", flush=True)
            return []
        if df.empty or "position_id" not in df.columns:
            return []
        position_ids = pd.to_numeric(df["position_id"], errors="coerce")
        rows = df[position_ids == int(position_ticket)]

        def number(row, key: str, default: float = 0.0) -> float:
            try:
                value = row.get(key, default)
                if pd.isna(value):
                    return default
                return float(value)
            except Exception:
                return default

        out = []
        for _, row in rows.iterrows():
            profit = number(row, "profit")
            swap = number(row, "swap")
            commission = number(row, "commission")
            net_profit = number(row, "net_profit", profit + swap + commission)
            out.append(
                SimpleNamespace(
                    position_id=int(number(row, "position_id")),
                    price=number(row, "price"),
                    profit=profit,
                    swap=swap,
                    commission=commission,
                    net_profit=net_profit,
                    time_utc=self._bridge_utc_epoch(row, "time_utc", "time_broker", "time"),
                    time_broker=int(number(row, "time_broker", number(row, "time"))),
                    broker_utc_offset_seconds=int(number(row, "broker_utc_offset_seconds", self.broker_utc_offset_seconds())),
                )
            )
        return out

    def normalize_price(self, spec: MT5SymbolSpec, price: float) -> float:
        if not price:
            return 0.0
        digits = max(0, int(spec.digits or 0))
        return round(float(price), digits)

    def normalize_volume(self, spec: MT5SymbolSpec, volume: float) -> float:
        requested = float(volume or 0.0)
        if requested <= 0:
            return 0.0
        step = float(spec.volume_step or 0.01)
        min_volume = float(spec.volume_min or step)
        max_volume = float(spec.volume_max or requested)
        if step <= 0:
            step = min_volume if min_volume > 0 else 0.01
        if requested < min_volume:
            return 0.0
        steps = math.floor((requested + step * 1e-9) / step)
        normalized = steps * step
        if normalized < min_volume:
            return 0.0
        normalized = min(normalized, max_volume)
        return round(normalized, self._volume_digits(step))

    def _send_native_order(self, request: dict):
        assert self.mt5 is not None
        invalid_fill = 10030
        fillings = [
            getattr(self.mt5, "ORDER_FILLING_IOC", None),
            getattr(self.mt5, "ORDER_FILLING_FOK", None),
            getattr(self.mt5, "ORDER_FILLING_RETURN", None),
        ]
        last_result = None
        seen: set[int] = set()
        for filling in fillings:
            if filling is None or int(filling) in seen:
                continue
            seen.add(int(filling))
            req = dict(request)
            req["type_filling"] = filling
            result = self.mt5.order_send(req)
            if result is None:
                return None
            last_result = result
            if int(getattr(result, "retcode", 0) or 0) != invalid_fill:
                return result
        return last_result

    def _timeframe_label(self, timeframe) -> str:
        text = str(timeframe or "M15").upper()
        aliases = {
            "1": "M1", "M1": "M1",
            "5": "M5", "M5": "M5",
            "15": "M15", "M15": "M15",
            "30": "M30", "M30": "M30",
            "60": "H1", "H1": "H1",
            "240": "H4", "H4": "H4",
            "1440": "D1", "D1": "D1",
            "10080": "W1", "W1": "W1",
            "MN1": "MN", "MN": "MN",
        }
        return aliases.get(text, "M15")

    def _native_timeframe(self, label: str):
        assert self.mt5 is not None
        mapping = {
            "M1": self.mt5.TIMEFRAME_M1,
            "M5": self.mt5.TIMEFRAME_M5,
            "M15": self.mt5.TIMEFRAME_M15,
            "M30": self.mt5.TIMEFRAME_M30,
            "H1": self.mt5.TIMEFRAME_H1,
            "H4": self.mt5.TIMEFRAME_H4,
            "D1": self.mt5.TIMEFRAME_D1,
            "W1": self.mt5.TIMEFRAME_W1,
            "MN": self.mt5.TIMEFRAME_MN1,
        }
        return mapping[label]

    def _bridge_symbol_map(self) -> dict[str, dict[str, str]]:
        path = self.config.bridge_dir / "symbols.csv"
        if not path.exists():
            return {}
        df = self._read_bridge_csv(path)
        if df.empty:
            return {}
        rows: dict[str, dict[str, str]] = {}
        for _, row in df.iterrows():
            sym = str(row.get("symbol", "")).strip().upper()
            if not sym:
                continue
            rows[sym] = {str(k): "" if pd.isna(v) else str(v) for k, v in row.to_dict().items()}
        return rows

    def _volume_digits(self, step: float) -> int:
        text = f"{float(step):.10f}".rstrip("0")
        return len(text.split(".", 1)[1]) if "." in text else 0

    def _default_digits(self, symbol: str) -> int:
        sym = symbol.upper()
        if len(sym) == 6 and sym.isalpha():
            return 3 if "JPY" in sym else 5
        if sym.startswith(("XAU", "XAG", "XPT", "XPD")):
            return 2
        return 2

    def _default_point(self, symbol: str) -> float:
        digits = self._default_digits(symbol)
        return 10 ** (-digits)

    def _default_contract_size(self, symbol: str) -> float:
        sym = symbol.upper()
        if len(sym) == 6 and sym.isalpha():
            return 100000.0
        if sym.startswith("XAU"):
            return 100.0
        if sym.startswith("XAG"):
            return 5000.0
        return 1.0

    def _write_command(self, request_id: str, payload: dict[str, str]) -> None:
        lines = [f"request_id={request_id}"] + [f"{key}={value}" for key, value in payload.items()]
        final_path = self.config.commands_dir / f"command_{request_id}.txt"
        staging_path = self.config.commands_dir / f"pending_{request_id}.part"
        staging_path.write_text("\n".join(lines) + "\n")
        staging_path.replace(final_path)

    def _await_result(self, request_id: str) -> _BridgeResult:
        result_path = self.config.results_dir / f"result_{request_id}.txt"
        deadline = time.time() + 45
        while time.time() < deadline:
            if result_path.exists():
                payload = self._read_key_values(result_path)
                status = payload.get("status", "").upper()
                if not status:
                    time.sleep(0.025)
                    continue
                try:
                    result_path.unlink()
                except OSError:
                    pass
                if status != "OK":
                    raise RuntimeError(payload.get("message", f"MT5 bridge request failed: {request_id}"))
                return _BridgeResult(
                    retcode=int(payload.get("retcode", "10009") or 10009),
                    order=int(payload.get("order", "0") or 0),
                    deal=int(payload.get("deal", "0") or 0),
                    price=float(payload.get("price", "0") or 0.0),
                    value_name=str(payload.get("value_name", "")),
                    value=float(payload.get("value", "0") or 0.0),
                    currency=str(payload.get("currency", "")),
                    message=str(payload.get("message", "")),
                )
            time.sleep(0.05)
        raise RuntimeError(f"Timed out waiting for MT5 bridge result: {request_id}")

    def _bridge_file_meta(self, path: Path) -> dict[str, object]:
        meta: dict[str, object] = {"bridge_file": str(path), "bridge_file_exists": path.exists()}
        try:
            stat = path.stat()
            mtime = datetime.fromtimestamp(float(stat.st_mtime), tz=timezone.utc)
            now = datetime.now(timezone.utc)
            meta.update({
                "bridge_mtime": mtime.isoformat(),
                "bridge_file_age_minutes": (now - mtime).total_seconds() / 60.0,
                "bridge_file_size": int(stat.st_size),
            })
        except OSError as exc:
            meta["bridge_file_error"] = str(exc)[:180]
        return meta

    def _normalized_bridge_times(self, raw_times, path: Path, label: str):
        parsed = pd.to_datetime(pd.to_numeric(raw_times, errors="coerce"), unit="s", utc=True, errors="coerce")
        meta = self._bridge_file_meta(path)
        meta["timeframe"] = label
        valid = pd.Series(parsed).dropna()
        if valid.empty:
            meta.update({
                "last_bar_time_raw": "",
                "last_bar_time_normalized": "",
                "timestamp_normalized": False,
                "broker_time_offset_seconds": 0,
                "vps_time": datetime.now(timezone.utc).isoformat(),
                "age_minutes": None,
                "raw_age_minutes": None,
            })
            return parsed, meta
        now = pd.Timestamp.now(tz="UTC")
        latest_raw = valid.iloc[-1]
        raw_age_minutes = (now - latest_raw).total_seconds() / 60.0
        offset_seconds = self.broker_utc_offset_seconds()
        offset_source = "account_export" if offset_seconds else "none"
        future_seconds = (latest_raw - now).total_seconds()
        if not offset_seconds and future_seconds > 120:
            offset_hours = int(round(future_seconds / 3600.0))
            if offset_hours != 0 and abs(offset_hours) <= 14:
                offset_seconds = offset_hours * 3600
                offset_source = "future_timestamp_inference"
        if offset_seconds:
            parsed = parsed - pd.to_timedelta(offset_seconds, unit="s")
        normalized_valid = pd.Series(parsed).dropna()
        latest_normalized = normalized_valid.iloc[-1] if not normalized_valid.empty else latest_raw
        age_minutes = (now - latest_normalized).total_seconds() / 60.0
        meta.update({
            "last_bar_time_raw": latest_raw.isoformat(),
            "last_bar_time_normalized": latest_normalized.isoformat(),
            "timestamp_normalized": bool(offset_seconds),
            "broker_time_offset_seconds": int(offset_seconds),
            "broker_time_offset_source": offset_source,
            "vps_time": now.isoformat(),
            "receipt_time_utc": datetime.now(timezone.utc).isoformat(),
            "age_minutes": age_minutes,
            "raw_age_minutes": raw_age_minutes,
        })
        return parsed, meta

    def _stable_signature(self, path: Path):
        stat = path.stat()
        return int(stat.st_mtime_ns), int(stat.st_size)

    def _read_text_stable(self, path: Path) -> str:
        last_error = None
        for _ in range(5):
            try:
                before = self._stable_signature(path)
                if before[1] <= 0:
                    return ""
                time.sleep(0.025)
                if before != self._stable_signature(path):
                    time.sleep(0.05)
                    continue
                text = path.read_text()
                if before == self._stable_signature(path):
                    return text
            except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
                last_error = exc
                time.sleep(0.05)
        if last_error is not None:
            return ""
        try:
            return path.read_text()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return ""

    def _read_key_values(self, path: Path) -> dict[str, str]:
        payload: dict[str, str] = {}
        text = self._read_text_stable(path)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            payload[key.strip()] = value.strip()
        return payload

    def _read_bridge_csv(self, path: Path) -> pd.DataFrame:
        last_df = pd.DataFrame()
        for _ in range(5):
            try:
                if not path.exists():
                    return pd.DataFrame()
                before = self._stable_signature(path)
                if before[1] <= 0:
                    return pd.DataFrame()
                time.sleep(0.025)
                if before != self._stable_signature(path):
                    time.sleep(0.05)
                    continue
                df = pd.read_csv(path)
                after = self._stable_signature(path)
                if before == after:
                    return df
                last_df = df
                time.sleep(0.05)
            except (
                FileNotFoundError,
                OSError,
                UnicodeDecodeError,
                pd.errors.EmptyDataError,
                pd.errors.ParserError,
            ):
                time.sleep(0.05)
        return last_df

    def _native_pending_order_type(self, direction: str, pending_type: str) -> int:
        assert self.mt5 is not None
        key = f"{direction.upper()}_{pending_type.upper()}"
        mapping = {
            "BUY_LIMIT": self.mt5.ORDER_TYPE_BUY_LIMIT,
            "SELL_LIMIT": self.mt5.ORDER_TYPE_SELL_LIMIT,
            "BUY_STOP": self.mt5.ORDER_TYPE_BUY_STOP,
            "SELL_STOP": self.mt5.ORDER_TYPE_SELL_STOP,
        }
        if key not in mapping:
            raise RuntimeError(f"Unsupported pending order type: {key}")
        return mapping[key]

    def _native_order_type_name(self, value: int) -> str:
        assert self.mt5 is not None
        mapping = {
            self.mt5.ORDER_TYPE_BUY: "BUY",
            self.mt5.ORDER_TYPE_SELL: "SELL",
            self.mt5.ORDER_TYPE_BUY_LIMIT: "BUY_LIMIT",
            self.mt5.ORDER_TYPE_SELL_LIMIT: "SELL_LIMIT",
            self.mt5.ORDER_TYPE_BUY_STOP: "BUY_STOP",
            self.mt5.ORDER_TYPE_SELL_STOP: "SELL_STOP",
        }
        return mapping.get(value, f"TYPE_{value}")

    def _native_order_state_name(self, value: int) -> str:
        assert self.mt5 is not None
        mapping = {
            self.mt5.ORDER_STATE_STARTED: "STARTED",
            self.mt5.ORDER_STATE_PLACED: "PLACED",
            self.mt5.ORDER_STATE_CANCELED: "CANCELED",
            self.mt5.ORDER_STATE_PARTIAL: "PARTIAL",
            self.mt5.ORDER_STATE_FILLED: "FILLED",
            self.mt5.ORDER_STATE_REJECTED: "REJECTED",
            self.mt5.ORDER_STATE_EXPIRED: "EXPIRED",
            self.mt5.ORDER_STATE_REQUEST_ADD: "REQUEST_ADD",
            self.mt5.ORDER_STATE_REQUEST_MODIFY: "REQUEST_MODIFY",
            self.mt5.ORDER_STATE_REQUEST_CANCEL: "REQUEST_CANCEL",
        }
        return mapping.get(value, f"STATE_{value}")
