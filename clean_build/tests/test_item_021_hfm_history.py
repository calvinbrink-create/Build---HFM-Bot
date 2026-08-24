from cipherfx_clean.hfm_history import reconcile_hfm_history
from cipherfx_clean.store import EvidenceStore


HEADER = "deal,position_id,order,symbol,deal_type,deal_entry,deal_reason,magic,comment,volume,price,profit,swap,commission,net_profit,time_broker,time_utc,broker_utc_offset_seconds\n"


def test_hfm_deals_reconcile_partial_closes_and_orphans_without_guessing(tmp_path):
    path = tmp_path / "deals.csv"
    path.write_text(
        HEADER
        + "1,10,100,XAUUSD,DEAL_TYPE_BUY,DEAL_ENTRY_IN,DEAL_REASON_EXPERT,1,edge,2,100,0,0,-1,-1,11000,200,10800\n"
        + "2,10,101,XAUUSD,DEAL_TYPE_SELL,DEAL_ENTRY_OUT,DEAL_REASON_EXPERT,1,close,1,102,2,0,-1,1,11060,260,10800\n"
        + "3,10,102,XAUUSD,DEAL_TYPE_SELL,DEAL_ENTRY_OUT,DEAL_REASON_EXPERT,1,close,1,103,3,-.5,-1,1.5,11120,320,10800\n"
        + "4,99,103,UK100,DEAL_TYPE_SELL,DEAL_ENTRY_OUT,DEAL_REASON_CLIENT,0,manual,1,1000,-2,0,0,-2,11180,380,10800\n"
    )

    report, deals, lifecycles = reconcile_hfm_history(path)
    closed = next(row for row in lifecycles if row.position_id == 10)
    orphan = next(row for row in lifecycles if row.position_id == 99)

    assert report.deal_count == 4
    assert report.closed_count == 1
    assert report.orphan_close_count == 1
    assert closed.status == "CLOSED"
    assert closed.entry_price == 100
    assert closed.exit_price == 102.5
    assert closed.holding_seconds == 120
    assert closed.net_profit == 1.5
    assert orphan.reason_codes == ("MISSING_OPEN_DEAL",)

    store = EvidenceStore(tmp_path / "history.sqlite3")
    store.write_hfm_history(report, deals, lifecycles)
    store.write_hfm_history(report, deals, lifecycles)
    assert store._conn.execute("SELECT COUNT(*) FROM hfm_deals").fetchone()[0] == 4
    assert store._conn.execute("SELECT COUNT(*) FROM trade_lifecycles").fetchone()[0] == 2
    store.close()


def test_hfm_history_rejects_broker_utc_offset_mismatch(tmp_path):
    path = tmp_path / "deals.csv"
    path.write_text(
        HEADER
        + "1,10,100,XAUUSD,DEAL_TYPE_BUY,DEAL_ENTRY_IN,DEAL_REASON_EXPERT,1,edge,1,100,0,0,0,0,11000,200,7200\n"
    )

    try:
        reconcile_hfm_history(path)
        assert False, "offset mismatch must fail"
    except ValueError as error:
        assert "offset mismatch" in str(error)
