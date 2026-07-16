from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import pandas as pd

from mt5_xm_gateway import MT5Gateway
from .contracts import Candle, Frame, MarketSnapshot, Tick


class MarketDataError(RuntimeError):
    pass


class MarketDataEngine:
    """Reads MT5/bridge data and constructs one synchronized snapshot."""

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

    @staticmethod
    def _as_utc(value) -> datetime:
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)

    def _m3_from_m1(self, symbol: str) -> pd.DataFrame:
        raw = self.gateway.rates(symbol, "M1", self.history_bars * 3)
        if raw is None or raw.empty:
            raise MarketDataError(f"{symbol} M3: M1 source unavailable")
        frame = raw.sort_index()
        result = frame.resample("3min", label="left", closed="left").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }).dropna(subset=["Open", "High", "Low", "Close"])
        return result.tail(self.history_bars)

    def _raw(self, symbol: str, timeframe: str) -> pd.DataFrame:
        if timeframe == "M3":
            try:
                return self.gateway.rates(symbol, "M3", self.history_bars)
            except Exception:
                return self._m3_from_m1(symbol)
        return self.gateway.rates(symbol, timeframe, self.history_bars)

    def _frame(self, symbol: str, timeframe: str) -> Frame:
        raw = self._raw(symbol, timeframe)
        if raw is None or raw.empty:
            raise MarketDataError(f"{symbol} {timeframe}: no candles")
        ordered = raw[~raw.index.duplicated(keep="last")].sort_index()
        candles = []
        for index, row in ordered.iterrows():
            candles.append(Candle(
                timestamp=self._as_utc(index),
                open=float(row.get("Open", 0.0) or 0.0),
                high=float(row.get("High", 0.0) or 0.0),
                low=float(row.get("Low", 0.0) or 0.0),
                close=float(row.get("Close", 0.0) or 0.0),
                volume=float(row.get("Volume", 0.0) or 0.0),
            ))
        if not candles:
            raise MarketDataError(f"{symbol} {timeframe}: candle construction failed")
        return Frame(timeframe, tuple(candles))

    def snapshot(self, symbol: str, asset_class: str) -> MarketSnapshot:
        frames = {label: self._frame(symbol, label) for label, _minutes in self.TIMEFRAMES}
        resolved, raw_tick = self.gateway.symbol_tick(symbol)
        tick_epoch = float(getattr(raw_tick, "time_utc", getattr(raw_tick, "time", 0.0)) or 0.0)
        if tick_epoch <= 0:
            raise MarketDataError(f"{symbol}: tick timestamp missing")
        tick = Tick(
            symbol=resolved,
            bid=float(getattr(raw_tick, "bid", 0.0) or 0.0),
            ask=float(getattr(raw_tick, "ask", 0.0) or 0.0),
            timestamp=datetime.fromtimestamp(tick_epoch, tz=timezone.utc),
            received_at=datetime.now(timezone.utc),
        )
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            raise MarketDataError(f"{symbol}: invalid tick geometry")
        return MarketSnapshot(
            symbol=symbol,
            asset_class=asset_class,
            frames=frames,
            tick=tick,
            captured_at=datetime.now(timezone.utc),
            freshness={"source": "MT5Gateway", "resolved_symbol": resolved, "timeframes": [x[0] for x in self.TIMEFRAMES]},
        )

    def symbols(self, configured: Iterable[str]) -> list[str]:
        return [str(symbol).strip().upper() for symbol in configured if str(symbol).strip()]
