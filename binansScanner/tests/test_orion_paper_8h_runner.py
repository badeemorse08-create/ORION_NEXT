from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from integration.paper_realtime_lifecycle import PaperRealtimeLifecycle
from integration.paper_runtime_supervisor import PaperRuntimeSupervisor
from models.market_event import MarketEvent, MarketEventType
from models.opportunity import MarketMetrics, OpportunityCandidate, OpportunityCandidateSet
from models.paper_capital import PaperLedger
from models.scalping_opportunity import DecisionTrace, EntryState, OpportunityClass, RejectionReason, ScalpingCandidateSet
from tools.orion_paper_8h_runner import DynamicMarketStream, JsonlRunLog, Paper8HConfig, Paper8HRunner, parse_args

UTC = timezone.utc


def trade_event(symbol: str, price: float, event_id: str, timestamp: datetime) -> MarketEvent:
    return MarketEvent(symbol=symbol, event_timestamp=timestamp, event_type=MarketEventType.TRADE, payload={"price": price}, source_event_id=event_id)


def candle_event(symbol: str, price: float, event_id: str, timestamp: datetime) -> MarketEvent:
    return MarketEvent(symbol=symbol, event_timestamp=timestamp, event_type=MarketEventType.CANDLE_CLOSE, payload={"close": price, "price": price, "timeframe": "1m", "is_closed": True}, source_event_id=event_id)


class FakeScalpingOpportunity:
    def __init__(self, symbols=("BTCUSDT",)):
        self.symbols_sequence = [tuple(symbols)]
        self.calls = 0
        self.entry_state = EntryState.A
        self.entry_allowed = True
        self.directional_evidence = 0.8
        self.rejection_reasons: tuple[RejectionReason, ...] = ()

    def _candidate(self, symbol: str, rank: int) -> OpportunityCandidate:
        score = 90.0 - rank + 1
        trace = DecisionTrace(
            discovered=True,
            eligible=True,
            measured_features=("regime", "trend", "momentum", "acceleration", "directional_evidence"),
            opportunity_class=OpportunityClass.TREND_CONTINUATION,
            opportunity_score=score,
            directional_evidence=self.directional_evidence,
            entry_state=self.entry_state,
            entry_allowed=self.entry_allowed,
            rejection_reasons=self.rejection_reasons,
            reasons=("trend_continuation",),
        )
        return OpportunityCandidate(symbol=symbol, opportunity_score=score, rank=rank, metrics=MarketMetrics(symbol, 200_000_000.0, 0.03, None, True, 100.0), eligibility_reasons=(), directional_evidence=self.directional_evidence, opportunity_class=OpportunityClass.TREND_CONTINUATION.value, entry_state=self.entry_state.value, entry_readiness=0.9 if self.entry_allowed else 0.2, decision_trace=trace)

    def discover(self, *args, **kwargs):
        symbols = self.symbols_sequence[min(self.calls, len(self.symbols_sequence) - 1)]
        self.calls += 1
        active = tuple(self._candidate(symbol, index + 1) for index, symbol in enumerate(symbols))
        broad = OpportunityCandidateSet(candidates=active, top_n=max(len(active) + 1, 2))
        active_set = OpportunityCandidateSet(candidates=active, top_n=len(active))
        return ScalpingCandidateSet(broad_pool=broad, active_set=active_set, refreshed=True)


class TestPaper8HConfig(unittest.TestCase):
    def test_default_is_dynamic_and_uses_top_n(self):
        config = Paper8HConfig()
        self.assertTrue(config.dynamic_universe)
        self.assertEqual(config.symbols, ())
        self.assertEqual(config.top_n, 10)
        self.assertEqual(config.duration_hours, 8.0)
        self.assertEqual(config.starting_capital, 200.0)

    def test_fixed_override_is_explicit(self):
        config = Paper8HConfig(dynamic_universe=False, symbols=("BTCUSDT",), top_n=1)
        self.assertFalse(config.dynamic_universe)
        with self.assertRaises(ValueError):
            Paper8HConfig(dynamic_universe=True, symbols=("BTCUSDT",))
        with self.assertRaises(ValueError):
            Paper8HConfig(dynamic_universe=False, symbols=())

    def test_cli_defaults_to_dynamic_and_accepts_top_n(self):
        args = parse_args(["--top-n", "3"])
        self.assertEqual(args.universe, "dynamic")
        self.assertEqual(args.top_n, 3)
        self.assertEqual(args.symbols, "")


