from datetime import datetime, timedelta, timezone
from pathlib import Path

from cipherfx_clean.historical_memory import HistoricalState, feature_kinds, feature_names
from cipherfx_clean.research_orchestrator import build_candidate_universes, code_manifest
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES


def test_code_manifest_is_content_addressed_and_excludes_runtime_evidence(tmp_path):
    root = tmp_path / "clean_build"
    root.mkdir()
    (root / "module.py").write_text("value = 1\n")
    (root / "README.md").write_text("scope\n")
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "mutable.json").write_text("{}\n")

    first = code_manifest(root)
    (evidence / "mutable.json").write_text('{"changed":true}\n')
    second = code_manifest(root)

    assert first == second
    assert {row["path"] for row in first["files"]} == {"README.md", "module.py"}
    (root / "module.py").write_text("value = 2\n")
    assert code_manifest(root)["manifest_digest"] != first["manifest_digest"]


def test_orchestrator_uses_full_empirical_universes_not_eleven_fixed_conditions():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    names = feature_names(RESEARCH_TIMEFRAMES)
    kinds = feature_kinds(RESEARCH_TIMEFRAMES)
    states = tuple(
        HistoricalState(
            f"s-{index}", "dataset", "XAUUSD", start + timedelta(hours=index),
            tuple(
                float(index % 3 - 1) if kinds[feature] == "CATEGORICAL"
                else float(index % 7)
                for feature in range(len(names))
            ),
            ("SESSION:LONDON", "M5:REGIME:TREND_UP"),
            (f"source-{index}",),
        )
        for index in range(140)
    )
    universes, manifest = build_candidate_universes(
        states, ("XAUUSD",), ("M5",), interaction_order=2,
    )

    assert len(universes) == 1
    assert len(universes[0].condition_sets) > 11
    assert manifest["total_condition_sets"] == len(universes[0].condition_sets)
    assert any(len(item) == 2 for item in universes[0].condition_sets)
