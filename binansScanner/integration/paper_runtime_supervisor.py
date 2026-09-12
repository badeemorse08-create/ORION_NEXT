"""Release-readiness supervision for the integrated paper runtime.

Recovery is journal-driven and preserves canonical aggregate identities. This
module adds orchestration only; D1-D6 contracts remain authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from integration.paper_realtime_lifecycle import PaperRealtimeLifecycle
from models.market_event import MarketEvent
from models.order_position_lifecycle import OrderState
from models.paper_capital import PaperLedger
from models.signal_snapshot import SignalSnapshot
from tools.pending_order_revalidation import PendingOrder, RevalidationAction
from integration.trading_control import TradingControlStore, TradingState


def _default_control_path() -> Path:
    return Path.home() / ".orion" / "trading_control.json"


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    healthy: bool
    paper_only: bool
    last_market_event_id: Optional[str]
    last_market_event_at: Optional[datetime]
    processed_events: int
    duplicate_events: int
    active_orders: int
    active_positions: int
    trading_state: TradingState


@dataclass(slots=True)
class PaperRuntimeSupervisor:
    runtime: PaperRealtimeLifecycle = field(default_factory=PaperRealtimeLifecycle)
    event_processor: Optional[Callable[[MarketEvent], tuple[str, ...]]] = None
    control: Optional[TradingControlStore] = None
    control_path: Path = field(default_factory=_default_control_path)
    _operations: list[tuple] = field(default_factory=list, init=False, repr=False)
    _processed_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _last_event: Optional[MarketEvent] = field(default=None, init=False, repr=False)
    _duplicate_events: int = field(default=0, init=False, repr=False)
    _failed: bool = field(default=False, init=False, repr=False)
    _equity_high_water: Optional[float] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.control is None:
            self.control = TradingControlStore(self.control_path)
        self.control.initialize()
        if self.event_processor is None:
            self.event_processor = self.runtime.on_market_event

    @property
    def trading_state(self) -> TradingState:
        assert self.control is not None
        return self.control.state

    def pause_new_entries(self, *, source: str = "user", reason: str = "pause new entries") -> TradingState:
        assert self.control is not None
        state = self.control.pause(source=source, reason=reason)
        self._operations.append(("control", "PAUSED", source, reason))
        return state

    def resume_trading(self, *, source: str = "user", reason: str = "resume trading") -> TradingState:
        assert self.control is not None
        state = self.control.resume(source=source, reason=reason)
        self._operations.append(("control", "RUNNING", source, reason))
        return state

    @property
    def control_events(self) -> tuple:
        assert self.control is not None
        return self.control.events

    @property
    def active_orders(self) -> tuple[PendingOrder, ...]:
        return tuple(self.runtime.pending.pending())

    @property
    def terminal_orders(self) -> tuple[str, ...]:
        ids = {event.aggregate_id for event in self.runtime.orders.events if event.aggregate_type == "ORDER"}
        return tuple(order_id for order_id in sorted(ids) if self.runtime.orders.get(order_id).state is not OrderState.PENDING)

    @property
    def active_positions(self) -> tuple:
        symbols = {event.payload.get("symbol") for event in self.runtime.positions.events if event.payload.get("symbol")}
        return tuple(position for symbol in sorted(symbols) if (position := self.runtime.positions.active_for_symbol(str(symbol))) is not None)

    @property
    def account_equity(self) -> float:
        equity = float(self.runtime.replay_account().wallet.cash)
        if self._equity_high_water is None or equity > self._equity_high_water:
            self._equity_high_water = equity
        return equity

    @property
    def current_drawdown(self) -> float:
        equity = self.account_equity
        high_water = self._equity_high_water if self._equity_high_water is not None else equity
        return max(0.0, high_water - equity)

    @property
    def last_processed_market_event(self) -> Optional[MarketEvent]:
        return self._last_event

    @property
    def health(self) -> RuntimeHealth:
        return RuntimeHealth(healthy=not self._failed, paper_only=self.runtime.no_live_execution(), last_market_event_id=self._last_event.event_id if self._last_event else None, last_market_event_at=self._last_event.event_timestamp if self._last_event else None, processed_events=len(self._processed_event_ids), duplicate_events=self._duplicate_events, active_orders=len(self.active_orders), active_positions=len(self.active_positions), trading_state=self.trading_state)

    def submit_signal(self, snapshot: SignalSnapshot, *, now: datetime, timeframe: str = "1m", market_regime: str = "PAPER", intent_id: Optional[str] = None, order_id: Optional[str] = None) -> PendingOrder:
        if self._failed:
            raise RuntimeError("paper runtime is failed closed")
        assert self.control is not None
        self.control.require_entry_allowed(source="runtime", reason="supervisor.submit_signal")
        pending = self.runtime.submit_signal(snapshot, now=now, timeframe=timeframe, market_regime=market_regime, intent_id=intent_id, order_id=order_id)
        self._operations.append(("submit", snapshot, now, timeframe, market_regime, pending.intent_id, pending.order_id))
        return pending

    def revalidate(self, *, intent_id: str, snapshot: SignalSnapshot, market_price: float, now: datetime, timeframe: str = "1m", market_regime: str = "PAPER", replacement_order_id: Optional[str] = None) -> RevalidationAction:
        if self._failed:
            raise RuntimeError("paper runtime is failed closed")
        action = self.runtime.revalidate(intent_id=intent_id, snapshot=snapshot, market_price=market_price, now=now, timeframe=timeframe, market_regime=market_regime, replacement_order_id=replacement_order_id)
        current = self.runtime.pending.active_for_intent(intent_id)
        canonical_replacement_id = current.order_id if action is RevalidationAction.REPLACE and current is not None else None
        self._operations.append(("revalidate", intent_id, snapshot, market_price, now, timeframe, market_regime, canonical_replacement_id))
        return action

    def process_market_event(self, event: MarketEvent) -> tuple[str, ...]:
        if self._failed:
            return ()
        if event.event_id in self._processed_event_ids:
            self._duplicate_events += 1
            return ()
        try:
            assert self.event_processor is not None
            result = self.event_processor(event)
        except Exception:
            self._failed = True
            raise
        self._processed_event_ids.add(event.event_id)
        self._last_event = event
        self._operations.append(("market", event))
        self.account_equity
        return result

    def consume(self, events: Iterable[MarketEvent]) -> int:
        processed = 0
        for event in events:
            before = len(self._processed_event_ids)
            self.process_market_event(event)
            processed += int(len(self._processed_event_ids) > before)
        return processed

    def recover(self) -> "PaperRuntimeSupervisor":
        """Rebuild canonical aggregate state without treating recovery as a new entry."""
        assert self.control is not None
        recovered_control = TradingControlStore(self.control.path)
        # The model default is a convenience for standalone PaperLedger use, not
        # the authoritative baseline of a live experiment. Recovery must carry
        # forward the durable run's configured starting equity exactly.
        recovered_ledger = PaperLedger(starting_equity=self.runtime.ledger.starting_equity)
        recovered = PaperRuntimeSupervisor(runtime=PaperRealtimeLifecycle(ledger=recovered_ledger, revalidation_policy=self.runtime.revalidation_policy), control=recovered_control)
        for operation in self._operations:
            if operation[0] == "submit":
                _, snapshot, now, timeframe, market_regime, intent_id, order_id = operation
                recovered.runtime.submit_signal(snapshot, now=now, timeframe=timeframe, market_regime=market_regime, intent_id=intent_id, order_id=order_id)
                recovered._operations.append(operation)
            elif operation[0] == "revalidate":
                _, intent_id, snapshot, market_price, now, timeframe, market_regime, replacement_order_id = operation
                recovered.revalidate(intent_id=intent_id, snapshot=snapshot, market_price=market_price, now=now, timeframe=timeframe, market_regime=market_regime, replacement_order_id=replacement_order_id)
            elif operation[0] == "market":
                recovered.process_market_event(operation[1])
            elif operation[0] == "control":
                recovered._operations.append(operation)
        return recovered

    def replay_state(self) -> tuple:
        order_ids = sorted({event.aggregate_id for event in self.runtime.orders.events if event.aggregate_type == "ORDER"})
        position_ids = sorted({event.aggregate_id for event in self.runtime.positions.events if event.aggregate_type == "POSITION"})
        return (
            self.trading_state.value,
            tuple((order_id, self.runtime.orders.get(order_id).state.value, self.runtime.orders.get(order_id).price, self.runtime.orders.get(order_id).quantity) for order_id in order_ids),
            tuple((position_id, next(event.payload.get("symbol") for event in self.runtime.positions.events if event.aggregate_id == position_id), next(event.payload.get("side") for event in self.runtime.positions.events if event.aggregate_id == position_id), next(event.payload.get("quantity") for event in self.runtime.positions.events if event.aggregate_id == position_id)) for position_id in position_ids),
            self.runtime.replay_account(),
        )

    def no_live_path(self) -> bool:
        return self.runtime.no_live_execution()


__all__ = ["PaperRuntimeSupervisor", "RuntimeHealth"]