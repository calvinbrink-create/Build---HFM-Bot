from __future__ import annotations

import hashlib
import os
import math
from datetime import timedelta
from typing import Any

from ..contracts import MarketSnapshot, TradeProposal, SIDES, utc_now
from ..database import DatabaseLayer
from .. import entry_timing


class ForexLearningEngine:
    """Independent FX intelligence, scoring, probability and adaptive history."""

    ENGINE = "FOREX"
    MODEL_VERSION = "cfx-learning-v2"
    # Collapsed to the 5 timeframes that actually drive the immediate
    # snapshot-score-execute decision (no MN1/W1/D1/M30/M3): each of these
    # was largely re-testing the same trend/momentum signal, inflating the
    # score during ordinary trending noise without adding independent
    # confirmation.
    # M1 dropped: backtested against 517 real matched trades (2026-07-16/17
    # window) via exact historical MarketSnapshot replay through this same
    # scoring code. M1 never flipped a trade's direction (0/9985 snapshots)
    # - it only tipped marginal setups over threshold. Removing it dropped
    # 402 of 517 real proposals, and those 402 accounted for -$12,950 of a
    # -$15,289 net loss in that window (84.7%), while raising win rate on
    # the survivors from 30.4% to 37.4%.
    TIME_WEIGHTS = {"H4": 12, "H1": 13, "M15": 13, "M5": 12}
    THRESHOLD = 65.0
    ADAPTIVE_ADJUSTMENT_CAP = 8.0
    FEATURE_MAX = 90.0
    FEATURE_WEIGHT = 5.0
    _score_scale = float(sum(TIME_WEIGHTS.values()))
    # Quality-over-quantity gate: a high point total isn't enough on its own
    # if it comes from one repeated signal (e.g. a single strong trend
    # showing up in every timeframe check). Require independent agreement
    # across trend, momentum, AND at least one structural price-action
    # pattern before a proposal can pass, regardless of total score.
    MIN_TREND_TIMEFRAME_AGREEMENT = 3
    MIN_MOMENTUM_TIMEFRAME_AGREEMENT = 3
    STRUCTURE_REASONS = frozenset({"BOS", "CHOCH", "CONTINUATION", "LIQUIDITY_SWEEP"})

    def __init__(self, database: DatabaseLayer | None = None):
        self.database = database
        # SCORE_FLOOR is a true safety floor below THRESHOLD by exactly the max
        # adaptive adjustment by default, so the reward half of the adaptive
        # loop (good performance lowering the bar) stays reachable instead of
        # being clamped away - but MT5_SCORE_FLOOR lets an operator raise this
        # floor at runtime (e.g. during an incident) without a code change.
        default_floor = self.THRESHOLD - self.ADAPTIVE_ADJUSTMENT_CAP
        try:
            self.SCORE_FLOOR = float(os.getenv("MT5_SCORE_FLOOR", str(default_floor)))
        except (TypeError, ValueError):
            self.SCORE_FLOOR = default_floor
        try:
            self.base_threshold = max(
                self.SCORE_FLOOR,
                float(os.getenv("MT5_MIN_SCORE_FOREX", os.getenv("MT5_SCORE_FLOOR", str(self.THRESHOLD)))),
            )
        except (TypeError, ValueError):
            self.base_threshold = float(self.THRESHOLD)
        try:
            self.learning_min_samples = max(
                5, int(os.getenv("MT5_FOREX_LEARNING_MIN_SAMPLES", "5"))
            )
        except (TypeError, ValueError):
            self.learning_min_samples = 5
        self.last_learning_sample_size = 0
        self.last_learning_average_r = 0.0
        self.last_report: dict[str, Any] = {}

    @staticmethod
    def _vals(frame): return [float(c.close) for c in frame.candles]
    @staticmethod
    def _ema(frame, period):
        values = ForexLearningEngine._vals(frame)
        if not values: return 0.0
        alpha = 2.0 / (period + 1.0); value = values[0]
        for x in values[1:]: value = alpha * x + (1.0 - alpha) * value
        return value
    @staticmethod
    def _atr(frame, period=14):
        if len(frame.candles) < 2: return 0.0
        tr=[]
        for i,c in enumerate(frame.candles):
            prev=frame.candles[i-1].close if i else c.open
            tr.append(max(c.high-c.low,abs(c.high-prev),abs(c.low-prev)))
        sample=tr[-period:]; return sum(sample)/len(sample) if sample else 0.0
    @staticmethod
    def _rmi(frame, period=14, momentum=5):
        v=ForexLearningEngine._vals(frame)
        if len(v)<=momentum+2:return 50.0
        gains=[max(v[i]-v[i-momentum],0.0) for i in range(momentum,len(v))]
        losses=[max(v[i-momentum]-v[i],0.0) for i in range(momentum,len(v))]
        g=sum(gains[-period:])/max(1,len(gains[-period:])); l=sum(losses[-period:])/max(1,len(losses[-period:]))
        return 100.0 if l<=1e-12 and g>0 else 50.0 if l<=1e-12 else 100.0-100.0/(1.0+g/l)
    @staticmethod
    def _adx(frame, period=14):
        if len(frame.candles)<period+2:return 0.0
        sample=frame.candles[-period:]; moves=[abs(frame.candles[i].close-frame.candles[i-1].close) for i in range(len(frame.candles)-period,len(frame.candles))]
        ranges=[max(c.high-c.low,1e-12) for c in sample]
        return min(100.0,100.0*(sum(moves)/len(moves))/(sum(ranges)/len(ranges)))
    @staticmethod
    def _body(c): return abs(c.close-c.open)/max(c.high-c.low,1e-12)
    @staticmethod
    def _location(c): return (c.close-c.low)/max(c.high-c.low,1e-12)

    def _timeframe(self, frame, side):
        if not frame or not frame.candles:return 0.0
        c=frame.candles[-1]; e20=self._ema(frame,20); e50=self._ema(frame,50); rmi=self._rmi(frame); adx=self._adx(frame)
        points=0.0
        points+=25 if (c.close>e20 if side=="BUY" else c.close<e20) else 0
        points+=20 if (e20>e50 if side=="BUY" else e20<e50) else 0
        points+=20 if (rmi>=52 if side=="BUY" else rmi<=48) else 0
        points+=15 if adx>=15 else 0
        points+=10 if (self._location(c)>=0.55 if side=="BUY" else self._location(c)<=0.45) else 0
        points+=10 if (c.close>c.open if side=="BUY" else c.close<c.open) else 0
        return points

    def _trend_confirms(self, frame, side) -> bool:
        if not frame or not frame.candles:
            return False
        c = frame.candles[-1]
        e20 = self._ema(frame, 20); e50 = self._ema(frame, 50)
        price_side = c.close > e20 if side == "BUY" else c.close < e20
        cross_side = e20 > e50 if side == "BUY" else e20 < e50
        return bool(price_side and cross_side)

    def _momentum_confirms(self, frame, side) -> bool:
        if not frame or not frame.candles:
            return False
        rmi = self._rmi(frame); adx = self._adx(frame)
        rmi_side = rmi >= 52 if side == "BUY" else rmi <= 48
        return bool(rmi_side and adx >= 15)

    @classmethod
    def _normalize_score(cls, timeframe_total, feature_score):
        bounded_features = max(0.0, min(cls.FEATURE_MAX, float(feature_score)))
        raw_total = max(0.0, float(timeframe_total)) + bounded_features * cls.FEATURE_WEIGHT / cls.FEATURE_MAX
        maximum = cls._score_scale + cls.FEATURE_WEIGHT
        return round(max(0.0, min(100.0, raw_total / maximum * 100.0)), 2)

    def _feature_points(self, snapshot, side):
        m15=snapshot.frames["M15"]; m5=snapshot.frames["M5"]; h1=snapshot.frames["H1"]; c15=m15.candles[-1]; c5=m5.candles[-1]
        prior15=m15.candles[-8:-1]; prior5=m5.candles[-6:-1]
        points=0.0; reasons=[]
        if prior15 and ((c15.close>max(x.high for x in prior15)) if side=="BUY" else (c15.close<min(x.low for x in prior15))):
            points+=18; reasons.append("BOS")
        if len(m15.candles)>=12 and ((c15.close>max(x.high for x in m15.candles[-6:-1])) if side=="BUY" else (c15.close<min(x.low for x in m15.candles[-6:-1]))):
            points+=12; reasons.append("CHOCH")
        if prior5 and ((c5.close>max(x.high for x in prior5)) if side=="BUY" else (c5.close<min(x.low for x in prior5))):
            points+=16; reasons.append("CONTINUATION")
        if ((c5.low < min(x.low for x in m5.candles[-8:-1]) and c5.close>c5.open) if side=="BUY" else (c5.high>max(x.high for x in m5.candles[-8:-1]) and c5.close<c5.open)):
            points+=14; reasons.append("LIQUIDITY_SWEEP")
        if ((c5.close>c5.open and self._body(c5)>=0.35) if side=="BUY" else (c5.close<c5.open and self._body(c5)>=0.35)):
            points+=10; reasons.append("PRESSURE")
        if ((c15.close>self._ema(m15,20)) if side=="BUY" else (c15.close<self._ema(m15,20))):
            points+=10; reasons.append("FLOW_LOCATION")
        hour=utc_now().hour
        if 7<=hour<=17: points+=5; reasons.append("LIQUID_SESSION")
        if ((c5.close>h1.candles[-1].close) if side=="BUY" else (c5.close<h1.candles[-1].close)): points+=5; reasons.append("INSTITUTIONAL_FLOW")
        # No tick-volume proxy for forex: MT5/XM forex volume is tick count,
        # not real traded size, and is broker-specific/unreliable (retail FX
        # has no central order book) - weaker evidence than its point value
        # would imply, so it's dropped here rather than de-weighted.
        return min(self.FEATURE_MAX,points),reasons

    def _history_adjustment(self):
        self.last_learning_sample_size = 0
        self.last_learning_average_r = 0.0
        if not self.database:
            return 0.0
        rows = self.database.engine_history(
            self.ENGINE,
            50,
            model_version=self.MODEL_VERSION,
            learning_only=True,
        )
        self.last_learning_sample_size = len(rows)
        if len(rows) < self.learning_min_samples:
            return 0.0
        average_r = sum(float(x["result_r"]) for x in rows) / len(rows)
        self.last_learning_average_r = average_r
        return max(-self.ADAPTIVE_ADJUSTMENT_CAP, min(self.ADAPTIVE_ADJUSTMENT_CAP, average_r * 2.0))

    def propose(self, snapshot: MarketSnapshot) -> TradeProposal | None:
        scores={}
        for side in SIDES:
            scores[side]={tf: self._timeframe(snapshot.frames[tf],side) for tf in self.TIME_WEIGHTS}
        feature_scores={side:self._feature_points(snapshot,side) for side in SIDES}
        raw_totals={side:sum(self.TIME_WEIGHTS[tf]*scores[side][tf]/100.0 for tf in self.TIME_WEIGHTS) for side in SIDES}
        totals={side:self._normalize_score(raw_totals[side], feature_scores[side][0]) for side in SIDES}
        side=max(totals,key=totals.get); raw=totals[side]; adjustment=self._history_adjustment(); threshold=max(self.SCORE_FLOOR, self.base_threshold-adjustment)
        trend_agreement = sum(1 for tf in self.TIME_WEIGHTS if self._trend_confirms(snapshot.frames[tf], side))
        momentum_agreement = sum(1 for tf in self.TIME_WEIGHTS if self._momentum_confirms(snapshot.frames[tf], side))
        structure_confirmed = any(reason in self.STRUCTURE_REASONS for reason in feature_scores[side][1])
        diversity_ok = (
            trend_agreement >= self.MIN_TREND_TIMEFRAME_AGREEMENT
            and momentum_agreement >= self.MIN_MOMENTUM_TIMEFRAME_AGREEMENT
            and structure_confirmed
        )
        entry_guard=entry_timing.evaluate(snapshot,side)
        self.last_report={"engine":self.ENGINE,"scores":scores,"feature_scores":{k:v[0] for k,v in feature_scores.items()},"raw_totals":raw_totals,"totals":totals,"score":raw,"score_scale":"0-100","feature_max":self.FEATURE_MAX,"feature_weight":self.FEATURE_WEIGHT,"score_floor":self.SCORE_FLOOR,"threshold":round(threshold,2),"base_threshold":round(self.base_threshold,2),"adaptive_adjustment":adjustment,"learning_min_samples":self.learning_min_samples,"learning_sample_size":self.last_learning_sample_size,"learning_average_r":round(self.last_learning_average_r,4),"timeframes":list(self.TIME_WEIGHTS),"reasons":feature_scores[side][1],"trend_agreement":trend_agreement,"momentum_agreement":momentum_agreement,"structure_confirmed":structure_confirmed,"diversity_ok":diversity_ok,"entry_guard":entry_guard}
        if raw<threshold or totals[side]<=totals["SELL" if side=="BUY" else "BUY"] or side not in SIDES or not diversity_ok or entry_guard["blocked"]:return None
        # Stop/target anchored to H4 (not M5): the entry thesis is scored
        # heavily on H4/H1 (the 44% majority of TIME_WEIGHTS), so the trade
        # needs H4-scale room to actually reach that move instead of being
        # stopped/targeted off 5-minute noise before the thesis can play out.
        c=snapshot.frames["M5"].candles[-1]; atr=max(self._atr(snapshot.frames["H4"]),snapshot.tick.mid*0.0002); stop_distance=atr*1.30; entry=snapshot.tick.ask if side=="BUY" else snapshot.tick.bid; target_distance=stop_distance*1.60
        material=f"{self.ENGINE}|{snapshot.symbol}|{side}|{c.timestamp.isoformat()}"; pid=hashlib.sha256(material.encode()).hexdigest()[:24]; created=utc_now()
        calibration=self.database.calibrated_probability(self.ENGINE,side,raw,self.MODEL_VERSION) if self.database else {"probability":raw,"status":"PRIOR_ONLY","sample_size":0,"score_bucket":int(raw//10)*10}
        confidence=round(float(calibration["probability"]),2); probability=confidence
        context={"engine":self.ENGINE,"asset_class":"forex","model_version":self.MODEL_VERSION,"probability_source":calibration["status"],"probability_sample_size":calibration["sample_size"],"probability_score_bucket":calibration["score_bucket"]}
        return TradeProposal(pid,snapshot.symbol,"forex",side,entry,entry-stop_distance if side=="BUY" else entry+stop_distance,entry+target_distance if side=="BUY" else entry-target_distance,0.0,"FOREX",raw,{"timeframes":scores[side],"features":feature_scores[side][0],"adaptive_adjustment":adjustment},created,created+timedelta(seconds=30),context,confidence,probability,tuple(feature_scores[side][1]),stop_distance)

    def record_outcome(self, trade_id, symbol, result_r, pnl, metrics=None):
        if self.database:
            self.database.record_engine_outcome(
                self.ENGINE, trade_id, symbol, result_r, pnl, metrics=metrics or {}
            )
