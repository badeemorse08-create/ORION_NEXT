"""Canonical market-universe and opportunity contracts.

Discovery/ranking only. No signal, decision, execution, or position state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class UniverseCandidate:
    symbol: str
    base_asset: str
    quote_asset: str


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    symbol: str
    quote_volume_24h: float
    volatility: float
    spread_bps: Optional[float] = None
    tradable: bool = True
    last_price: Optional[float] = None
    volume_quality: Optional[float] = None
    trend_quality: Optional[float] = None
    momentum_quality: Optional[float] = None
    structure_quality: Optional[float] = None
    price_change_pct_24h: Optional[float] = None
    weighted_avg_price_24h: Optional[float] = None
    trend_direction: Optional[float] = None
    trend_persistence: Optional[float] = None
    momentum_direction: Optional[float] = None
    # Optional fast-recall signals. These are recall evidence only; they never
    # grant Entry Readiness or sizing authority.
    short_term_acceleration: Optional[float] = None
    volume_expansion: Optional[float] = None
    range_expansion: Optional[float] = None
    breakout_score: Optional[float] = None


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    symbol: str
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    symbol: str
    opportunity_score: float
    rank: int
    metrics: MarketMetrics
    eligibility_reasons: tuple[str, ...]
    score_components: tuple[tuple[str, float], ...] = ()
    directional_evidence: float = 0.0
    opportunity_class: str = "UNCLASSIFIED"
    entry_state: str = "C"
    entry_readiness: float = 0.0
    risk_reward: object | None = None
    timeframe_evidence: tuple[object, ...] = ()
    decision_trace: object | None = None
    recall_lanes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OpportunityCandidateSet:
    candidates: tuple[OpportunityCandidate, ...]
    top_n: int
    snapshot_timestamp: float | None = None

    def symbols(self) -> tuple[str, ...]:
        return tuple(candidate.symbol for candidate in self.candidates)
