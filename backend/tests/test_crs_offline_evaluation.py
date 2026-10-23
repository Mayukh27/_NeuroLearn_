import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.crs_offline_evaluation import build_ablation_rows, build_sensitivity_rows


def test_ablation_covers_each_component_and_renormalizes_remaining_weights():
    rows = build_ablation_rows()

    assert len(rows) == 6 * 5
    assert {row["varied_factor"] for row in rows} == {
        "omit_P", "omit_B", "omit_I", "omit_T", "omit_C",
    }
    for row in rows:
        weights = json.loads(row["weights"])
        omitted = row["varied_factor"][-1].lower()
        field = {"p": "alpha", "b": "beta", "i": "gamma", "t": "delta", "c": "epsilon"}[omitted]
        assert weights[field] == 0.0
        assert sum(weights.values()) == pytest.approx(1.0)
        assert row["evidence_class"] == "SYNTHETIC_DETERMINISTIC"


def test_sensitivity_sweeps_inputs_and_valid_weight_profiles():
    rows = build_sensitivity_rows()

    assert len(rows) == 20 * 6
    assert {row["weight_profile"] for row in rows} == {
        "equal_020", "emphasize_P_040", "emphasize_B_040", "emphasize_I_040",
        "emphasize_T_040", "emphasize_C_040",
    }
    assert {row["varied_factor"] for row in rows} == {
        "score_history_P_and_T", "behavioral_cue_B_percent",
        "integrity_time_ratio", "content_complexity_C",
    }
    for row in rows:
        weights = json.loads(row["weights"])
        assert all(0.0 <= weight <= 1.0 for weight in weights.values())
        assert sum(weights.values()) == pytest.approx(1.0)
        assert row["evidence_class"] == "SYNTHETIC_DETERMINISTIC"
