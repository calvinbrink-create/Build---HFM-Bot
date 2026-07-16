from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from ..contracts import MarketSnapshot, TradeProposal, SIDES, utc_now
from ..database import DatabaseLayer


class MetalsLearningEngine:
    """Independent metals intelligence, scoring, probability and history."""

    ENGINE="METALS"
    TIME_WEIGHTS={"MN1":3,"W1":6,"D1":8,"H4":12,"H1":14,"M30":13,"M15":14,"M5":13,"M3":9,"M1":8}
    THRESHOLD=57.0

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
            p=frame.candles[i-1].close if i else c.open;tr.append(max(c.high-c.low,abs(c.high-p),abs(c.low-p)))
        sample=tr[-period:];return sum(sample)/len(sample)
    @staticmethod
    def _rmi(frame):
        v=[c.close for c in frame.candles]
        if len(v)<8:return 50.0
        g=[max(v[i]-v[i-5],0) for i in range(5,len(v))];l=[max(v[i-5]-v[i],0) for i in range(5,len(v))]
        ag=sum(g[-14:])/max(1,len(g[-14:]));al=sum(l[-14:])/max(1,len(l))
        return 100 if al==0 and ag else 50 if al==0 else 100-100/(1+ag/al)
    @staticmethod
    def _body(c):return abs(c.close-c.open)/max(c.high-c.low,1e-12)
    @staticmethod
    def _location(c):return (c.close-c.low)/max(c.high-c.low,1e-12)
    @staticmethod
    def _volatility(frame):return (frame.candles[-1].high-frame.candles[-1].low)/max(MetalsLearningEngine._atr(frame),1e-12) if frame.candles else 0.0

    def _timeframe(self,frame,side):
        if not frame or not frame.candles:return 0.0
        c=frame.candles[-1];e20=self._ema(frame,20);e50=self._ema(frame,50);rmi=self._rmi(frame);vol=self._volatility(frame);body=self._body(c)
        pts=0.0
        pts+=20 if (c.close>e20 if side=="BUY" else c.close<e20) else 0
        pts+=20 if (e20>e50 if side=="BUY" else e20<e50) else 0
        pts+=25 if (rmi>=52 if side=="BUY" else rmi<=48) else 0
        pts+=20 if vol>=0.80 else 0
        pts+=10 if (self._location(c)>=0.55 if side=="BUY" else self._location(c)<=0.45) else 0
        pts+=5 if (c.close>c.open if side=="BUY" else c.close<c.open) and body>=0.25 else 0
        return pts

    def _features(self,snapshot,side):
        m15=snapshot.frames["M15"];m5=snapshot.frames["M5"];c15=m15.candles[-1];c5=m5.candles[-1];pts=0.0;reasons=[]
        atr15=max(self._atr(m15),1e-12);atr5=max(self._atr(m5),1e-12)
        if (c15.high-c15.low)>=atr15*1.10:pts+=18;reasons.append("IMPULSE")
        if self._volatility(m5)>=1.20:pts+=18;reasons.append("VOLATILITY_EXPANSION")
        if ((c5.low<min(x.low for x in m5.candles[-8:-1]) and c5.close>c5.open) if side=="BUY" else (c5.high>max(x.high for x in m5.candles[-8:-1]) and c5.close<c5.open)):pts+=16;reasons.append("LIQUIDITY_GRAB")
        if self._body(c5)>=0.35:pts+=12;reasons.append("PRESSURE_CANDLE")
        if ((c15.close>self._ema(m15,20)) if side=="BUY" else (c15.close<self._ema(m15,20))):pts+=12;reasons.append("ORDER_BLOCK_LOCATION")
        if len(m15.candles)>=3 and ((m15.candles[-3].high<m15.candles[-1].low) if side=="BUY" else (m15.candles[-3].low>m15.candles[-1].high)):pts+=10;reasons.append("FVG")
        if ((c5.close>c5.open) if side=="BUY" else (c5.close<c5.open)):pts+=8;reasons.append("MOMENTUM")
        if abs(c15.close-self._ema(m15,20))<=atr15*0.5:pts+=6;reasons.append("MEAN_REVERSION_ZONE")
        return min(100.0,pts),reasons

    def _adjustment(self):
        if not self.database:return 0.0
        rows=self.database.engine_history(self.ENGINE,50)
        if not rows:return 0.0
        return max(-7.0,min(7.0,sum(x["result_r"] for x in rows)/len(rows)*2.0))

    def propose(self,snapshot:MarketSnapshot)->TradeProposal|None:
        scores={side:{tf:self._timeframe(snapshot.frames[tf],side) for tf in self.TIME_WEIGHTS} for side in SIDES}
        features={side:self._features(snapshot,side) for side in SIDES};totals={side:sum(self.TIME_WEIGHTS[tf]*scores[side][tf]/100 for tf in self.TIME_WEIGHTS)+features[side][0]*0.08 for side in SIDES}
        side=max(totals,key=totals.get);raw=round(totals[side],2);adj=self._adjustment();threshold=self.THRESHOLD-adj
        self.last_report={"engine":self.ENGINE,"scores":scores,"feature_scores":{k:v[0] for k,v in features.items()},"totals":totals,"score":raw,"threshold":round(threshold,2),"adaptive_adjustment":adj,"timeframes":list(self.TIME_WEIGHTS),"reasons":features[side][1]}
        if raw<threshold or totals[side]<=totals["SELL" if side=="BUY" else "BUY"] or side not in SIDES:return None
        c=snapshot.frames["M5"].candles[-1];stop_distance=max(self._atr(snapshot.frames["M5"])*1.50,snapshot.tick.mid*0.0007);entry=snapshot.tick.ask if side=="BUY" else snapshot.tick.bid;target_distance=stop_distance*1.80
        pid=hashlib.sha256(f"{self.ENGINE}|{snapshot.symbol}|{side}|{c.timestamp.isoformat()}".encode()).hexdigest()[:24];created=utc_now();confidence=round(min(99,max(1,raw)),2);probability=round(min(95,max(5,50+(raw-50)*0.72)),2)
        return TradeProposal(pid,snapshot.symbol,"metal",side,entry,entry-stop_distance if side=="BUY" else entry+stop_distance,entry+target_distance if side=="BUY" else entry-target_distance,0.0,"METALS",raw,{"timeframes":scores[side],"features":features[side][0],"adaptive_adjustment":adj},created,created+timedelta(seconds=20),{"engine":self.ENGINE,"asset_class":"metal"},confidence,probability,tuple(features[side][1]),stop_distance)

    def record_outcome(self,trade_id,symbol,result_r,pnl):
        if self.database:self.database.record_engine_outcome(self.ENGINE,trade_id,symbol,result_r,pnl)
