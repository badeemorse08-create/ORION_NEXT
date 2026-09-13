"""Deterministic order and position state machines for the paper bot.

This module owns lifecycle state, transition validation, fill metadata,
position actions, duplicate protection, and event replay. It does not own
cancel/replace policy, live execution, ingestion, ranking, or signal versioning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional
import uuid

from models.execution import ExecutionRequest, ExecutionResult, ExecutionSide, ExecutionStatus


class LifecycleError(Exception):
    """Base lifecycle error."""


class InvalidTransitionError(LifecycleError):
    """Illegal state mutation."""


class DuplicatePositionError(LifecycleError):
    """More than one active position for one symbol."""


class ReplayError(LifecycleError):
    """Malformed or non-deterministic event history."""


class OrderState(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REPLACED = "REPLACED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class PositionState(str, Enum):
    OPEN = "OPEN"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    REVERSE = "REVERSE"
    CLOSED = "CLOSED"


class PositionAction(str, Enum):
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    REVERSE = "REVERSE"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    RISK_EXIT = "RISK_EXIT"
    TIME_EXIT = "TIME_EXIT"


@dataclass(frozen=True, slots=True)
class FillMetadata:
    fill_id: str
    quantity: float
    price: float
    occurred_at: datetime
    source: str = "PAPER"
    fee: float = 0.0
    fee_asset: Optional[str] = None

    def __post_init__(self) -> None:
        quantity = float(self.quantity)
        price = float(self.price)
        fee = float(self.fee)
        if not self.fill_id.strip():
            raise LifecycleError("fill_id is required")
        if not math.isfinite(quantity) or quantity <= 0:
            raise LifecycleError("fill quantity must be finite and > 0")
        if not math.isfinite(price) or price <= 0:
            raise LifecycleError("fill price must be finite and > 0")
        if not math.isfinite(fee) or fee < 0:
            raise LifecycleError("fill fee must be finite and >= 0")
        if self.occurred_at.tzinfo is None:
            raise LifecycleError("fill timestamp must be timezone-aware")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "fee", fee)


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    event_id: str
    aggregate_id: str
    aggregate_type: str
    sequence: int
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ReplayError("event sequence must be positive")
        if self.occurred_at.tzinfo is None:
            raise ReplayError("event timestamp must be timezone-aware")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": dict(self.payload),
        }


class LifecycleEventStore:
    """Append-only, deterministic event history."""

    def __init__(self, events: Iterable[LifecycleEvent] = ()) -> None:
        self._events: list[LifecycleEvent] = []
        for event in events:
            self.append(event)

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(self._events)

    def append(self, event: LifecycleEvent) -> None:
        expected = len(self._events) + 1
        if event.sequence != expected:
            raise ReplayError(f"expected event sequence {expected}, got {event.sequence}")
        if any(existing.event_id == event.event_id for existing in self._events):
            raise ReplayError(f"duplicate event id: {event.event_id}")
        self._events.append(event)

    def for_aggregate(self, aggregate_id: str) -> tuple[LifecycleEvent, ...]:
        return tuple(e for e in self._events if e.aggregate_id == aggregate_id)


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_id: str
    symbol: str
    side: ExecutionSide
    quantity: float
    price: float
    state: OrderState
    created_at: datetime
    updated_at: datetime
    fill: Optional[FillMetadata] = None
    terminal_reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PositionRecord:
    position_id: str
    symbol: str
    side: ExecutionSide
    quantity: float
    state: PositionState
    opened_at: datetime
    updated_at: datetime
    source_order_id: str
    closed_at: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None


_ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING: frozenset(
        {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REPLACED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REPLACED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.FAILED: frozenset(),
}
_ACTIVE_POSITION_STATES = frozenset({PositionState.OPEN, PositionState.HOLD, PositionState.REDUCE})


def _timestamp(value: Optional[datetime]) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise LifecycleError("timestamp must be timezone-aware")
    return value


class OrderLifecycle:
    """Order FSM. Every order has at most one successful fill."""

    def __init__(self, event_store: Optional[LifecycleEventStore] = None) -> None:
        self._store = event_store or LifecycleEventStore()
        self._orders: dict[str, OrderRecord] = {}

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return self._store.events

    def create(
        self,
        *,
        order_id: str,
        symbol: str,
        side: ExecutionSide,
        quantity: float,
        price: float,
        created_at: Optional[datetime] = None,
    ) -> OrderRecord:
        if order_id in self._orders:
            raise LifecycleError(f"order already exists: {order_id}")
        if not symbol.strip() or side not in (ExecutionSide.BUY, ExecutionSide.SELL):
            raise LifecycleError("order requires a symbol and BUY/SELL side")
        quantity = float(quantity)
        price = float(price)
        if not math.isfinite(quantity) or quantity <= 0:
            raise LifecycleError("order quantity must be finite and > 0")
        if not math.isfinite(price) or price <= 0:
            raise LifecycleError("order price must be finite and > 0")
        ts = _timestamp(created_at)
        self._emit(
            order_id,
            "ORDER_CREATED",
            ts,
            {
                "order_id": order_id,
                "symbol": symbol,
                "side": side.value,
                "quantity": quantity,
                "price": price,
                "created_at": ts.isoformat(),
            },
        )
        return self._orders[order_id]

    def fill(self, order_id: str, fill: FillMetadata) -> OrderRecord:
        order = self.get(order_id)
        self._assert_transition(order, OrderState.FILLED)
        if order.fill is not None:
            raise InvalidTransitionError(f"order already filled: {order_id}")
        if not math.isclose(order.quantity, fill.quantity, rel_tol=0.0, abs_tol=1e-12):
            raise LifecycleError("fill quantity must exactly match pending order quantity")
        self._emit(
            order_id,
            "ORDER_FILLED",
            fill.occurred_at,
            {
                "fill_id": fill.fill_id,
                "quantity": fill.quantity,
                "price": fill.price,
                "occurred_at": fill.occurred_at.isoformat(),
                "source": fill.source,
                "fee": fill.fee,
                "fee_asset": fill.fee_asset,
            },
        )
        return self._orders[order_id]

    def cancel(self, order_id: str, *, reason: str = "", occurred_at: Optional[datetime] = None) -> OrderRecord:
        return self._terminal(order_id, OrderState.CANCELLED, reason, occurred_at)

    def replace(self, order_id: str, *, replacement_order_id: str, occurred_at: Optional[datetime] = None) -> OrderRecord:
        if not replacement_order_id.strip():
            raise LifecycleError("replacement_order_id is required")
        return self._terminal(
            order_id,
            OrderState.REPLACED,
            replacement_order_id,
            occurred_at,
            {"replacement_order_id": replacement_order_id},
        )

    def expire(self, order_id: str, *, reason: str = "", occurred_at: Optional[datetime] = None) -> OrderRecord:
        return self._terminal(order_id, OrderState.EXPIRED, reason, occurred_at)

    def fail(self, order_id: str, *, reason: str = "", occurred_at: Optional[datetime] = None) -> OrderRecord:
        return self._terminal(order_id, OrderState.FAILED, reason, occurred_at)

    def get(self, order_id: str) -> OrderRecord:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise LifecycleError(f"unknown order: {order_id}") from exc

    def history(self, order_id: str) -> tuple[LifecycleEvent, ...]:
        return self._store.for_aggregate(order_id)

    def replay(self, events: Iterable[LifecycleEvent]) -> None:
        replay_store = LifecycleEventStore(events)
        for event in replay_store.events:
            if event.aggregate_type == "ORDER":
                self._apply(event)

    def _terminal(
        self,
        order_id: str,
        target: OrderState,
        reason: str,
        occurred_at: Optional[datetime],
        extra: Optional[Mapping[str, Any]] = None,
    ) -> OrderRecord:
        order = self.get(order_id)
        self._assert_transition(order, target)
        payload: dict[str, Any] = {"reason": reason}
        if extra:
            payload.update(extra)
        self._emit(order_id, f"ORDER_{target.value}", _timestamp(occurred_at), payload)
        return self._orders[order_id]

    def _emit(self, order_id: str, event_type: str, occurred_at: datetime, payload: Mapping[str, Any]) -> None:
        event = LifecycleEvent(
            event_id=str(uuid.uuid4()),
            aggregate_id=order_id,
            aggregate_type="ORDER",
            sequence=len(self._store.events) + 1,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
        )
        self._store.append(event)
        self._apply(event)

    def _apply(self, event: LifecycleEvent) -> None:
        p = event.payload
        if event.event_type == "ORDER_CREATED":
            order_id = str(p["order_id"])
            if order_id in self._orders:
                raise ReplayError(f"duplicate order creation: {order_id}")
            self._orders[order_id] = OrderRecord(
                order_id=order_id,
                symbol=str(p["symbol"]),
                side=ExecutionSide(str(p["side"])),
                quantity=float(p["quantity"]),
                price=float(p["price"]),
                state=OrderState.PENDING,
                created_at=datetime.fromisoformat(str(p["created_at"])),
                updated_at=event.occurred_at,
            )
            return
        order = self.get(event.aggregate_id)
        target = OrderState(event.event_type.removeprefix("ORDER_"))
        self._assert_transition(order, target)
        fill = order.fill
        reason = order.terminal_reason
        if target is OrderState.FILLED:
            fill = FillMetadata(
                fill_id=str(p["fill_id"]),
                quantity=float(p["quantity"]),
                price=float(p["price"]),
                occurred_at=datetime.fromisoformat(str(p["occurred_at"])),
                source=str(p["source"]),
                fee=float(p["fee"]),
                fee_asset=p.get("fee_asset"),
            )
        else:
            reason = str(p.get("reason", "")) or None
        self._orders[order.order_id] = OrderRecord(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            state=target,
            created_at=order.created_at,
            updated_at=event.occurred_at,
            fill=fill,
            terminal_reason=reason,
        )

    @staticmethod
    def _assert_transition(order: OrderRecord, target: OrderState) -> None:
        if target not in _ORDER_TRANSITIONS[order.state]:
            raise InvalidTransitionError(f"illegal order transition: {order.state.value} -> {target.value}")


class PositionBook:
    """Position FSM with one active position per symbol and replayable events."""

    def __init__(self, event_store: Optional[LifecycleEventStore] = None) -> None:
        self._store = event_store or LifecycleEventStore()
        self._positions: dict[str, PositionRecord] = {}
        self._active_by_symbol: dict[str, str] = {}

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return self._store.events

    def create_from_fill(
        self,
        *,
        fill: FillMetadata,
        symbol: str,
        side: ExecutionSide,
        source_order_id: str,
        position_id: Optional[str] = None,
    ) -> PositionRecord:
        if side not in (ExecutionSide.BUY, ExecutionSide.SELL):
            raise LifecycleError("position side must be BUY or SELL")
        if self.active_for_symbol(symbol) is not None:
            raise DuplicatePositionError(f"active position already exists for {symbol}")
        position_id = position_id or f"POS-{uuid.uuid4()}"
        if position_id in self._positions:
            raise LifecycleError(f"position already exists: {position_id}")
        self._emit(
            position_id,
            "POSITION_OPENED",
            fill.occurred_at,
            {
                "position_id": position_id,
                "symbol": symbol,
                "side": side.value,
                "quantity": fill.quantity,
                "opened_at": fill.occurred_at.isoformat(),
                "source_order_id": source_order_id,
                "fill_id": fill.fill_id,
            },
        )
        return self._positions[position_id]

    def hold(self, position_id: str, *, occurred_at: Optional[datetime] = None) -> PositionRecord:
        self._assert_active(self.get(position_id))
        self._emit(position_id, "POSITION_HOLD", _timestamp(occurred_at), {})
        return self._positions[position_id]

    def reduce(self, position_id: str, quantity: float, *, occurred_at: Optional[datetime] = None) -> PositionRecord:
        position = self.get(position_id)
        self._assert_active(position)
        quantity = float(quantity)
        if not math.isfinite(quantity) or quantity <= 0 or quantity >= position.quantity:
            raise LifecycleError("reduce quantity must be > 0 and < current quantity")
        self._emit(
            position_id,
            "POSITION_REDUCE",
            _timestamp(occurred_at),
            {"reduced_quantity": quantity, "remaining_quantity": position.quantity - quantity},
        )
        return self._positions[position_id]

    def exit(
        self,
        position_id: str,
        *,
        reason: ExitReason,
        occurred_at: Optional[datetime] = None,
    ) -> PositionRecord:
        self._close(position_id, PositionState.EXIT, reason, _timestamp(occurred_at))
        return self._positions[position_id]

    def reverse(
        self,
        position_id: str,
        *,
        reason: ExitReason = ExitReason.SIGNAL_REVERSAL,
        occurred_at: Optional[datetime] = None,
    ) -> PositionRecord:
        self._close(position_id, PositionState.REVERSE, reason, _timestamp(occurred_at))
        return self._positions[position_id]

    def get(self, position_id: str) -> PositionRecord:
        try:
            return self._positions[position_id]
        except KeyError as exc:
            raise LifecycleError(f"unknown position: {position_id}") from exc

    def active_for_symbol(self, symbol: str) -> Optional[PositionRecord]:
        position_id = self._active_by_symbol.get(symbol)
        return self._positions.get(position_id) if position_id else None

    def history(self, position_id: str) -> tuple[LifecycleEvent, ...]:
        return self._store.for_aggregate(position_id)

    def replay(self, events: Iterable[LifecycleEvent]) -> None:
        replay_store = LifecycleEventStore(events)
        for event in replay_store.events:
            if event.aggregate_type == "POSITION":
                self._apply(event)

    def _close(self, position_id: str, action_state: PositionState, reason: ExitReason, timestamp: datetime) -> None:
        position = self.get(position_id)
        self._assert_active(position)
        if action_state not in (PositionState.EXIT, PositionState.REVERSE):
            raise InvalidTransitionError("close action must be EXIT or REVERSE")
        self._emit(position_id, f"POSITION_{action_state.value}", timestamp, {"reason": reason.value})
        self._emit(position_id, "POSITION_CLOSED", timestamp, {"reason": reason.value})

    def _emit(self, position_id: str, event_type: str, occurred_at: datetime, payload: Mapping[str, Any]) -> None:
        event = LifecycleEvent(
            event_id=str(uuid.uuid4()),
            aggregate_id=position_id,
            aggregate_type="POSITION",
            sequence=len(self._store.events) + 1,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
        )
        self._store.append(event)
        self._apply(event)

    def _apply(self, event: LifecycleEvent) -> None:
        p = event.payload
        if event.event_type == "POSITION_OPENED":
            position_id = str(p["position_id"])
            symbol = str(p["symbol"])
            if position_id in self._positions:
                raise ReplayError(f"duplicate position creation: {position_id}")
            if self.active_for_symbol(symbol) is not None:
                raise ReplayError(f"replay would create duplicate active position: {symbol}")
            self._positions[position_id] = PositionRecord(
                position_id=position_id,
                symbol=symbol,
                side=ExecutionSide(str(p["side"])),
                quantity=float(p["quantity"]),
                state=PositionState.OPEN,
                opened_at=datetime.fromisoformat(str(p["opened_at"])),
                updated_at=event.occurred_at,
                source_order_id=str(p["source_order_id"]),
            )
            self._active_by_symbol[symbol] = position_id
            return
        position = self.get(event.aggregate_id)
        if event.event_type == "POSITION_HOLD":
            self._assert_active(position)
            state = PositionState.HOLD
            self._positions[position.position_id] = self._replace(position, state=state, updated_at=event.occurred_at)
        elif event.event_type == "POSITION_REDUCE":
            self._assert_active(position)
            remaining = float(p["remaining_quantity"])
            if not math.isfinite(remaining) or remaining <= 0 or remaining >= position.quantity:
                raise ReplayError("invalid REDUCE remaining quantity")
            self._positions[position.position_id] = self._replace(
                position,
                state=PositionState.REDUCE,
                quantity=remaining,
                updated_at=event.occurred_at,
            )
        elif event.event_type in {"POSITION_EXIT", "POSITION_REVERSE"}:
            self._assert_active(position)
            state = PositionState(event.event_type.removeprefix("POSITION_"))
            self._positions[position.position_id] = self._replace(position, state=state, updated_at=event.occurred_at)
        elif event.event_type == "POSITION_CLOSED":
            if position.state not in (PositionState.EXIT, PositionState.REVERSE):
                raise InvalidTransitionError(f"illegal position transition: {position.state.value} -> CLOSED")
            reason = ExitReason(str(p["reason"]))
            closed = self._replace(
                position,
                state=PositionState.CLOSED,
                updated_at=event.occurred_at,
                closed_at=event.occurred_at,
                exit_reason=reason,
            )
            self._positions[position.position_id] = closed
            self._active_by_symbol.pop(position.symbol, None)
        else:
            raise ReplayError(f"unknown position event: {event.event_type}")

    @staticmethod
    def _replace(position: PositionRecord, **changes: Any) -> PositionRecord:
        values = {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side,
            "quantity": position.quantity,
            "state": position.state,
            "opened_at": position.opened_at,
            "updated_at": position.updated_at,
            "source_order_id": position.source_order_id,
            "closed_at": position.closed_at,
            "exit_reason": position.exit_reason,
        }
        values.update(changes)
        return PositionRecord(**values)

    @staticmethod
    def _assert_active(position: PositionRecord) -> None:
        if position.state not in _ACTIVE_POSITION_STATES:
            raise InvalidTransitionError(f"position is not active: {position.position_id} [{position.state.value}]")


class ExecutionLifecycleBridge:
    """Maps a successful canonical paper execution to PENDING -> FILLED."""

    @staticmethod
    def record_paper_execution(
        result: ExecutionResult,
        *,
        lifecycle: OrderLifecycle,
        fill_id: str,
        source: str = "PAPER",
    ) -> OrderRecord:
        if result.status is not ExecutionStatus.EXECUTED or result.request is None or not result.order_id:
            raise LifecycleError("only a successful executed request can become a filled order")
        request: ExecutionRequest = result.request
        lifecycle.create(
            order_id=result.order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            created_at=request.created_at,
        )
        return lifecycle.fill(
            result.order_id,
            FillMetadata(
                fill_id=fill_id,
                quantity=request.quantity,
                price=request.price,
                occurred_at=result.executed_at or datetime.now(timezone.utc),
                source=source,
            ),
        )


__all__ = [
    "DuplicatePositionError",
    "ExecutionLifecycleBridge",
    "ExitReason",
    "FillMetadata",
    "InvalidTransitionError",
    "LifecycleError",
    "LifecycleEvent",
    "LifecycleEventStore",
    "OrderLifecycle",
    "OrderRecord",
    "OrderState",
    "PositionBook",
    "PositionRecord",
    "PositionAction",
    "PositionState",
    "ReplayError",
]
