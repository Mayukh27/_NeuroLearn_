"""
backend/config/crs_config.py

Central, externalized configuration for the Cognitive Readiness Score (CRS)
and the modules that feed it. Nothing in ml/crs.py or its component modules
should hardcode a weight, threshold, or window size — it should be read from
here, so the values can later become trainable / tunable without touching
module code.

This addresses CR1 from the peer-review packet directly: the paper's
Eq. (1) weights (alpha..epsilon) must exist somewhere as actual numbers,
not just as symbols in the manuscript.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import math
from typing import Dict


@dataclass(frozen=True)
class CRSWeights:
    """
    CRS = alpha*P + beta*B + gamma*I + delta*T + epsilon*C

    All five weights must sum to 1.0 so that CRS remains a clean convex
    combination of five [0,1]-normalized component scores (per the peer
    review packet's "Concrete Fixes" §14 recommendation).

    Initial values per the implementation spec: equal weighting (0.20 each).
    Treat these as a documented starting point, not a validated result —
    nothing here should be reported as "tuned" until backed by real data.
    """
    alpha: float = 0.20  # Performance (P)
    beta: float = 0.20   # Behavioral Cue (B)
    gamma: float = 0.20  # Response Integrity (I)
    delta: float = 0.20  # Learning Trend (T)
    epsilon: float = 0.20  # Content Complexity (C)

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)

    def validate(self) -> None:
        if any(not math.isfinite(weight) or not 0.0 <= weight <= 1.0 for weight in self.as_dict().values()):
            raise ValueError(f"CRS weights must be finite values in [0, 1]. Weights: {self.as_dict()}")
        total = self.alpha + self.beta + self.gamma + self.delta + self.epsilon
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"CRS weights must sum to 1.0, got {total:.6f}. "
                f"Weights: {self.as_dict()}"
            )


@dataclass(frozen=True)
class DifficultyThresholds:
    """
    CRS -> difficulty tier mapping, per the implementation spec (Phase 9):

        CRS > hard_threshold        -> "hard"
        medium_threshold..hard      -> "medium"
        CRS < medium_threshold      -> "easy"
    """
    medium_threshold: float = 0.45
    hard_threshold: float = 0.75

    def validate(self) -> None:
        if not (0.0 <= self.medium_threshold < self.hard_threshold <= 1.0):
            raise ValueError(
                "Thresholds must satisfy 0 <= medium_threshold < hard_threshold <= 1, "
                f"got medium={self.medium_threshold}, hard={self.hard_threshold}"
            )


@dataclass(frozen=True)
class PerformanceConfig:
    """Rolling-average window for the Performance (P) component."""
    window_size: int = 5
    # Linear recency weighting: most recent assessment counts `recency_weight`
    # times more than the oldest one in the window. 1.0 = simple mean.
    recency_weight: float = 2.0
    # Score used when a student has no assessment history at all.
    default_score_pct: float = 50.0


@dataclass(frozen=True)
class IntegrityConfig:
    """
    Timing-curve configuration for the Response Integrity (I) component.

    Replaces the old single rule (time_ratio < 0.3 AND score < 70) with a
    continuous curve over time_ratio alone, independent of correctness,
    per Phase 5 and the peer review packet's CR3 finding that the old rule
    never flagged "fast + correct" — exactly the case the paper's
    Introduction uses to motivate this component.
    """
    # Time-ratio band considered "thoughtful pace" (peak integrity score).
    thoughtful_low: float = 0.45
    thoughtful_high: float = 0.85
    # Below this ratio, integrity score is driven toward its minimum
    # (very likely a rushed/guessed response, regardless of correctness).
    very_fast_ratio: float = 0.15
    # Above this ratio, integrity score is also penalized (very likely
    # extended inactivity, distraction, or the student walked away mid-question).
    very_slow_ratio: float = 1.0  # = used the full time limit or more
    # Floor for the integrity score at the extremes (never exactly 0 —
    # a single timing data point should never zero out a student's CRS).
    min_integrity: float = 0.15


@dataclass(frozen=True)
class TrendConfig:
    """Window and slope-to-score mapping for the Learning Trend (T) component."""
    window_size: int = 5
    # A slope (in score-percentage-points per assessment) at or beyond this
    # magnitude is treated as "fully improving" / "fully declining" (T = +-1
    # before rescaling to [0,1]).
    slope_saturation: float = 8.0


@dataclass(frozen=True)
class ComplexityConfig:
    """Weighting between sub-signals inside the Content Complexity (C) component."""
    # Relative weights of the three complexity sub-signals; need not sum to 1
    # since the module renormalizes internally, but are kept here so all
    # three are visible and tunable in one place.
    readability_weight: float = 0.45
    technical_density_weight: float = 0.35
    sentence_length_weight: float = 0.20
    # Score assigned when no transcript text is available at all (neutral —
    # absence of a complexity signal should not silently push CRS toward
    # "easy" or "hard").
    default_complexity: float = 0.5


@dataclass(frozen=True)
class CRSConfig:
    weights: CRSWeights = field(default_factory=CRSWeights)
    thresholds: DifficultyThresholds = field(default_factory=DifficultyThresholds)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    integrity: IntegrityConfig = field(default_factory=IntegrityConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    complexity: ComplexityConfig = field(default_factory=ComplexityConfig)
    # Feature flag: lets adaptive_engine.py fall back to the legacy rule
    # cascade if ever needed (e.g. for A/B comparison, or while components
    # are still being validated), without deleting the old code path.
    crs_enabled: bool = True

    def validate(self) -> None:
        self.weights.validate()
        self.thresholds.validate()


# Module-level singleton — import this, don't re-instantiate, so that any
# future move to a YAML/env-driven config only changes this one spot.
CRS_CONFIG = CRSConfig()
CRS_CONFIG.validate()
