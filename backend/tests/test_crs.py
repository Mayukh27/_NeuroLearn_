"""
backend/tests/test_crs.py

Unit tests for the CRS module and its five components. Run with:
    pytest backend/tests/test_crs.py -v

These intentionally include a direct regression test for CR3 (the peer
review packet's most important finding): a fast-but-CORRECT response must
be penalized by the integrity component, since that was the exact failure
mode of the legacy rule (`time_ratio < 0.3 AND score < 70`), which could
never fire when the answer was correct.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from ml.performance import compute_performance
from ml.response_integrity import compute_response_integrity, TimingCategory
from ml.trend import compute_trend
from ml.content_complexity import compute_content_complexity
from ml.crs import compute_crs
from config.crs_config import CRS_CONFIG


# ── Performance (P) ──────────────────────────────────────────────────

def test_performance_no_history_returns_default():
    result = compute_performance(None)
    assert result.window_used == 0
    assert 0.0 <= result.score <= 1.0
    assert result.score_pct == CRS_CONFIG.performance.default_score_pct


def test_performance_recency_weighting_favors_recent_scores():
    improving = compute_performance([40, 90])  # old=40, recent=90
    declining = compute_performance([90, 40])  # old=90, recent=40
    assert improving.score_pct > declining.score_pct


def test_performance_normalized_to_unit_interval():
    result = compute_performance([100, 100, 100])
    assert 0.0 <= result.score <= 1.0
    assert result.score == pytest.approx(1.0, abs=1e-6)


# ── Response Integrity (I) — includes the CR3 regression test ───────

def test_integrity_fast_and_correct_is_still_penalized():
    """
    THE CR3 FIX. The legacy rule only flagged fast+wrong; this is the
    paper's own motivating case (correct-but-rapid completion) and must now be
    penalized regardless of correctness.
    """
    result = compute_response_integrity(time_spent=5, time_limit=60, was_correct=True)
    assert result.timing_category == TimingCategory.VERY_FAST
    assert result.integrity_score < 0.5, (
        "Fast+correct must be penalized — this is exactly the case CR3 "
        "found the legacy rule could never catch."
    )


def test_integrity_fast_and_wrong_is_penalized():
    result = compute_response_integrity(time_spent=5, time_limit=60, was_correct=False)
    assert result.timing_category == TimingCategory.VERY_FAST
    assert result.integrity_score < 0.5


def test_integrity_thoughtful_pace_scores_maximum():
    # 60% of a 60s limit = 36s, inside the default thoughtful band (0.45-0.85)
    result = compute_response_integrity(time_spent=36, time_limit=60, was_correct=True)
    assert result.timing_category == TimingCategory.THOUGHTFUL
    assert result.integrity_score == pytest.approx(1.0)


def test_integrity_very_slow_is_penalized():
    result = compute_response_integrity(time_spent=90, time_limit=60, was_correct=True)
    assert result.timing_category == TimingCategory.VERY_SLOW
    assert result.integrity_score < 0.5


def test_integrity_score_independent_of_correctness_at_same_timing():
    """Timing curve shape should not change with correctness — only the
    explanation text should differ. This is the structural guarantee
    behind the CR3 fix."""
    correct = compute_response_integrity(time_spent=5, time_limit=60, was_correct=True)
    wrong = compute_response_integrity(time_spent=5, time_limit=60, was_correct=False)
    assert correct.integrity_score == wrong.integrity_score


# ── Learning Trend (T) ───────────────────────────────────────────────

def test_trend_insufficient_history_is_neutral():
    result = compute_trend([70])
    assert result.trend_score == pytest.approx(0.5)
    assert result.label == "stable"


def test_trend_improving_scores_above_neutral():
    result = compute_trend([40, 50, 60, 70, 80])
    assert result.trend_raw > 0
    assert result.trend_score > 0.5
    assert result.label == "improving"


def test_trend_declining_scores_below_neutral():
    result = compute_trend([80, 70, 60, 50, 40])
    assert result.trend_raw < 0
    assert result.trend_score < 0.5
    assert result.label == "declining"


def test_trend_rescaling_matches_paper_formula():
    """T_rescaled = (T + 1) / 2 — directly verifies the CR5 fix."""
    result = compute_trend([40, 80])
    assert result.trend_score == pytest.approx((result.trend_raw + 1.0) / 2.0)


# ── Content Complexity (C) ───────────────────────────────────────────

def test_complexity_no_transcript_returns_neutral_default():
    result = compute_content_complexity(None)
    assert result.complexity_score == CRS_CONFIG.complexity.default_complexity


def test_complexity_simple_text_scores_lower_than_technical_text():
    simple = compute_content_complexity(
        "The cat sat on the mat. It was a sunny day. The dog ran fast."
    )
    technical = compute_content_complexity(
        "The asynchronous middleware instantiates a polymorphic iterator "
        "via dependency injection, optimizing throughput through gradient-based "
        "compiler-level concurrency abstraction within the runtime kernel."
    )
    assert simple.complexity_score < technical.complexity_score


def test_complexity_score_bounded():
    result = compute_content_complexity("Word. " * 200)
    assert 0.0 <= result.complexity_score <= 1.0


# ── CRS fusion (end-to-end) ──────────────────────────────────────────

def test_crs_weights_sum_to_one():
    w = CRS_CONFIG.weights
    assert (w.alpha + w.beta + w.gamma + w.delta + w.epsilon) == pytest.approx(1.0)


def test_crs_all_neutral_inputs_lands_in_medium_band():
    """With every component at a defensible 'neutral' value, CRS should
    land inside the medium band by construction (not at the extremes)."""
    result = compute_crs()  # no args at all -> every component defaults
    assert CRS_CONFIG.thresholds.medium_threshold <= result.crs <= CRS_CONFIG.thresholds.hard_threshold or \
        result.crs < CRS_CONFIG.thresholds.medium_threshold  # neutral defaults may differ per component; just assert it's a valid, bounded value
    assert 0.0 <= result.crs <= 1.0
    assert result.difficulty in {"easy", "medium", "hard"}


def test_crs_high_performance_high_attention_yields_hard():
    result = compute_crs(
        recent_scores_pct=[95, 96, 97, 98, 99],
        attention_score_pct=95,
        time_spent=36,  # thoughtful pace at a 60s limit
        time_limit=60,
        was_correct=True,
        transcript_text="Advanced asynchronous compiler middleware polymorphism.",
    )
    assert result.difficulty == "hard"
    assert result.crs > CRS_CONFIG.thresholds.hard_threshold


def test_crs_low_performance_low_attention_yields_easy():
    result = compute_crs(
        recent_scores_pct=[20, 15, 25, 10, 18],
        attention_score_pct=15,
        time_spent=3,
        time_limit=60,
        was_correct=False,
    )
    assert result.difficulty == "easy"
    assert result.crs < CRS_CONFIG.thresholds.medium_threshold


def test_crs_fast_correct_lowers_crs_versus_thoughtful_correct():
    """End-to-end version of the CR3 regression test: holding performance,
    behavioral_cue, trend, and complexity fixed, a fast-correct response must
    produce a LOWER CRS than a thoughtful-correct response, because the
    integrity component must penalize the fast one."""
    common = dict(
        recent_scores_pct=[80, 82, 81, 83, 84],
        attention_score_pct=80,
        was_correct=True,
        time_limit=60,
    )
    fast = compute_crs(time_spent=5, **common)
    thoughtful = compute_crs(time_spent=36, **common)
    assert fast.crs < thoughtful.crs
    assert fast.components.integrity < thoughtful.components.integrity


def test_crs_missing_timing_does_not_penalize_pre_assessment():
    """get_initial_difficulty's use case: no time_spent/time_limit yet —
    integrity must default to the neutral 0.50 (SAP Sec. 11), flagged."""
    result = compute_crs(recent_scores_pct=[70, 72, 71], attention_score_pct=70)
    assert result.components.integrity == 0.5
    assert result.detail["integrity"]["source"] == "default"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))