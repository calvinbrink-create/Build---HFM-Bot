from datetime import datetime, timedelta, timezone

from cipherfx_clean.intelligence.attribution import marginal_value
from cipherfx_clean.intelligence.validation_full import TimedObservation, purged_folds, validate_candidate
from cipherfx_clean.requirement_runtime import C106_ATTRIBUTION_WINDOW_MINUTES


def test_attribution_window_covers_a_full_trading_day_without_changing_filters():
    assert C106_ATTRIBUTION_WINDOW_MINUTES == 24 * 60


def test_attribution_is_marginal_and_validation_is_purged_with_embargo():
    assert marginal_value((2.0, 3.0), (0.0, 1.0)) == 2.0
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        TimedObservation(
            start + timedelta(minutes=index),
            0.2 if index % 2 == 0 else -0.1,
            "UK100",
            "LONDON",
            "TREND_UP",
            start + timedelta(minutes=index, seconds=30),
            "M1",
            "BUY",
            source_id=f"o{index}",
        )
        for index in range(240)
    )
    folds = purged_folds(rows, train_size=60, test_size=30, embargo=timedelta(minutes=2))
    assert len(folds) >= 2
    assert all(
        max(item.label_end for item in fold.train) < fold.test[0].timestamp - timedelta(minutes=2)
        for fold in folds
    )
    report = validate_candidate(
        "validation",
        rows,
        train_size=60,
        test_size=30,
        embargo=timedelta(minutes=2),
        multiple_test_count=3,
        holdout_size=30,
        minimum_oos_samples=30,
        minimum_holdout_samples=30,
    )
    assert report.folds
    assert report.holdout.sample_size == 30
    assert report.multiple_test_count == 3
