from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from models.execution import ExecutionRequest, ExecutionResult, ExecutionSide, ExecutionStatus
from models.order_position_lifecycle import (
    DuplicatePositionError,
    ExecutionLifecycleBridge,
    ExitReason,
    FillMetadata,
    InvalidTransitionError,
    LifecycleError,
    LifecycleEvent,
    LifecycleEventStore,
    OrderLifecycle,
    OrderState,
    PositionBook,
    PositionState,
    ReplayError,
)


class TestOrderLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.lifecycle = OrderLifecycle()

    def _create(self, order_id: str = "O-1") -> None:
        self.lifecycle.create(
            order_id=order_id,
            symbol="BTCUSDT",
            side=ExecutionSide.BUY,
            quantity=2.0,
            price=100.0,
            created_at=self.t0,
        )

    def _fill(self, fill_id: str = "F-1", quantity: float = 2.0) -> FillMetadata:
        return FillMetadata(fill_id, quantity, 101.0, self.t0 + timedelta(seconds=1))

    def test_pending_to_filled_and_exactly_one_fill(self) -> None:
        self._create()
        filled = self.lifecycle.fill("O-1", self._fill())
        self.assertEqual(filled.state, OrderState.FILLED)
        self.assertEqual(filled.fill.fill_id, "F-1")
        self.assertEqual(len(self.lifecycle.history("O-1")), 2)
        with self.assertRaises(InvalidTransitionError):
            self.lifecycle.fill("O-1", self._fill("F-2"))

    def test_all_terminal_order_states_are_reachable_from_pending(self) -> None:
        for state, operation in (
            (OrderState.CANCELLED, lambda oid: self.lifecycle.cancel(oid, reason="cancel")),
            (OrderState.REPLACED, lambda oid: self.lifecycle.replace(oid, replacement_order_id="O-2")),
            (OrderState.EXPIRED, lambda oid: self.lifecycle.expire(oid, reason="timeout")),
            (OrderState.FAILED, lambda oid: self.lifecycle.fail(oid, reason="adapter")),
        ):
            oid = f"{state.value}-1"
            self._create(oid)
            self.assertEqual(operation(oid).state, state)

    def test_terminal_order_cannot_transition_again(self) -> None:
        self._create()
        self.lifecycle.cancel("O-1", reason="manual")
        with self.assertRaises(InvalidTransitionError):
            self.lifecycle.expire("O-1")

    def test_fill_quantity_must_match_order(self) -> None:
        self._create()
        with self.assertRaisesRegex(LifecycleError, "exactly match"):
            self.lifecycle.fill("O-1", self._fill(quantity=1.0))

    def test_history_replays_to_identical_order_state(self) -> None:
        self._create()
        self.lifecycle.fill("O-1", self._fill())
        replayed = OrderLifecycle()
        replayed.replay(self.lifecycle.history("O-1"))
        self.assertEqual(replayed.get("O-1"), self.lifecycle.get("O-1"))

    def test_event_store_rejects_non_contiguous_history(self) -> None:
        event = LifecycleEvent(
            event_id="E-1",
            aggregate_id="O-1",
            aggregate_type="ORDER",
            sequence=2,
            event_type="ORDER_CREATED",
            occurred_at=self.t0,
        )
        with self.assertRaises(ReplayError):
            LifecycleEventStore([event])


class TestPositionLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.fill = FillMetadata("F-1", 5.0, 100.0, self.t0)
        self.book = PositionBook()

    def _open(self, symbol: str = "BTCUSDT", position_id: str = "P-1") -> None:
        self.book.create_from_fill(
            fill=self.fill,
            symbol=symbol,
            side=ExecutionSide.BUY,
            source_order_id="O-1",
            position_id=position_id,
        )

    def test_position_is_created_from_fill(self) -> None:
        position = self.book.create_from_fill(
            fill=self.fill,
            symbol="BTCUSDT",
            side=ExecutionSide.BUY,
            source_order_id="O-1",
            position_id="P-1",
        )
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertEqual(position.quantity, 5.0)
        self.assertEqual(position.source_order_id, "O-1")

    def test_duplicate_active_position_is_rejected(self) -> None:
        self._open()
        with self.assertRaises(DuplicatePositionError):
            self._open(position_id="P-2")

    def test_hold_then_reduce_preserves_positive_remaining_quantity(self) -> None:
        self._open()
        self.assertEqual(self.book.hold("P-1").state, PositionState.HOLD)
        reduced = self.book.reduce("P-1", 2.0)
        self.assertEqual(reduced.state, PositionState.REDUCE)
        self.assertEqual(reduced.quantity, 3.0)
        with self.assertRaises(LifecycleError):
            self.book.reduce("P-1", 3.0)

    def test_exit_closes_position_for_every_supported_reason(self) -> None:
        for index, reason in enumerate(tuple(ExitReason), start=1):
            book = PositionBook()
            pid = f"P-{index}"
            symbol = f"BTC{index}USDT"
            book.create_from_fill(
                fill=self.fill,
                symbol=symbol,
                side=ExecutionSide.BUY,
                source_order_id=f"O-{index}",
                position_id=pid,
            )
            closed = book.exit(pid, reason=reason)
            self.assertEqual(closed.state, PositionState.CLOSED)
            self.assertEqual(closed.exit_reason, reason)
            self.assertIsNotNone(closed.closed_at)
            self.assertIsNone(book.active_for_symbol(symbol))

    def test_reverse_closes_old_position_and_releases_symbol(self) -> None:
        self._open()
        reversed_position = self.book.reverse("P-1")
        self.assertEqual(reversed_position.state, PositionState.CLOSED)
        self.assertEqual(reversed_position.exit_reason, ExitReason.SIGNAL_REVERSAL)
        new_position = self.book.create_from_fill(
            fill=self.fill,
            symbol="BTCUSDT",
            side=ExecutionSide.SELL,
            source_order_id="O-2",
            position_id="P-2",
        )
        self.assertEqual(new_position.state, PositionState.OPEN)
        self.assertEqual(self.book.active_for_symbol("BTCUSDT").position_id, "P-2")

    def test_closed_position_cannot_be_mutated(self) -> None:
        self._open()
        self.book.exit("P-1", reason=ExitReason.RISK_EXIT)
        with self.assertRaises(InvalidTransitionError):
            self.book.hold("P-1")
        with self.assertRaises(InvalidTransitionError):
            self.book.reverse("P-1")

    def test_position_history_replays_deterministically(self) -> None:
        self._open()
        self.book.hold("P-1", occurred_at=self.t0 + timedelta(seconds=1))
        self.book.reduce("P-1", 1.0, occurred_at=self.t0 + timedelta(seconds=2))
        self.book.exit("P-1", reason=ExitReason.TIME_EXIT, occurred_at=self.t0 + timedelta(seconds=3))
        replayed = PositionBook()
        replayed.replay(self.book.history("P-1"))
        self.assertEqual(replayed.get("P-1"), self.book.get("P-1"))
        self.assertEqual([e.sequence for e in self.book.history("P-1")], [1, 2, 3, 4, 5])


class TestPaperExecutionLifecycleBridge(unittest.TestCase):
    def test_executed_paper_result_maps_to_pending_then_filled(self) -> None:
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        request = ExecutionRequest(
            symbol="BTCUSDT",
            side=ExecutionSide.BUY,
            price=100.0,
            quantity=1.5,
            confidence=95.0,
            created_at=created,
        )
        result = ExecutionResult(
            request=request,
            status=ExecutionStatus.EXECUTED,
            executed_at=created + timedelta(seconds=1),
            order_id="PAPER-ORD-1",
        )
        lifecycle = OrderLifecycle()
        record = ExecutionLifecycleBridge.record_paper_execution(result, lifecycle=lifecycle, fill_id="F-1")
        self.assertEqual(record.state, OrderState.FILLED)
        self.assertEqual(
            [event.event_type for event in lifecycle.history("PAPER-ORD-1")],
            ["ORDER_CREATED", "ORDER_FILLED"],
        )

    def test_failed_execution_cannot_be_recorded_as_fill(self) -> None:
        result = ExecutionResult(status=ExecutionStatus.FAILED, message="failed")
        with self.assertRaises(LifecycleError):
            ExecutionLifecycleBridge.record_paper_execution(result, lifecycle=OrderLifecycle(), fill_id="F-1")


if __name__ == "__main__":
    unittest.main()
