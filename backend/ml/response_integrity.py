"""
backend/ml/response_integrity.py

Response Integrity (I) component of the Cognitive Readiness Score.

FIXES CR3 (peer review packet): the legacy rule in adaptive_engine.py only
fired when `time_ratio < 0.3 AND current_score < 70` — i.e. it could only
ever flag "fast + wrong", and the paper's own Introduction motivates this
component with the opposite, harder case: a learner who answers correctly
while rapid completion quickly. A rule gated on low score structurally cannot catch
that case. This module computes integrity from timing ALONE, independent
of correctness, exactly as Phase 5 specifies:

    Fast            -> penalty (regardless of whether the answer was right)
    Thoughtful pace -> maximum score
    Very slow       -> penalty

Correctness still matters for CRS overall (that's what the Performance
component P is for) — but it must not be the gate that decides whether
timing-based rapid completion is even checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from config.crs_config import CRS_CONFIG, IntegrityConfig


class TimingCategory(str, Enum):
    VERY_FAST = "very_fast"
    FAST = "fast"
    THOUGHTFUL = "thoughtful_pace"
    SLOW = "slow"
    VERY_SLOW = "very_slow"


@dataclass(frozen=True)
class IntegrityResult:
    integrity_score: float  # [0,1] — peaks in the thoughtful-pace band
    timing_category: TimingCategory
    time_ratio: float
    was_correct: bool
    reason: str


def _triangular_score(
    time_ratio: float, cfg: IntegrityConfig
) -> tuple[float, TimingCategory]:
    """
    Triangular timing curve (Phase 5's suggested shape):

        0 ............ very_fast_ratio ... thoughtful_low ... thoughtful_high ... very_slow_ratio ... +inf
        floor --------> rising ----------> PLATEAU = 1.0 ----> falling ---------> floor

    Below `very_fast_ratio`: minimum score (floor).
    Between very_fast_ratio and thoughtful_low: linear ramp up.
    Between thoughtful_low and thoughtful_high: plateau at 1.0 (max score).
    Between thoughtful_high and very_slow_ratio: linear ramp down.
    At/after very_slow_ratio: minimum score (floor).
    """
    floor = cfg.min_integrity

    if time_ratio <= cfg.very_fast_ratio:
        return floor, TimingCategory.VERY_FAST

    if time_ratio < cfg.thoughtful_low:
        # Ramp from floor (at very_fast_ratio) up to 1.0 (at thoughtful_low).
        span = cfg.thoughtful_low - cfg.very_fast_ratio
        progress = (time_ratio - cfg.very_fast_ratio) / span if span > 0 else 1.0
        score = floor + (1.0 - floor) * progress
        return score, TimingCategory.FAST

    if time_ratio <= cfg.thoughtful_high:
        return 1.0, TimingCategory.THOUGHTFUL

    if time_ratio < cfg.very_slow_ratio:
        # Ramp from 1.0 (at thoughtful_high) down to floor (at very_slow_ratio).
        span = cfg.very_slow_ratio - cfg.thoughtful_high
        progress = (time_ratio - cfg.thoughtful_high) / span if span > 0 else 1.0
        score = 1.0 - (1.0 - floor) * progress
        return score, TimingCategory.SLOW

    return floor, TimingCategory.VERY_SLOW


def compute_response_integrity(
    time_spent: float,
    time_limit: float,
    was_correct: bool,
    config: IntegrityConfig = CRS_CONFIG.integrity,
) -> IntegrityResult:
    """
    Compute the Response Integrity (I) component from timing alone.

    Args:
        time_spent: seconds the student actually took.
        time_limit: seconds allotted for the question/assessment.
        was_correct: whether the response was correct — used ONLY for the
            human-readable explanation (e.g. distinguishing "fast + correct"
            from "fast + wrong" in the reason string), never to gate whether
            the timing penalty applies. This is the direct fix for CR3.
        config: IntegrityConfig (timing bands, floor).

    Returns:
        IntegrityResult with integrity_score in [0,1].
    """
    if not math.isfinite(time_spent) or time_spent < 0:
        raise ValueError("time_spent must be finite and non-negative")
    if not math.isfinite(time_limit) or time_limit <= 0:
        raise ValueError("time_limit must be finite and positive")
    time_ratio = time_spent / time_limit
    if not math.isfinite(time_ratio):
        raise ValueError("time_spent / time_limit must be finite")

    score, category = _triangular_score(time_ratio, config)

    if category == TimingCategory.VERY_FAST:
        if was_correct:
            reason = (
                f"Answered correctly in {time_ratio:.0%} of the allotted time — "
                "too fast to confirm genuine reasoning; likely a guess or prior "
                "exposure to this exact item. Flagged regardless of correctness."
            )
        else:
            reason = (
                f"Answered incorrectly in {time_ratio:.0%} of the allotted time — "
                "consistent with rapid rapid completion."
            )
    elif category == TimingCategory.FAST:
        reason = (
            f"Completed in {time_ratio:.0%} of the allotted time — faster than "
            "the thoughtful-pace band, mild integrity discount."
        )
    elif category == TimingCategory.THOUGHTFUL:
        reason = (
            f"Completed in {time_ratio:.0%} of the allotted time — within the "
            "thoughtful-pace band; full integrity score."
        )
    elif category == TimingCategory.SLOW:
        reason = (
            f"Completed in {time_ratio:.0%} of the allotted time — slower than "
            "the thoughtful-pace band, mild integrity discount (possible "
            "difficulty or distraction)."
        )
    else:  # VERY_SLOW
        reason = (
            f"Took {time_ratio:.0%} of the allotted time (at or beyond the "
            "limit) — consistent with extended inactivity or being away from the "
            "screen for part of the question."
        )

    return IntegrityResult(
        integrity_score=round(max(0.0, min(1.0, score)), 4),
        timing_category=category,
        time_ratio=round(time_ratio, 4),
        was_correct=was_correct,
        reason=reason,
    )
