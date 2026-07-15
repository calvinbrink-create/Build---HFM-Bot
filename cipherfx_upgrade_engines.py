"""Real-data, shadow-only intelligence engines for the CipherFX upgrade."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics

try:
    from mt5_systematic_engine import RegimeEngine as _SystematicRegime
    from mt5_systematic_engine import SessionManager as _SystematicSession
    from mt5_systematic_engine import WalkForwardValidator as _WalkForward
except Exception:
    _SystematicRegime = _SystematicSession = _WalkForward = None

try:
    import strategy_architecture_v1 as _strategy_v1
except Exception:
    _strategy_v1 = None


UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _num(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def _candle_dict(candle):
    return {
        "timestamp": str(getattr(candle, "timestamp", "")),
        "open": _num(getattr(candle, "open", 0.0)),
        "high": _num(getattr(candle, "high", 0.0)),
        "low": _num(getattr(candle, "low", 0.0)),
        "close": _num(getattr(candle, "close", 0.0)),
        "volume": _num(getattr(candle, "volume", 0.0)),
    }


def _candles(frames, label):
    frame = (frames or {}).get(label)
    return list(getattr(frame, "candles", ()) or ())


def _last(frames, label):
    values = _candles(frames, label)
    return values[-1] if values else None


def _range(values):
    if not values:
        return 0.0, 0.0
    return min(_num(getattr(x, "low", 0.0)) for x in values), max(_num(getattr(x, "high", 0.0)) for x in values)


class NextSessionPlanningEngine:
    def create(self, symbol, asset_class, frames, sig):
        h4 = _candles(frames, "H4")
        h1 = _candles(frames, "H1")
        m15 = _candles(frames, "M15")
        minimum = min(len(h4), len(h1), len(m15))
        if minimum < 3:
            return {
                "status": "DATA_MISSING",
                "reason": "H4/H1/M15 completed-candle evidence incomplete",
                "data_quality": {"H4": len(h4), "H1": len(h1), "M15": len(m15), "minimum_required": 3},
            }

        def inferred_context(candles, fallback):
            closes = [_num(getattr(candle, "close", 0.0)) for candle in candles[-20:]]
            closes = [value for value in closes if value > 0]
            if len(closes) < 3:
                return str(fallback or "UNCLASSIFIED").upper(), 55.0
            mean_close = sum(closes) / len(closes)
            previous_close = closes[-2]
            last_close = closes[-1]
            if last_close > mean_close and last_close >= previous_close:
                return "BUY", 100.0
            if last_close < mean_close and last_close <= previous_close:
                return "SELL", 0.0
            return "FLAT", 50.0

        side = str(sig.get("direction") or "").upper()
        h4_inferred, h4_inferred_score = inferred_context(h4, sig.get("h4_bias"))
        h1_inferred, h1_inferred_score = inferred_context(h1, sig.get("h1_bias"))
        m15_inferred, m15_inferred_score = inferred_context(m15, sig.get("m15_bias"))
        h4_bias = str(sig.get("h4_bias") or h4_inferred).upper()
        h1_bias = str(sig.get("h1_bias") or h1_inferred).upper()
        m15_bias = str(sig.get("m15_bias") or m15_inferred).upper()
        h4_score = _num(sig.get("h4_score"), h4_inferred_score)
        h1_score = _num(sig.get("h1_score"), h1_inferred_score)
        m15_score = _num(sig.get("m15_score") or sig.get("setup_15m_score"), m15_inferred_score)
        alignment = "ALIGNED" if side and side == h4_bias == h1_bias and m15_bias in {side, "FLAT", "UNCLASSIFIED", ""} else "MIXED"
        h4_low, h4_high = _range(h4[-20:])
        h1_low, h1_high = _range(h1[-20:])
        m15_low, m15_high = _range(m15[-20:])
        latest_ids = {
            label: str(getattr(candles[-1], "timestamp", ""))
            for label, candles in (("H4", h4), ("H1", h1), ("M15", m15))
        }
        plan_seed = "|".join((str(symbol), str(asset_class), side, *latest_ids.values()))
        plan_id = "plan_" + hashlib.sha256(plan_seed.encode("utf-8")).hexdigest()[:16]
        contradictions = []
        if h4_bias not in {"", "FLAT", side}:
            contradictions.append(f"H4 opposes {side or 'UNSPECIFIED'}")
        if h1_bias not in {"", "FLAT", side}:
            contradictions.append(f"H1 opposes {side or 'UNSPECIFIED'}")
        if m15_bias not in {"", "FLAT", side}:
            contradictions.append(f"M15 opposes {side or 'UNSPECIFIED'}")
        return {
            "plan_id": plan_id,
            "version": 1,
            "symbol": symbol,
            "asset_class": asset_class,
            "created_at": _now().isoformat(),
            "status": "READY",
            "plan_scope": "NEXT_ELIGIBLE_SESSION",
            "session": str(sig.get("session") or "UNKNOWN"),
            "alignment": alignment,
            "weekly_bias": h4_bias or "UNCLASSIFIED",
            "daily_bias": h4_bias or "UNCLASSIFIED",
            "h4_bias": h4_bias or "UNCLASSIFIED",
            "h4_score": h4_score,
            "h1_bias": h1_bias or "UNCLASSIFIED",
            "h1_score": h1_score,
            "m15_bias": m15_bias or "UNCLASSIFIED",
            "m15_score": m15_score,
            "previous_range": {"low": h4_low, "high": h4_high},
            "h1_range": {"low": h1_low, "high": h1_high},
            "m15_range": {"low": m15_low, "high": m15_high},
            "entry_zone": {"low": m15_low, "high": m15_high},
            "invalidation": h1_low if side == "BUY" else h1_high if side == "SELL" else None,
            "required_conditions": {
                "htf_alignment": True,
                "m5_trigger_side": side or "UNSPECIFIED",
                "m1_confirmation": True,
            },
            "confidence": 80.0 if alignment == "ALIGNED" else 45.0,
            "data_quality": {"H4": len(h4), "H1": len(h1), "M15": len(m15), "latest_candle_ids": latest_ids},
            "evidence": ["live H4/H1/M15 completed candles", "current strategy context"],
            "contradictions": contradictions or (["higher-timeframe stack is mixed"] if alignment != "ALIGNED" else ["none"]),
        }


class MarketRegimeEngine:
    def classify(self, sig, frames):
        adx = _num(sig.get("adx"))
        atr = _num(sig.get("atr"))
        price = _num(sig.get("price"))
        vwap_z = abs(_num(sig.get("vwap_z")))
        feature_source = "signal_payload"
        m15_frame = (frames or {}).get("M15")
        if m15_frame is not None and _strategy_v1 is not None:
            try:
                if adx <= 0:
                    adx_value = _strategy_v1.adx_proxy(m15_frame)
                    if math.isfinite(float(adx_value)):
                        adx = float(adx_value)
                if atr <= 0:
                    atr_value = _strategy_v1.atr(m15_frame)
                    if math.isfinite(float(atr_value)):
                        atr = float(atr_value)
                if price <= 0 and getattr(m15_frame, "candles", None):
                    price = _num(m15_frame.candles[-1].close)
                if vwap_z <= 0 and price > 0:
                    vwap_value = _strategy_v1.vwap_proxy(m15_frame)
                    closes = [float(candle.close) for candle in m15_frame.candles[-30:]]
                    deviation = statistics.pstdev(closes) if len(closes) >= 2 else 0.0
                    if math.isfinite(float(vwap_value)) and deviation > 0:
                        vwap_z = abs((price - float(vwap_value)) / deviation)
                feature_source = "completed_M15_frame_fallback"
            except Exception:
                feature_source = "signal_payload"
        regime_sig = dict(sig or {})
        regime_sig.update({"adx": adx, "atr": atr, "price": price, "vwap_z": vwap_z})
        h4 = str(sig.get("h4_bias") or "")
        h1 = str(sig.get("h1_bias") or "")
        side = str(sig.get("direction") or "")
        if _SystematicRegime is not None:
            try:
                result = _SystematicRegime().detect(regime_sig, str(sig.get("market") or ""))
            except Exception:
                result = {}
        else:
            result = {}
        if not result:
            ratio = atr / price if price else 0.0
            regime = "HIGH_VOLATILITY" if adx >= 38 or ratio > 0.0045 else "TREND" if adx >= 25 else "MEAN_REVERSION" if vwap_z >= 1.8 else "CHOP" if adx < 18 else "UNCLASSIFIED"
            result = {"regime": regime, "reason": "live ADX/ATR/VWAP features"}
        result["h4_h1_alignment"] = bool(side and side == h4 == h1)
        result["supporting_features"] = {"adx": adx, "atr": atr, "price": price, "vwap_z": vwap_z, "source": feature_source}
        result["contradictions"] = [] if result["h4_h1_alignment"] else ["H4/H1 not aligned"]
        return result


class StrategyRouter:
    def route(self, asset_class, regime, sig):
        regime_name = str(regime.get("regime") or "UNCLASSIFIED").upper()
        side = str(sig.get("direction") or "").upper()
        if asset_class == "index":
            families = ["OPENING_DRIVE", "GAP_AND_GO", "GAP_FILL", "TREND_PULLBACK"] if regime_name in {"TREND", "BREAKOUT", "HIGH_VOLATILITY"} else ["LIQUIDITY_REVERSAL", "RANGE_REJECTION"]
        elif asset_class == "metal":
            families = ["SESSION_SWEEP", "TREND_PULLBACK", "VWAP_CONTINUATION"] if regime_name in {"TREND", "BREAKOUT"} else ["LIQUIDITY_REVERSAL", "MEAN_REVERSION"]
        else:
            families = ["TREND_PULLBACK", "VWAP_CONTINUATION"] if regime_name in {"TREND", "BREAKOUT"} else ["MEAN_REVERSION", "LIQUIDITY_REVERSAL"]
        return {"status": "ROUTED", "asset_class": asset_class, "regime": regime_name, "side": side, "permitted_strategies": families, "discouraged_strategies": []}


class LiquidityIntelligenceEngine:
    def detect(self, frames):
        m5 = _candles(frames, "M5")
        if len(m5) < 10:
            return {"status": "DATA_MISSING", "reason": "M5 liquidity history incomplete"}
        recent = m5[-100:]
        low, high = _range(recent)
        last = _candle_dict(recent[-1])
        levels = [
            {"type": "recent_external_high", "price": high, "timeframe": "M5", "touch_count": sum(1 for x in recent if _num(getattr(x, "high", 0)) >= high * 0.9999)},
            {"type": "recent_external_low", "price": low, "timeframe": "M5", "touch_count": sum(1 for x in recent if _num(getattr(x, "low", 0)) <= low * 1.0001)},
        ]
        return {"status": "READY", "levels": levels, "last_candle": last, "target_liquidity": "recent_external_high_or_low"}


class SessionProfileEngine:
    def __init__(self, repo):
        self.repo = repo

    def profile(self, symbol, session):
        rows = self.repo.query("SELECT realized,result_r,outcome FROM trades WHERE sym=? AND session=? AND closed_at IS NOT NULL", (symbol, session))
        sample = len(rows)
        wins = [r for r in rows if _num(r.get("realized")) > 0]
        return {
            "symbol": symbol,
            "session": session or "unknown",
            "sample_size": sample,
            "win_rate": round(len(wins) / sample, 4) if sample else None,
            "expectancy_r": round(statistics.mean([_num(r.get("result_r")) for r in rows]), 4) if rows else None,
            "confidence": "INSUFFICIENT_SAMPLE" if sample < 30 else "OBSERVABLE",
            "last_updated": _now().isoformat(),
        }


class GapAndOpeningRangeEngine:
    def detect(self, asset_class, frames):
        m5 = _candles(frames, "M5")
        if asset_class != "index" or len(m5) < 12:
            return {"status": "NOT_APPLICABLE" if asset_class != "index" else "DATA_MISSING"}
        today = _now().date().isoformat()
        today_bars = [x for x in m5 if str(getattr(x, "timestamp", "")).startswith(today)]
        if len(today_bars) < 2:
            return {"status": "DATA_MISSING", "reason": "current-day opening range incomplete"}
        opening = today_bars[:6]
        low, high = _range(opening)
        prior = m5[-min(len(m5), 30):-6]
        prior_close = _num(getattr(prior[-1], "close", 0.0)) if prior else 0.0
        gap = _num(getattr(opening[0], "open", 0.0)) - prior_close if prior_close else None
        return {"status": "READY", "gap": gap, "opening_range": {"low": low, "high": high, "bars": len(opening)}, "classification": "NO_EDGE" if gap is None else "GAP_AND_GO_CANDIDATE"}


class NewsRiskEngine:
    def current(self, symbol):
        path = Path(os.getenv("MT5_NEWS_EVENTS_FILE", "/opt/cipherfx_mt5/state/news_events.json"))
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:
            return {"status": "NEWS_DATA_UNAVAILABLE", "symbol": symbol, "reason": str(exc)[:160], "hard_block": False}
        events = payload.get("events", payload) if isinstance(payload, dict) else payload
        matches = [x for x in events if isinstance(x, dict) and (not x.get("symbol") or str(x.get("symbol")).upper() == symbol.upper())]
        return {"status": "READY", "symbol": symbol, "events": matches[:20], "hard_block": False}


class CostAndExecutionForecastEngine:
    def forecast(self, sig, route):
        price = _num(sig.get("price"))
        atr = _num(sig.get("atr"))
        spread = _num(sig.get("spread_signal") or sig.get("spread"))
        target_r = _num(sig.get("target_r") or 1.8, 1.8)
        expected_target = price + atr * target_r if str(sig.get("direction")).upper() == "BUY" else price - atr * target_r
        target_distance = abs(expected_target - price)
        cost = spread / target_distance if target_distance else None
        return {"status": "READY" if cost is not None else "UNAVAILABLE", "expected_spread": spread, "expected_target_distance": target_distance, "expected_cost_as_percent_of_target": round(cost * 100.0, 4) if cost is not None else None, "warning_codes": ["COST_DATA_UNAVAILABLE"] if cost is None else []}


class SetupQualityEngine:
    def score(self, sig, regime, route, plan, liquidity, news, cost):
        base = _num(sig.get("score"))
        adjustment = 0.0
        reasons = []
        if regime.get("h4_h1_alignment"):
            adjustment += 5.0
        else:
            adjustment -= 5.0
            reasons.append("HTF alignment mixed")
        if plan.get("status") == "READY" and plan.get("alignment") == "ALIGNED":
            adjustment += 4.0
        if liquidity.get("status") != "READY":
            reasons.append("liquidity evidence incomplete")
        if news.get("status") == "NEWS_DATA_UNAVAILABLE":
            reasons.append("NEWS_DATA_UNAVAILABLE")
        if cost.get("expected_cost_as_percent_of_target") is not None and cost["expected_cost_as_percent_of_target"] > 30:
            adjustment -= 5.0
            reasons.append("cost quality warning")
        return {"score_before": base, "score_adjustment": adjustment, "score_after": max(0.0, min(100.0, base + adjustment)), "reasons": reasons, "decision_owner": "TradePermissionGovernor", "hard_block": False}


class PortfolioExposureEngine:
    def __init__(self, gateway):
        self.gateway = gateway

    def snapshot(self):
        try:
            positions = list(self.gateway.positions())
        except Exception as exc:
            return {"status": "BROKER_DATA_UNAVAILABLE", "reason": str(exc)[:160]}
        by_symbol = Counter(str(getattr(x, "symbol", "")).upper() for x in positions)
        by_direction = Counter(str(getattr(x, "direction", "")).upper() for x in positions)
        return {"status": "READY", "open_positions": len(positions), "by_symbol": dict(by_symbol), "by_direction": dict(by_direction)}


class TradeOutcomeIntelligenceEngine:
    def __init__(self, repo):
        self.repo = repo

    def summary(self, symbol=""):
        sql = "SELECT * FROM trades WHERE closed_at IS NOT NULL"
        params = ()
        if symbol:
            sql += " AND sym=?"
            params = (symbol,)
        rows = self.repo.query(sql, params)
        results = [_num(r.get("result_r")) for r in rows]
        excursion_sql = "SELECT COUNT(*) sample FROM trade_excursions WHERE status='CAPTURED'"
        excursion_params = ()
        if symbol:
            excursion_sql += " AND symbol=?"
            excursion_params = (symbol,)
        excursion_rows = self.repo.query(excursion_sql, excursion_params)
        excursion_sample = int((excursion_rows[0] or {}).get("sample") or 0) if excursion_rows else 0
        return {
            "symbol": symbol,
            "sample_size": len(results),
            "mfe_mae_status": "CAPTURED" if excursion_sample else ("NOT_CAPTURED" if rows else "NO_CLOSED_TRADES"),
            "excursion_sample_size": excursion_sample,
            "expectancy_r": round(statistics.mean(results), 4) if results else None,
            "wins": sum(1 for x in results if x > 0),
            "losses": sum(1 for x in results if x < 0),
        }


class StrategyPerformanceAttributionEngine:
    """Closed-trade attribution for reporting only; never a live permission gate."""

    def __init__(self, repo):
        self.repo = repo

    @staticmethod
    def _engine_from_trade(row: dict) -> str:
        breakdown = row.get("score_breakdown")
        if isinstance(breakdown, str):
            try:
                breakdown = json.loads(breakdown)
            except Exception:
                breakdown = {}
        if isinstance(breakdown, dict):
            for key in ("engine", "engine_name", "decision_engine"):
                if breakdown.get(key):
                    return str(breakdown[key])
        return str(row.get("engine") or row.get("sig_mode") or "unknown")

    def summary(self):
        source = self.repo.query(
            "SELECT sym,strategy,session,population,direction,result_r,realized,score_breakdown "
            "FROM trades WHERE closed_at IS NOT NULL ORDER BY datetime(closed_at)"
        )
        buckets = {}
        for row in source:
            row = dict(row)
            key = (
                str(row.get("sym") or ""),
                self._engine_from_trade(row),
                str(row.get("strategy") or ""),
                str(row.get("session") or "unknown"),
                str(row.get("population") or "direct_strategy"),
                str(row.get("direction") or ""),
            )
            item = buckets.setdefault(key, {
                "symbol": key[0],
                "engine": key[1],
                "strategy": key[2],
                "session": key[3],
                "population": key[4],
                "direction": key[5],
                "sample": 0,
                "wins": 0,
                "losses": 0,
                "breakevens": 0,
                "sum_result_r": 0.0,
                "sum_realized": 0.0,
            })
            result_r = _num(row.get("result_r"))
            realized = _num(row.get("realized"))
            item["sample"] += 1
            item["sum_result_r"] += result_r
            item["sum_realized"] += realized
            if result_r > 0:
                item["wins"] += 1
            elif result_r < 0:
                item["losses"] += 1
            else:
                item["breakevens"] += 1
        result_rows = []
        for item in buckets.values():
            sample = int(item["sample"])
            item["sum_result_r"] = round(item["sum_result_r"], 6)
            item["sum_realized"] = round(item["sum_realized"], 2)
            item["expectancy_r"] = round(item["sum_result_r"] / sample, 6) if sample else None
            item["win_rate"] = round(item["wins"] / sample, 4) if sample else None
            result_rows.append(item)
        result_rows.sort(key=lambda item: (item["symbol"], item["engine"], item["strategy"], item["session"], item["population"]))
        return {
            "status": "READY" if source else "NO_CLOSED_TRADES",
            "rows": result_rows,
            "sample_size": len(source),
            "bucket_count": len(result_rows),
            "dimensions": ["symbol", "engine", "strategy", "session", "population", "direction"],
        }

    def persist_summary(self, summary: dict) -> int:
        """Persist idempotent attribution buckets as shadow evidence."""
        inserted = 0
        for row in summary.get("rows") or []:
            event_id = "attribution:" + hashlib.sha256(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if self.repo.query("SELECT 1 FROM trade_attribution WHERE event_id=? LIMIT 1", (event_id,)):
                continue
            self.repo.persist(
                "trade_attribution",
                symbol=str(row.get("symbol") or ""),
                setup_id="",
                status="READY",
                reason="CLOSED_TRADE_ATTRIBUTION",
                payload=row,
                event_id=event_id,
            )
            inserted += 1
        return inserted


class MissedTradeIntelligenceEngine:
    """Records rejected candidates for later forward-outcome replay only."""

    def __init__(self, repo):
        self.repo = repo

    def evaluate(self, sig, reason):
        sig = dict(sig or {})
        symbol = str(sig.get("symbol") or "")
        setup_id = str(sig.get("decision_trace_id") or sig.get("setup_id") or "")
        reason_text = str(reason or "NOT_QUALIFIED")
        event_id = "missed:" + hashlib.sha256(
            f"{symbol}|{setup_id}|{reason_text}".encode("utf-8")
        ).hexdigest()
        if self.repo.query("SELECT 1 FROM missed_trades WHERE event_id=? LIMIT 1", (event_id,)):
            return {"status": "DUPLICATE", "symbol": symbol, "side": sig.get("direction"), "reason": reason_text, "future_outcome": "PENDING_REPLAY", "event_id": event_id}
        payload = {
            "signal_time": sig.get("generated_at") or sig.get("m5_candle_closed_at"),
            "side": sig.get("direction"),
            "engine": sig.get("engine"),
            "strategy": sig.get("strategy"),
            "score": sig.get("score"),
            "score_components": sig.get("score_components") or sig.get("score_breakdown"),
            "m5_trigger_candle_id": sig.get("m5_trigger_candle_id"),
            "final_status": sig.get("final_status") or sig.get("state"),
            "mode": "OBSERVE_ONLY",
            "forward_outcome": "PENDING_REPLAY",
        }
        self.repo.persist(
            "missed_trades",
            symbol=symbol,
            setup_id=setup_id,
            status="PENDING_REPLAY",
            reason=reason_text,
            payload=payload,
            event_id=event_id,
        )
        return {"status": "RECORDED", "symbol": symbol, "side": sig.get("direction"), "reason": reason_text, "future_outcome": "PENDING_REPLAY", "event_id": event_id}

    def summary(self):
        rows = self.repo.query("SELECT status,COUNT(*) count FROM missed_trades GROUP BY status")
        return {"status": "READY", "rows": rows, "sample_size": sum(int(row.get("count") or 0) for row in rows)}


class FalseEntryIntelligenceEngine:
    """Loss review evidence; a loss is not labelled a false entry without replay proof."""

    def __init__(self, repo):
        self.repo = repo

    @staticmethod
    def _engine_from_trade(row: dict) -> str:
        breakdown = row.get("score_breakdown")
        if isinstance(breakdown, str):
            try:
                breakdown = json.loads(breakdown)
            except Exception:
                breakdown = {}
        if isinstance(breakdown, dict) and breakdown.get("engine"):
            return str(breakdown["engine"])
        return str(row.get("sig_mode") or "unknown")

    def summary(self):
        source = self.repo.query(
            "SELECT id,trade_id,sym,strategy,session,population,direction,result_r,realized,exit_reason,score_breakdown "
            "FROM trades WHERE closed_at IS NOT NULL AND COALESCE(result_r,0)<0 ORDER BY datetime(closed_at)"
        )
        buckets = {}
        for row in source:
            row = dict(row)
            key = (
                str(row.get("sym") or ""),
                self._engine_from_trade(row),
                str(row.get("strategy") or ""),
                str(row.get("session") or "unknown"),
                str(row.get("population") or "direct_strategy"),
            )
            item = buckets.setdefault(key, {
                "symbol": key[0],
                "engine": key[1],
                "strategy": key[2],
                "session": key[3],
                "population": key[4],
                "loss_sample": 0,
                "sum_result_r": 0.0,
                "sum_realized": 0.0,
                "causal_status": "UNPROVEN",
            })
            item["loss_sample"] += 1
            item["sum_result_r"] += _num(row.get("result_r"))
            item["sum_realized"] += _num(row.get("realized"))
        result_rows = []
        for item in buckets.values():
            item["sum_result_r"] = round(item["sum_result_r"], 6)
            item["sum_realized"] = round(item["sum_realized"], 2)
            result_rows.append(item)
        result_rows.sort(key=lambda item: (item["symbol"], item["engine"], item["strategy"], item["session"], item["population"]))
        return {
            "status": "READY" if source else "NO_LOSSES",
            "loss_buckets": result_rows,
            "sample_size": len(source),
            "causality": "UNPROVEN_UNTIL_REPLAY",
        }

    def persist_summary(self, summary: dict) -> int:
        inserted = 0
        for row in summary.get("loss_buckets") or []:
            event_id = "false-entry-review:" + hashlib.sha256(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if self.repo.query("SELECT 1 FROM false_entries WHERE event_id=? LIMIT 1", (event_id,)):
                continue
            self.repo.persist(
                "false_entries",
                symbol=str(row.get("symbol") or ""),
                setup_id="",
                status="LOSS_REVIEW",
                reason="CAUSALITY_UNPROVEN",
                payload=row,
                event_id=event_id,
            )
            inserted += 1
        return inserted


class WalkForwardValidationEngine:
    """Chronological walk-forward validation; shadow evidence only."""

    def __init__(self, repo):
        self.repo = repo

    def evaluate(self):
        rows = self.repo.query(
            "SELECT result_r FROM trades WHERE closed_at IS NOT NULL ORDER BY datetime(closed_at),id"
        )
        values = [_num(row.get("result_r")) for row in rows]
        if len(values) < 20:
            return {
                "status": "INSUFFICIENT_SAMPLE",
                "sample_size": len(values),
                "folds": [],
                "leakage_guard": "CHRONOLOGICAL_NO_SHUFFLE",
            }
        test_size = max(3, min(10, len(values) // 5))
        min_train = max(10, len(values) // 2)
        folds = []
        start = min_train
        fold_id = 1
        while start < len(values) and fold_id <= 5:
            test = values[start:start + test_size]
            if len(test) < 3:
                break
            train = values[:start]
            train_avg = statistics.mean(train)
            test_avg = statistics.mean(test)
            degradation = (
                ((train_avg - test_avg) / abs(train_avg) * 100.0)
                if train_avg else None
            )
            folds.append({
                "fold": fold_id,
                "train_end_index": start,
                "train_sample": len(train),
                "test_sample": len(test),
                "train_expectancy_r": round(train_avg, 6),
                "test_expectancy_r": round(test_avg, 6),
                "degradation_pct": round(degradation, 4) if degradation is not None else None,
                "overfit": bool(degradation is not None and train_avg > 0 and degradation > 40),
            })
            start += test_size
            fold_id += 1
        overfit_folds = sum(1 for fold in folds if fold["overfit"])
        return {
            "status": "READY" if folds else "INSUFFICIENT_SAMPLE",
            "sample_size": len(values),
            "folds": folds,
            "fold_count": len(folds),
            "overfit_folds": overfit_folds,
            "overfit": bool(folds and overfit_folds >= max(1, len(folds) // 2)),
            "leakage_guard": "CHRONOLOGICAL_NO_SHUFFLE",
        }

    def persist_evaluation(self, result: dict) -> int:
        if result.get("status") != "READY":
            return 0
        signature = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        run_id = "walk-forward:" + signature
        if self.repo.query("SELECT 1 FROM walk_forward_runs WHERE event_id=? LIMIT 1", (run_id,)):
            return 0
        self.repo.persist(
            "walk_forward_runs",
            status="READY",
            reason="CHRONOLOGICAL_WALK_FORWARD",
            payload={k: v for k, v in result.items() if k != "folds"},
            event_id=run_id,
        )
        for fold in result.get("folds") or []:
            fold_id = f"{run_id}:fold:{fold['fold']}"
            if self.repo.query("SELECT 1 FROM walk_forward_results WHERE event_id=? LIMIT 1", (fold_id,)):
                continue
            self.repo.persist(
                "walk_forward_results",
                status="READY",
                reason="WALK_FORWARD_FOLD",
                payload={"run_id": run_id, **fold},
                event_id=fold_id,
            )
        return 1


class LiveBacktestDriftEngine:
    """Observe-only live performance drift detector using closed-trade R windows."""

    def __init__(self, repo):
        self.repo = repo

    def evaluate(self):
        rows = self.repo.query(
            "SELECT result_r FROM trades WHERE closed_at IS NOT NULL "
            "ORDER BY datetime(closed_at) DESC,id DESC LIMIT 100"
        )
        values = [_num(row.get("result_r")) for row in rows]
        if len(values) < 20:
            return {
                "status": "INSUFFICIENT_SAMPLE",
                "sample_size": len(values),
                "window_size": 0,
                "drift_status": "INCONCLUSIVE",
            }
        window_size = min(20, len(values) // 2)
        recent = values[:window_size]
        prior = values[window_size:window_size * 2]
        recent_avg = statistics.mean(recent)
        prior_avg = statistics.mean(prior)
        delta = recent_avg - prior_avg
        recent_win_rate = sum(1 for value in recent if value > 0) / len(recent)
        prior_win_rate = sum(1 for value in prior if value > 0) / len(prior)
        threshold = 0.25
        drift_status = "DRIFT_DETECTED" if delta <= -threshold else "STABLE"
        return {
            "status": "READY",
            "sample_size": len(values),
            "window_size": window_size,
            "recent_expectancy_r": round(recent_avg, 6),
            "prior_expectancy_r": round(prior_avg, 6),
            "expectancy_delta_r": round(delta, 6),
            "recent_win_rate": round(recent_win_rate, 4),
            "prior_win_rate": round(prior_win_rate, 4),
            "drift_threshold_r": threshold,
            "drift_status": drift_status,
            "mode": "OBSERVE_ONLY",
        }

    def persist_snapshot(self, result: dict) -> int:
        if result.get("status") != "READY":
            return 0
        event_id = "live-drift:" + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.repo.query("SELECT 1 FROM live_drift_snapshots WHERE event_id=? LIMIT 1", (event_id,)):
            return 0
        self.repo.persist(
            "live_drift_snapshots",
            status=str(result.get("drift_status") or "STABLE"),
            reason="CLOSED_TRADE_R_WINDOW_COMPARISON",
            payload=result,
            event_id=event_id,
        )
        return 1


class ShadowStrategyEngine:
    """Non-authoritative strategy evidence with deterministic setup identity."""

    def __init__(self, repo):
        self.repo = repo

    def record(self, symbol, setup_id, sig, quality, route):
        symbol = str(symbol or "")
        setup_id = str(setup_id or "")
        event_id = "shadow:" + hashlib.sha256(f"{symbol}|{setup_id}".encode("utf-8")).hexdigest()
        if self.repo.query("SELECT 1 FROM shadow_trades WHERE event_id=? LIMIT 1", (event_id,)):
            return {"status": "DUPLICATE", "event_id": event_id, "mode": "SHADOW_ONLY"}
        payload = {
            "sig": dict(sig or {}),
            "quality": dict(quality or {}),
            "route": dict(route or {}),
            "mode": "SHADOW_ONLY",
            "decision_owner": "SHADOW_OBSERVER",
            "applied_to_live_permission": False,
        }
        self.repo.persist(
            "shadow_trades",
            symbol=symbol,
            setup_id=setup_id,
            status="SHADOW_ONLY",
            reason="SHADOW_EVIDENCE_ONLY",
            payload=payload,
            event_id=event_id,
        )
        return {"status": "RECORDED", "event_id": event_id, "mode": "SHADOW_ONLY"}

    def summary(self):
        rows = self.repo.query("SELECT status,COUNT(*) count FROM shadow_trades GROUP BY status")
        return {
            "status": "READY",
            "rows": rows,
            "sample_size": sum(int(row.get("count") or 0) for row in rows),
            "live_permission": False,
        }


class TradeReplayEngine:
    """Read-only replay evidence builder; never submits or modifies a trade."""

    def __init__(self, repo):
        self.repo = repo

    def replay(self, trade_id):
        trade_id = str(trade_id or "")
        rows = self.repo.query("SELECT * FROM trades WHERE trade_id=?", (trade_id,))
        if not rows:
            return {"status": "NOT_FOUND", "trade_id": trade_id}
        row = dict(rows[0])
        symbol = str(row.get("sym") or "")
        candles = self.repo.query(
            "SELECT * FROM m1_candles WHERE sym=? ORDER BY ts",
            (symbol,),
        )
        replay_id = "replay:" + hashlib.sha256(trade_id.encode("utf-8")).hexdigest()
        result = {
            "status": "READY" if candles else "DATA_UNAVAILABLE",
            "trade_id": trade_id,
            "symbol": symbol,
            "candle_count": len(candles),
            "replay_id": replay_id,
            "mode": "READ_ONLY",
            "trade": row,
            "candles": candles,
        }
        return result

    def persist_replay(self, result: dict) -> int:
        if result.get("status") not in {"READY", "DATA_UNAVAILABLE"}:
            return 0
        replay_id = str(result.get("replay_id") or "")
        if not replay_id or self.repo.query("SELECT 1 FROM trade_replay_events WHERE event_id=? LIMIT 1", (replay_id,)):
            return 0
        payload = {k: v for k, v in result.items() if k not in {"trade", "candles"}}
        self.repo.persist(
            "trade_replay_events",
            symbol=str(result.get("symbol") or ""),
            setup_id=str(result.get("trade_id") or ""),
            status=str(result.get("status") or ""),
            reason="READ_ONLY_REPLAY",
            payload=payload,
            event_id=replay_id,
        )
        return 1


class StagedDeploymentValidator:
    """Deployment-stage evidence; never promotes demo to live automatically."""

    def __init__(self, repo):
        self.repo = repo

    @staticmethod
    def check_environment() -> dict:
        trade_mode = str(os.getenv("MT5_TRADE_MODE") or "").lower()
        execution_mode = str(os.getenv("MT5_EXECUTION_MODE") or "").lower()
        dry_run = str(os.getenv("MT5_DRY_RUN") or "").lower() in {"1", "true", "yes", "on"}
        replacement_live = str(os.getenv("MT5_REPLACEMENT_STRATEGY_V1_LIVE") or "").lower() in {"1", "true", "yes", "on"}
        live_approval = str(os.getenv("CIPHERFX_LIVE_APPROVAL") or "").lower() in {"1", "true", "yes", "on"}
        if live_approval and trade_mode in {"live", "real"}:
            stage = "LIVE_APPROVED"
        elif trade_mode in {"demo", "test", "paper"} or dry_run:
            stage = "DEMO_BRIDGE" if not dry_run else "DRY_RUN"
        else:
            stage = "UNCLASSIFIED"
        status = "PASS" if stage in {"DEMO_BRIDGE", "DRY_RUN", "LIVE_APPROVED"} and replacement_live else "FAIL"
        return {
            "status": status,
            "stage": stage,
            "trade_mode": trade_mode,
            "execution_mode": execution_mode,
            "dry_run": dry_run,
            "replacement_strategy_live": replacement_live,
            "live_approval": live_approval,
            "approval_boundary": "explicit_CIPHERFX_LIVE_APPROVAL_required",
        }

    def check(self) -> dict:
        return self.check_environment()

    def persist_check(self, result: dict) -> int:
        event_id = "deployment:" + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.repo.query("SELECT 1 FROM deployment_versions WHERE event_id=? LIMIT 1", (event_id,)):
            return 0
        self.repo.persist(
            "deployment_versions",
            status=str(result.get("status") or "FAIL"),
            reason="STAGED_DEPLOYMENT_VALIDATION",
            payload=result,
            event_id=event_id,
        )
        return 1


class NotificationOutbox:
    """Deduplicated notification outbox; no external send and no trade gating."""

    def __init__(self, repo):
        self.repo = repo

    def emit(self, event: str, severity: str, payload: dict | None = None) -> dict:
        payload = dict(payload or {})
        fingerprint = hashlib.sha256(
            json.dumps({"event": event, "severity": severity, "payload": payload},
                       sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        event_id = "notification:" + fingerprint
        if self.repo.query("SELECT 1 FROM notifications WHERE event_id=? LIMIT 1", (event_id,)):
            return {"status": "DUPLICATE", "event_id": event_id, "delivery": "OUTBOX_ONLY"}
        self.repo.persist(
            "notifications",
            status="QUEUED",
            reason=str(event or "AUDIT_EVENT"),
            payload={
                "event": str(event or ""),
                "severity": str(severity or "INFO"),
                "delivery": "OUTBOX_ONLY",
                "sent": False,
                **payload,
            },
            event_id=event_id,
        )
        return {"status": "QUEUED", "event_id": event_id, "delivery": "OUTBOX_ONLY"}

    def summary(self):
        rows = self.repo.query("SELECT status,COUNT(*) count FROM notifications GROUP BY status")
        return {
            "status": "READY",
            "rows": rows,
            "sample_size": sum(int(row.get("count") or 0) for row in rows),
            "delivery": "OUTBOX_ONLY",
        }


class DatabaseIntegrityEngine:
    """Read-only SQLite integrity and required-table audit."""

    def __init__(self, repo):
        self.repo = repo

    def check(self):
        try:
            quick_rows = self.repo.query("PRAGMA quick_check")
            journal_rows = self.repo.query("PRAGMA journal_mode")
            table_rows = self.repo.query("SELECT name FROM sqlite_master WHERE type='table'")
            quick_value = str((quick_rows[0] or {}).get("quick_check") or "") if quick_rows else ""
            journal_value = str((journal_rows[0] or {}).get("journal_mode") or "") if journal_rows else ""
            present = {str(row.get("name") or "") for row in table_rows}
            required = {
                "trades",
                "positions",
                "m1_candles",
                "trade_permission_decisions",
                "trade_outcomes",
                "trade_attribution",
                "database_integrity_events",
            }
            missing = sorted(required - present)
            status = "PASS" if quick_value.lower() == "ok" and not missing else "FAIL"
            return {
                "status": status,
                "quick_check": quick_value,
                "journal_mode": journal_value,
                "missing_tables": missing,
                "table_count": len(present),
                "mode": "OBSERVE_ONLY",
            }
        except Exception as exc:
            return {"status": "FAIL", "reason": str(exc)[:180], "mode": "OBSERVE_ONLY"}

    def persist_check(self, result: dict) -> int:
        event_id = "database-integrity:" + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.repo.query("SELECT 1 FROM database_integrity_events WHERE event_id=? LIMIT 1", (event_id,)):
            return 0
        self.repo.persist(
            "database_integrity_events",
            status=str(result.get("status") or "FAIL"),
            reason="SQLITE_INTEGRITY_AUDIT",
            payload=result,
            event_id=event_id,
        )
        return 1


class ReliabilityAndKillSwitchController:
    """Runtime health evidence and explicit kill-switch state history."""

    def __init__(self, repo):
        self.repo = repo

    @staticmethod
    def _kill_switch_enabled() -> bool:
        values = (
            os.getenv("MT5_KILL_SWITCH"),
            os.getenv("CIPHERFX_KILL_SWITCH"),
            os.getenv("MT5_EMERGENCY_HALT"),
        )
        return any(str(value or "").strip().lower() in {"1", "true", "yes", "on", "active"} for value in values)

    def check(self):
        try:
            self.repo.query("SELECT 1")
            db = "PASS"
        except Exception as exc:
            db = f"FAIL:{exc}"
        usage = shutil.disk_usage("/opt/cipherfx_mt5")
        disk_free_gb = usage.free / (1024 ** 3)
        kill_switch = self._kill_switch_enabled()
        if kill_switch:
            status = "KILL_SWITCH_ACTIVE"
        elif db != "PASS" or disk_free_gb <= 1:
            status = "DEGRADED"
        else:
            status = "NORMAL"
        return {
            "status": status,
            "database": db,
            "disk_free_gb": round(disk_free_gb, 3),
            "kill_switch": kill_switch,
            "mode": "HARD_SAFETY" if kill_switch else "OBSERVE_ONLY",
        }

    def persist_check(self, result: dict) -> int:
        event_id = "kill-switch:" + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.repo.query("SELECT 1 FROM kill_switch_events WHERE event_id=? LIMIT 1", (event_id,)):
            return 0
        self.repo.persist(
            "kill_switch_events",
            status=str(result.get("status") or "UNKNOWN"),
            reason="RUNTIME_RELIABILITY_CHECK",
            payload=result,
            event_id=event_id,
        )
        return 1


class BrokerReconciliationEngine:
    """Read-only broker-versus-state reconciliation with explicit discrepancies."""

    def __init__(self, repo, gateway):
        self.repo = repo
        self.gateway = gateway

    def check(self):
        try:
            broker = {}
            for position in self.gateway.positions():
                symbol = str(getattr(position, "symbol", "")).upper()
                broker[symbol] = broker.get(symbol, 0.0) + _num(getattr(position, "volume", 0.0))
        except Exception as exc:
            return {"status": "BROKER_DATA_UNAVAILABLE", "reason": str(exc)[:160], "mode": "OBSERVE_ONLY"}
        bot_rows = self.repo.query("SELECT sym,SUM(qty) qty FROM positions GROUP BY sym")
        bot = {str(row.get("sym") or "").upper(): _num(row.get("qty")) for row in bot_rows}
        symbols = sorted(set(broker) | set(bot))
        discrepancies = []
        for symbol in symbols:
            broker_qty = round(_num(broker.get(symbol)), 8)
            bot_qty = round(_num(bot.get(symbol)), 8)
            if abs(broker_qty - bot_qty) > 1e-8:
                discrepancies.append({
                    "symbol": symbol,
                    "broker_qty": broker_qty,
                    "state_qty": bot_qty,
                    "delta": round(broker_qty - bot_qty, 8),
                })
        return {
            "status": "PASS" if not discrepancies else "MISMATCH_OBSERVE_ONLY",
            "broker": broker,
            "bot": bot,
            "discrepancies": discrepancies,
            "mode": "OBSERVE_ONLY",
        }

    def persist_check(self, result: dict) -> int:
        event_id = "reconciliation:" + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.repo.query("SELECT 1 FROM reconciliation_events WHERE event_id=? LIMIT 1", (event_id,)):
            return 0
        self.repo.persist(
            "reconciliation_events",
            status=str(result.get("status") or "UNKNOWN"),
            reason="BROKER_VERSUS_STATE",
            payload=result,
            event_id=event_id,
        )
        return 1


class ConfigurationIntegrityEngine:
    """Persistent configuration checksum history; reporting only."""

    def __init__(self, repo, coordinator):
        self.repo = repo
        self.coordinator = coordinator

    def check(self):
        checksum = self.coordinator.configuration_checksum()
        previous_rows = self.repo.query(
            "SELECT payload_json FROM configuration_versions ORDER BY id DESC LIMIT 1"
        )
        previous_checksum = ""
        if previous_rows:
            payload = previous_rows[0].get("payload_json")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            if isinstance(payload, dict):
                previous_checksum = str(payload.get("checksum") or "")
        changed = bool(previous_checksum and previous_checksum != checksum)
        result = {
            "status": "CHANGED" if changed else "PASS",
            "checksum": checksum,
            "previous_checksum": previous_checksum or None,
            "changed": changed,
            "source": "environment_and_runtime_prefixes",
            "mode": "OBSERVE_ONLY",
        }
        event_id = "configuration:" + checksum
        if not self.repo.query("SELECT 1 FROM configuration_versions WHERE event_id=? LIMIT 1", (event_id,)):
            self.coordinator.persist(
                "configuration_versions",
                status=result["status"],
                reason="RUNTIME_CONFIGURATION_CHECKSUM",
                payload=result,
                event_id=event_id,
            )
        return result


class TradeFlowHealthMonitor:
    def __init__(self, repo):
        self.repo = repo

    def snapshot(self):
        rows = self.repo.query("SELECT status,COUNT(*) count FROM trade_permission_decisions GROUP BY status")
        result = {"status": "READY", "counts": rows, "created_at": _now().isoformat()}
        self.repo.persist("trade_flow_snapshots", status="READY", payload=result)
        return result


class CipherFXUpgradeSuite:
    """Runs all new intelligence as shadow evidence around existing engines."""

    def __init__(self, coordinator, gateway):
        self.coordinator = coordinator
        self.repo = coordinator
        self.gateway = gateway
        self.planning = NextSessionPlanningEngine()
        self.regime = MarketRegimeEngine()
        self.router = StrategyRouter()
        self.liquidity = LiquidityIntelligenceEngine()
        self.sessions = SessionProfileEngine(coordinator)
        self.gaps = GapAndOpeningRangeEngine()
        self.news = NewsRiskEngine()
        self.cost = CostAndExecutionForecastEngine()
        self.quality = SetupQualityEngine()
        self.exposure = PortfolioExposureEngine(gateway)
        self.outcomes = TradeOutcomeIntelligenceEngine(coordinator)
        self.attribution = StrategyPerformanceAttributionEngine(coordinator)
        self.missed = MissedTradeIntelligenceEngine(coordinator)
        self.false_entries = FalseEntryIntelligenceEngine(coordinator)
        self.walk_forward = WalkForwardValidationEngine(coordinator)
        self.drift = LiveBacktestDriftEngine(coordinator)
        self.shadow = ShadowStrategyEngine(coordinator)
        self.replay = TradeReplayEngine(coordinator)
        self.deployment = StagedDeploymentValidator(coordinator)
        self.notifications = NotificationOutbox(coordinator)
        self.database = DatabaseIntegrityEngine(coordinator)
        self.reliability = ReliabilityAndKillSwitchController(coordinator)
        self.reconciliation = BrokerReconciliationEngine(coordinator, gateway)
        self.config = ConfigurationIntegrityEngine(coordinator, coordinator)
        self.flow = TradeFlowHealthMonitor(coordinator)
        self._last_scan_key = {}

    def observe_scan(self, symbol, asset_class, market, frames, sig):
        scan_key = (str(symbol), str(sig.get("m5_trigger_candle_id") or sig.get("m5_candle_closed_at") or ""))
        if scan_key[1] and self._last_scan_key.get(scan_key[0]) == scan_key[1]:
            return {"status": "DUPLICATE_SCAN", "symbol": symbol, "setup_id": str(sig.get("decision_trace_id") or "")}
        if scan_key[1]:
            self._last_scan_key[scan_key[0]] = scan_key[1]
        regime = self.regime.classify(sig, frames)
        route = self.router.route(asset_class, regime, sig)
        plan = self.planning.create(symbol, asset_class, frames, sig)
        liquidity = self.liquidity.detect(frames)
        session = (_SystematicSession().session_info(symbol, asset_class) if _SystematicSession is not None else {"session": "UNKNOWN", "session_score": 0})
        gap = self.gaps.detect(asset_class, frames)
        news = self.news.current(symbol)
        cost = self.cost.forecast(sig, route)
        quality = self.quality.score(sig, regime, route, plan, liquidity, news, cost)
        setup_id = str(sig.get("decision_trace_id") or "")
        missed = None
        final_status = str(sig.get("final_status") or sig.get("state") or "").upper()
        if final_status in {"NOT_QUALIFIED", "BLOCKED", "REJECTED"} or bool(sig.get("not_qualified")):
            missed = self.missed.evaluate(sig, str(sig.get("block_reason") or sig.get("reason") or final_status))
        payload = {"symbol": symbol, "asset_class": asset_class, "market": market, "setup_id": setup_id, "regime": regime, "route": route, "plan": plan, "liquidity": liquidity, "session": session, "gap": gap, "news": news, "cost": cost, "quality": quality, "missed_trade": missed}
        self.coordinator.persist("market_regimes", symbol=symbol, setup_id=setup_id, status=str(regime.get("regime") or ""), payload=regime)
        self.coordinator.persist("strategy_permissions", symbol=symbol, setup_id=setup_id, status="ROUTED", payload=route)
        self.coordinator.persist("premarket_plans", symbol=symbol, setup_id=setup_id, status=str(plan.get("status") or ""), payload=plan)
        self.coordinator.persist("premarket_plan_versions", symbol=symbol, setup_id=setup_id, status=f"VERSION_{plan.get('version', 1)}", payload={"plan_id": plan.get("plan_id"), "version": plan.get("version", 1), "plan": plan})
        self.coordinator.persist("premarket_plan_events", symbol=symbol, setup_id=setup_id, status="PLAN_READY" if plan.get("status") == "READY" else "PLAN_DATA_MISSING", reason=str(plan.get("reason") or ""), payload={"event": "PLAN_CREATED", "plan_id": plan.get("plan_id"), "data_quality": plan.get("data_quality")})
        self.coordinator.persist("liquidity_levels", symbol=symbol, setup_id=setup_id, status=str(liquidity.get("status") or ""), payload=liquidity)
        self.coordinator.persist("session_profiles", symbol=symbol, setup_id=setup_id, status=str(session.get("session") or ""), payload=session)
        self.coordinator.persist("gap_events", symbol=symbol, setup_id=setup_id, status=str(gap.get("status") or ""), payload=gap)
        if isinstance(gap.get("opening_range"), dict):
            self.coordinator.persist(
                "opening_ranges",
                symbol=symbol,
                setup_id=setup_id,
                status="READY",
                reason=str(gap.get("classification") or ""),
                payload=gap.get("opening_range") or {},
                event_id=f"opening_range:{symbol}:{setup_id}",
            )
        self.coordinator.persist("news_events", symbol=symbol, setup_id=setup_id, status=str(news.get("status") or ""), payload=news)
        self.coordinator.persist("execution_cost_models", symbol=symbol, setup_id=setup_id, status=str(cost.get("status") or ""), payload=cost)
        self.coordinator.persist("setup_scores", symbol=symbol, setup_id=setup_id, status="SHADOW_SCORE", payload=quality)
        self.coordinator.persist("portfolio_exposure_snapshots", symbol=symbol, setup_id=setup_id, status="SHADOW", payload=self.exposure.snapshot())
        self.shadow.record(symbol, setup_id, sig, quality, route)
        sig["cipherfx_upgrade_shadow"] = payload
        return payload

    def runtime_audit(self):
        result = {
            "database": self.database.check(),
            "deployment": self.deployment.check(),
            "notifications": self.notifications.summary(),
            "reliability": self.reliability.check(),
            "reconciliation": self.reconciliation.check(),
            "configuration": self.config.check(),
            "outcomes": self.outcomes.summary(),
            "attribution": self.attribution.summary(),
            "missed_trades": self.missed.summary(),
            "false_entries": self.false_entries.summary(),
            "shadow": self.shadow.summary(),
            "walk_forward": self.walk_forward.evaluate(),
            "drift": self.drift.evaluate(),
            "trade_flow": self.flow.snapshot(),
        }
        result["attribution"]["persisted_buckets"] = self.attribution.persist_summary(result["attribution"])
        result["false_entries"]["persisted_buckets"] = self.false_entries.persist_summary(result["false_entries"])
        result["walk_forward"]["persisted_runs"] = self.walk_forward.persist_evaluation(result["walk_forward"])
        result["drift"]["persisted_snapshots"] = self.drift.persist_snapshot(result["drift"])
        if result["reliability"].get("status") != "NORMAL":
            result["notifications"]["last_event"] = self.notifications.emit(
                "RELIABILITY_DEGRADED",
                "HIGH",
                {"reliability": result["reliability"]},
            )
        if result["reconciliation"].get("status") != "PASS":
            result["notifications"]["last_event"] = self.notifications.emit(
                "BROKER_RECONCILIATION_MISMATCH",
                "HIGH",
                {"reconciliation": result["reconciliation"]},
            )
        result["notifications"] = self.notifications.summary() | {
            "last_event": result["notifications"].get("last_event")
        }
        result["deployment"]["persisted_events"] = self.deployment.persist_check(result["deployment"])
        result["database"]["persisted_events"] = self.database.persist_check(result["database"])
        result["reliability"]["persisted_events"] = self.reliability.persist_check(result["reliability"])
        result["reconciliation"]["persisted_events"] = self.reconciliation.persist_check(result["reconciliation"])
        self.coordinator.persist("system_health_events", status=result["reliability"].get("status"), payload=result["reliability"])
        self.coordinator.persist("reconciliation_events", status=result["reconciliation"].get("status"), payload=result["reconciliation"])
        return result


__all__ = [
    "NextSessionPlanningEngine", "MarketRegimeEngine", "StrategyRouter",
    "LiquidityIntelligenceEngine", "SessionProfileEngine", "GapAndOpeningRangeEngine",
    "NewsRiskEngine", "CostAndExecutionForecastEngine", "SetupQualityEngine",
    "PortfolioExposureEngine", "TradeOutcomeIntelligenceEngine",
    "StrategyPerformanceAttributionEngine", "MissedTradeIntelligenceEngine",
    "FalseEntryIntelligenceEngine", "WalkForwardValidationEngine",
    "LiveBacktestDriftEngine", "ShadowStrategyEngine", "TradeReplayEngine",
    "StagedDeploymentValidator", "NotificationOutbox", "DatabaseIntegrityEngine", "ReliabilityAndKillSwitchController", "BrokerReconciliationEngine",
    "ConfigurationIntegrityEngine", "TradeFlowHealthMonitor", "CipherFXUpgradeSuite",
]
