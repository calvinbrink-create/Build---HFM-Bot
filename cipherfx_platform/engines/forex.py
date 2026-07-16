from __future__ import annotations

import hashlib
import math
from datetime import timedelta
from typing import Any

from ..contracts import MarketSnapshot, TradeProposal, SIDES, utc_now
from ..database import DatabaseLayer


class ForexLearningEngine:
    """Independent FX intelligence, scoring, probability and adaptive history."""

    ENGINE = "FOREX"
    TIME_WEIGHTS = {"MN1": 4, "W1": 5, "D1": 7, "H4": 12, "H1": 13, "M30": 13, "M15": 13, "M5": 12, "M3": 9, "M1": 7}
    THRESHOLD = 58.0

    def __init__(self, database: DatabaseLayer | None = None):
        self.database = database
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
        return min(100.0,points),reasons

    def _history_adjustment(self):
        if not self.database:return 0.0
        rows=self.database.engine_history(self.ENGINE,50)
        if not rows:return 0.0
        avg=sum(float(x["result_r"]) for x in rows)/len(rows)
        return max(-8.0,min(8.0,avg*2.0))

    def propose(self, snapshot: MarketSnapshot) -> TradeProposal | None:
        scores={}
        for side in SIDES:
            scores[side]={tf: self._timeframe(snapshot.frames[tf],side) for tf in self.TIME_WEIGHTS}
        feature_scores={side:self._feature_points(snapshot,side) for side in SIDES}
        totals={}
        for side in SIDES:
            totals[side]=sum(self.TIME_WEIGHTS[tf]*scores[side][tf]/100.0 for tf in self.TIME_WEIGHTS)+feature_scores[side][0]*0.05
        side=max(totals,key=totals.get); raw=round(totals[side],2); adjustment=self._history_adjustment(); threshold=self.THRESHOLD-adjustment
        self.last_report={"engine":self.ENGINE,"scores":scores,"feature_scores":{k:v[0] for k,v in feature_scores.items()},"totals":totals,"score":raw,"threshold":round(threshold,2),"adaptive_adjustment":adjustment,"timeframes":list(self.TIME_WEIGHTS),"reasons":feature_scores[side][1]}
        if raw<threshold or totals[side]<=totals["SELL" if side=="BUY" else "BUY"] or side not in SIDES:return None
        c=snapshot.frames["M5"].candles[-1]; atr=max(self._atr(snapshot.frames["M5"]),snapshot.tick.mid*0.0002); stop_distance=atr*1.30; entry=snapshot.tick.ask if side=="BUY" else snapshot.tick.bid; target_distance=stop_distance*1.60
        material=f"{self.ENGINE}|{snapshot.symbol}|{side}|{c.timestamp.isoformat()}"; pid=hashlib.sha256(material.encode()).hexdigest()[:24]; created=utc_now()
        confidence=round(min(99.0,max(1.0,raw)),2); probability=round(min(95.0,max(5.0,50.0+(raw-50.0)*0.75)),2)
        return TradeProposal(pid,snapshot.symbol,"forex",side,entry,entry-stop_distance if side=="BUY" else entry+stop_distance,entry+target_distance if side=="BUY" else entry-target_distance,0.0,"FOREX",raw,{"timeframes":scores[side],"features":feature_scores[side][0],"adaptive_adjustment":adjustment},created,created+timedelta(seconds=30),{"engine":self.ENGINE,"asset_class":"forex"},confidence,probability,tuple(feature_scores[side][1]),stop_distance)

    def record_outcome(self, trade_id, symbol, result_r, pnl):
        if self.database:self.database.record_engine_outcome(self.ENGINE,trade_id,symbol,result_r,pnl)
