"""Pending-order signal revalidation and cancel/replace orchestration.

D5-owned, paper/test-only orchestration. It consumes signal validity/material-change
semantics supplied by the D3 signal-versioning boundary and cancellation/replace
semantics supplied by the D4 order lifecycle boundary. It performs no exchange I/O.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Protocol

from models.execution import ExecutionSide, ExecutionPlan
from models.signal_journal import SignalObservation


class RevalidationAction(str, Enum):
    KEEP = "KEEP"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"
    NO_TRADE = "NO_TRADE"
    REJECT_DUPLICATE = "REJECT_DUPLICATE"


class CancelReason(str, Enum):
    EXPIRED = "EXPIRED"
    WAIT = "WAIT"
    OPPOSITE_DIRECTION = "OPPOSITE_DIRECTION"
    RISK_BREACH = "RISK_BREACH"
    MATERIAL_SIGNAL_CHANGE = "MATERIAL_SIGNAL_CHANGE"
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
    REPRICING_LIMIT = "REPRICING_LIMIT"
    CUMULATIVE_DRIFT_LIMIT = "CUMULATIVE_DRIFT_LIMIT"
    MARKET_DISTANCE_LIMIT = "MARKET_DISTANCE_LIMIT"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    INVALID_SIGNAL_VALIDITY = "INVALID_SIGNAL_VALIDITY"


class SignalValidityPort(Protocol):
    """Read-only boundary for D3 SignalValidity semantics."""

    @property
    def value(self) -> str: ...


class OrderLifecyclePort(Protocol):
    """Minimal D4 order-lifecycle boundary consumed by D5."""

    def cancel(self, order_id: str, *, reason: str = "", occurred_at: Optional[datetime] = None) -> object: ...

    def replace(self, order_id: str, *, replacement_order_id: str, occurred_at: Optional[datetime] = None) -> object: ...


class PositionState(Protocol):
    """Minimal read-only boundary owned by the position subsystem."""

    def has_open_position(self, symbol: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class RevalidationPolicy:
    """Explicit D5 guardrails; callers may supply stricter policy values."""

    max_repricing_count: int = 2
    max_cumulative_entry_drift_pct: float = 20.0
    minimum_entry_change_pct: float = 2.0
    minimum_confidence_change_points: float = 10.0
    signal_validity_window: timedelta = timedelta(minutes=15)
    max_market_distance_pct: float = 5.0

    def __post_init__(self) -> None:
        if self.max_repricing_count < 0:
            raise ValueError("max_repricing_count must be >= 0")
        for name in (
            "max_cumulative_entry_drift_pct",
            "minimum_entry_change_pct",
            "minimum_confidence_change_points",
            "max_market_distance_pct",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and >= 0")
        if self.signal_validity_window <= timedelta(0):
            raise ValueError("signal_validity_window must be > 0")


@dataclass(frozen=True, slots=True)
class PendingOrder:
    order_id: str
    intent_id: str
    symbol: str
    side: ExecutionSide
    entry_price: float
    quantity: float
    signal_id: str
    signal_timestamp: datetime
    confidence: float
    expires_at: datetime
    repricing_count: int = 0
    cumulative_entry_drift_pct: float = 0.0
    signal_version: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ValueError("order_id must be non-empty")
        if not self.intent_id.strip():
            raise ValueError("intent_id must be non-empty")
        if not self.symbol.strip():
            raise ValueError("symbol must be non-empty")
        if self.side not in (ExecutionSide.BUY, ExecutionSide.SELL):
            raise ValueError("pending order side must be BUY or SELL")
        if not math.isfinite(float(self.entry_price)) or float(self.entry_price) <= 0.0:
            raise ValueError("entry_price must be finite and > 0")
        if not math.isfinite(float(self.quantity)) or float(self.quantity) <= 0.0:
            raise ValueError("quantity must be finite and > 0")
        if not self.signal_id.strip():
            raise ValueError("signal_id must be non-empty")
        if self.signal_timestamp.tzinfo is None or self.signal_timestamp.utcoffset() is None:
            raise ValueError("signal_timestamp must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.expires_at <= self.signal_timestamp:
            raise ValueError("expires_at must be after signal_timestamp")
        if not math.isfinite(float(self.confidence)) or not 0.0 <= float(self.confidence) <= 100.0:
            raise ValueError("confidence must be between 0 and 100")
        if self.repricing_count < 0:
            raise ValueError("repricing_count must be >= 0")
        if not math.isfinite(float(self.cumulative_entry_drift_pct)) or self.cumulative_entry_drift_pct < 0.0:
            raise ValueError("cumulative_entry_drift_pct must be finite and >= 0")
        if self.signal_version is not None and (not isinstance(self.signal_version, int) or isinstance(self.signal_version, bool) or self.signal_version < 1):
            raise ValueError("signal_version must be a positive integer when provided")


@dataclass(frozen=True, slots=True)
class RevalidationResult:
    action: RevalidationAction
    reason: Optional[CancelReason] = None
    replacement: Optional[PendingOrder] = None


class PendingOrderBook:
    """Deterministic in-memory paper/test state; no exchange I/O."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingOrder] = {}
        self._filled: set[str] = set()

    def add(self, order: PendingOrder) -> None:
        existing = self._pending_for_intent(order.intent_id)
        if existing is not None:
            raise ValueError(CancelReason.DUPLICATE_INTENT.value)
        self._pending[order.order_id] = order

    def get(self, order_id: str) -> PendingOrder:
        return self._pending[order_id]

    def pending(self) -> tuple[PendingOrder, ...]:
        return tuple(self._pending.values())

    def active_for_intent(self, intent_id: str) -> Optional[PendingOrder]:
        return self._pending_for_intent(intent_id)

    def cancel(self, order_id: str) -> PendingOrder:
        return self._pending.pop(order_id)

    def replace(self, old_order_id: str, replacement: PendingOrder) -> PendingOrder:
        old = self._pending.pop(old_order_id)
        if self._pending_for_intent(replacement.intent_id) is not None:
            self._pending[old.order_id] = old
            raise ValueError(CancelReason.DUPLICATE_INTENT.value)
        self._pending[replacement.order_id] = replacement
        return old

    def try_fill(self, order_id: str, market_price: float, now: datetime) -> PendingOrder:
        """Fill once if the still-valid pending entry is touched by market price."""
        if order_id in self._filled:
            raise RuntimeError("duplicate fill")
        order = self._pending.get(order_id)
        if order is None:
            raise KeyError(order_id)
        _ensure_aware(now, "now")
        if now >= order.expires_at:
            raise RuntimeError("expired pending order")
        if not math.isfinite(float(market_price)) or float(market_price) <= 0.0:
            raise ValueError("market_price must be finite and > 0")
        touched = market_price <= order.entry_price if order.side is ExecutionSide.BUY else market_price >= order.entry_price
        if not touched:
            raise RuntimeError("entry not touched")
        del self._pending[order_id]
        self._filled.add(order_id)
        return order

    def was_filled(self, order_id: str) -> bool:
        return order_id in self._filled

    def _pending_for_intent(self, intent_id: str) -> Optional[PendingOrder]:
        for order in self._pending.values():
            if order.intent_id == intent_id:
                return order
        return None


