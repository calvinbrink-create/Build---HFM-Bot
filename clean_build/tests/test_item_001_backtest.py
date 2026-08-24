from datetime import datetime, timedelta, timezone

from cipherfx_clean.backtest import BacktestConfig, HistoricalQuote, run_backtest


def test_backtest_uses_executable_bid_ask_and_costs():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quotes = tuple(HistoricalQuote(base + timedelta(seconds=i), 10 + i * .5, 10.1 + i * .5) for i in range(7))
    report = run_backtest(quotes, BacktestConfig("edge", "BUY", 1, 2, .1, .0, 0), lambda rows, index: index == 0)
    assert len(report.trades) == 1
    assert report.trades[0].entry_price == 10.1
    assert report.trades[0].exit_reason == "TARGET"
    assert abs(report.total_net_r - 2.3) < 1e-9
