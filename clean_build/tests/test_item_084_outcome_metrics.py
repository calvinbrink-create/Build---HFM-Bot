from cipherfx_clean.intelligence.outcome_metrics import (
    OUTCOME_HORIZONS_SECONDS,
    classify_r_multiple,
    forward_metrics,
    summarize_outcomes,
)


def test_forward_metrics_cover_mfe_mae_and_time_to_extreme():
    path = ((5, 101.0, 101.1), (15, 102.0, 102.1), (30, 99.0, 99.1), (60, 103.0, 103.1))
    metrics = forward_metrics("BUY", 100.0, path)
    assert metrics[0].horizon_seconds == 5
    sixty_second = next(item for item in metrics if item.horizon_seconds == 60)
    assert sixty_second.mfe > 0
    assert sixty_second.mae < 0
    assert sixty_second.time_to_mfe_seconds == 60
    assert sixty_second.time_to_mae_seconds == 30
    assert set(OUTCOME_HORIZONS_SECONDS).issuperset(item.horizon_seconds for item in metrics)


def test_r_multiple_summary_is_cost_aware_and_thresholded():
    path = ((60, 101.0, 101.1), (120, 102.0, 102.1))
    r_multiple, tp_before_sl, target_at, stop_at = classify_r_multiple(
        "BUY", 100.0, path, 1.0, 2.0, total_cost=0.1
    )
    summary = summarize_outcomes(((r_multiple, tp_before_sl), (-1.0, False)))
    assert r_multiple == 1.9
    assert tp_before_sl is True
    assert target_at == 120
    assert stop_at is None
    assert summary.sample_size == 2
    assert 0.0 <= summary.r_multiple_probabilities["1R"] <= 1.0
