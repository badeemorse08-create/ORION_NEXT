from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from integration.paper_realtime_lifecycle import PaperRealtimeLifecycle
from models.market_event import MarketEvent, MarketEventType
from models.order_position_lifecycle import ExitReason, OrderState
from models.signal_snapshot import SignalIdentity, SignalSnapshot
from tools.pending_order_revalidation import RevalidationPolicy

UTC = timezone.utc


def snapshot(*, version: int, price: float, decision: str = "FAVORABLE", direction: str = "BUY", valid_for_minutes: int = 15, generated_at: datetime | None = None) -> SignalSnapshot:
    generated_at = generated_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return SignalSnapshot(
        identity=SignalIdentity("BTCUSDT", "PAPER", "ENTRY"),
        version=version,
        direction=direction,
        decision=decision,
        confidence=80.0,
        entry_plan={"entry_price": price, "quantity": 1.0},
        generated_at=generated_at,
        valid_until=generated_at + timedelta(minutes=valid_for_minutes),
        quality=90.0,
    )


def event(price: float, timestamp: datetime, event_id: str) -> MarketEvent:
    return MarketEvent(
        symbol="BTCUSDT",
        event_timestamp=timestamp,
        event_type=MarketEventType.TRADE,
        payload={"price": price},
        source_event_id=event_id,
    )


def open_long(runtime: PaperRealtimeLifecycle, t0: datetime, price: float = 118.0) -> None:
    order = runtime.submit_signal(snapshot(version=1, price=price, generated_at=t0), now=t0)
    runtime.on_market_event(event(price, t0 + timedelta(seconds=1), "open-long"))
    assert runtime.orders.get(order.order_id).state is OrderState.FILLED


