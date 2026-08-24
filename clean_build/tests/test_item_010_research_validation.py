from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.backtest import BacktestConfig, HistoricalQuote, run_backtest
from cipherfx_clean.intelligence.governance import EdgeVersion, transition_version
from cipherfx_clean.intelligence.outcomes import future_path_outcome
from cipherfx_clean.intelligence.validation_full import TimedObservation, purged_folds, validate_candidate


def test_outcome_uses_executable_side_for_both_mfe_and_mae():
    result = future_path_outcome(
        "s1",
        "BUY",
        100.2,
        ((1, 100.0, 100.2), (2, 101.0, 101.2), (3, 99.0, 99.2)),
        1.0,
        1.0,
        total_cost=.1,
    )
    assert result.mfe_value == pytest.approx(.8)
    assert result.mae_value == pytest.approx(-1.2)
    assert result.time_to_mfe_seconds == 2
    assert result.time_to_mae_seconds == 3
    assert result.stop_at_seconds == 3
    assert result.tp_before_sl is False
    assert result.r_multiple == pytest.approx(-1.3)


def test_backtest_hides_future_quotes_and_applies_latency_and_slippage():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quotes = tuple(HistoricalQuote(start + timedelta(seconds=i), 100 + i, 100.2 + i) for i in range(8))
    observations = []

    def setup(visible, index):
        observations.append((len(visible), index, visible[-1].timestamp))
        assert len(visible) == index + 1
        return index == 0

    report = run_backtest(
        quotes,
        BacktestConfig("edge", "BUY", 2, 2, .1, 0, 2, .05, 10),
        setup,
    )
    trade = report.trades[0]
    assert trade.entry_time == start + timedelta(seconds=2)
    assert trade.entry_price == pytest.approx(102.25)
    assert observations[0][0] == 1
    assert trade.exit_time > trade.entry_time


def _observations(value=1.0):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(
        TimedObservation(
            start + timedelta(minutes=10 * i),
            value if not callable(value) else value(i),
            "XAUUSD",
            "LONDON",
            "TREND_UP",
            start + timedelta(minutes=10 * i + 15),
            "M5",
            "BUY",
            "NORMAL",
            "LOW",
            f"sample-{i}",
            .1,
        )
        for i in range(24)
    )


def test_purged_walk_forward_removes_label_overlap_and_keeps_holdout_untouched():
    rows = _observations()
    folds = purged_folds(rows[:-4], 8, 4, timedelta(minutes=5))
    assert folds
    for fold in folds:
        assert all((item.label_end or item.timestamp) < fold.test[0].timestamp - timedelta(minutes=5) for item in fold.train)
        assert all(
            (left.label_end or left.timestamp) < right.timestamp
            for left, right in zip(fold.test, fold.test[1:])
        )
    report = validate_candidate(
        "edge",
        rows,
        train_size=8,
        test_size=4,
        embargo=timedelta(minutes=5),
        multiple_test_count=20,
        holdout_size=4,
        minimum_oos_samples=2,
        minimum_holdout_samples=2,
    )
    assert report.status == "PASS"
    assert report.holdout.sample_size == 2
    assert report.out_of_sample.sample_size >= 2
    assert report.multiplicity_adjusted_lower > 0
    assert "instrument=XAUUSD" in report.slices


def test_no_edge_is_a_valid_validation_result():
    report = validate_candidate(
        "no-edge",
        _observations(-.2),
        train_size=8,
        test_size=4,
        embargo=timedelta(minutes=5),
        multiple_test_count=100,
        holdout_size=4,
        minimum_oos_samples=2,
        minimum_holdout_samples=2,
    )
    assert report.status == "NO_EDGE"
    assert "MULTIPLICITY_ADJUSTED_EDGE_NOT_POSITIVE" in report.reasons


def test_governance_cannot_skip_historical_shadow_and_limited_live_stages():
    edge = EdgeVersion("edge", 1, "CANDIDATE", datetime.now(timezone.utc), None, ("definition",))
    with pytest.raises(ValueError, match="illegal governance transition"):
        transition_version(edge, "FULL_LIVE", evidence_ids=("unsupported",))
    historical = transition_version(edge, "HISTORICAL_VALIDATED", evidence_ids=("oos", "holdout"))
    shadow = transition_version(historical, "SHADOW", evidence_ids=("shadow-start",))
    limited = transition_version(shadow, "LIMITED_LIVE", evidence_ids=("shadow-pass",))
    full = transition_version(limited, "FULL_LIVE", evidence_ids=("limited-live-pass",))
    assert full.status == "FULL_LIVE"