def build_pending_order(
    observation: SignalObservation,
    plan: ExecutionPlan,
    *,
    intent_id: str,
    now: Optional[datetime] = None,
    policy: RevalidationPolicy = RevalidationPolicy(),
    signal_version: Optional[int] = None,
) -> PendingOrder:
    """Create a pending order snapshot without contacting execution adapters."""
    if plan.symbol != observation.symbol:
        raise ValueError("execution plan symbol must match signal observation symbol")
    if plan.side not in (ExecutionSide.BUY, ExecutionSide.SELL):
        raise ValueError("pending order requires BUY or SELL execution intent")
    _ensure_aware(observation.timestamp, "observation.timestamp")
    if now is not None:
        _ensure_aware(now, "now")
    return PendingOrder(
        order_id=f"PENDING-{uuid.uuid4().hex[:12]}",
        intent_id=intent_id,
        symbol=observation.symbol,
        side=plan.side,
        entry_price=plan.price,
        quantity=plan.quantity,
        signal_id=observation.observation_id,
        signal_timestamp=observation.timestamp,
        confidence=observation.confidence,
        expires_at=observation.timestamp + policy.signal_validity_window,
        signal_version=signal_version,
    )


def revalidate_pending_order(
    order: PendingOrder,
    new_observation: SignalObservation,
    new_plan: ExecutionPlan,
    *,
    market_price: float,
    now: datetime,
    policy: RevalidationPolicy = RevalidationPolicy(),
    risk_breached: bool = False,
    position_state: Optional[PositionState] = None,
    signal_validity: Optional[str | SignalValidityPort] = None,
    material_signal_change: Optional[bool] = None,
    signal_version: Optional[int] = None,
) -> RevalidationResult:
    """Apply D3 signal validity plus D5 order-revalidation guards."""
    _ensure_aware(now, "now")
    if new_plan.symbol != order.symbol or new_observation.symbol != order.symbol:
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.OPPOSITE_DIRECTION)
    if signal_validity is not None:
        validity = _signal_validity_value(signal_validity)
        if validity == "EXPIRED":
            return RevalidationResult(RevalidationAction.CANCEL, CancelReason.EXPIRED)
        if validity == "STALE":
            if material_signal_change is False:
                return RevalidationResult(RevalidationAction.CANCEL, CancelReason.MATERIAL_SIGNAL_CHANGE)
            return RevalidationResult(RevalidationAction.CANCEL, CancelReason.MATERIAL_SIGNAL_CHANGE)
        if validity != "ACTIVE":
            return RevalidationResult(RevalidationAction.CANCEL, CancelReason.INVALID_SIGNAL_VALIDITY)
    if risk_breached:
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.RISK_BREACH)
    if now >= order.expires_at:
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.EXPIRED)
    decision = str(new_observation.decision).strip().upper()
    if decision == "WAIT" or new_plan.side is ExecutionSide.HOLD:
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.WAIT)
    if new_plan.side not in (ExecutionSide.BUY, ExecutionSide.SELL):
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.OPPOSITE_DIRECTION)
    if new_plan.side is not order.side:
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.OPPOSITE_DIRECTION)
    if position_state is not None and position_state.has_open_position(order.symbol):
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.POSITION_ALREADY_OPEN)

    market_distance_pct = abs(float(new_plan.price) - float(market_price)) / float(market_price) * 100.0
    if not math.isfinite(market_distance_pct) or market_distance_pct > policy.max_market_distance_pct:
        return RevalidationResult(RevalidationAction.NO_TRADE, CancelReason.MARKET_DISTANCE_LIMIT)

    entry_change_pct = abs(float(new_plan.price) - float(order.entry_price)) / float(order.entry_price) * 100.0
    confidence_change = abs(float(new_observation.confidence) - float(order.confidence))
    if material_signal_change is None:
        same_signal = new_observation.observation_id == order.signal_id
        materially_same = entry_change_pct < policy.minimum_entry_change_pct and confidence_change < policy.minimum_confidence_change_points
        material_signal_change = not (same_signal and materially_same)

    if not material_signal_change and entry_change_pct < policy.minimum_entry_change_pct:
        return RevalidationResult(RevalidationAction.KEEP)
    if not material_signal_change and entry_change_pct >= policy.minimum_entry_change_pct:
        material_signal_change = True

    if entry_change_pct < policy.minimum_entry_change_pct and material_signal_change:
        return RevalidationResult(RevalidationAction.CANCEL, CancelReason.MATERIAL_SIGNAL_CHANGE)

    next_drift = order.cumulative_entry_drift_pct + entry_change_pct
    if order.repricing_count >= policy.max_repricing_count:
        return RevalidationResult(RevalidationAction.NO_TRADE, CancelReason.REPRICING_LIMIT)
    if next_drift > policy.max_cumulative_entry_drift_pct:
        return RevalidationResult(RevalidationAction.NO_TRADE, CancelReason.CUMULATIVE_DRIFT_LIMIT)

    replacement = replace(
        order,
        order_id=f"PENDING-{uuid.uuid4().hex[:12]}",
        entry_price=float(new_plan.price),
        quantity=float(new_plan.quantity),
        signal_id=new_observation.observation_id,
        signal_timestamp=new_observation.timestamp,
        confidence=float(new_observation.confidence),
        expires_at=new_observation.timestamp + policy.signal_validity_window,
        repricing_count=order.repricing_count + 1,
        cumulative_entry_drift_pct=next_drift,
        signal_version=signal_version,
    )
    return RevalidationResult(RevalidationAction.REPLACE, CancelReason.MATERIAL_SIGNAL_CHANGE, replacement)


