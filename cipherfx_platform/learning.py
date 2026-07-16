from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
from typing import Iterable

from .contracts import Frame, MarketAnalysis, MarketSnapshot, TimeframeAnalysis, SIDES, utc_now


@dataclass(frozen=True)
class LearningProfile:
    stop_atr: float
    target_r: float
    htf_min_bars: int = 40
    m15_min_bars: int = 40
    m5_min_bars: int = 12


class LearningEngine:
    """The only market-analysis module.

    It produces raw market facts and structural classifications. It does not
    assign weighted strategy scores and it never talks to a broker.
    """

    PROFILES = {
        "forex": LearningProfile(1.25, 1.60),
        "index": LearningProfile(1.50, 1.80),
        "metal": LearningProfile(1.60, 1.80),
    }

    @staticmethod
    def _close(frame: Frame) -> list[float]:
        return [float(c.close) for c in frame.candles if isfinite(float(c.close))]

    @staticmethod
    def _ema(frame: Frame, period: int) -> float:
        values = LearningEngine._close(frame)
        if not values:
            return 0.0
        alpha = 2.0 / (period + 1.0)
        value = values[0]
        for item in values[1:]:
            value = alpha * item + (1.0 - alpha) * value
        return value

    @staticmethod
    def _atr(frame: Frame, period: int = 14) -> float:
        if len(frame.candles) < 2:
            return 0.0
        values = []
        for index, candle in enumerate(frame.candles):
            previous = frame.candles[index - 1].close if index else candle.open
            values.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
        values = values[-period:]
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _rmi(frame: Frame, period: int = 14, momentum: int = 5) -> float:
        values = LearningEngine._close(frame)
        if len(values) <= momentum + 2:
            return 50.0
        gains, losses = [], []
        for index in range(momentum, len(values)):
            delta = values[index] - values[index - momentum]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        gain = sum(gains[-period:]) / max(1, len(gains[-period:]))
        loss = sum(losses[-period:]) / max(1, len(losses[-period:]))
        if loss <= 1e-12:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    @staticmethod
    def _adx_proxy(frame: Frame, period: int = 14) -> float:
        if len(frame.candles) < period + 2:
            return 0.0
        sample = frame.candles[-period:]
        ranges = [max(c.high - c.low, 1e-12) for c in sample]
        moves = [abs(frame.candles[index].close - frame.candles[index - 1].close) for index in range(len(frame.candles) - period, len(frame.candles))]
        return min(100.0, 100.0 * (sum(moves) / len(moves)) / (sum(ranges) / len(ranges)))

    @staticmethod
    def _body(candle) -> float:
        return abs(candle.close - candle.open) / max(candle.high - candle.low, 1e-12)

    @staticmethod
    def _location(candle) -> float:
        return (candle.close - candle.low) / max(candle.high - candle.low, 1e-12)

    @staticmethod
    def _vwap(frame: Frame) -> float:
        total_volume = sum(max(c.volume, 0.0) for c in frame.candles)
        if total_volume <= 0:
            return frame.last.close if frame.last else 0.0
        return sum(((c.high + c.low + c.close) / 3.0) * max(c.volume, 0.0) for c in frame.candles) / total_volume

    @staticmethod
    def _rsi(frame: Frame, period: int = 14) -> float:
        values = LearningEngine._close(frame)
        if len(values) <= period:
            return 50.0
        gains, losses = [], []
        for previous, current in zip(values[-period - 1:-1], values[-period:]):
            delta = current - previous
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        average_gain = sum(gains) / max(1, len(gains))
        average_loss = sum(losses) / max(1, len(losses))
        if average_loss <= 1e-12:
            return 100.0 if average_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

    @staticmethod
    def _times(frame: Frame, minutes: int) -> tuple:
        opened = frame.last.timestamp if frame.last else utc_now()
        return opened, opened + timedelta(minutes=minutes)

    def _htf(self, frame: Frame | None, timeframe: str, expected: str | None, minimum_bars: int) -> TimeframeAnalysis:
        if frame is None or len(frame.candles) < minimum_bars:
            opened, closed = self._times(frame or Frame(timeframe, ()), {"H4": 240, "H1": 60}[timeframe])
            return TimeframeAnalysis(timeframe, "NO_TRADE", opened, closed, {}, {"data_status": "INSUFFICIENT"}, ("DATA_INSUFFICIENT",))
        last = frame.last
        ema20, ema50 = self._ema(frame, 20), self._ema(frame, 50)
        prior = self._ema(Frame(frame.timeframe, frame.candles[:-4]), 20)
        rmi, adx = self._rmi(frame), self._adx_proxy(frame)
        values = self._close(frame)
        mean20 = sum(values[-20:]) / max(1, len(values[-20:]))
        ranges = [max(c.high - c.low, 1e-12) for c in frame.candles]
        recent_range = sum(ranges[-5:]) / max(1, len(ranges[-5:]))
        base_range = sum(ranges[-21:-1]) / max(1, len(ranges[-21:-1]))
        features = {
            "ema_stack_buy": last.close > ema20 and ema20 > ema50,
            "ema_stack_sell": last.close < ema20 and ema20 < ema50,
            "slope_buy": ema20 > prior,
            "slope_sell": ema20 < prior,
            "rmi_buy": rmi >= (52 if timeframe != "H4" else 52),
            "rmi_sell": rmi <= (48 if timeframe != "H4" else 48),
            "adx_regime": adx >= (18 if timeframe == "H4" else 15),
            "location_buy": last.close >= mean20,
            "location_sell": last.close <= mean20,
            "range_expansion": recent_range >= base_range,
            "volatility_expansion": recent_range >= base_range,
            "candle_pressure_buy": self._body(last) >= 0.30 and last.close > last.open,
            "candle_pressure_sell": self._body(last) >= 0.30 and last.close < last.open,
        }
        if timeframe == "H4":
            buy_keys = ("ema_stack_buy", "rmi_buy", "slope_buy", "adx_regime", "location_buy")
            sell_keys = ("ema_stack_sell", "rmi_sell", "slope_sell", "adx_regime", "location_sell")
        else:
            buy_keys = ("ema_stack_buy", "rmi_buy", "slope_buy", "adx_regime", "location_buy")
            sell_keys = ("ema_stack_sell", "rmi_sell", "slope_sell", "adx_regime", "location_sell")
        buy_count, sell_count = sum(bool(features[key]) for key in buy_keys), sum(bool(features[key]) for key in sell_keys)
        side = "BUY" if buy_count >= 3 and buy_count > sell_count else "SELL" if sell_count >= 3 and sell_count > buy_count else "NO_TRADE"
        reasons = []
        if side in SIDES:
            reasons.append("DIRECTIONAL_CONTEXT")
        else:
            reasons.append("MIXED_OR_NEUTRAL_STRUCTURE")
        if expected in SIDES and side != expected:
            reasons.append(f"{timeframe}_NOT_ALIGNED")
        opened, closed = self._times(frame, {"H4": 240, "H1": 60}[timeframe])
        metrics = {
            "ema20": ema20, "ema50": ema50, "prior_ema20": prior,
            "rmi": rmi, "adx_proxy": adx, "vwap": self._vwap(frame), "rsi": self._rsi(frame),
            "atr": self._atr(frame), "close": last.close, "mean20": mean20,
            "recent_range": recent_range, "base_range": base_range,
            "buy_evidence": buy_count, "sell_evidence": sell_count,
        }
        return TimeframeAnalysis(timeframe, side, opened, closed, features, metrics, tuple(reasons))

    def _m15(self, frame: Frame | None, expected: str, asset: str, minimum_bars: int) -> TimeframeAnalysis:
        if frame is None or len(frame.candles) < minimum_bars:
            opened, closed = self._times(frame or Frame("M15", ()), 15)
            return TimeframeAnalysis("M15", "NO_TRADE", opened, closed, {}, {"data_status": "INSUFFICIENT"}, ("DATA_INSUFFICIENT",))
        candles, last = list(frame.candles), frame.last
        atr, ema20 = self._atr(frame), self._ema(frame, 20)
        price = last.close
        tolerance = max(atr * (0.75 if asset != "metal" else 0.75), abs(last.close) * (0.0002 if asset == "forex" else 0.0003), 1e-12)
        recent = candles[-16:-1]
        poi = bool(recent) and (abs(price - min(c.low for c in recent)) <= tolerance or abs(price - max(c.high for c in recent)) <= tolerance or abs(price - ema20) <= tolerance)
        prior = candles[-9:-1]
        if expected == "BUY":
            bos = last.close > max(c.high for c in prior)
            retracement = any(c.low <= ema20 + atr * 0.2 for c in candles[-5:-1]) and last.close >= ema20
            countertrend = min(c.close for c in candles[-6:-1]) < min(c.close for c in candles[-12:-6])
            choch = countertrend and last.close > max(c.high for c in candles[-5:-1])
            impulse = (last.high - last.low) >= max(atr * (1.10 if asset == "metal" else 0.0), 1e-12)
            reclaim = last.close > ema20 and last.close > last.open
        else:
            bos = last.close < min(c.low for c in prior)
            retracement = any(c.high >= ema20 - atr * 0.2 for c in candles[-5:-1]) and last.close <= ema20
            countertrend = max(c.close for c in candles[-6:-1]) > max(c.close for c in candles[-12:-6])
            choch = countertrend and last.close < min(c.low for c in candles[-5:-1])
            impulse = (last.high - last.low) >= max(atr * (1.10 if asset == "metal" else 0.0), 1e-12)
            reclaim = last.close < ema20 and last.close < last.open
        if asset == "forex":
            features = {"poi": poi, "choch": choch, "bos": bos, "retracement": retracement}
        elif asset == "index":
            session_high, session_low = max(c.high for c in recent), min(c.low for c in recent)
            features = {"session_poi": abs(price - session_high) <= atr * 0.75 or abs(price - session_low) <= atr * 0.75,
                        "session_break": last.close > session_high if expected == "BUY" else last.close < session_low,
                        "range_retest": (any(c.low <= session_high for c in candles[-5:-1]) and last.close > session_high) if expected == "BUY" else (any(c.high >= session_low for c in candles[-5:-1]) and last.close < session_low),
                        "momentum": self._body(last) >= 0.20}
        else:
            features = {"volatility_poi": poi, "impulse": impulse, "retracement": retracement, "volatility_reclaim": reclaim}
        side = expected if all(features.values()) else "NO_TRADE"
        opened, closed = self._times(frame, 15)
        return TimeframeAnalysis("M15", side, opened, closed, features,
            {"atr": atr, "ema20": ema20, "vwap": self._vwap(frame), "rsi": self._rsi(frame), "close": last.close},
            tuple("M15_" + key.upper() + "_MISSING" for key, value in features.items() if not value))

    def _m5(self, frame: Frame | None, expected: str, asset: str, minimum_bars: int) -> TimeframeAnalysis:
        if frame is None or len(frame.candles) < minimum_bars:
            opened, closed = self._times(frame or Frame("M5", ()), 5)
            return TimeframeAnalysis("M5", "NO_TRADE", opened, closed, {}, {"data_status": "INSUFFICIENT"}, ("M5_DATA_INSUFFICIENT",))
        candles, last = list(frame.candles), frame.last
        previous = candles[-6:-1]
        high_level, low_level = max(c.high for c in previous), min(c.low for c in previous)
        body, location = self._body(last), self._location(last)
        buy_break = last.close > high_level and last.close > last.open
        sell_break = last.close < low_level and last.close < last.open
        buy_retest = last.low <= high_level and last.close > high_level and last.close > last.open
        sell_retest = last.high >= low_level and last.close < low_level and last.close < last.open
        body_min, buy_loc, sell_loc = (0.30, 0.60, 0.40) if asset == "metal" else (0.25 if asset == "index" else 0.20, 0.55, 0.45)
        range_ok = True if asset != "metal" else (last.high - last.low) >= max(self._atr(frame) * 1.10, 1e-12)
        trigger = buy_break or buy_retest if expected == "BUY" else sell_break or sell_retest
        directional = location >= buy_loc if expected == "BUY" else location <= sell_loc
        features = {"break_or_retest": trigger, "direction": directional, "body": body >= body_min, "range": range_ok}
        side = expected if all(features.values()) else "NO_TRADE"
        opened, closed = self._times(frame, 5)
        trigger_type = "BREAK" if buy_break or sell_break else "RETEST" if buy_retest or sell_retest else ""
        return TimeframeAnalysis("M5", side, opened, closed, features,
            {"body_fraction": body, "close_location": location, "high_level": high_level, "low_level": low_level, "atr": self._atr(frame), "trigger_type": trigger_type},
            tuple("M5_" + key.upper() + "_MISSING" for key, value in features.items() if not value))

    def analyze(self, snapshot: MarketSnapshot, strategy_family: str = "") -> MarketAnalysis:
        profile = self.PROFILES.get(snapshot.asset_class, self.PROFILES["forex"])
        h4 = self._htf(snapshot.frames.get("H4"), "H4", None, profile.htf_min_bars)
        expected = h4.side if h4.side in SIDES else None
        h1 = self._htf(snapshot.frames.get("H1"), "H1", expected, profile.htf_min_bars)
        m15 = self._m15(snapshot.frames.get("M15"), expected or "BUY", snapshot.asset_class, profile.m15_min_bars)
        m5 = self._m5(snapshot.frames.get("M5"), expected or "BUY", snapshot.asset_class, profile.m5_min_bars)
        stop_distance = max(self._atr(snapshot.frames["M5"]) * profile.stop_atr, 1e-12)
        return MarketAnalysis(snapshot.symbol, snapshot.asset_class, expected, h4, h1, m15, m5,
            stop_distance, profile.target_r, strategy_family or snapshot.asset_class, utc_now(),
            {"current_price": snapshot.tick.mid, "vwap": m5.metrics.get("vwap", 0.0), "rsi": m5.metrics.get("rsi", 50.0), "adx_proxy": h1.metrics.get("adx_proxy", 0.0)})
