from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Iterable

import pandas as pd

from mt5_xm_gateway import MT5Gateway
from .contracts import Candle, Frame, MarketSnapshot, Tick


class MarketDataError(RuntimeError):
    pass


class MarketDataEngine:
    """Reads MT5/bridge data and constructs one synchronized snapshot."""

    # Long context frames naturally advance less often, so only intraday
    # frames use freshness limits. The bridge timestamp is the candle open.
    FRESHNESS_MAX_AGE_SECONDS = {
        "D1": 172800,
        "H4": 43200,
        "H1": 10800,
        "M30": 5400,
        "M15": 2700,
        "M5": 900,
        "M3": 540,
        "M1": 180,
    }

    TIMEFRAMES = (
        ("MN1", 43200),
        ("W1", 10080),
        ("D1", 1440),
        ("H4", 240),
        ("H1", 60),
        ("M30", 30),
        ("M15", 15),
        ("M5", 5),
        ("M3", 3),
        ("M1", 1),
    )

    def __init__(self, gateway: MT5Gateway, history_bars: int = 220):
        self.gateway = gateway
        self.history_bars = max(60, int(history_bars))
        # Strategy snapshots accept only ticks received through the live
        # WebSocket transport. Bridge tick files remain diagnostic data and
        # are deliberately not an execution fallback.
        self.websocket_max_tick_age_seconds = 10.0
        self._live_ticks: dict[str, Tick] = {}
        self._tick_lock = threading.RLock()
        self._last_websocket_event_at: datetime | None = None
        self.websocket_event_count = 0
        self.websocket_rejected_events = 0

    @staticmethod
    def _as_utc(value) -> datetime:
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        raw = str(value).strip()
        try:
            numeric = float(raw)
            if numeric > 1000000000:
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (TypeError, ValueError):
            pass
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)

    def ingest_tick(self, event: dict) -> None:
        '''Accept one factual tick from the production WebSocket listener.'''
        if not isinstance(event, dict) or str(event.get("type", "tick")).lower() != "tick":
            return
        symbol = str(event.get("symbol", "")).strip().upper()
        try:
            tick_epoch = float(event.get("time_utc", 0) or 0)
            bid = float(event.get("bid", 0) or 0)
            ask = float(event.get("ask", 0) or 0)
        except (TypeError, ValueError):
            self.websocket_rejected_events += 1
            return
        if not symbol or tick_epoch <= 0 or bid <= 0 or ask <= 0 or ask < bid:
            self.websocket_rejected_events += 1
            return
        now = datetime.now(timezone.utc)
        timestamp = datetime.fromtimestamp(tick_epoch, tz=timezone.utc)
        if timestamp > now.replace(microsecond=0) + pd.Timedelta(seconds=5):
            self.websocket_rejected_events += 1
            return
        resolved = symbol
        try:
            resolved = str(self.gateway.config.resolved_symbol(symbol) or symbol).strip().upper()
        except Exception:
            pass
        tick = Tick(
            symbol=resolved,
            bid=bid,
            ask=ask,
            timestamp=timestamp,
            received_at=now,
        )
        keys = {symbol, resolved}
        with self._tick_lock:
            current = None
            for key in keys:
                candidate = self._live_ticks.get(key)
                if candidate is not None and (current is None or candidate.timestamp > current.timestamp):
                    current = candidate
            if current is not None and tick.timestamp <= current.timestamp:
                return
            for key in keys:
                self._live_ticks[key] = tick
            self._last_websocket_event_at = now
            self.websocket_event_count += 1

    def live_tick_status(self) -> dict:
        now = datetime.now(timezone.utc)
        with self._tick_lock:
            latest = dict(self._live_ticks)
            last_event = self._last_websocket_event_at
        ages = {
            symbol: max(0.0, (now - tick.timestamp).total_seconds())
            for symbol, tick in latest.items()
        }
        fresh = {symbol: age for symbol, age in ages.items() if age <= self.websocket_max_tick_age_seconds}
        return {
            "source": "websocket",
            "tracked_symbols": len(latest),
            "fresh_symbols": len(fresh),
            "stale_symbols": sorted(symbol for symbol, age in ages.items() if age > self.websocket_max_tick_age_seconds),
            "max_tick_age_seconds": round(max(ages.values()), 3) if ages else None,
            "last_websocket_event_at": last_event.isoformat() if last_event else "",
            "websocket_event_count": self.websocket_event_count,
            "websocket_rejected_events": self.websocket_rejected_events,
        }

    def _websocket_tick(self, symbol: str) -> tuple[str, Tick, float]:
        # Production MT5Gateway exposes ensure_symbol and therefore must use
        # the ingested websocket cache. Small contract-test gateways may only
        # expose their factual symbol_tick adapter.
        ensure_symbol = getattr(self.gateway, "ensure_symbol", None)
        if callable(ensure_symbol):
            resolved = str(ensure_symbol(symbol) or symbol).strip().upper()
        else:
            resolved = str(symbol).strip().upper()
        keys = (resolved, str(symbol).strip().upper())
        with self._tick_lock:
            tick = next((self._live_ticks.get(key) for key in keys if self._live_ticks.get(key) is not None), None)
        if tick is None and not callable(ensure_symbol):
            raw = self.gateway.symbol_tick(symbol)
            raw_symbol, raw_tick = raw if isinstance(raw, tuple) and len(raw) == 2 else (symbol, raw)
            resolved = str(raw_symbol or symbol).strip().upper()
            raw_time = getattr(raw_tick, "time_utc", None)
            if raw_time is None:
                raise MarketDataError(f"{symbol}: websocket tick unavailable")
            timestamp = self._as_utc(raw_time)
            bid = float(getattr(raw_tick, "bid", 0.0) or 0.0)
            ask = float(getattr(raw_tick, "ask", 0.0) or 0.0)
            now = datetime.now(timezone.utc)
            if bid <= 0 or ask <= 0 or ask < bid:
                raise MarketDataError(f"{symbol}: invalid websocket tick")
            tick = Tick(symbol=resolved, bid=bid, ask=ask, timestamp=timestamp, received_at=now)
        if tick is None:
            raise MarketDataError(f"{symbol}: websocket tick unavailable")
        age_seconds = max(0.0, (datetime.now(timezone.utc) - tick.timestamp).total_seconds())
        if age_seconds > self.websocket_max_tick_age_seconds:
            raise MarketDataError(
                f"{symbol}: websocket tick stale age_seconds={age_seconds:.3f} "
                f"max_age_seconds={self.websocket_max_tick_age_seconds:.3f}"
            )
        return resolved, tick, age_seconds

    def live_tick_rows(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        with self._tick_lock:
            ticks = list(self._live_ticks.values())
        rows = []
        for tick in sorted(ticks, key=lambda item: item.symbol):
            age = max(0.0, (now - tick.timestamp).total_seconds())
            rows.append({
                "symbol": tick.symbol,
                "bid": tick.bid,
                "ask": tick.ask,
                "last": (tick.bid + tick.ask) / 2.0,
                "spread": tick.ask - tick.bid,
                "time": tick.timestamp.isoformat(),
                "received_at": tick.received_at.isoformat(),
                "age_seconds": round(age, 3),
                "fresh": age <= self.websocket_max_tick_age_seconds,
                "status": "LIVE_DATA" if age <= self.websocket_max_tick_age_seconds else "STALE_MARKET_DATA",
                "source": "websocket",
            })
        return rows

    def _raw(self, symbol: str, timeframe: str) -> pd.DataFrame:
        # Every snapshot frame is an independent broker timeframe. In
        # particular, M3 must never be synthesized from M1.
        return self.gateway.rates(symbol, timeframe, self.history_bars)

    def _frame(self, symbol: str, timeframe: str) -> Frame:
        raw = self._raw(symbol, timeframe)
        if raw is None or raw.empty:
            raise MarketDataError(f"{symbol} {timeframe}: no candles")

        period_seconds = dict(self.TIMEFRAMES)[timeframe] * 60
        now = datetime.now(timezone.utc)
        ordered = raw[~raw.index.duplicated(keep="last")].sort_index()
        candles = []
        forming = 0
        for index, row in ordered.iterrows():
            timestamp = self._as_utc(index)
            # A candle is usable only after its complete interval has closed.
            # This excludes any still-forming broker candle.
            if timestamp + pd.Timedelta(seconds=period_seconds) > now:
                forming += 1
                continue
            candles.append(Candle(
                timestamp=timestamp,
                open=float(row.get("Open", 0.0) or 0.0),
                high=float(row.get("High", 0.0) or 0.0),
                low=float(row.get("Low", 0.0) or 0.0),
                close=float(row.get("Close", 0.0) or 0.0),
                volume=float(row.get("Volume", 0.0) or 0.0),
            ))
        if not candles:
            raise MarketDataError(f"{symbol} {timeframe}: no completed candles")
        latest = candles[-1].timestamp
        max_age = self.FRESHNESS_MAX_AGE_SECONDS.get(timeframe)
        age_seconds = max(0.0, (now - (latest + pd.Timedelta(seconds=period_seconds))).total_seconds())
        if max_age is not None and age_seconds > max_age:
            raise MarketDataError(
                f"{symbol} {timeframe}: stale completed candle "
                f"age_seconds={age_seconds:.1f} max_age_seconds={max_age}"
            )
        return Frame(timeframe, tuple(candles))

    def snapshot(self, symbol: str, asset_class: str) -> MarketSnapshot:
        # Reject before history work when the live quote is absent or stale.
        # There is intentionally no bridge-file fallback in this path.
        resolved, tick, tick_age_seconds = self._websocket_tick(symbol)
        frames = {label: self._frame(symbol, label) for label, _minutes in self.TIMEFRAMES}
        now = datetime.now(timezone.utc)
        freshness = {
            "source": "websocket",
            "resolved_symbol": resolved,
            "captured_at": now.isoformat(),
            "tick_timestamp": tick.timestamp.isoformat(),
            "tick_age_seconds": round(tick_age_seconds, 3),
            "timeframes": [x[0] for x in self.TIMEFRAMES],
            "completed_only": True,
            "latest_completed": {
                name: frame.last.timestamp.isoformat() if frame.last else ""
                for name, frame in frames.items()
            },
        }
        return MarketSnapshot(
            symbol=symbol,
            asset_class=asset_class,
            frames=frames,
            tick=tick,
            captured_at=now,
            freshness=freshness,
        )

    def symbols(self, configured: Iterable[str]) -> list[str]:
        return [str(symbol).strip().upper() for symbol in configured if str(symbol).strip()]
