from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from typing import Any

from ..contracts import MarketSnapshot, TradeProposal, SIDES, utc_now
from ..database import DatabaseLayer
from .. import entry_timing


class IndicesLearningEngine:
    """Independent index intelligence, scoring, probability and history."""

    ENGINE="INDICES"
    MODEL_VERSION = "cfx-learning-v2"
    # Collapsed to the 5 timeframes that actually drive the immediate
    # snapshot-score-execute decision (no MN1/W1/D1/M30/M3) - see forex.py
    # for why. Note _features() below still reads D1/M30 directly for the
    # ADR_RANGE/SESSION_BIAS feature checks; market_data.py fetches all 10
    # timeframes unconditionally, so those frames remain available.
    # M1 dropped - see forex.py TIME_WEIGHTS comment for the backtest
    # evidence (real 517-trade replay: indices lost 519/1855 proposals on
    # ablation, zero direction flips across 4881 snapshots checked).
    TIME_WEIGHTS={"H4":12,"H1":14,"M15":14,"M5":13}
    THRESHOLD=56.0
    ADAPTIVE_ADJUSTMENT_CAP=7.0
    FEATURE_MAX=108.0
    FEATURE_WEIGHT=8.0
    _score_scale=float(sum(TIME_WEIGHTS.values()))
    # Quality-over-quantity gate: require independent agreement across
    # trend, momentum, AND at least one structural price-action pattern
    # before a proposal can pass, regardless of total score.
    MIN_TREND_TIMEFRAME_AGREEMENT = 3
    MIN_MOMENTUM_TIMEFRAME_AGREEMENT = 3
    STRUCTURE_REASONS = frozenset({"SESSION_BREAK", "RETEST", "OPENING_GAP"})

    def __init__(self,database:DatabaseLayer|None=None):
        self.database=database
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
        self.base_threshold=max(
            self.SCORE_FLOOR,
            float(os.getenv("MT5_MIN_SCORE_INDICES", os.getenv("MT5_SCORE_FLOOR", str(self.THRESHOLD)))),
        )
        try:self.learning_min_samples=max(5,int(os.getenv("MT5_INDICES_LEARNING_MIN_SAMPLES","5")))
        except (TypeError,ValueError):self.learning_min_samples=5
        self.last_learning_sample_size=0
        self.last_learning_average_r=0.0
        self.last_report={}

    @staticmethod
    def _ema(frame,period):
        v=[c.close for c in frame.candles]
        if not v:return 0.0
        a=2/(period+1); out=v[0]
        for x in v[1:]:out=a*x+(1-a)*out
        return out
    @staticmethod
    def _atr(frame,period=14):
        if len(frame.candles)<2:return 0.0
        tr=[]
        for i,c in enumerate(frame.candles):
            p=frame.candles[i-1].close if i else c.open; tr.append(max(c.high-c.low,abs(c.high-p),abs(c.low-p)))
        return sum(tr[-period:])/len(tr[-period:])
    @staticmethod
    def _body(c):return abs(c.close-c.open)/max(c.high-c.low,1e-12)
    @staticmethod
    def _momentum(frame):
        if len(frame.candles)<6:return 0.0
        return abs(frame.candles[-1].close-frame.candles[-6].close)/max(IndicesLearningEngine._atr(frame),1e-12)
    @staticmethod
    def _gap(frame):
        if len(frame.candles)<2:return 0.0
        return abs(frame.candles[-1].open-frame.candles[-2].close)/max(IndicesLearningEngine._atr(frame),1e-12)
    @staticmethod
    def _relative_volume(frame):
        volumes=[float(c.volume or 0.0) for c in frame.candles]
        if len(volumes)<5 or volumes[-1]<=0.0:return 0.0
        baseline=[value for value in volumes[-21:-1] if value>0.0]
        if len(baseline)<4:return 0.0
        return volumes[-1]/(sum(baseline)/len(baseline))

    @classmethod
    def _normalize_score(cls,timeframe_total,feature_score):
        bounded_features=max(0.0,min(cls.FEATURE_MAX,float(feature_score)))
        raw_total=max(0.0,float(timeframe_total))+bounded_features*cls.FEATURE_WEIGHT/cls.FEATURE_MAX
        maximum=cls._score_scale+cls.FEATURE_WEIGHT
        return round(max(0.0,min(100.0,raw_total/maximum*100.0)),2)

    @staticmethod
    def _session_bias(frame,side):
        if not frame.candles:return False
        c=frame.candles[-1]; return c.close>c.open if side=="BUY" else c.close<c.open

    def _timeframe(self,frame,side):
        if not frame or not frame.candles:return 0.0
        c=frame.candles[-1]; e20=self._ema(frame,20); e50=self._ema(frame,50); atr=self._atr(frame); momentum=self._momentum(frame); body=self._body(c)
        ranges=[x.high-x.low for x in frame.candles[-21:-1]]; avg=sum(ranges)/len(ranges) if ranges else atr
        points=0.0
        points+=25 if (c.close>e20 if side=="BUY" else c.close<e20) else 0
        points+=20 if (e20>e50 if side=="BUY" else e20<e50) else 0
        points+=20 if momentum>=0.35 else 0
        points+=15 if self._session_bias(frame,side) else 0
        points+=10 if (c.high-c.low)>=avg else 0
        points+=10 if body>=0.25 else 0
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
        return bool(self._momentum(frame) >= 0.35)

    def _features(self,snapshot,side):
        d1=snapshot.frames["D1"]; m30=snapshot.frames["M30"]; m15=snapshot.frames["M15"]; m5=snapshot.frames["M5"]; c5=m5.candles[-1]; c15=m15.candles[-1]
        pts=0.0; reasons=[]
        adr=sum(x.high-x.low for x in d1.candles[-20:])/max(1,len(d1.candles[-20:])); day_range=d1.candles[-1].high-d1.candles[-1].low
        if day_range>=adr*0.70:pts+=16;reasons.append("ADR_RANGE")
        if self._gap(d1)>=0.20:pts+=12;reasons.append("OPENING_GAP")
        if self._momentum(m5)>=0.40:pts+=16;reasons.append("OPENING_DRIVE_MOMENTUM")
        if self._body(c5)>=0.35:pts+=12;reasons.append("RANGE_EXPANSION")
        prior=m15.candles[-8:-1]
        if prior and ((c15.close>max(x.high for x in prior)) if side=="BUY" else (c15.close<min(x.low for x in prior))):pts+=16;reasons.append("SESSION_BREAK")
        if ((c5.low<=max(x.high for x in m5.candles[-6:-1]) and c5.close>c5.open) if side=="BUY" else (c5.high>=min(x.low for x in m5.candles[-6:-1]) and c5.close<c5.open)):pts+=10;reasons.append("RETEST")
        if self._session_bias(m30,side):pts+=10;reasons.append("SESSION_BIAS")
        if abs(c15.close-self._ema(m15,20))<=self._atr(m15)*0.8:pts+=8;reasons.append("EMA_LOCATION")
        if self._relative_volume(m5) >= 1.20:pts+=8;reasons.append("OPENING_PARTICIPATION_PROXY")
        return min(self.FEATURE_MAX,pts),reasons

    def _adjustment(self):
        self.last_learning_sample_size=0
        self.last_learning_average_r=0.0
        if not self.database:return 0.0
        rows=self.database.engine_history(self.ENGINE,50, model_version=self.MODEL_VERSION, learning_only=True)
        self.last_learning_sample_size=len(rows)
        if len(rows)<self.learning_min_samples:return 0.0
        average_r=sum(float(x["result_r"]) for x in rows)/len(rows)
        self.last_learning_average_r=average_r
        return max(-self.ADAPTIVE_ADJUSTMENT_CAP,min(self.ADAPTIVE_ADJUSTMENT_CAP,average_r*2.0))

    def propose(self,snapshot:MarketSnapshot)->TradeProposal|None:
        scores={side:{tf:self._timeframe(snapshot.frames[tf],side) for tf in self.TIME_WEIGHTS} for side in SIDES}
        features={side:self._features(snapshot,side) for side in SIDES}; raw_totals={side:sum(self.TIME_WEIGHTS[tf]*scores[side][tf]/100 for tf in self.TIME_WEIGHTS) for side in SIDES}; totals={side:self._normalize_score(raw_totals[side],features[side][0]) for side in SIDES}
        side=max(totals,key=totals.get); raw=totals[side]; adj=self._adjustment(); threshold=max(self.SCORE_FLOOR,self.base_threshold-adj)
        trend_agreement = sum(1 for tf in self.TIME_WEIGHTS if self._trend_confirms(snapshot.frames[tf], side))
        momentum_agreement = sum(1 for tf in self.TIME_WEIGHTS if self._momentum_confirms(snapshot.frames[tf], side))
        structure_confirmed = any(reason in self.STRUCTURE_REASONS for reason in features[side][1])
        diversity_ok = (
            trend_agreement >= self.MIN_TREND_TIMEFRAME_AGREEMENT
            and momentum_agreement >= self.MIN_MOMENTUM_TIMEFRAME_AGREEMENT
            and structure_confirmed
        )
        entry_guard=entry_timing.evaluate(snapshot,side)
        self.last_report={"engine":self.ENGINE,"scores":scores,"feature_scores":{k:v[0] for k,v in features.items()},"raw_totals":raw_totals,"totals":totals,"score":raw,"score_scale":"0-100","feature_max":self.FEATURE_MAX,"feature_weight":self.FEATURE_WEIGHT,"score_floor":self.SCORE_FLOOR,"threshold":round(threshold,2),"base_threshold":round(self.base_threshold,2),"adaptive_adjustment":adj,"learning_min_samples":self.learning_min_samples,"learning_sample_size":self.last_learning_sample_size,"learning_average_r":round(self.last_learning_average_r,4),"timeframes":list(self.TIME_WEIGHTS),"reasons":features[side][1],"trend_agreement":trend_agreement,"momentum_agreement":momentum_agreement,"structure_confirmed":structure_confirmed,"diversity_ok":diversity_ok,"entry_guard":entry_guard}
        if raw<threshold or totals[side]<=totals["SELL" if side=="BUY" else "BUY"] or side not in SIDES or not diversity_ok or entry_guard["blocked"]:return None
        # Stop/target anchored to H4 (not M5) - see forex.py propose() comment.
        c=snapshot.frames["M5"].candles[-1]; stop_distance=max(self._atr(snapshot.frames["H4"])*1.45,snapshot.tick.mid*0.0005);entry=snapshot.tick.ask if side=="BUY" else snapshot.tick.bid;target_distance=stop_distance*1.70
        pid=hashlib.sha256(f"{self.ENGINE}|{snapshot.symbol}|{side}|{c.timestamp.isoformat()}".encode()).hexdigest()[:24];created=utc_now();calibration=self.database.calibrated_probability(self.ENGINE,side,raw,self.MODEL_VERSION) if self.database else {"probability":raw,"status":"PRIOR_ONLY","sample_size":0,"score_bucket":int(raw//10)*10};confidence=round(float(calibration["probability"]),2);probability=confidence
        context={"engine":self.ENGINE,"asset_class":"index","model_version":self.MODEL_VERSION,"probability_source":calibration["status"],"probability_sample_size":calibration["sample_size"],"probability_score_bucket":calibration["score_bucket"]}
        return TradeProposal(pid,snapshot.symbol,"index",side,entry,entry-stop_distance if side=="BUY" else entry+stop_distance,entry+target_distance if side=="BUY" else entry-target_distance,0.0,"INDICES",raw,{"timeframes":scores[side],"features":features[side][0],"adaptive_adjustment":adj},created,created+timedelta(seconds=20),context,confidence,probability,tuple(features[side][1]),stop_distance)

    def record_outcome(self, trade_id, symbol, result_r, pnl, metrics=None):
        if self.database:
            self.database.record_engine_outcome(
                self.ENGINE, trade_id, symbol, result_r, pnl, metrics=metrics or {}
            )
