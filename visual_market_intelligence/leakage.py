"""Tests and checks that enforce causal ordering and split hygiene."""
from __future__ import annotations
from datetime import datetime
from typing import Iterable, Mapping

def assert_causal_rows(rows: Iterable[Mapping], *, feature_time_key: str = "feature_time",
                       label_time_key: str = "label_time") -> None:
    for row in rows:
        feature = str(row.get(feature_time_key, ""))
        label = str(row.get(label_time_key, ""))
        if feature and label:
            if datetime.fromisoformat(feature.replace("Z", "+00:00")) > datetime.fromisoformat(label.replace("Z", "+00:00")):
                raise AssertionError("feature timestamp is after label timestamp")

def assert_no_future_bar_access(feature_times: Iterable[str], available_times: Iterable[str]) -> None:
    available = set(str(item) for item in available_times)
    unknown = [str(item) for item in feature_times if str(item) not in available]
    if unknown:
        raise AssertionError("features reference unavailable/future bars: " + ",".join(unknown[:3]))

def assert_split_gap(train_end: str, validation_start: str, embargo_seconds: int = 0) -> None:
    train = datetime.fromisoformat(train_end.replace("Z", "+00:00"))
    validation = datetime.fromisoformat(validation_start.replace("Z", "+00:00"))
    if (validation - train).total_seconds() < embargo_seconds:
        raise AssertionError("time split violates embargo gap")
