"""Deterministic contracts for ORION paper A/B experiments.

This module is experiment infrastructure only. It does not own strategy,
allocation, accounting, execution, or trading-control semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Mapping, Optional

from models.capital_management import CapitalMode


@dataclass(frozen=True, slots=True)
class PaperExperimentConfig:
    """Explicit experiment inputs; values are never business-policy defaults."""

    experiment_id: str
    starting_capital: float
    capital_mode: CapitalMode
    allocation_rate: Optional[float]
    fixed_allocation: Optional[float]
    broad_pool_size: int
    active_top_n: int
    run_duration: timedelta
    universe_mode: str
    output_dir: Path

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("experiment_id must be non-empty")
        if self.starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        if self.broad_pool_size <= 0 or self.active_top_n <= 0:
            raise ValueError("pool sizes must be positive")
        if self.active_top_n > self.broad_pool_size:
            raise ValueError("active_top_n cannot exceed broad_pool_size")
        if self.run_duration.total_seconds() <= 0:
            raise ValueError("run_duration must be positive")
        if self.universe_mode not in {"dynamic", "fixed"}:
            raise ValueError("universe_mode must be dynamic or fixed")
        if self.allocation_rate is not None and self.allocation_rate < 0:
            raise ValueError("allocation_rate must be non-negative")
        if self.fixed_allocation is not None and self.fixed_allocation < 0:
            raise ValueError("fixed_allocation must be non-negative")

    @classmethod
    def reference_50(cls, *, experiment_id: str, output_dir: Path) -> "PaperExperimentConfig":
        """Reference experiment configuration; $50 is deliberately not a system default."""
        return cls(
            experiment_id=experiment_id,
            starting_capital=50.0,
            capital_mode=CapitalMode.FIXED_ALLOCATION,
            allocation_rate=0.10,
            fixed_allocation=None,
            broad_pool_size=100,
            active_top_n=10,
            run_duration=timedelta(hours=8),
            universe_mode="dynamic",
            output_dir=output_dir,
        )


@dataclass(frozen=True, slots=True)
class PaperExperimentMetrics:
    """A/B report schema with explicit deterministic metric definitions.

    ``profit_factor`` is ``gross_profit / gross_loss``. When gross profit is
    positive and gross loss is zero it is ``+inf``; when both are zero it is
    deterministically ``0.0``. ``false_negative_rate`` is
    ``missed_profitable_opportunities / profitable_opportunities`` and is
    deterministically ``0.0`` when its denominator is zero. ``capital_utilization``
    is ``peak_committed_capital / starting_equity`` and is ``0.0`` when there
    is no committed-capital sample or starting equity is non-positive.
    """

    starting_equity: float
    ending_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fees: float
    slippage: float
    maximum_drawdown: float
    trades: int
    fills: int
    win_rate: float
    expectancy: float
    profit_factor: float
    capital_utilization: float
    average_hold_time: float
    opportunity_capture_rate: float
    entry_acceptance_rate: float
    false_negative_rate: float
    rejected_by_strategy: int
    rejected_by_capital: int
    rejected_by_pause: int
    rejected_by_duplicate: int
    market_data_failures: int
    recovery_count: int
    duplicate_event_count: int

    def __post_init__(self) -> None:
        if self.trades < 0 or self.fills < 0:
            raise ValueError("trade/fill counts must be non-negative")
        for name in (
            "starting_equity", "ending_equity", "realized_pnl", "unrealized_pnl",
            "fees", "slippage", "maximum_drawdown", "win_rate", "expectancy",
            "capital_utilization", "average_hold_time", "opportunity_capture_rate",
            "entry_acceptance_rate", "false_negative_rate",
        ):
            value = float(getattr(self, name))
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{name} must be finite")
        profit_factor = float(self.profit_factor)
        if profit_factor != profit_factor or profit_factor == float("-inf"):
            raise ValueError("profit_factor must be a number or positive infinity")

    def as_dict(self) -> dict[str, object]:
        return {
            "starting_equity": self.starting_equity,
            "ending_equity": self.ending_equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "fees": self.fees,
            "slippage": self.slippage,
            "maximum_drawdown": self.maximum_drawdown,
            "trades": self.trades,
            "fills": self.fills,
            "win_rate": self.win_rate,
            "expectancy": self.expectancy,
            "profit_factor": self.profit_factor,
            "capital_utilization": self.capital_utilization,
            "average_hold_time": self.average_hold_time,
            "opportunity_capture_rate": self.opportunity_capture_rate,
            "entry_acceptance_rate": self.entry_acceptance_rate,
            "false_negative_rate": self.false_negative_rate,
            "rejected_by_strategy": self.rejected_by_strategy,
            "rejected_by_capital": self.rejected_by_capital,
            "rejected_by_pause": self.rejected_by_pause,
            "rejected_by_duplicate": self.rejected_by_duplicate,
            "market_data_failures": self.market_data_failures,
            "recovery_count": self.recovery_count,
            "duplicate_event_count": self.duplicate_event_count,
        }


@dataclass(frozen=True, slots=True)
class PaperExperimentObservation:
    """Canonical observation used to derive report metrics.

    ``profitable_opportunities`` is the authoritative denominator for the
    false-negative rate, and ``missed_profitable_opportunities`` is its
    authoritative numerator. The legacy ``actionable_outcomes`` and
    ``false_negatives`` fields remain accepted for compatibility but are not
    used to derive ``false_negative_rate``.
    """

    opportunity_evaluated: int = 0
    opportunity_accepted: int = 0
    entry_evaluated: int = 0
    entry_accepted: int = 0
    actionable_outcomes: int = 0
    false_negatives: int = 0
    profitable_opportunities: int = 0
    missed_profitable_opportunities: int = 0
    strategy_rejections: int = 0
    capital_rejections: int = 0
    pause_rejections: int = 0
    duplicate_rejections: int = 0
    market_data_failures: int = 0
    recovery_count: int = 0
    duplicate_event_count: int = 0
    committed_capital_samples: tuple[float, ...] = ()
    closed_trade_pnl: tuple[float, ...] = ()
    hold_seconds: tuple[float, ...] = ()


def _rate(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def build_metrics(*, account: Mapping[str, float], observation: PaperExperimentObservation) -> PaperExperimentMetrics:
    """Build the complete metric contract from canonical accounting/observations."""
    pnls = tuple(float(v) for v in observation.closed_trade_pnl)
    winners = tuple(v for v in pnls if v > 0.0)
    losers = tuple(v for v in pnls if v < 0.0)
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    if gross_loss > 0.0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0.0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0
    committed = tuple(float(v) for v in observation.committed_capital_samples)
    starting = float(account["starting_equity"])
    utilization = (max(committed) / starting) if committed and starting > 0 else 0.0
    return PaperExperimentMetrics(
        starting_equity=starting,
        ending_equity=float(account["ending_equity"]),
        realized_pnl=float(account["realized_pnl"]),
        unrealized_pnl=float(account["unrealized_pnl"]),
        fees=float(account["fees"]),
        slippage=float(account["slippage"]),
        maximum_drawdown=float(account["maximum_drawdown"]),
        trades=len(pnls),
        fills=int(account.get("fills", 0)),
        win_rate=_rate(len(winners), len(pnls)),
        expectancy=(sum(pnls) / len(pnls)) if pnls else 0.0,
        profit_factor=profit_factor,
        capital_utilization=utilization,
        average_hold_time=(sum(observation.hold_seconds) / len(observation.hold_seconds)) if observation.hold_seconds else 0.0,
        opportunity_capture_rate=_rate(observation.opportunity_accepted, observation.opportunity_evaluated),
        entry_acceptance_rate=_rate(observation.entry_accepted, observation.entry_evaluated),
        false_negative_rate=_rate(observation.missed_profitable_opportunities, observation.profitable_opportunities),
        rejected_by_strategy=observation.strategy_rejections,
        rejected_by_capital=observation.capital_rejections,
        rejected_by_pause=observation.pause_rejections,
        rejected_by_duplicate=observation.duplicate_rejections,
        market_data_failures=observation.market_data_failures,
        recovery_count=observation.recovery_count,
        duplicate_event_count=observation.duplicate_event_count,
    )


@dataclass(frozen=True, slots=True)
class PaperABComparison:
    """Neutral comparison contract for later A/B runs; it does not rank A vs B."""

    baseline: PaperExperimentMetrics
    improved: PaperExperimentMetrics

    def delta(self, field_name: str) -> float:
        """Return the deterministic improved-minus-baseline metric delta.

        ``profit_factor`` may legitimately be ``+inf`` when there are profits
        and no losses. IEEE ``inf - inf`` would yield ``NaN``, which is not a
        valid deterministic A/B result. Therefore the comparison contract
        explicitly maps infinity cases to ``0.0`` when both sides are ``+inf``
        and to ``+inf`` when exactly one side is ``+inf``. Finite values retain
        the normal ``improved - baseline`` delta.
        """
        if field_name not in self.baseline.as_dict() or field_name in {"trades", "fills"}:
            raise ValueError("field is not a numeric comparison metric")
        baseline = float(getattr(self.baseline, field_name))
        improved = float(getattr(self.improved, field_name))
        if field_name == "profit_factor" and (baseline == float("inf") or improved == float("inf")):
            if baseline == float("inf") and improved == float("inf"):
                return 0.0
            return float("inf")
        return improved - baseline

    def structurally_equal(self) -> bool:
        left = self.baseline.as_dict()
        right = self.improved.as_dict()
        return left.keys() == right.keys()


__all__ = [
    "PaperExperimentConfig",
    "PaperExperimentMetrics",
    "PaperExperimentObservation",
    "PaperABComparison",
    "build_metrics",
]
