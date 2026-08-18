from __future__ import annotations

from datetime import datetime, timezone
import os
import threading

import pandas as pd

from mt5_xm_gateway import MT5Gateway
from .contracts import Candle, Frame, MarketSnapshot, Tick


class MarketDataError(RuntimeError):
    pass


class MarketDataEngine:
    """One live MT5 WebSocket quote plus completed H4/M15/M5 bars."""

    # H1 added 2026-08-17: the direction filter needs H4 AND H1 agreement.
    # Backtested on the LIVE_STRUCTURE_BREAK path (the one that actually runs)
    # over 69 days of M1-reconstructed entries: H4+H1 gives 52.0% win /
    # +0.244 R-per-trade / +736R, positive in all 4 chronological folds,
    # vs -0.025 R-per-trade with no direction filter. Also matches today's
    # live ledger and the pre-rebuild 1000-day validation.
    TIMEFRAMES = (("H4", 240), ("H1", 60), ("M15", 15), ("M5", 5))
    FRESHNESS_SECONDS = {"H4": 43200, "H1": 10800, "M15": 3600, "M5": 900}

    def __init__(self, gateway: MT5Gateway, history_bars: int = 220):
        self.gateway = gateway
        self.history_bars = max(60, int(history_bars))
        try:
            configured_age = float(os.getenv("MT5_WS_MAX_TICK_AGE_SECONDS", "60"))
        except (TypeError, ValueError):
            configured_age = 60.0
        self.websocket_max_tick_age_seconds = max(10.0, min(60.0, configured_age))
        self._ticks: dict[str, Tick] = {}
        self._frames_cache: dict[str, dict[str, Frame]] = {}
        self._changed_symbols: set[str] = set()
        self._lock = threading.RLock()
        self._last_event_at: datetime | None = None
        self.websocket_event_count = 0
        self.websocket_rejected_events = 0

    @staticmethod
    def _utc(value) -> datetime:
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    def ingest_tick(self, event: dict) -> None:
        if not isinstance(event, dict) or str(event.get("type", "tick")).lower() != "tick":
            return
        try:
            symbol = str(event["symbol"]).strip().upper()
            timestamp = self._utc(event["time_utc"])
            bid = float(event["bid"])
            ask = float(event["ask"])
        except (KeyError, TypeError, ValueError, OverflowError):
            self.websocket_rejected_events += 1
            return
        now = datetime.now(timezone.utc)
        if not symbol or bid <= 0 or ask < bid or timestamp > now + pd.Timedelta(seconds=5):
            self.websocket_rejected_events += 1
            return
        try:
            resolved = str(self.gateway.ensure_symbol(symbol) or symbol).strip().upper()
        except Exception:
            resolved = symbol
        tick = Tick(resolved, bid, ask, timestamp, now)
        with self._lock:
            previous = self._ticks.get(resolved)
            if previous is not None and timestamp <= previous.timestamp:
                return
            self._ticks[resolved] = tick
            self._ticks[symbol] = tick
            self._changed_symbols.add(resolved)
            self._last_event_at = now
            self.websocket_event_count += 1

    def _tick(self, symbol: str) -> tuple[str, Tick, float]:
        resolved = str(self.gateway.ensure_symbol(symbol) or symbol).strip().upper()
        with self._lock:
            tick = self._ticks.get(resolved) or self._ticks.get(str(symbol).upper())
        if tick is None:
            raise MarketDataError(f"{symbol}: live WebSocket tick unavailable")
        age = max(0.0, (datetime.now(timezone.utc) - tick.timestamp).total_seconds())
        if age > self.websocket_max_tick_age_seconds:
            raise MarketDataError(f"{symbol}: stale WebSocket tick age={age:.3f}s")
        return resolved, tick, age

    def _frame(self, symbol: str, label: str, minutes: int) -> Frame:
        raw = self.gateway.rates(symbol, label, self.history_bars)
        if raw is None or raw.empty:
            raise MarketDataError(f"{symbol} {label}: no MT5 bars")
        now = datetime.now(timezone.utc)
        candles: list[Candle] = []
        for index, row in raw[~raw.index.duplicated(keep="last")].sort_index().iterrows():
            timestamp = self._utc(index)
            if timestamp + pd.Timedelta(minutes=minutes) > now:
                continue
            candles.append(Candle(timestamp, float(row.get("Open", 0.0) or 0.0), float(row.get("High", 0.0) or 0.0), float(row.get("Low", 0.0) or 0.0), float(row.get("Close", 0.0) or 0.0), float(row.get("Volume", 0.0) or 0.0)))
        if len(candles) < 30:
            raise MarketDataError(f"{symbol} {label}: insufficient completed MT5 bars")
        age = max(0.0, (now - (candles[-1].timestamp + pd.Timedelta(minutes=minutes))).total_seconds())
        if age > self.FRESHNESS_SECONDS[label]:
            raise MarketDataError(f"{symbol} {label}: stale completed MT5 bar age={age:.1f}s")
        return Frame(label, tuple(candles))

    @staticmethod
    def _snapshot_payload(resolved: str, asset_class: str, frames: dict[str, Frame], tick: Tick, tick_age: float, *, cached: bool) -> MarketSnapshot:
        captured = datetime.now(timezone.utc)
        return MarketSnapshot(
            symbol=resolved,
            asset_class=asset_class,
            frames=frames,
            tick=tick,
            captured_at=captured,
            freshness={
                "source": "websocket",
                "tick_age_seconds": round(tick_age, 3),
                "completed_only": True,
                "cached_completed_frames": bool(cached),
                "timeframes": list(frames),
                "latest_completed": {label: frame.last.timestamp.isoformat() for label, frame in frames.items()},
            },
        )

    def snapshot(self, symbol: str, asset_class: str) -> MarketSnapshot:
        resolved, tick, tick_age = self._tick(symbol)
        with self._lock:
            frames = dict(self._frames_cache.get(resolved, {}))
        refresh_labels: list[tuple[str, int]] = []
        now = datetime.now(timezone.utc)
        for label, minutes in self.TIMEFRAMES:
            frame = frames.get(label)
            expected_last = (tick.timestamp.timestamp() // (minutes * 60.0)) * (minutes * 60.0) - (minutes * 60.0)
            stale = (
                frame is None
                or frame.last is None
                or abs(frame.last.timestamp.timestamp() - expected_last) > 5.0
                or (now - (frame.last.timestamp + pd.Timedelta(minutes=minutes))).total_seconds() > self.FRESHNESS_SECONDS[label]
            )
            if stale:
                refresh_labels.append((label, minutes))
        for label, minutes in refresh_labels:
            frames[label] = self._frame(resolved, label, minutes)
        with self._lock:
            self._frames_cache[resolved] = dict(frames)
        return self._snapshot_payload(resolved, asset_class, frames, tick, tick_age, cached=not bool(refresh_labels))

    def cached_snapshot(self, symbol: str, asset_class: str) -> MarketSnapshot:
        """Return live WebSocket price plus the last completed MT5 bars.

        The fast trigger path never downloads history on every tick. It refreshes
        only the newly completed M5 bar when a five-minute boundary is crossed.
        """
        resolved, tick, tick_age = self._tick(symbol)
        with self._lock:
            frames = dict(self._frames_cache.get(resolved, {}))
        if not frames:
            raise MarketDataError(f"{symbol}: completed MT5 frame cache is not initialized")
        current_start = (tick.timestamp.timestamp() // 300.0) * 300.0
        expected_last_m5 = current_start - 300.0
        m5 = frames.get("M5")
        if m5 is None or abs(m5.last.timestamp.timestamp() - expected_last_m5) > 5.0:
            refreshed_m5 = self._frame(resolved, "M5", 5)
            frames["M5"] = refreshed_m5
            with self._lock:
                self._frames_cache[resolved] = dict(frames)
        now = datetime.now(timezone.utc)
        for label, minutes in self.TIMEFRAMES:
            frame = frames.get(label)
            if frame is None or frame.last is None:
                raise MarketDataError(f"{symbol} {label}: cached MT5 frame unavailable")
            age = max(0.0, (now - (frame.last.timestamp + pd.Timedelta(minutes=minutes))).total_seconds())
            if age > self.FRESHNESS_SECONDS[label]:
                raise MarketDataError(f"{symbol} {label}: cached completed MT5 bar stale age={age:.1f}s")
        return self._snapshot_payload(resolved, asset_class, frames, tick, tick_age, cached=True)

    def drain_changed_symbols(self) -> list[str]:
        with self._lock:
            symbols = sorted(self._changed_symbols)
            self._changed_symbols.clear()
        return symbols

    def live_tick_status(self) -> dict:
        now = datetime.now(timezone.utc)
        with self._lock:
            ticks = dict(self._ticks)
            last_event = self._last_event_at
        ages = {symbol: max(0.0, (now - tick.timestamp).total_seconds()) for symbol, tick in ticks.items()}
        return {"source": "websocket", "tracked_symbols": len(ticks), "fresh_symbols": sum(age <= self.websocket_max_tick_age_seconds for age in ages.values()), "stale_symbols": sorted(symbol for symbol, age in ages.items() if age > self.websocket_max_tick_age_seconds), "last_websocket_event_at": last_event.isoformat() if last_event else "", "websocket_event_count": self.websocket_event_count, "websocket_rejected_events": self.websocket_rejected_events}

    def live_tick_rows(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        with self._lock:
            values = list({id(tick): tick for tick in self._ticks.values()}.values())
        return [{"symbol": tick.symbol, "bid": tick.bid, "ask": tick.ask, "last": tick.mid, "spread": tick.ask - tick.bid, "time": tick.timestamp.isoformat(), "received_at": tick.received_at.isoformat(), "age_seconds": round(max(0.0, (now - tick.timestamp).total_seconds()), 3), "source": "websocket"} for tick in sorted(values, key=lambda item: item.symbol)]
