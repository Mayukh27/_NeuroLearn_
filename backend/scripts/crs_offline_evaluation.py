"""Generate reproducible synthetic CRS ablation and sensitivity tables.

Run from backend/:
    python scripts/crs_offline_evaluation.py

All rows are deterministic evaluations of constructed inputs. They are
synthetic policy diagnostics, not learner or participant evidence. Ablation
sets one component's weight to zero and renormalizes the other four weights
proportionally so they continue to sum to one; no component scores are clipped.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.crs_config import CRS_CONFIG, CRSWeights
from ml.crs import compute_crs
from scripts.scenario_validation import SCENARIOS


COMPONENT_FIELDS = {
    "P": "alpha",
    "B": "beta",
    "I": "gamma",
    "T": "delta",
    "C": "epsilon",
}
EVIDENCE_CLASS = "SYNTHETIC_DETERMINISTIC"

WEIGHT_PROFILES: list[tuple[str, CRSWeights]] = [
    ("equal_020", CRSWeights()),
    ("emphasize_P_040", CRSWeights(alpha=0.40, beta=0.15, gamma=0.15, delta=0.15, epsilon=0.15)),
    ("emphasize_B_040", CRSWeights(alpha=0.15, beta=0.40, gamma=0.15, delta=0.15, epsilon=0.15)),
    ("emphasize_I_040", CRSWeights(alpha=0.15, beta=0.15, gamma=0.40, delta=0.15, epsilon=0.15)),
    ("emphasize_T_040", CRSWeights(alpha=0.15, beta=0.15, gamma=0.15, delta=0.40, epsilon=0.15)),
    ("emphasize_C_040", CRSWeights(alpha=0.15, beta=0.15, gamma=0.15, delta=0.15, epsilon=0.40)),
]

SIMPLE_TEXT = "The cat sat on the mat. The dog ran outside."
MODERATE_TEXT = "A function processes an input and returns a result. State stores values that can change."
TECHNICAL_TEXT = (
    "Asynchronous middleware instantiates a polymorphic iterator through dependency injection, "
    "optimizing throughput with compiler-level concurrency abstractions."
)


def _scenario_inputs(scenario: tuple[str, dict]) -> dict:
    return dict(scenario[1])


def _config_with_weights(weights: CRSWeights):
    return replace(CRS_CONFIG, weights=weights)


def _ablation_weights(component: str) -> CRSWeights:
    values = asdict(CRS_CONFIG.weights)
    omitted_field = COMPONENT_FIELDS[component]
    remaining_total = sum(value for field, value in values.items() if field != omitted_field)
    for field in values:
        values[field] = 0.0 if field == omitted_field else values[field] / remaining_total
    weights = CRSWeights(**values)
    weights.validate()
    return weights


def _result_row(
    *,
    evaluation: str,
    scenario_name: str,
    varied_factor: str,
    varied_value: str,
    weight_profile: str,
    inputs: dict,
    weights: CRSWeights,
    reference_score: float,
    reference_tier: str,
) -> dict:
    result = compute_crs(**inputs, config=_config_with_weights(weights))
    return {
        "evidence_class": EVIDENCE_CLASS,
        "evaluation": evaluation,
        "scenario": scenario_name,
        "varied_factor": varied_factor,
        "varied_value": varied_value,
        "weight_profile": weight_profile,
        "weights": json.dumps(asdict(weights), sort_keys=True),
        "reference_crs": f"{reference_score:.4f}",
        "reference_tier": reference_tier,
        "crs": f"{result.crs:.4f}",
        "tier": result.difficulty,
        "crs_delta": f"{result.crs - reference_score:+.4f}",
        "tier_changed": result.difficulty != reference_tier,
    }


def build_ablation_rows() -> list[dict]:
    rows = []
    for scenario in SCENARIOS:
        name, _ = scenario
        inputs = _scenario_inputs(scenario)
        baseline = compute_crs(**inputs)
        for component in COMPONENT_FIELDS:
            rows.append(_result_row(
                evaluation="one_component_ablation",
                scenario_name=name,
                varied_factor=f"omit_{component}",
                varied_value="weight=0; remaining weights renormalized proportionally",
                weight_profile=f"omit_{component}",
                inputs=inputs,
                weights=_ablation_weights(component),
                reference_score=baseline.crs,
                reference_tier=baseline.difficulty,
            ))
    return rows


def _sensitivity_variants() -> list[tuple[str, str, dict]]:
    base = {
        "recent_scores_pct": [50.0, 50.0, 50.0],
        "attention_score_pct": 50.0,
        "time_spent": 36.0,
        "time_limit": 60.0,
        "was_correct": True,
        "transcript_text": MODERATE_TEXT,
    }
    variants: list[tuple[str, str, dict]] = []
    histories = [
        ("all_0", [0.0, 0.0, 0.0]),
        ("all_25", [25.0, 25.0, 25.0]),
        ("flat_50", [50.0, 50.0, 50.0]),
        ("rising_25_50_75", [25.0, 50.0, 75.0]),
        ("all_100", [100.0, 100.0, 100.0]),
    ]
    for label, values in histories:
        variants.append(("score_history_P_and_T", label, {**base, "recent_scores_pct": values}))
    for value in (0.0, 25.0, 50.0, 75.0, 100.0):
        variants.append(("behavioral_cue_B_percent", str(value), {**base, "attention_score_pct": value}))
    for ratio in (0.05, 0.15, 0.45, 0.65, 0.85, 1.2):
        variants.append(("integrity_time_ratio", str(ratio), {**base, "time_spent": ratio * base["time_limit"]}))
    for label, text in (
        ("simple", SIMPLE_TEXT),
        ("moderate", MODERATE_TEXT),
        ("technical", TECHNICAL_TEXT),
        ("missing_transcript_neutral_default", None),
    ):
        variants.append(("content_complexity_C", label, {**base, "transcript_text": text}))
    return variants


def build_sensitivity_rows() -> list[dict]:
    reference_inputs = _sensitivity_variants()[2][2]
    reference = compute_crs(**reference_inputs)
    rows = []
    for factor, value, inputs in _sensitivity_variants():
        for profile, weights in WEIGHT_PROFILES:
            rows.append(_result_row(
                evaluation="input_and_weight_sensitivity",
                scenario_name="controlled_one_factor_sweeps",
                varied_factor=factor,
                varied_value=value,
                weight_profile=profile,
                inputs=inputs,
                weights=weights,
                reference_score=reference.crs,
                reference_tier=reference.difficulty,
            ))
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError(f"Refusing to write an empty synthetic evaluation: {path}")
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="exports/synthetic_crs_evaluation")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ablation = build_ablation_rows()
    sensitivity = build_sensitivity_rows()
    _write_csv(out_dir / "synthetic_crs_ablation.csv", ablation)
    _write_csv(out_dir / "synthetic_crs_sensitivity.csv", sensitivity)
    (out_dir / "README.txt").write_text(
        "SYNTHETIC DETERMINISTIC CRS EVALUATION\n"
        "These outputs use constructed inputs and are not learner or participant evidence.\n"
        "Ablation sets one component weight to zero and divides each remaining weight by their sum (0.8), preserving a total weight of 1.\n"
        "Sensitivity sweeps valid score histories, B percentages, timing ratios, transcript complexity examples, and six valid weight profiles.\n"
        "The score-history axis affects both P and T because both use the same assessment history in the documented implementation.\n"
        "Run from backend/: python scripts/crs_offline_evaluation.py --out exports/synthetic_crs_evaluation\n",
        encoding="utf-8",
    )
    print(f"evidence_class={EVIDENCE_CLASS}")
    print(f"ablation_rows={len(ablation)} -> {out_dir / 'synthetic_crs_ablation.csv'}")
    print(f"sensitivity_rows={len(sensitivity)} -> {out_dir / 'synthetic_crs_sensitivity.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
