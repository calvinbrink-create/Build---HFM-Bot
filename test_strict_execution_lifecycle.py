#!/usr/bin/env python3
from datetime import datetime, timedelta
from pathlib import Path
import mt5_bot

def bot():
    b=object.__new__(mt5_bot.XM_MT5_Bot)
    b._canonical_from_resolved=lambda x:str(x)
    b._market_for_symbol=lambda x:"index_cfd" if str(x)=="NAS100" else "forex"
    b.state={"setup_history":{}}
    b._audit_decision=lambda *a,**k:None
    b._log_pending_setup_event=lambda *a,**k:None
    b._save_state=lambda:None
    return b

def test_asset_ttls():
    b=bot()
    assert b._pending_setup_ttl_seconds("NAS100","index_cfd","INDEX_ENGINE")==90
    assert b._pending_setup_ttl_seconds("EURUSD","forex","FOREX_ENGINE")==120
    assert b._pending_setup_ttl_seconds("XAUUSD","metal","METALS_ENGINE")==120

def test_execution_deadline_not_now_plus_window():
    b=bot()
    created=datetime(2026,7,13,12,0,0)
    closed=datetime(2026,7,13,12,1,0)
    setup={"symbol":"NAS100","market":"index_cfd","engine":"INDEX_ENGINE","status":"confirmed_1m","created_at":created.isoformat(),"confirmation_deadline":(created+timedelta(seconds=90)).isoformat(),"execution_deadline":(closed+timedelta(seconds=15)).isoformat()}
    assert b._pending_setup_expires_at(setup,datetime(2026,7,13,12,1,5))==closed+timedelta(seconds=15)
    assert b._pending_setup_is_expired(setup,datetime(2026,7,13,12,1,16))

def test_terminal_state_is_irreversible():
    b=bot()
    setup={"setup_id":"A","symbol":"EURUSD","side":"BUY","status":"waiting_1m","state":"WAITING_M1"}
    b._set_pending_setup_status("EURUSD:BUY",setup,"expired","SETUP_EXPIRED","deadline")
    assert setup["state"]=="EXPIRED"
    b._set_pending_setup_status("EURUSD:BUY",setup,"confirmed_1m","SETUP_CONFIRMED","late")
    assert setup["state"]=="EXPIRED"

def test_terminal_history_persists():
    b=bot()
    setup={"setup_id":"A","symbol":"EURUSD","side":"BUY","status":"waiting_1m","state":"WAITING_M1"}
    b._set_pending_setup_status("EURUSD:BUY",setup,"expired","SETUP_EXPIRED","deadline")
    assert b.state["setup_history"]["A"]["state"]=="EXPIRED"

def test_source_has_one_active_strategy_evaluator():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert source.count("_strategy_v1.evaluate(canonical, profile, frames, m5_is_forming=m5_is_forming)") == 1
    assert "m5_trigger_is_forming" in source
    assert "raise RuntimeError(" in source
    assert "replacement strategy has no route" in source

def test_source_has_deterministic_setup_fields():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert "m5_bar_open_time" in source and "context_id" in source
    assert "DUPLICATE_SUPPRESSED" in source
    assert "last_checked_m1_open_time" in source
    assert "execution_deadline" in source

def test_source_has_single_bot_submission_function():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert source.count("    def _place_trade(")==1
    assert source.count("self._place_trade(")==2

def test_late_entry_regression():
    b=bot()
    created=datetime(2026,7,13,12,0,0); closed=datetime(2026,7,13,12,1,0)
    setup={"symbol":"NAS100","market":"index_cfd","engine":"INDEX_ENGINE","status":"confirmed_1m","created_at":created.isoformat(),"confirmation_deadline":(created+timedelta(seconds=90)).isoformat(),"execution_deadline":(closed+timedelta(seconds=15)).isoformat()}
    assert b._pending_setup_is_expired(setup,closed+timedelta(minutes=3))
    assert setup["status"]=="confirmed_1m"

def test_pending_setup_wakeup_interrupts_poll_sleep():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert "self._pending_monitor_wakeup.wait(wait_for)" in source
    assert "self._pending_monitor_stop.wait(wait_for)" not in source

def test_m1_confirmation_is_anchored_to_m5_close_not_detection_time():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert '"confirmation_anchor_at": confirmation_anchor_dt.isoformat()' in source
    assert 'candle_closed_dt < confirmation_anchor_dt' in source
    assert 'candle_closed_dt <= setup_created_dt' not in source
    assert '"last_m1_candle_id": ""' in source
    assert '"confirmation_requires_post_setup_candle": bool(live_m5_trigger)' in source
    assert '"state_machine_state": "ARMED" if live_m5_trigger else "WAITING_M1"' in source

def test_confirmation_deadline_is_m5_candle_bound():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert "confirmation_deadline = confirmation_anchor_dt + timedelta(seconds=ttl_seconds)" in source
    assert "confirmation_deadline = now + timedelta(seconds=ttl_seconds)" not in source

def test_live_decision_carries_full_score_evidence():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert '"score_breakdown": decision_score_breakdown' in source
    assert '"timeframe_decisions"' in source
    assert '"execution_1m_score": 0.0' in source
    assert '20.0 if not decision.reasons' not in source

def test_live_m5_is_the_trigger_and_m1_is_confirmation():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert "self._scan_m1_event_cycle(bootstrap=False)" not in source
    assert 'scan_kind="m1_fast_trigger"' not in source
    assert 'scan_kind in {"m5_event_trigger", "m1_fast_trigger"}' not in source
    assert 'self._strategy_v1_frame(m5, "M5", completed_only=False)' in source
    assert '"trigger_clock": "LIVE_M5_PROGRESS_PENDING_M1"' in source
    assert '"timeframe_setup": "H4+H1+M15 -> LIVE_M5 -> COMPLETED_M1 -> EXECUTION"' in source
    assert 'sig["m1_role"] = "confirmation"' in source

def test_fast_m1_path_uses_completed_m5_then_completed_m1():
    source=Path("/opt/cipherfx_mt5/mt5_bot.py").read_text()
    assert 'm5_closed_at, m5_trigger_id = _completed_candle_identity(m5, "M5", 300)' in source
    assert "m5_closed_at = m1_trigger_closed_at" not in source
    assert "execution_deadline = candle_closed_dt + timedelta(seconds=execution_window)" in source
    assert "execution_deadline = datetime.utcnow() +" not in source

if __name__=="__main__":
    tests=[v for k,v in globals().items() if k.startswith("test_")]
    for t in tests:t();print("PASS",t.__name__)
    print("PASS lifecycle tests=",len(tests))
