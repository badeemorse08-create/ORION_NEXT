"""Deterministic capital management and portfolio allocation contracts.

This layer owns sizing/allocation policy only. It does not discover opportunities,
execute orders, or implement accounting. Accounting state is supplied through a
small read-only boundary so the package can consume the authoritative D6 ledger
when that package is integrated into main.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable, Protocol, Optional


class CapitalMode(str, Enum):
    FIXED_ALLOCATION = "FIXED_ALLOCATION"
    COMPOUNDING = "COMPOUNDING"


class AllocationRejection(str, Enum):
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    MAX_CONCURRENT_POSITIONS = "MAX_CONCURRENT_POSITIONS"
    DUPLICATE_ALLOCATION = "DUPLICATE_ALLOCATION"
    INVALID_NOTIONAL = "INVALID_NOTIONAL"
    INELIGIBLE_OPPORTUNITY = "INELIGIBLE_OPPORTUNITY"


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _positive(value: float, name: str) -> float:
    value = _finite(value, name)
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


class AccountingView(Protocol):
    """Read-only compatibility boundary for the authoritative accounting layer."""

    @property
    def equity(self) -> float: ...

    @property
    def realized_pnl(self) -> float: ...

    @property
    def unrealized_pnl(self) -> float: ...

    @property
    def reserved_capital(self) -> float: ...

    @property
    def committed_capital(self) -> float: ...


@dataclass(frozen=True, slots=True)
class CapitalSnapshot:
    starting_capital: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    reserved_capital: float
    committed_capital: float
    trading_capital: float
    available_capital: float

    def __post_init__(self) -> None:
        for name in (
            "starting_capital", "total_equity", "realized_pnl", "unrealized_pnl",
            "reserved_capital", "committed_capital", "trading_capital", "available_capital",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.starting_capital <= 0.0:
            raise ValueError("starting_capital must be positive")
        if self.reserved_capital < 0.0 or self.committed_capital < 0.0:
            raise ValueError("reserved_capital and committed_capital must be non-negative")
        if self.available_capital < -1e-9:
            raise ValueError("available_capital cannot be negative")


@dataclass(frozen=True, slots=True)
class AllocationConfig:
    starting_capital: float = 50.0
    mode: CapitalMode = CapitalMode.FIXED_ALLOCATION
    allocation_rate: Optional[float] = None
    fixed_allocation: Optional[float] = None
    max_concurrent_positions: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "starting_capital", _positive(self.starting_capital, "starting_capital"))
        if self.allocation_rate is not None:
            rate = _finite(self.allocation_rate, "allocation_rate")
            if rate <= 0.0 or rate > 1.0:
                raise ValueError("allocation_rate must be > 0 and <= 1")
            object.__setattr__(self, "allocation_rate", rate)
        if self.fixed_allocation is not None:
            object.__setattr__(self, "fixed_allocation", _positive(self.fixed_allocation, "fixed_allocation"))
        if self.max_concurrent_positions is not None and self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be at least 1 when configured")
        if self.allocation_rate is None and self.fixed_allocation is None:
            raise ValueError("allocation_rate or fixed_allocation must be configured")


@dataclass(frozen=True, slots=True)
class AllocationCandidate:
    """Neutral boundary representation for a D1-ranked opportunity."""

    symbol: str
    rank: int
    opportunity_score: float
    eligible: bool = True
    intent: str = "ENTRY"

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        if self.rank < 1:
            raise ValueError("rank must be positive")
        object.__setattr__(self, "opportunity_score", _finite(self.opportunity_score, "opportunity_score"))
        if not self.intent:
            raise ValueError("intent must be non-empty")


@dataclass(frozen=True, slots=True)
class AllocationAudit:
    allocation_id: str
    symbol: str
    intent: str
    desired_allocation: float
    required_symbol_minimum: float
    final_order_notional: float
    capital_mode: CapitalMode
    available_capital_before: float
    reserved_capital_before: float
    available_capital_after: float
    reason: str
    minimum_adjustment_applied: bool
    accepted: bool
    rejection_reason: Optional[AllocationRejection] = None


@dataclass(frozen=True, slots=True)
class PendingReservation:
    allocation_id: str
    symbol: str
    intent: str
    notional: float


class CapitalManager:
    """Deterministic sizing and allocation engine over externally-owned accounting state."""

    def __init__(self, config: AllocationConfig, *, accounting: Optional[AccountingView] = None) -> None:
        self.config = config
        self._accounting = accounting
        self._starting_capital = config.starting_capital
        self._realized_pnl = 0.0
        self._unrealized_pnl = 0.0
        self._reserved: dict[str, PendingReservation] = {}
        self._committed: dict[str, float] = {}
        self._positions: set[str] = set()
        self._audit: list[AllocationAudit] = []

    @property
    def audits(self) -> tuple[AllocationAudit, ...]:
        return tuple(self._audit)

    @property
    def realized_pnl(self) -> float:
        return self._accounting.realized_pnl if self._accounting is not None else self._realized_pnl

    @property
    def unrealized_pnl(self) -> float:
        return self._accounting.unrealized_pnl if self._accounting is not None else self._unrealized_pnl

    @property
    def total_equity(self) -> float:
        if self._accounting is not None:
            return _finite(self._accounting.equity, "accounting.equity")
        return self._starting_capital + self.realized_pnl + self.unrealized_pnl

    @property
    def reserved_capital(self) -> float:
        if self._accounting is not None:
            return max(_finite(self._accounting.reserved_capital, "accounting.reserved_capital"), 0.0)
        return sum(item.notional for item in self._reserved.values())

    @property
    def committed_capital(self) -> float:
        if self._accounting is not None:
            return max(_finite(self._accounting.committed_capital, "accounting.committed_capital"), 0.0)
        return sum(self._committed.values())

    @property
    def trading_capital(self) -> float:
        return max(self._starting_capital + self.realized_pnl, 0.0)

    @property
    def available_capital(self) -> float:
        available = self.trading_capital - self.reserved_capital - self.committed_capital
        if available < -1e-9:
            raise ValueError("capital commitments exceed trading capital")
        return max(available, 0.0)

    def snapshot(self) -> CapitalSnapshot:
        return CapitalSnapshot(
            self._starting_capital, self.total_equity, self.realized_pnl, self.unrealized_pnl,
            self.reserved_capital, self.committed_capital, self.trading_capital, self.available_capital,
        )

    def record_realized_pnl(self, pnl: float) -> None:
        if self._accounting is not None:
            raise ValueError("realized P&L is owned by the accounting layer")
        self._realized_pnl += _finite(pnl, "pnl")

    def record_unrealized_pnl(self, pnl: float) -> None:
        if self._accounting is not None:
            raise ValueError("unrealized P&L is owned by the accounting layer")
        self._unrealized_pnl = _finite(pnl, "pnl")

    def set_active_positions(self, symbols: Iterable[str]) -> None:
        self._positions = set(symbols)

    def _desired_allocation(self) -> float:
        if self.config.mode is CapitalMode.FIXED_ALLOCATION:
            if self.config.fixed_allocation is not None:
                return self.config.fixed_allocation
            assert self.config.allocation_rate is not None
            return self.config.starting_capital * self.config.allocation_rate
        if self.config.allocation_rate is None:
            raise ValueError("COMPOUNDING requires allocation_rate")
        return self.trading_capital * self.config.allocation_rate

    def desired_allocation(self) -> float:
        return self._desired_allocation()

    @staticmethod
    def _duplicate_key(symbol: str, intent: str) -> str:
        return f"{symbol}::{intent}"

    def _concurrent_count(self) -> int:
        pending_symbols = {reservation.symbol for reservation in self._reserved.values()}
        return len(self._positions | pending_symbols)

    def calculate(self, candidate: AllocationCandidate, required_symbol_minimum: float) -> AllocationAudit:
        minimum = _finite(required_symbol_minimum, "required_symbol_minimum")
        if minimum < 0.0:
            raise ValueError("required_symbol_minimum cannot be negative")
        before = self.available_capital
        reserved_before = self.reserved_capital
        desired = self._desired_allocation()
        final = max(desired, minimum)
        adjusted = minimum > desired
        allocation_id = self._duplicate_key(candidate.symbol, candidate.intent)

        rejection: Optional[AllocationRejection] = None
        reason = "ALLOCATED"
        accepted = True
        if not candidate.eligible:
            rejection = AllocationRejection.INELIGIBLE_OPPORTUNITY
            reason = rejection.value
            accepted = False
        elif candidate.symbol in self._positions or allocation_id in self._reserved or allocation_id in self._committed:
            rejection = AllocationRejection.DUPLICATE_ALLOCATION
            reason = rejection.value
            accepted = False
        elif self.config.max_concurrent_positions is not None and self._concurrent_count() >= self.config.max_concurrent_positions:
            rejection = AllocationRejection.MAX_CONCURRENT_POSITIONS
            reason = rejection.value
            accepted = False
        elif not math.isfinite(final) or final <= 0.0:
            rejection = AllocationRejection.INVALID_NOTIONAL
            reason = rejection.value
            accepted = False
        elif final > before + 1e-9:
            rejection = AllocationRejection.INSUFFICIENT_CAPITAL
            reason = rejection.value
            accepted = False

        after = before - final if accepted else before
        if accepted:
            self._reserved[allocation_id] = PendingReservation(allocation_id, candidate.symbol, candidate.intent, final)

        audit = AllocationAudit(
            allocation_id, candidate.symbol, candidate.intent, desired, minimum, final, self.config.mode,
            before, reserved_before, max(after, 0.0), reason, adjusted, accepted, rejection,
        )
        self._audit.append(audit)
        return audit

    def allocate_ranked(self, candidates: Iterable[tuple[AllocationCandidate, float]]) -> tuple[AllocationAudit, ...]:
        ordered = sorted(candidates, key=lambda item: (item[0].rank, -item[0].opportunity_score, item[0].symbol, item[0].intent))
        return tuple(self.calculate(candidate, minimum) for candidate, minimum in ordered)

    def on_cancel(self, allocation_id: str) -> None:
        self._reserved.pop(allocation_id, None)

    def on_reject(self, allocation_id: str) -> None:
        self._reserved.pop(allocation_id, None)

    def on_expire(self, allocation_id: str) -> None:
        self._reserved.pop(allocation_id, None)

    def on_fill(self, allocation_id: str) -> None:
        reservation = self._reserved.pop(allocation_id, None)
        if reservation is None:
            raise ValueError("unknown allocation")
        self._committed[allocation_id] = reservation.notional
        self._positions.add(reservation.symbol)

    def on_exit(self, allocation_id: str) -> None:
        committed = self._committed.pop(allocation_id, None)
        if committed is None:
            raise ValueError("unknown active allocation")
        symbol = allocation_id.split("::", 1)[0]
        self._positions.discard(symbol)

    def sync_from_accounting(self, accounting: AccountingView) -> None:
        self._accounting = accounting

    def as_dict(self) -> dict[str, object]:
        snapshot = self.snapshot()
        return {
            "starting_capital": snapshot.starting_capital,
            "total_equity": snapshot.total_equity,
            "realized_pnl": snapshot.realized_pnl,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "reserved_capital": snapshot.reserved_capital,
            "committed_capital": snapshot.committed_capital,
            "trading_capital": snapshot.trading_capital,
            "available_capital": snapshot.available_capital,
            "mode": self.config.mode.value,
        }
