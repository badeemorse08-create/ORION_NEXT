from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestPaperProcessCrashRecovery(unittest.TestCase):
    def test_crash_before_commit_is_not_replayed(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_path = root / "control.json"
            runtime_path = root / "runtime_state.jsonl"
            supervisor = PaperRuntimeSupervisor(
                runtime=PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=50.0)),
                control_path=control_path,
                durable_state_path=runtime_path,
            )
            original_append = supervisor._append_durable_record

            def crash_before_commit(record_type: str, payload: dict) -> None:
                if record_type == "SUBMIT_COMMIT":
                    raise RuntimeError("simulated crash before durable commit")
                original_append(record_type, payload)

            # Process A reaches the runtime mutation after the INTENT fsync, but
            # crashes before the COMMIT fsync. The mutation is therefore transient
            # in Process A and must not become recoverable state.
            with patch.object(PaperRuntimeSupervisor, "_append_durable_record", side_effect=crash_before_commit):
                with self.assertRaisesRegex(RuntimeError, "simulated crash before durable commit"):
                    supervisor.submit_signal(_snapshot(now), now=now, order_id="ORDER-UNCOMMITTED")

            records = _records(runtime_path)
            self.assertEqual([record["type"] for record in records], ["RUN_INIT", "SUBMIT_INTENT"])
            self.assertEqual(records[1]["operation_id"], 2)
            self.assertEqual(tuple(order.order_id for order in supervisor.active_orders), ("ORDER-UNCOMMITTED",))

            # Process B starts from fresh in-memory state. Recovery must replay
            # committed operations only, so the unmatched INTENT disappears with
            # Process A and the order must be absent from recovered state.
            restarted = PaperRuntimeSupervisor(
                runtime=PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=200.0)),
                control_path=control_path,
                durable_state_path=runtime_path,
            )
            self.assertEqual(restarted._operations, [])
            self.assertEqual(restarted.active_orders, ())
            recovered = restarted.recover()
            self.assertEqual(recovered.active_orders, ())
            self.assertEqual(recovered.terminal_orders, ())
            self.assertEqual(recovered.runtime.ledger.events, ())
            self.assertEqual(recovered.runtime.replay_account().starting_equity, 50.0)
            self.assertEqual(recovered.runtime.replay_account().wallet.cash, 50.0)
            self.assertTrue(recovered.runtime.no_live_execution())

    def test_successful_commit_survives_process_loss_and_recovery_is_deterministic(self) -> None:
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

            records = _records(runtime_path)
            types = [record["type"] for record in records]
            self.assertEqual(types, ["RUN_INIT", "SUBMIT_INTENT", "SUBMIT_COMMIT", "MARKET_INTENT", "MARKET_COMMIT"])
            self.assertEqual(records[2]["operation_id"], records[1]["operation_id"])
            self.assertEqual(records[4]["operation_id"], records[3]["operation_id"])

            canonical_state = first.replay_state()
            canonical_ledger_event_count = len(first.runtime.ledger.events)
            canonical_position_id = first.active_positions[0].position_id
            self.assertEqual(first.runtime.replay_account().wallet.cash, 10.0)
            self.assertTrue(first.no_live_path())

            restarted = PaperRuntimeSupervisor(
                runtime=PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=200.0)),
                control_path=control_path,
                durable_state_path=runtime_path,
            )
            self.assertEqual(restarted._operations, [])
            self.assertEqual(restarted.runtime.ledger.events, ())
            recovered = restarted.recover()
            self.assertEqual(recovered.replay_state(), canonical_state)
            self.assertEqual(len(recovered.runtime.ledger.events), canonical_ledger_event_count)
            self.assertIn("ORDER-CRASH-1", recovered.terminal_orders)
            self.assertEqual(recovered.active_positions[0].position_id, canonical_position_id)
            self.assertEqual(recovered.runtime.replay_account().starting_equity, 50.0)
            self.assertEqual(recovered.runtime.replay_account().wallet.cash, 10.0)
            self.assertTrue(recovered.runtime.no_live_execution())

            before = len(recovered.runtime.ledger.events)
            self.assertEqual(recovered.process_market_event(fill), ())
            self.assertEqual(len(recovered.runtime.ledger.events), before)

            recovered_again = recovered.recover()
            self.assertEqual(recovered_again.replay_state(), canonical_state)
            self.assertEqual(recovered_again.runtime.replay_account(), recovered.runtime.replay_account())
            self.assertEqual(recovered_again.active_positions[0].position_id, canonical_position_id)

    def test_corruption_and_sequence_discontinuity_fail_closed(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_path = root / "runtime_state.jsonl"
            control_path = root / "control.json"
            supervisor = PaperRuntimeSupervisor(
                control_path=control_path,
                durable_state_path=runtime_path,
            )
            supervisor.submit_signal(_snapshot(now), now=now, order_id="ORDER-DURABLE-1")

            records = _records(runtime_path)
            self.assertEqual([int(record["sequence"]) for record in records], list(range(1, len(records) + 1)))
            records[1]["sequence"] = 99
            runtime_path.write_text(
                "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "sequence mismatch"):
                supervisor.recover()

            runtime_path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid durable runtime journal JSON"):
                supervisor.recover()


if __name__ == "__main__":
    unittest.main()