class TestPaperRealtimeLifecycleIntegration(unittest.TestCase):
    def test_buy_100_reprice_118_old_order_never_fills(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        t1 = t0 + timedelta(minutes=1)
        action = runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=118.0, generated_at=t1), market_price=120.0, now=t1)
        self.assertEqual(action.value, "REPLACE")
        self.assertEqual(runtime.orders.get(first.order_id).state, OrderState.REPLACED)
        replacement = runtime.pending.active_for_intent(first.intent_id)
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.entry_price, 118.0)
        self.assertEqual(runtime.on_market_event(event(100.0, t1 + timedelta(minutes=1), "p100")), ())
        self.assertEqual(runtime.orders.get(replacement.order_id).state, OrderState.CANCELLED)
        self.assertEqual(runtime.on_market_event(event(118.0, t1 + timedelta(minutes=2), "p118")), ())
        self.assertIsNone(runtime.positions.active_for_symbol("BTCUSDT"))

    def test_replacement_buy_limit_fills_below_current_entry(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        t1 = t0 + timedelta(minutes=1)
        runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=118.0, generated_at=t1), market_price=120.0, now=t1)
        replacement = runtime.pending.active_for_intent(first.intent_id)
        self.assertIsNotNone(replacement)
        self.assertEqual(runtime.on_market_event(event(117.0, t1 + timedelta(minutes=1), "p117")), (replacement.order_id,))
        self.assertEqual(runtime.orders.get(replacement.order_id).state, OrderState.FILLED)

    def test_buy_limit_above_entry_does_not_fill(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        order = runtime.submit_signal(snapshot(version=1, price=118.0, generated_at=t0), now=t0)
        self.assertEqual(runtime.on_market_event(event(119.0, t0 + timedelta(seconds=1), "p119")), ())
        self.assertEqual(runtime.orders.get(order.order_id).state, OrderState.PENDING)

    def test_buy_limit_at_entry_fills_once(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        order = runtime.submit_signal(snapshot(version=1, price=118.0, generated_at=t0), now=t0)
        self.assertEqual(runtime.on_market_event(event(118.0, t0 + timedelta(seconds=1), "p118")), (order.order_id,))
        self.assertEqual(runtime.on_market_event(event(118.0, t0 + timedelta(seconds=2), "p118-duplicate-source")), ())
        self.assertEqual(runtime.orders.get(order.order_id).state, OrderState.FILLED)

    def test_sell_limit_at_or_above_entry_fills(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        open_long(runtime, t0)
        order = runtime.submit_signal(snapshot(version=2, price=118.0, direction="SELL", generated_at=t0 + timedelta(minutes=2)), now=t0 + timedelta(minutes=2))
        self.assertEqual(runtime.on_market_event(event(119.0, t0 + timedelta(minutes=2, seconds=1), "sell119")), (order.order_id,))
        self.assertEqual(runtime.orders.get(order.order_id).state, OrderState.FILLED)
        self.assertIsNone(runtime.positions.active_for_symbol("BTCUSDT"))

    def test_sell_limit_below_entry_does_not_fill(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        open_long(runtime, t0)
        order = runtime.submit_signal(snapshot(version=2, price=118.0, direction="SELL", generated_at=t0 + timedelta(minutes=2)), now=t0 + timedelta(minutes=2))
        self.assertEqual(runtime.on_market_event(event(117.0, t0 + timedelta(minutes=2, seconds=1), "sell117")), ())
        self.assertEqual(runtime.orders.get(order.order_id).state, OrderState.PENDING)

    def test_sell_requires_existing_long_position(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        with self.assertRaises(ValueError):
            runtime.submit_signal(snapshot(version=1, price=118.0, direction="SELL", generated_at=t0), now=t0)

    def test_replacement_does_not_inherit_terminal_state(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        t1 = t0 + timedelta(minutes=1)
        runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=118.0, generated_at=t1), market_price=120.0, now=t1)
        replacement = runtime.pending.active_for_intent(first.intent_id)
        self.assertIsNotNone(replacement)
        self.assertEqual(runtime.orders.get(first.order_id).state, OrderState.REPLACED)
        self.assertEqual(runtime.orders.get(replacement.order_id).state, OrderState.PENDING)

    def test_replacement_has_single_active_intent_and_old_is_terminal(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        t1 = t0 + timedelta(minutes=1)
        runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=118.0, generated_at=t1), market_price=120.0, now=t1)
        replacement = runtime.pending.active_for_intent(first.intent_id)
        self.assertIsNotNone(replacement)
        active = runtime.pending.pending()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].order_id, replacement.order_id)
        self.assertEqual(runtime.orders.get(first.order_id).state, OrderState.REPLACED)
        self.assertEqual(runtime.orders.history(first.order_id)[-1].event_type, "ORDER_REPLACED")

    def test_cancelled_order_cannot_fill_position_or_ledger(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        action = runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=100.0, decision="WAIT", generated_at=t0 + timedelta(minutes=1)), market_price=105.0, now=t0 + timedelta(minutes=1))
        self.assertEqual(action.value, "CANCEL")
        before = len(runtime.ledger.events)
        self.assertEqual(runtime.on_market_event(event(100.0, t0 + timedelta(minutes=2), "late-cancelled")), ())
        self.assertEqual(runtime.orders.get(first.order_id).state, OrderState.CANCELLED)
        self.assertEqual(len(runtime.ledger.events), before)
        self.assertIsNone(runtime.positions.active_for_symbol("BTCUSDT"))

    def test_wait_cancels_pending(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        action = runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=100.0, decision="WAIT", generated_at=t0 + timedelta(minutes=1)), market_price=105.0, now=t0 + timedelta(minutes=1))
        self.assertEqual(action.value, "CANCEL")
        self.assertEqual(runtime.orders.get(first.order_id).state, OrderState.CANCELLED)
        self.assertEqual(runtime.pending.pending(), ())

    def test_sell_signal_cancels_pending_buy(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        action = runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=99.0, direction="SELL", generated_at=t0 + timedelta(minutes=1)), market_price=101.0, now=t0 + timedelta(minutes=1))
        self.assertEqual(action.value, "CANCEL")
        self.assertEqual(runtime.orders.get(first.order_id).state, OrderState.CANCELLED)

    def test_expired_signal_cancels_and_late_touch_does_not_fill(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, valid_for_minutes=1, generated_at=t0), now=t0)
        expired = snapshot(version=2, price=100.0, generated_at=t0 + timedelta(minutes=2))
        action = runtime.revalidate(intent_id=first.intent_id, snapshot=expired, market_price=105.0, now=t0 + timedelta(minutes=2))
        self.assertEqual(action.value, "CANCEL")
        self.assertEqual(runtime.on_market_event(event(100.0, t0 + timedelta(minutes=3), "late")), ())

    def test_market_distance_cancels_before_fill(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        order = runtime.submit_signal(snapshot(version=1, price=118.0, generated_at=t0), now=t0)
        self.assertEqual(runtime.on_market_event(event(100.0, t0 + timedelta(minutes=1), "distance")), ())
        self.assertEqual(runtime.orders.get(order.order_id).state, OrderState.CANCELLED)
        self.assertEqual(runtime.pending.pending(), ())
        self.assertIsNone(runtime.positions.active_for_symbol("BTCUSDT"))

    def test_market_distance_boundary_within_policy_allows_buy_fill(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        order = runtime.submit_signal(snapshot(version=1, price=118.0, generated_at=t0), now=t0)
        self.assertEqual(runtime.on_market_event(event(117.0, t0 + timedelta(minutes=1), "within-distance")), (order.order_id,))

    def test_market_distance_intermediate_decision_is_policy_driven(self) -> None:
        runtime = PaperRealtimeLifecycle()
        runtime.revalidation_policy = RevalidationPolicy(max_market_distance_pct=3.0)
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        order = runtime.submit_signal(snapshot(version=1, price=118.0, generated_at=t0), now=t0)
        self.assertEqual(runtime.on_market_event(event(114.0, t0 + timedelta(minutes=1), "policy-distance")), ())
        self.assertEqual(runtime.orders.get(order.order_id).state, OrderState.CANCELLED)

    def test_open_position_blocks_uncontrolled_duplicate_intent(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        self.assertEqual(runtime.on_market_event(event(100.0, t0 + timedelta(seconds=1), "fill")), (first.order_id,))
        with self.assertRaises(ValueError):
            runtime.submit_signal(snapshot(version=2, price=101.0, generated_at=t0 + timedelta(minutes=1)), now=t0 + timedelta(minutes=1), intent_id=first.intent_id)

    def test_repricing_limit_returns_no_trade(self) -> None:
        runtime = PaperRealtimeLifecycle()
        runtime.revalidation_policy = runtime.revalidation_policy.__class__(max_repricing_count=0)
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        action = runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=118.0, generated_at=t0 + timedelta(minutes=1)), market_price=120.0, now=t0 + timedelta(minutes=1))
        self.assertIn(action.value, {"CANCEL", "NO_TRADE"})

    def test_duplicate_market_event_is_suppressed(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        duplicate = event(100.0, t0 + timedelta(seconds=1), "same-source-event")
        self.assertEqual(runtime.on_market_event(duplicate), (first.order_id,))
        event_count = len(runtime.ledger.events)
        self.assertEqual(runtime.on_market_event(duplicate), ())
        self.assertEqual(len(runtime.ledger.events), event_count)

    def test_exit_updates_capital_and_replay_is_stable(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        runtime.on_market_event(event(100.0, t0 + timedelta(seconds=1), "fill"))
        runtime.exit_position(symbol="BTCUSDT", price=110.0, now=t0 + timedelta(minutes=2), reason=ExitReason.TAKE_PROFIT)
        state_a = runtime.replay_account()
        state_b = runtime.replay_account()
        self.assertEqual(state_a, state_b)
        self.assertEqual(state_a.position("BTCUSDT").quantity, 0.0)
        self.assertAlmostEqual(state_a.wallet.cash, 210.0)
        self.assertTrue(runtime.no_live_execution())

    def test_repriced_order_only_fills_at_or_below_current_buy_entry(self) -> None:
        runtime = PaperRealtimeLifecycle()
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = runtime.submit_signal(snapshot(version=1, price=100.0, generated_at=t0), now=t0)
        t1 = t0 + timedelta(minutes=1)
        runtime.revalidate(intent_id=first.intent_id, snapshot=snapshot(version=2, price=118.0, generated_at=t1), market_price=120.0, now=t1)
        replacement = runtime.pending.active_for_intent(first.intent_id)
        self.assertIsNotNone(replacement)
        self.assertEqual(runtime.on_market_event(event(100.0, t1 + timedelta(minutes=1), "old-entry")), ())
        self.assertEqual(runtime.orders.get(replacement.order_id).state, OrderState.CANCELLED)
        self.assertEqual(runtime.on_market_event(event(117.0, t1 + timedelta(minutes=2), "below-current-entry")), ())
        self.assertIsNone(runtime.positions.active_for_symbol("BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
