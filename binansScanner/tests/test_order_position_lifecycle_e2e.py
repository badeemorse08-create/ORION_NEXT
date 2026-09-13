from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from models.execution import ExecutionRequest, ExecutionResult, ExecutionSide, ExecutionStatus
from models.order_position_lifecycle import (
    ExecutionLifecycleBridge,
    ExitReason,
    OrderLifecycle,
    OrderState,
    PositionBook,
    PositionState,
)


class TestOrderPositionLifecycleE2E(unittest.TestCase):
    def test_paper_order_fill_position_hold_reduce_reverse_and_reopen(self) -> None:
        t0 = datetime(2026, 2, 1, tzinfo=timezone.utc)
        request = ExecutionRequest(
            symbol="BTCUSDT",
            side=ExecutionSide.BUY,
            price=100.0,
            quantity=4.0,
            confidence=90.0,
            created_at=t0,
        )
        result = ExecutionResult(
            request=request,
            status=ExecutionStatus.EXECUTED,
            executed_at=t0 + timedelta(seconds=1),
            order_id="PAPER-ORD-E2E-1",
        )

        orders = OrderLifecycle()
        filled_order = ExecutionLifecycleBridge.record_paper_execution(
            result,
            lifecycle=orders,
            fill_id="F-E2E-1",
        )
        self.assertEqual(filled_order.state, OrderState.FILLED)

        positions = PositionBook()
        position = positions.create_from_fill(
            fill=filled_order.fill,
            symbol=filled_order.symbol,
            side=filled_order.side,
            source_order_id=filled_order.order_id,
            position_id="POS-E2E-1",
        )
        self.assertEqual(position.state, PositionState.OPEN)

        self.assertEqual(positions.hold(position.position_id).state, PositionState.HOLD)
        self.assertEqual(positions.reduce(position.position_id, 1.0).quantity, 3.0)
        closed = positions.reverse(
            position.position_id,
            reason=ExitReason.SIGNAL_REVERSAL,
            occurred_at=t0 + timedelta(seconds=2),
        )
        self.assertEqual(closed.state, PositionState.CLOSED)

        new_position = positions.create_from_fill(
            fill=filled_order.fill,
            symbol="BTCUSDT",
            side=ExecutionSide.SELL,
            source_order_id="PAPER-ORD-E2E-2",
            position_id="POS-E2E-2",
        )
        self.assertEqual(new_position.state, PositionState.OPEN)
        self.assertEqual(positions.active_for_symbol("BTCUSDT").position_id, "POS-E2E-2")

        replayed_orders = OrderLifecycle()
        replayed_orders.replay(orders.events)
        self.assertEqual(replayed_orders.get(filled_order.order_id), filled_order)
        replayed_positions = PositionBook()
        replayed_positions.replay(positions.events)
        self.assertEqual(replayed_positions.get(new_position.position_id), new_position)


if __name__ == "__main__":
    unittest.main()
