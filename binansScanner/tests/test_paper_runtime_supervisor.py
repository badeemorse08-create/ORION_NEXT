from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import unittest

from integration.paper_runtime_supervisor import PaperRuntimeSupervisor
from models.market_event import MarketEvent, MarketEventType
from models.order_position_lifecycle import OrderState
from models.signal_snapshot import SignalIdentity, SignalSnapshot

UTC = timezone.utc


def snapshot(*, version: int, price: float, valid_for_minutes: int = 15, generated_at: datetime) -> SignalSnapshot:
    return SignalSnapshot(identity=SignalIdentity("BTCUSDT", "PAPER", "ENTRY"), version=version, direction="BUY", decision="FAVORABLE", confidence=80.0, entry_plan={"entry_price": price, "quantity": 1.0}, generated_at=generated_at, valid_until=generated_at + timedelta(minutes=valid_for_minutes), quality=90.0)


def market(price: float, timestamp: datetime, source_id: str) -> MarketEvent:
    return MarketEvent(symbol="BTCUSDT", event_timestamp=timestamp, event_type=MarketEventType.TRADE, payload={"price": price}, source_event_id=source_id)


class TestPaperRuntimeSupervisor(unittest.TestCase):
    def test_continuous_consume_processes_events_immediately(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        events = [market(105.0, t0 + timedelta(seconds=1), "no-fill"), market(100.0, t0 + timedelta(seconds=2), "fill")]
        self.assertEqual(runtime.consume(events), 2)
        self.assertEqual(runtime.runtime.orders.get(order.order_id).state, OrderState.FILLED)
        self.assertEqual(runtime.health.processed_events, 2)
        self.assertEqual(runtime.last_processed_market_event, events[-1])

    def test_duplicate_event_is_suppressed_across_runtime(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        event = market(100.0, t0 + timedelta(seconds=1), "same-event")
        self.assertEqual(runtime.process_market_event(event), (order.order_id,))
        self.assertEqual(runtime.process_market_event(event), ())
        self.assertEqual(runtime.health.duplicate_events, 1)
        self.assertEqual(runtime.health.processed_events, 1)

    def test_stale_signal_is_cancelled_before_fill(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, valid_for_minutes=1, generated_at=t0), now=t0)
        self.assertEqual(runtime.process_market_event(market(100.0, t0 + timedelta(minutes=2), "late")), ())
        self.assertEqual(runtime.runtime.orders.get(order.order_id).state, OrderState.CANCELLED)
        self.assertEqual(runtime.active_orders, ())
        self.assertEqual(runtime.active_positions, ())

    def test_submit_recover_preserves_order_identity(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        recovered = runtime.recover()
        self.assertEqual(recovered.active_orders[0].order_id, order.order_id)
        self.assertEqual(recovered.replay_state(), runtime.replay_state())

    def test_submit_fill_recover_preserves_order_and_position_identity(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        runtime.process_market_event(market(100.0, t0 + timedelta(seconds=1), "fill"))
        recovered = runtime.recover()
        self.assertEqual(recovered.replay_state(), runtime.replay_state())
        self.assertIn(order.order_id, recovered.terminal_orders)
        self.assertEqual(recovered.active_positions[0].position_id, f"POS-{order.order_id}")

    def test_replace_recover_preserves_terminal_old_and_active_replacement_identity(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        action = runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=118.0, generated_at=t0 + timedelta(minutes=1)), market_price=120.0, now=t0 + timedelta(minutes=1))
        self.assertEqual(action.value, "REPLACE")
        replacement = runtime.active_orders[0]
        recovered = runtime.recover()
        self.assertEqual(recovered.replay_state(), runtime.replay_state())
        self.assertEqual(recovered.active_orders[0].order_id, replacement.order_id)
        self.assertEqual(recovered.runtime.orders.get(first.order_id).state, OrderState.REPLACED)

    def test_pending_order_survives_reconnect_and_fills_once(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        recovered = runtime.recover()
        self.assertEqual(recovered.active_orders[0].order_id, order.order_id)
        self.assertEqual(recovered.process_market_event(market(100.0, t0 + timedelta(minutes=1), "reconnect-fill")), (order.order_id,))
        self.assertEqual(recovered.process_market_event(market(100.0, t0 + timedelta(minutes=2), "duplicate-after-fill")), ())
        self.assertEqual(len(recovered.active_positions), 1)

    def test_position_and_ledger_recover_continuously(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        runtime.process_market_event(market(100.0, t0 + timedelta(seconds=1), "fill"))
        recovered = runtime.recover()
        self.assertEqual(recovered.replay_state(), runtime.replay_state())
        self.assertEqual(recovered.account_equity, runtime.account_equity)

    def test_replay_is_deterministic_after_recovery_and_recovery_twice(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=101.0, generated_at=t0 + timedelta(minutes=1)), market_price=102.0, now=t0 + timedelta(minutes=1))
        runtime.process_market_event(market(101.0, t0 + timedelta(minutes=2), "fill"))
        recovered_a = runtime.recover()
        recovered_b = recovered_a.recover()
        self.assertEqual(runtime.replay_state(), recovered_a.replay_state())
        self.assertEqual(recovered_a.replay_state(), recovered_b.replay_state())

    def test_duplicate_event_after_recovery_is_suppressed(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        event = market(100.0, t0 + timedelta(seconds=1), "same-event")
        runtime.process_market_event(event)
        recovered = runtime.recover()
        before = len(recovered.runtime.ledger.events)
        self.assertEqual(recovered.process_market_event(event), ())
        self.assertEqual(len(recovered.runtime.ledger.events), before)

    def test_observability_exposes_orders_positions_equity_drawdown_and_health(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        self.assertEqual(len(runtime.active_orders), 1)
        self.assertEqual(runtime.active_orders[0].order_id, order.order_id)
        self.assertEqual(runtime.active_positions, ())
        self.assertGreater(runtime.account_equity, 0.0)
        self.assertEqual(runtime.current_drawdown, 0.0)
        self.assertTrue(runtime.health.healthy)
        self.assertTrue(runtime.health.paper_only)
        self.assertTrue(runtime.no_live_path())
        runtime.process_market_event(market(100.0, t0 + timedelta(seconds=1), "fill"))
        self.assertEqual(runtime.health.active_orders, 0)
        self.assertEqual(runtime.health.active_positions, 1)
        self.assertIn(order.order_id, runtime.terminal_orders)

    def test_fail_closed_after_runtime_error_uses_injection_seam(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        processor = Mock(side_effect=ValueError("simulated runtime failure"))
        runtime = PaperRuntimeSupervisor(event_processor=processor)
        runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        with self.assertRaises(ValueError):
            runtime.process_market_event(market(100.0, t0 + timedelta(seconds=1), "first"))
        self.assertFalse(runtime.health.healthy)
        self.assertEqual(runtime.process_market_event(market(100.0, t0 + timedelta(seconds=2), "second")), ())
        self.assertEqual(processor.call_count, 1)

    def test_stale_signal_after_recovery_has_same_cancellation_semantics(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        order = runtime.submit_signal(snapshot(version=1, price=100.0, valid_for_minutes=1, generated_at=t0), now=t0)
        recovered = runtime.recover()
        self.assertEqual(recovered.process_market_event(market(100.0, t0 + timedelta(minutes=2), "late")), ())
        self.assertEqual(recovered.runtime.orders.get(order.order_id).state, OrderState.CANCELLED)
        self.assertEqual(recovered.active_orders, ())

    def test_no_duplicate_ledger_events_after_recovery(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime = PaperRuntimeSupervisor()
        runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        runtime.process_market_event(market(100.0, t0 + timedelta(seconds=1), "fill"))
        recovered = runtime.recover()
        self.assertEqual(len(recovered.runtime.ledger.events), len(runtime.runtime.ledger.events))
        self.assertEqual(recovered.replay_state(), runtime.replay_state())


if __name__ == "__main__":
    unittest.main()
