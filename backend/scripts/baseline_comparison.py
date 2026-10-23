"""
baseline_comparison.py — compares the CRS-driven policy with the
preserved LEGACY rule-cascade baseline on identical synthetic inputs.

The LEGACY cascade uses performance, attention, timing, and trend rules; it
is not an accuracy-only baseline. Policy disagreement in these scenarios is
not evidence of superior learner outcomes.

Run from backend/ (note: requires backend/ on PYTHONPATH):
    PYTHONPATH=. python3 scripts/baseline_comparison.py
"""
from ml.adaptive_engine import AdaptiveEngine

SCENARIOS = [
    ("Fast + correct (CR3 motivating case)", dict(
        current_score=85, attention_score=88, time_spent=4, time_limit=60,
        previous_difficulty="medium", previous_scores=[80, 82, 85],
    )),
    ("Fast + wrong", dict(
        current_score=35, attention_score=45, time_spent=4, time_limit=60,
        previous_difficulty="medium", previous_scores=[40, 38, 35],
    )),
    ("Thoughtful pace, high score, high behavioral_cue", dict(
        current_score=92, attention_score=90, time_spent=35, time_limit=60,
        previous_difficulty="medium", previous_scores=[60, 70, 80, 88, 92],
    )),
    ("Slow/disengaged, declining trend", dict(
        current_score=40, attention_score=30, time_spent=62, time_limit=60,
        previous_difficulty="medium", previous_scores=[70, 60, 50, 40],
    )),
    ("High score but very low behavioral_cue", dict(
        current_score=90, attention_score=15, time_spent=35, time_limit=60,
        previous_difficulty="medium", previous_scores=[85, 88, 90],
    )),
]


def main():
    header = f'{"Scenario":45s} {"Legacy cascade":>15s} {"CRS":>10s} {"Diverges?":>10s}'
    print(header)
    print("-" * len(header))
    for i, (name, kw) in enumerate(SCENARIOS):
        # Fresh engine instances per call — AdaptiveEngine accumulates
        # per-student history, and legacy/CRS paths must not share state.
        legacy_engine = AdaptiveEngine()
        crs_engine = AdaptiveEngine()
        legacy = legacy_engine._determine_difficulty_legacy(student_id=f"legacy{i}", **kw)
        crs = crs_engine.determine_difficulty(student_id=f"crs{i}", **kw)
        ld = legacy["next_assessment_difficulty"]
        cd = crs["next_assessment_difficulty"]
        print(f"{name:45s} {ld:>10s} {cd:>10s} {str(ld != cd):>10s}")


if __name__ == "__main__":
    main()
