from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.execution import ExecutionEngine


def _engine(db, limit=2):
    return ExecutionEngine(
        gateway=None,
        config=SimpleNamespace(max_xauusd_losses_per_day=limit),
        database=db,
    )


def _record(db, trade_id, symbol, pnl, closed_at):
    db.record_trade_history(
        trade_id,
        None,
        symbol,
        "metal" if symbol == "XAUUSD" else "forex",
        "BUY",
        pnl,
        100.0,
        pnl / 100.0,
        "STOP_LOSS",
        closed_at,
        {"outcome_type": "closed_trade"},
    )


def test_gold_blocks_new_entries_after_two_sast_day_losses(tmp_path):
    db = DatabaseLayer(Path(tmp_path) / "state.db")
    now = datetime.now(timezone.utc)
    _record(db, "xau-loss-1", "XAUUSD", -10.0, now - timedelta(minutes=20))
    _record(db, "xau-loss-2", "XAUUSD", -12.0, now - timedelta(minutes=10))

    proposal = SimpleNamespace(symbol="XAUUSD")
    assert _engine(db)._gold_daily_loss_reason(proposal) == "GOLD_DAILY_LOSS_LIMIT:2/2"


def test_gold_remains_allowed_before_two_losses(tmp_path):
    db = DatabaseLayer(Path(tmp_path) / "state.db")
    now = datetime.now(timezone.utc)
    _record(db, "xau-loss-1", "XAUUSD", -10.0, now - timedelta(minutes=20))

    proposal = SimpleNamespace(symbol="XAUUSD")
    assert _engine(db)._gold_daily_loss_reason(proposal) == ""


def test_gold_limit_does_not_block_other_symbols(tmp_path):
    db = DatabaseLayer(Path(tmp_path) / "state.db")
    now = datetime.now(timezone.utc)
    _record(db, "xau-loss-1", "XAUUSD", -10.0, now - timedelta(minutes=20))
    _record(db, "xau-loss-2", "XAUUSD", -12.0, now - timedelta(minutes=10))

    assert _engine(db)._gold_daily_loss_reason(SimpleNamespace(symbol="USA100")) == ""


def test_gold_loss_count_excludes_previous_sast_day(tmp_path):
    db = DatabaseLayer(Path(tmp_path) / "state.db")
    old = datetime.now(timezone.utc) - timedelta(days=2)
    _record(db, "xau-old-1", "XAUUSD", -10.0, old)
    _record(db, "xau-old-2", "XAUUSD", -12.0, old + timedelta(minutes=1))

    assert _engine(db)._gold_daily_loss_reason(SimpleNamespace(symbol="XAUUSD")) == ""
