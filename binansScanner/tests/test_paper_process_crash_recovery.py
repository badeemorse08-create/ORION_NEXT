from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from integration.paper_realtime_lifecycle import PaperRealtimeLifecycle
from integration.paper_runtime_supervisor import PaperRuntimeSupervisor
from models.market_event import MarketEvent, MarketEventType
from models.paper_capital import PaperLedger
from models.signal_snapshot import SignalIdentity, SignalSnapshot

UTC = timezone.utc


def _snapshot(now: datetime) -> SignalSnapshot:
    return SignalSnapshot(
        identity=SignalIdentity("BTCUSDT", "PAPER", "ENTRY"),
        version=1,
        direction="BUY",
        decision="FAVORABLE",
        confidence=80.0,
        entry_plan={"entry_price": 100.0, "quantity": 0.4},
        generated_at=now,
        valid_until=now + timedelta(minutes=15),
        quality=90.0,
    )


def _market(now: datetime) -> MarketEvent:
    return MarketEvent(
        symbol="BTCUSDT",
        event_timestamp=now,
        event_type=MarketEventType.TRADE,
        payload={"price": 100.0},
        source_event_id="process-crash-fill-1",
    )


class TestPaperProcessCrashRecovery(unittest.TestCase):
    def test_fresh_supervisor_recovers_from_durable_runtime_after_process_loss(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_path = root / "control.json"
            runtime_path = root / "runtime_state.jsonl"

            first = PaperRuntimeSupervisor(
                runtime=PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=50.0)),
                control_path=control_path,
                durable_state_path=runtime_path,
            )
            order = first.submit_signal(_snapshot(now), now=now, order_id="ORDER-CRASH-1")
            fill = _market(now + timedelta(seconds=1))
            self.assertEqual(first.process_market_event(fill), (order.order_id,))

            canonical_state = first.replay_state()
            canonical_ledger_event_count = len(first.runtime.ledger.events)
            canonical_position_id = first.active_positions[0].position_id
            self.assertEqual(first.runtime.replay_account().wallet.cash, 10.0)
            self.assertTrue(first.no_live_path())
            self.assertGreater(runtime_path.stat().st_size, 0)

            # Simulate complete process/runtime memory loss. The replacement
            # supervisor starts with no lifecycle operations, orders, positions,
            # or processed-event set; only the durable files remain.
            del first
            restarted = PaperRuntimeSupervisor(
                runtime=PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=200.0)),
                control_path=control_path,
                durable_state_path=runtime_path,
            )
            self.assertEqual(restarted._operations, [])
            self.assertEqual(restarted.active_orders, ())
            self.assertEqual(restarted.active_positions, ())
            self.assertEqual(restarted.runtime.ledger.events, ())

            recovered = restarted.recover()
            self.assertEqual(recovered.replay_state(), canonical_state)
            self.assertEqual(recovered.active_orders, ())
            self.assertEqual(len(recovered.runtime.ledger.events), canonical_ledger_event_count)
            self.assertIn("ORDER-CRASH-1", recovered.terminal_orders)
            self.assertEqual(recovered.active_positions[0].position_id, canonical_position_id)
            self.assertEqual(recovered.active_positions[0].symbol, "BTCUSDT")
            self.assertEqual(recovered.runtime.replay_account().starting_equity, 50.0)
            self.assertEqual(recovered.runtime.replay_account().wallet.cash, 10.0)
            self.assertTrue(recovered.runtime.no_live_execution())

            # The durable replay restores the event-id suppression set. Replaying
            # the exact source event cannot mint another accounting event.
            before = len(recovered.runtime.ledger.events)
            self.assertEqual(recovered.process_market_event(fill), ())
            self.assertEqual(len(recovered.runtime.ledger.events), before)

            recovered_again = recovered.recover()
            self.assertEqual(recovered_again.replay_state(), canonical_state)
            self.assertEqual(
                len(recovered_again.runtime.ledger.events),
                canonical_ledger_event_count,
            )
            self.assertEqual(
                recovered_again.runtime.replay_account(),
                recovered.runtime.replay_account(),
            )
            self.assertEqual(
                recovered_again.active_positions[0].position_id,
                canonical_position_id,
            )

    def test_durable_runtime_journal_is_ordered_and_rejects_corruption(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_path = root / "runtime_state.jsonl"
            control_path = root / "control.json"
            now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            supervisor = PaperRuntimeSupervisor(
                control_path=control_path,
                durable_state_path=runtime_path,
            )
            supervisor.submit_signal(_snapshot(now), now=now, order_id="ORDER-DURABLE-1")
            records = runtime_path.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(records), 2)
            import json
            sequences = [int(json.loads(line)["sequence"]) for line in records]
            self.assertEqual(sequences, list(range(1, len(records) + 1)))

            runtime_path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                supervisor.recover()


if __name__ == "__main__":
    unittest.main()
