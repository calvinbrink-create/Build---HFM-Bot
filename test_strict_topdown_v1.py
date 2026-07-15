#!/usr/bin/env python3
from datetime import datetime, timedelta
import strategy_architecture_v1 as s

def trend(side, count=100, start=100.0):
    rows=[]; price=start; step=.25 if side=="BUY" else -.25
    for i in range(count):
        o=price; c=o+step; rows.append(s.Candle((datetime(2026,7,1)+timedelta(minutes=i)).isoformat(),o,max(o,c)+.08,min(o,c)-.08,c,100.0)); price=c
    return tuple(rows)

def frames(h4="BUY",h1="BUY",m15="BUY",m5="BUY",m1="BUY"):
    return {k:s.Frame(k,trend(v)) for k,v in {"H4":h4,"H1":h1,"M15":m15,"M5":m5,"M1":m1}.items()}

def profile(minimum=70.0):
    return s.Profile("TEST","forex",1.25,1.6,.35,.01,minimum_strategy_score=minimum)

def test_h4_owns_direction():
    d=s.evaluate("TEST",profile(),frames(h4="SELL",h1="BUY",m15="BUY",m5="BUY"))
    assert d.state=="REJECTED" and "H1" in d.reason

def test_three_of_four_directional_components_are_not_discarded():
    original=s._pack
    try:
        s._pack=lambda frame:{"ready":True,"bias":"NO_TRADE","close":100.0,"ema20":99.0,"ema50":98.0,"rmi":60.0,"vwap":101.0}
        decision=s.evaluate_h4(s.Frame("H4",trend("BUY",1)),70.0)
        assert decision.side=="BUY" and decision.score==75.0 and not decision.reasons
        confirmation=s.evaluate_h1(s.Frame("H1",trend("BUY",1)),"BUY",70.0)
        assert confirmation.side=="BUY" and confirmation.score==75.0 and not confirmation.reasons
    finally:
        s._pack=original

def test_h1_must_align():
    d=s.evaluate("TEST",profile(),frames(h4="BUY",h1="SELL",m15="BUY",m5="BUY"))
    assert d.state=="REJECTED" and "H1" in d.reason

def test_m15_must_align():
    d=s.evaluate("TEST",profile(),frames(h4="BUY",h1="BUY",m15="SELL",m5="BUY"))
    assert d.state=="REJECTED" and "M15" in d.reason

def test_m15_controlled_pullback_structure_is_not_discarded():
    original=s._pack
    try:
        s._pack=lambda frame:{
            "ready":True,"bias":"NO_TRADE","close":99.0,
            "ema20":99.5,"ema50":100.0,"rmi":60.0,"vwap":98.5,
        }
        decision=s.evaluate_m15(s.Frame("M15",trend("SELL",1)),"SELL",70.0)
        assert decision.side=="SELL" and decision.score==70.0
        assert decision.metrics["structure_mode"]=="CONTROLLED_PULLBACK"
    finally:
        s._pack=original

def test_rejected_live_decision_carries_stage_score():
    original=s._pack
    try:
        def fake_pack(frame):
            if frame.timeframe=="H1":
                return {"ready":True,"bias":"NO_TRADE","close":101.0,"ema20":100.0,"ema50":99.0,"rmi":40.0,"vwap":102.0,"adx":25.0}
            return {"ready":True,"bias":"BUY","close":103.0,"ema20":102.0,"ema50":101.0,"rmi":60.0,"vwap":100.0,"adx":25.0}
        s._pack=fake_pack
        decision=s.evaluate("TEST",profile(),frames())
        assert decision.state=="REJECTED" and decision.score>=70.0
        assert decision.evidence["score_metric"]["stage"]=="FINAL"
        assert decision.evidence["score_metric"]["final"] is True
    finally:
        s._pack=original

def test_score_is_hard_gate():
    d=s.evaluate("TEST",profile(101.0),frames())
    assert d.state=="REJECTED" and "STRATEGY_SCORE_BELOW_MINIMUM" in d.reason

def test_pass_has_one_context_id():
    d=s.evaluate("TEST",profile(),frames())
    assert d.state=="PASS"
    assert d.evidence["htf_context"]["context_id"]==d.evidence["setup_id"].split(":")[-1]
    assert d.evidence["side_consistency"]["setup_side"]=="BUY"
    assert d.evidence["side_consistency"]["confirmation_side"]=="M5_TRIGGER"

def test_m5_body_is_scored_once_not_a_second_hard_veto():
    original_pack,original_pull,original_sweep,original_break=s._pack,s._pull,s._sweep,s._break
    try:
        s._pack=lambda frame:{"ready":True,"bias":"SELL","close":99.3,"ema20":99.5,"ema50":100.0,"rmi":40.0,"vwap":99.6,"adx":25.0}
        s._pull=lambda frame,side:True
        s._sweep=lambda frame,side:False
        s._break=lambda frame,side:False
        candles=list(trend("SELL",14));last=candles[-1]
        candles[-1]=s.Candle(last.timestamp,99.48,100.2,99.0,99.3,100.0)
        decision=s._m5(s.Frame("M5",tuple(candles)),"SELL","metal",70.0)
        assert decision.side=="SELL" and decision.score==75.0
        assert "M5_TRIGGER_BODY_QUALITY_PENALTY" in decision.reasons
    finally:
        s._pack,s._pull,s._sweep,s._break=original_pack,original_pull,original_sweep,original_break

def test_m1_directional_and_doji():
    f=s.Frame("M1",trend("BUY",4))
    ok,_,_=s.m1_confirm(f,"BUY"); assert ok
    c=list(f.candles); last=c[-1]; c[-1]=s.Candle(last.timestamp,last.close,last.high,last.low,last.close,last.volume)
    ok,reason,_=s.m1_confirm(s.Frame("M1",tuple(c)),"BUY"); assert not ok and "REJECTED" in reason

def test_context_id_changes_with_higher_bar():
    d1=s.evaluate("TEST",profile(),frames())
    f=frames(); rows=list(f["H1"].candles); last=rows[-1]; rows[-1]=s.Candle((datetime(2026,7,2)).isoformat(),last.open,last.high,last.low,last.close,last.volume)
    f["H1"]=s.Frame("H1",tuple(rows)); d2=s.evaluate("TEST",profile(),f)
    assert d1.evidence["htf_context"]["context_id"]!=d2.evidence["htf_context"]["context_id"]

def test_no_forming_m1_is_used_by_evaluator():
    d=s.evaluate("TEST",profile(),frames())
    assert d.evidence["m1_confirmation"]["status"]=="NOT_USED_M5_TRIGGER_ENTRY"

def test_live_m5_mode_is_explicit_and_does_not_change_htf_authority():
    d=s.evaluate("TEST",profile(),frames(),m5_is_forming=True)
    assert d.state=="PASS"
    assert d.reason=="STRICT_TOPDOWN_FAST_M1_PASS"
    assert d.evidence["m5_trigger_mode"]=="LIVE_M5_PROGRESS_COMPLETED_M1"
    assert d.evidence["m5_trigger_candle"]["is_forming"] is True
    assert d.evidence["higher_timeframes"]["H4"]["side"]=="BUY"

if __name__=="__main__":
    tests=[v for k,v in globals().items() if k.startswith("test_")]
    for t in tests:t(); print("PASS",t.__name__)
    print("PASS strict_topdown tests=",len(tests))
