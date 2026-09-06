"""Release-readiness supervision for the integrated paper runtime.

Recovery is journal-driven and preserves canonical aggregate identities. This
module adds orchestration only; D1-D6 contracts remain authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from integration.paper_realtime_lifecycle import PaperRealtimeLifecycle
from integration.trading_control import TradingControlStore, TradingState
from models.market_event import MarketEvent, MarketEventType
from models.order_position_lifecycle import OrderState
from models.paper_capital import PaperLedger
from models.signal_snapshot import SignalIdentity, SignalSnapshot
from tools.pending_order_revalidation import PendingOrder, RevalidationAction


_DURABLE_SCHEMA_VERSION = 1
_DURABLE_INIT = "RUN_INIT"


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
    durable_state_path: Optional[Path] = None
    _operations: list[tuple] = field(default_factory=list, init=False, repr=False)
    _processed_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _last_event: Optional[MarketEvent] = field(default=None, init=False, repr=False)
    _duplicate_events: int = field(default=0, init=False, repr=False)
    _failed: bool = field(default=False, init=False, repr=False)
    _equity_high_water: Optional[float] = field(default=None, init=False, repr=False)
    _durable_sequence: int = field(default=1, init=False, repr=False)
    _replaying: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.control_path = Path(self.control_path)
        if self.durable_state_path is not None:
            self.durable_state_path = Path(self.durable_state_path)
        if self.control is None:
            self.control = TradingControlStore(self.control_path)
        self.control.initialize()
        if self.event_processor is None:
            self.event_processor = self.runtime.on_market_event
        if self.durable_state_path is not None:
            records = self._read_durable_records()
            if not records:
                self._append_durable_record(
                    _DURABLE_INIT,
                    {"starting_equity": self.runtime.ledger.starting_equity},
                )
            else:
                self._durable_sequence = int(records[-1]["sequence"]) + 1
                if records[0]["type"] != _DURABLE_INIT:
                    raise RuntimeError("invalid durable runtime journal: missing RUN_INIT")
                persisted_equity = float(records[0]["starting_equity"])
                if persisted_equity <= 0:
                    raise RuntimeError("invalid durable runtime journal: starting_equity must be positive")

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
        return RuntimeHealth(
            healthy=not self._failed,
            paper_only=self.runtime.no_live_execution(),
            last_market_event_id=self._last_event.event_id if self._last_event else None,
            last_market_event_at=self._last_event.event_timestamp if self._last_event else None,
            processed_events=len(self._processed_event_ids),
            duplicate_events=self._duplicate_events,
            active_orders=len(self.active_orders),
            active_positions=len(self.active_positions),
            trading_state=self.trading_state,
        )

    def submit_signal(
        self,
        snapshot: SignalSnapshot,
        *,
        now: datetime,
        timeframe: str = "1m",
        market_regime: str = "PAPER",
        intent_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> PendingOrder:
        if self._failed:
            raise RuntimeError("paper runtime is failed closed")
        assert self.control is not None
        self.control.require_entry_allowed(source="runtime", reason="supervisor.submit_signal")
        pending = self.runtime.submit_signal(
            snapshot,
            now=now,
            timeframe=timeframe,
            market_regime=market_regime,
            intent_id=intent_id,
            order_id=order_id,
        )
        operation = ("submit", snapshot, now, timeframe, market_regime, pending.intent_id, pending.order_id)
        self._record_operation(
            operation,
            "SUBMIT",
            {
                "snapshot": snapshot.canonical_payload(),
                "now": now.isoformat(),
                "timeframe": timeframe,
                "market_regime": market_regime,
                "intent_id": pending.intent_id,
                "order_id": pending.order_id,
            },
        )
        return pending

    def revalidate(
        self,
        *,
        intent_id: str,
        snapshot: SignalSnapshot,
        market_price: float,
        now: datetime,
        timeframe: str = "1m",
        market_regime: str = "PAPER",
        replacement_order_id: Optional[str] = None,
    ) -> RevalidationAction:
        if self._failed:
            raise RuntimeError("paper runtime is failed closed")
        action = self.runtime.revalidate(
            intent_id=intent_id,
            snapshot=snapshot,
            market_price=market_price,
            now=now,
            timeframe=timeframe,
            market_regime=market_regime,
            replacement_order_id=replacement_order_id,
        )
        current = self.runtime.pending.active_for_intent(intent_id)
        canonical_replacement_id = current.order_id if action is RevalidationAction.REPLACE and current is not None else None
        operation = (
            "revalidate",
            intent_id,
            snapshot,
            market_price,
            now,
            timeframe,
            market_regime,
            canonical_replacement_id,
        )
        self._record_operation(
            operation,
            "REVALIDATE",
            {
                "intent_id": intent_id,
                "snapshot": snapshot.canonical_payload(),
                "market_price": float(market_price),
                "now": now.isoformat(),
                "timeframe": timeframe,
                "market_regime": market_regime,
                "replacement_order_id": canonical_replacement_id,
            },
        )
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
        operation = ("market", event)
        self._record_operation(
            operation,
            "MARKET",
            {
                "event": {
                    "symbol": event.symbol,
                    "event_timestamp": event.event_timestamp.isoformat(),
                    "event_type": event.event_type.value,
                    "payload": dict(event.payload),
                    "source_timestamp": event.source_timestamp.isoformat() if event.source_timestamp else None,
                    "source_event_id": event.source_event_id,
                }
            },
        )
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
        """Rebuild canonical aggregate state from the durable operation journal when configured."""
        assert self.control is not None
        recovered_control = TradingControlStore(self.control.path)
        if self.durable_state_path is not None:
            records = self._read_durable_records()
            if not records:
                starting_equity = self.runtime.ledger.starting_equity
                operation_records: list[dict[str, Any]] = []
            else:
                starting_equity = float(records[0]["starting_equity"])
                operation_records = records[1:]
            recovered = PaperRuntimeSupervisor(
                runtime=PaperRealtimeLifecycle(
                    ledger=PaperLedger(starting_equity=starting_equity),
                    revalidation_policy=self.runtime.revalidation_policy,
                ),
                control=recovered_control,
                durable_state_path=self.durable_state_path,
            )
            recovered._replaying = True
            try:
                for record in operation_records:
                    self._replay_durable_record(recovered, record)
            finally:
                recovered._replaying = False
            return recovered

        recovered_ledger = PaperLedger(starting_equity=self.runtime.ledger.starting_equity)
        recovered = PaperRuntimeSupervisor(
            runtime=PaperRealtimeLifecycle(
                ledger=recovered_ledger,
                revalidation_policy=self.runtime.revalidation_policy,
            ),
            control=recovered_control,
        )
        recovered._replaying = True
        try:
            for operation in self._operations:
                self._replay_tuple_operation(recovered, operation)
        finally:
            recovered._replaying = False
        return recovered

    def replay_state(self) -> tuple:
        order_ids = sorted({event.aggregate_id for event in self.runtime.orders.events if event.aggregate_type == "ORDER"})
        position_ids = sorted({event.aggregate_id for event in self.runtime.positions.events if event.aggregate_type == "POSITION"})
        return (
            self.trading_state.value,
            tuple(
                (
                    order_id,
                    self.runtime.orders.get(order_id).state.value,
                    self.runtime.orders.get(order_id).price,
                    self.runtime.orders.get(order_id).quantity,
                )
                for order_id in order_ids
            ),
            tuple(
                (
                    position_id,
                    next(event.payload.get("symbol") for event in self.runtime.positions.events if event.aggregate_id == position_id),
                    next(event.payload.get("side") for event in self.runtime.positions.events if event.aggregate_id == position_id),
                    next(event.payload.get("quantity") for event in self.runtime.positions.events if event.aggregate_id == position_id),
                )
                for position_id in position_ids
            ),
            self.runtime.replay_account(),
        )

    def no_live_path(self) -> bool:
        return self.runtime.no_live_execution()

    def _record_operation(self, operation: tuple, durable_type: str, payload: dict[str, Any]) -> None:
        self._operations.append(operation)
        if self.durable_state_path is not None and not self._replaying:
            self._append_durable_record(durable_type, payload)

    def _append_durable_record(self, record_type: str, payload: dict[str, Any]) -> None:
        assert self.durable_state_path is not None
        self.durable_state_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": _DURABLE_SCHEMA_VERSION,
            "sequence": self._durable_sequence,
            "type": record_type,
            **payload,
        }
        with self.durable_state_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._durable_sequence += 1

    def _read_durable_records(self) -> list[dict[str, Any]]:
        if self.durable_state_path is None or not self.durable_state_path.exists():
            self._durable_sequence = 1
            return []
        records: list[dict[str, Any]] = []
        expected_sequence = 1
        with self.durable_state_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("invalid durable runtime journal JSON") from exc
                if not isinstance(record, dict):
                    raise RuntimeError("invalid durable runtime journal record")
                if int(record.get("schema_version", 0)) != _DURABLE_SCHEMA_VERSION:
                    raise RuntimeError("unsupported durable runtime journal schema")
                if int(record.get("sequence", 0)) != expected_sequence:
                    raise RuntimeError(
                        f"durable runtime journal sequence mismatch: expected {expected_sequence}, got {record.get('sequence')}"
                    )
                if not isinstance(record.get("type"), str):
                    raise RuntimeError("durable runtime journal record type is missing")
                records.append(record)
                expected_sequence += 1
        self._durable_sequence = expected_sequence
        return records

    @staticmethod
    def _replay_durable_record(recovered: "PaperRuntimeSupervisor", record: dict[str, Any]) -> None:
        record_type = str(record["type"])
        if record_type == _DURABLE_INIT:
            return
        if record_type == "SUBMIT":
            snapshot = PaperRuntimeSupervisor._snapshot_from_payload(record["snapshot"])
            recovered.submit_signal(
                snapshot,
                now=PaperRuntimeSupervisor._parse_datetime(record["now"]),
                timeframe=str(record["timeframe"]),
                market_regime=str(record["market_regime"]),
                intent_id=str(record["intent_id"]),
                order_id=str(record["order_id"]),
            )
            return
        if record_type == "REVALIDATE":
            snapshot = PaperRuntimeSupervisor._snapshot_from_payload(record["snapshot"])
            replacement_order_id = record.get("replacement_order_id")
            recovered.revalidate(
                intent_id=str(record["intent_id"]),
                snapshot=snapshot,
                market_price=float(record["market_price"]),
                now=PaperRuntimeSupervisor._parse_datetime(record["now"]),
                timeframe=str(record["timeframe"]),
                market_regime=str(record["market_regime"]),
                replacement_order_id=str(replacement_order_id) if replacement_order_id else None,
            )
            return
        if record_type == "MARKET":
            recovered.process_market_event(PaperRuntimeSupervisor._market_event_from_payload(record["event"]))
            return
        raise RuntimeError(f"unsupported durable runtime journal operation: {record_type}")

    @staticmethod
    def _replay_tuple_operation(recovered: "PaperRuntimeSupervisor", operation: tuple) -> None:
        if operation[0] == "submit":
            _, snapshot, now, timeframe, market_regime, intent_id, order_id = operation
            recovered.submit_signal(
                snapshot,
                now=now,
                timeframe=timeframe,
                market_regime=market_regime,
                intent_id=intent_id,
                order_id=order_id,
            )
        elif operation[0] == "revalidate":
            _, intent_id, snapshot, market_price, now, timeframe, market_regime, replacement_order_id = operation
            recovered.revalidate(
                intent_id=intent_id,
                snapshot=snapshot,
                market_price=market_price,
                now=now,
                timeframe=timeframe,
                market_regime=market_regime,
                replacement_order_id=replacement_order_id,
            )
        elif operation[0] == "market":
            recovered.process_market_event(operation[1])
        elif operation[0] == "control":
            recovered._operations.append(operation)

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        return datetime.fromisoformat(str(value))

    @staticmethod
    def _snapshot_from_payload(payload: dict[str, Any]) -> SignalSnapshot:
        identity = SignalIdentity(
            symbol=str(payload["symbol"]),
            strategy=str(payload["strategy"]),
            intent=str(payload["intent"]),
        )
        return SignalSnapshot(
            identity=identity,
            version=int(payload["version"]),
            direction=str(payload["direction"]),
            decision=str(payload["decision"]),
            confidence=float(payload["confidence"]),
            entry_plan=dict(payload["entry_plan"]),
            generated_at=PaperRuntimeSupervisor._parse_datetime(payload["generated_at"]),
            valid_until=PaperRuntimeSupervisor._parse_datetime(payload["valid_until"]),
            market_context_fingerprint=payload.get("market_context_fingerprint"),
            quality=float(payload["quality"]) if payload.get("quality") is not None else None,
        )

    @staticmethod
    def _market_event_from_payload(payload: dict[str, Any]) -> MarketEvent:
        source_timestamp = payload.get("source_timestamp")
        return MarketEvent(
            symbol=str(payload["symbol"]),
            event_timestamp=PaperRuntimeSupervisor._parse_datetime(payload["event_timestamp"]),
            event_type=MarketEventType(str(payload["event_type"])),
            payload=dict(payload.get("payload", {})),
            source_timestamp=PaperRuntimeSupervisor._parse_datetime(source_timestamp) if source_timestamp else None,
            source_event_id=payload.get("source_event_id"),
        )


__all__ = ["PaperRuntimeSupervisor", "RuntimeHealth"]