class TestDynamicMarketStream(unittest.TestCase):
    def test_subscription_changes_only_when_top_n_changes(self):
        stream = DynamicMarketStream(("BTCUSDT", "ETHUSDT"))
        self.assertFalse(stream.set_symbols(("ETHUSDT", "BTCUSDT")))
        self.assertTrue(stream.set_symbols(("SOLUSDT", "BTCUSDT")))
        self.assertEqual(stream.symbols, ("BTCUSDT", "SOLUSDT"))


class TestPaper8HRunnerE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        config = Paper8HConfig(output_dir=Path(self.tempdir.name))
        runtime = PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=200.0))
        supervisor = PaperRuntimeSupervisor(runtime=runtime, control_path=Path(self.tempdir.name) / "trading_control.json")
        self.opportunity = FakeScalpingOpportunity()
        self.runner = Paper8HRunner(config=config, stream=DynamicMarketStream(("BTCUSDT",)), supervisor=supervisor, opportunity=self.opportunity, log=JsonlRunLog(Path(self.tempdir.name) / "events.jsonl"), previous_top_symbols=("BTCUSDT",))
        self.runner.log.open()

    async def asyncTearDown(self):
        self.runner.log.close()
        self.tempdir.cleanup()

    async def _open_long(self):
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.runner.last_prices["BTCUSDT"] = 100.0
        await self.runner._on_market_event(candle_event("BTCUSDT", 100.0, "entry-candle", t0))
        await self.runner._on_market_event(trade_event("BTCUSDT", 99.0, "entry-fill", t0.replace(second=10)))
        position = self.runner.supervisor.runtime.positions.active_for_symbol("BTCUSDT")
        self.assertIsNotNone(position)
        return t0.replace(minute=1), position

    async def test_dynamic_candidate_replacement_refreshes_stream_without_touching_position(self):
        self.opportunity.symbols_sequence = [("BTCUSDT",), ("ETHUSDT",)]
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.runner.last_prices.update({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        await self.runner._on_market_event(candle_event("BTCUSDT", 100.0, "c1", t0))
        self.assertEqual(self.runner.stream.symbols, ("BTCUSDT",))
        await self.runner._on_market_event(candle_event("BTCUSDT", 100.0, "c2", t0.replace(minute=1)))
        self.assertEqual(self.runner.stream.symbols, ("ETHUSDT",))

    async def test_actual_runner_path_can_create_pending_buy(self):
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.runner.last_prices["BTCUSDT"] = 100.0
        await self.runner._on_market_event(candle_event("BTCUSDT", 100.0, "candle-1", t0))
        active = self.runner.supervisor.runtime.pending.pending()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].side.value, "BUY")

    async def test_d1_rejection_states_preserve_existing_position(self):
        next_time, position = await self._open_long()
        for index, entry_state in enumerate((EntryState.B, EntryState.C, EntryState.D), start=1):
            self.opportunity.entry_state = entry_state
            self.opportunity.entry_allowed = False
            self.opportunity.directional_evidence = 0.0
            self.opportunity.rejection_reasons = (RejectionReason.STRATEGY,)
            await self.runner._on_market_event(candle_event("BTCUSDT", 99.0, f"reject-{index}", next_time.replace(minute=next_time.minute + index)))
            current = self.runner.supervisor.runtime.positions.active_for_symbol("BTCUSDT")
            self.assertIsNotNone(current)
            self.assertEqual(current.position_id, position.position_id)
            self.assertAlmostEqual(current.quantity, position.quantity)

    async def test_directional_conflict_and_insufficient_preserve_existing_position(self):
        next_time, position = await self._open_long()
        for index, reason in enumerate((RejectionReason.DIRECTIONAL_CONFLICT, RejectionReason.DIRECTIONAL_INSUFFICIENT), start=1):
            self.opportunity.entry_state = EntryState.D
            self.opportunity.entry_allowed = False
            self.opportunity.directional_evidence = -0.8 if reason is RejectionReason.DIRECTIONAL_CONFLICT else 0.0
            self.opportunity.rejection_reasons = (reason,)
            await self.runner._on_market_event(candle_event("BTCUSDT", 99.0, f"direction-{index}", next_time.replace(minute=next_time.minute + index)))
            current = self.runner.supervisor.runtime.positions.active_for_symbol("BTCUSDT")
            self.assertIsNotNone(current)
            self.assertEqual(current.position_id, position.position_id)
            self.assertAlmostEqual(current.quantity, position.quantity)

    async def test_pause_preserves_existing_position(self):
        next_time, position = await self._open_long()
        self.runner.supervisor.pause_new_entries(source="test", reason="protect existing position")
        self.opportunity.entry_state = EntryState.A
        self.opportunity.entry_allowed = True
        self.opportunity.rejection_reasons = ()
        await self.runner._on_market_event(candle_event("BTCUSDT", 99.0, "paused-entry", next_time))
        current = self.runner.supervisor.runtime.positions.active_for_symbol("BTCUSDT")
        self.assertIsNotNone(current)
        self.assertEqual(current.position_id, position.position_id)
        self.assertAlmostEqual(current.quantity, position.quantity)
        self.assertEqual(self.runner.supervisor.trading_state.value, "PAUSED")

    async def test_top_n_removal_preserves_existing_position(self):
        next_time, position = await self._open_long()
        self.opportunity.symbols_sequence = [("BTCUSDT",), ()]
        await self.runner._on_market_event(candle_event("BTCUSDT", 99.0, "topn-removal", next_time))
        current = self.runner.supervisor.runtime.positions.active_for_symbol("BTCUSDT")
        self.assertIsNotNone(current)
        self.assertEqual(current.position_id, position.position_id)
        self.assertAlmostEqual(current.quantity, position.quantity)

    async def test_real_d4_exit_remains_available_when_d1_rejects_and_paused(self):
        next_time, position = await self._open_long()
        self.runner.supervisor.pause_new_entries(source="test", reason="exit-path check")
        self.opportunity.entry_state = EntryState.D
        self.opportunity.entry_allowed = False
        self.opportunity.rejection_reasons = (RejectionReason.STRATEGY,)
        await self.runner._on_market_event(candle_event("BTCUSDT", 101.0, "d1-rejected-paused", next_time))
        self.assertIsNotNone(self.runner.supervisor.runtime.positions.active_for_symbol("BTCUSDT"))
        exit_order_id = self.runner.supervisor.runtime.exit_position(symbol="BTCUSDT", price=101.0, now=next_time)
        self.assertTrue(exit_order_id)
        self.assertIsNone(self.runner.supervisor.runtime.positions.active_for_symbol("BTCUSDT"))
        self.assertEqual(self.runner.supervisor.runtime.orders.get(exit_order_id).state.value, "FILLED")

    async def test_runner_never_uses_d1_rejection_as_an_exit_trigger(self):
        source = Path(__file__).resolve().parents[2] / "tools" / "orion_paper_8h_runner.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("exit_trigger=\"D1_ENTRY_NOT_ALLOWED\"", text)
        self.assertNotIn("self.supervisor.runtime.exit_position", text)

    async def test_market_event_to_fill_to_position_to_ledger_to_recovery(self):
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        await self.runner._on_market_event(candle_event("BTCUSDT", 100.0, "candle-1", t0))
        await self.runner._on_market_event(trade_event("BTCUSDT", 99.0, "trade-1", t0.replace(second=10)))
        self.assertEqual(len(self.runner.supervisor.active_positions), 1)
        state = self.runner.supervisor.runtime.ledger.replay()
        self.assertGreater(state.position("BTCUSDT").quantity, 0.0)
        original = self.runner.supervisor.replay_state()
        recovered = self.runner.supervisor.recover()
        self.assertEqual(original, recovered.replay_state())
        self.assertEqual(recovered.replay_state(), recovered.recover().replay_state())

    async def test_duplicate_market_event_is_suppressed(self):
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        event = trade_event("BTCUSDT", 100.0, "duplicate", t0)
        await self.runner._on_market_event(event)
        before = len(self.runner.supervisor.runtime.ledger.events)
        await self.runner._on_market_event(event)
        self.assertEqual(len(self.runner.supervisor.runtime.ledger.events), before)
        self.assertEqual(self.runner.supervisor.health.duplicate_events, 1)


class TestPaper8HLog(unittest.TestCase):
    def test_jsonl_log_persists_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = JsonlRunLog(path)
            log.open(); log.write("run_start", starting_equity=200.0); log.close()
            content = path.read_text(encoding="utf-8")
            self.assertIn("run_start", content)
            self.assertIn("200.0", content)


if __name__ == "__main__":
    unittest.main()
