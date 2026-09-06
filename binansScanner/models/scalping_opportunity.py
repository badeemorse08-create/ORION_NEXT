"""Scalping-focused opportunity contracts built on the existing D1 candidate boundary."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from models.opportunity import OpportunityCandidate, OpportunityCandidateSet


class OpportunityClass(str, Enum):
    TREND_CONTINUATION = "TREND_CONTINUATION"
    BREAKOUT_ACCELERATION = "BREAKOUT_ACCELERATION"
    PULLBACK_CONTINUATION = "PULLBACK_CONTINUATION"
    UNCLASSIFIED = "UNCLASSIFIED"


class EntryState(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class RejectionReason(str, Enum):
    STRATEGY = "STRATEGY"
    RISK = "RISK"
    CAPITAL = "CAPITAL"
    PAUSE = "PAUSE"
    DUPLICATE_POSITION = "DUPLICATE_POSITION"
    MARKET_DATA_FAILURE = "MARKET_DATA_FAILURE"
    DIRECTIONAL_CONFLICT = "DIRECTIONAL_CONFLICT"
    DIRECTIONAL_INSUFFICIENT = "DIRECTIONAL_INSUFFICIENT"
    CLASSIFICATION_INSUFFICIENT = "CLASSIFICATION_INSUFFICIENT"
    ENTRY_STATE_CONFLICT = "ENTRY_STATE_CONFLICT"


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class TimeframeEvidence:
    timeframe: str
    regime_score: float
    trend_score: float
    trend_direction: float
    momentum_score: float
    momentum_direction: float
    acceleration_score: float
    volume_expansion: float
    range_expansion: float
    structure_score: float
    supertrend_evidence: float
    atr: float


@dataclass(frozen=True, slots=True)
class RiskReward:
    entry_price: float
    stop_price: float
    target_price: float
    risk_per_unit: float
    reward_per_unit: float
    ratio: float
    valid: bool


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    discovered: bool
    eligible: bool
    measured_features: tuple[str, ...]
    opportunity_class: OpportunityClass
    opportunity_score: float
    directional_evidence: float
    entry_state: EntryState
    entry_allowed: bool
    rejection_reasons: tuple[RejectionReason, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScalpingCandidateSet:
    """Broad candidate pool plus a stable active trading set."""

    broad_pool: OpportunityCandidateSet
    active_set: OpportunityCandidateSet
    refreshed: bool
    recall_provenance: tuple[tuple[str, tuple[str, ...]], ...] = ()
    recall_counts: tuple[tuple[str, int], ...] = ()

    @property
    def candidates(self) -> tuple[OpportunityCandidate, ...]:
        return self.active_set.candidates


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """One replay observation with explicit metric semantics."""

    opportunity_captured: bool
    return_pct: float
    entry_accepted: bool
    hold_time_hours: float
    fees_slippage_pct: float
    capital_utilization_pct: float
    profitable_opportunity: bool

    def __post_init__(self) -> None:
        if self.hold_time_hours < 0:
            raise ValueError("hold_time_hours must be non-negative")
        if self.fees_slippage_pct < 0:
            raise ValueError("fees_slippage_pct must be non-negative")
        if not 0.0 <= self.capital_utilization_pct <= 100.0:
            raise ValueError("capital_utilization_pct must be between 0 and 100")
        if self.entry_accepted and not self.opportunity_captured:
            raise ValueError("entry_accepted requires opportunity_captured")


@dataclass(frozen=True, slots=True)
class ReplayEvaluation:
    """Deterministic replay basis with explicit evaluation duration."""

    events: tuple[ReplayEvent, ...]
    evaluation_days: float

    def __post_init__(self) -> None:
        if self.evaluation_days <= 0:
            raise ValueError("evaluation_days must be positive")


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    opportunity_capture_rate: float
    entry_acceptance_rate: float
    trades_per_day: float
    win_rate: float
    expectancy: float
    profit_factor: float
    maximum_drawdown: float
    capital_utilization: float
    average_hold_time: float
    fees_slippage_impact: float
    false_negative_rate: float


@dataclass(frozen=True, slots=True)
class ABComparison:
    baseline: PerformanceMetrics
    improved: PerformanceMetrics
    opportunity_capture_delta: float
    entry_acceptance_delta: float
    expectancy_delta: float
    drawdown_delta: float
    profit_factor_delta: float
    false_negative_delta: float


@dataclass(frozen=True, slots=True)
class SupertrendABResult:
    baseline: PerformanceMetrics
    with_supertrend: PerformanceMetrics
    capture_delta: float
    expectancy_delta: float
    profit_factor_delta: float
    drawdown_delta: float
    false_negative_delta: float


def enrich_candidate(
    candidate: OpportunityCandidate,
    *,
    opportunity_class: OpportunityClass,
    entry_state: EntryState,
    entry_readiness: float,
    risk_reward: Optional[RiskReward],
    timeframe_evidence: tuple[TimeframeEvidence, ...],
    decision_trace: DecisionTrace,
) -> OpportunityCandidate:
    """Append scalping state without changing the D1 terminal type."""
    return OpportunityCandidate(
        candidate.symbol,
        candidate.opportunity_score,
        candidate.rank,
        candidate.metrics,
        candidate.eligibility_reasons,
        candidate.score_components,
        round(decision_trace.directional_evidence, 8),
        opportunity_class.value,
        entry_state.value,
        round(entry_readiness, 8),
        risk_reward,
        timeframe_evidence,
        decision_trace,
        candidate.recall_lanes,
    )