def apply_revalidation_to_lifecycle(
    order: PendingOrder,
    result: RevalidationResult,
    lifecycle: OrderLifecyclePort,
    *,
    now: datetime,
) -> None:
    """Apply D5 decision to the D4 lifecycle boundary; never places an exchange order."""
    _ensure_aware(now, "now")
    if result.action is RevalidationAction.CANCEL:
        lifecycle.cancel(order.order_id, reason=(result.reason.value if result.reason else ""), occurred_at=now)
    elif result.action is RevalidationAction.REPLACE:
        if result.replacement is None:
            raise ValueError("REPLACE requires replacement order")
        lifecycle.replace(order.order_id, replacement_order_id=result.replacement.order_id, occurred_at=now)
    elif result.action in (RevalidationAction.NO_TRADE, RevalidationAction.REJECT_DUPLICATE, RevalidationAction.KEEP):
        return
    else:
        raise ValueError(f"unsupported revalidation action: {result.action.value}")


def _signal_validity_value(value: str | SignalValidityPort) -> str:
    raw = getattr(value, "value", value)
    return str(raw).strip().upper()


def _ensure_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "CancelReason",
    "OrderLifecyclePort",
    "PendingOrder",
    "PendingOrderBook",
    "PositionState",
    "RevalidationAction",
    "RevalidationPolicy",
    "RevalidationResult",
    "SignalValidityPort",
    "apply_revalidation_to_lifecycle",
    "build_pending_order",
    "revalidate_pending_order",
]
