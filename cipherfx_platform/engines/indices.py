from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from ..contracts import MarketSnapshot, TradeProposal, SIDES, utc_now
from ..database import DatabaseLayer


class IndicesLearningEngine:
    """Independent index intelligence, scoring, probability and history."""

    ENGINE="INDICES"
    TIME_WEIGHTS={"MN1":3,"W1":5,"D1":8,"H4":12,"H1":14,"M30":14,"M15":14,"M5":13,"M3":8,"M1":6}
    THRESHOLD=56.0

    def __init__(self,database:DatabaseLayer|None=None):
        self.database=database; self.last_report={}

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
        return min(100.0,pts),reasons

    def _adjustment(self):
        if not self.database:return 0.0
        rows=self.database.engine_history(self.ENGINE,50)
        if not rows:return 0.0
        return max(-7.0,min(7.0,sum(x["result_r"] for x in rows)/len(rows)*2.0))

    def propose(self,snapshot:MarketSnapshot)->TradeProposal|None:
        scores={side:{tf:self._timeframe(snapshot.frames[tf],side) for tf in self.TIME_WEIGHTS} for side in SIDES}
        features={side:self._features(snapshot,side) for side in SIDES}; totals={side:sum(self.TIME_WEIGHTS[tf]*scores[side][tf]/100 for tf in self.TIME_WEIGHTS)+features[side][0]*0.08 for side in SIDES}
        side=max(totals,key=totals.get); raw=round(totals[side],2); adj=self._adjustment(); threshold=self.THRESHOLD-adj
        self.last_report={"engine":self.ENGINE,"scores":scores,"feature_scores":{k:v[0] for k,v in features.items()},"totals":totals,"score":raw,"threshold":round(threshold,2),"adaptive_adjustment":adj,"timeframes":list(self.TIME_WEIGHTS),"reasons":features[side][1]}
        if raw<threshold or totals[side]<=totals["SELL" if side=="BUY" else "BUY"] or side not in SIDES:return None
        c=snapshot.frames["M5"].candles[-1]; stop_distance=max(self._atr(snapshot.frames["M5"])*1.45,snapshot.tick.mid*0.0005);entry=snapshot.tick.ask if side=="BUY" else snapshot.tick.bid;target_distance=stop_distance*1.70
        pid=hashlib.sha256(f"{self.ENGINE}|{snapshot.symbol}|{side}|{c.timestamp.isoformat()}".encode()).hexdigest()[:24];created=utc_now();confidence=round(min(99,max(1,raw)),2);probability=round(min(95,max(5,50+(raw-50)*0.70)),2)
        return TradeProposal(pid,snapshot.symbol,"index",side,entry,entry-stop_distance if side=="BUY" else entry+stop_distance,entry+target_distance if side=="BUY" else entry-target_distance,0.0,"INDICES",raw,{"timeframes":scores[side],"features":features[side][0],"adaptive_adjustment":adj},created,created+timedelta(seconds=20),{"engine":self.ENGINE,"asset_class":"index"},confidence,probability,tuple(features[side][1]),stop_distance)

    def record_outcome(self,trade_id,symbol,result_r,pnl):
        if self.database:self.database.record_engine_outcome(self.ENGINE,trade_id,symbol,result_r,pnl)
