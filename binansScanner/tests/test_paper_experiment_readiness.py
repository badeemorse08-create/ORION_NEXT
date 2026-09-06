import asyncio
import json
import math
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from integration.paper_capital_runner_bridge import PaperRunnerCapitalBridge
from integration.paper_experiment import (
    PaperABComparison,
    PaperExperimentConfig,
    PaperExperimentObservation,
    build_metrics,
)
from integration.paper_realtime_lifecycle import PaperRealtimeLifecycle
from integration.paper_runtime_supervisor import PaperRuntimeSupervisor
from models.capital_management import AllocationConfig, CapitalMode
from models.paper_capital import PaperLedger
from tools.orion_paper_8h_runner import JsonlRunLog, Paper8HConfig, Paper8HRunner


class _FailingOpportunity:
    def __init__(self, error):
        self.error = error

    def discover(self):
        raise self.error


class TestPaperExperimentContract(unittest.TestCase):
    def test_reference_50_is_explicit_experiment_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = PaperExperimentConfig.reference_50(experiment_id="AB-50-001", output_dir=Path(tmp))
        self.assertEqual(config.starting_capital, 50.0)
        self.assertEqual(config.capital_mode, CapitalMode.FIXED_ALLOCATION)
        self.assertEqual(config.universe_mode, "dynamic")
        self.assertEqual(config.active_top_n, 10)
        self.assertEqual(config.broad_pool_size, 100)

    def test_config_rejects_invalid_experiment_shape(self):
        with self.assertRaises(ValueError):
            PaperExperimentConfig(
                experiment_id="x", starting_capital=50, capital_mode=CapitalMode.FIXED_ALLOCATION,
                allocation_rate=0.1, fixed_allocation=None, broad_pool_size=5, active_top_n=6,
                run_duration=timedelta(hours=1), universe_mode="dynamic", output_dir=Path("runs"),
            )

    def test_metrics_are_semantic_and_deterministic(self):
        observation = PaperExperimentObservation(
            opportunity_evaluated=10, opportunity_accepted=4, entry_evaluated=4, entry_accepted=3,
            actionable_outcomes=99, false_negatives=98,
            profitable_opportunities=10, missed_profitable_opportunities=2,
            strategy_rejections=3, capital_rejections=1, pause_rejections=2, duplicate_rejections=1,
            market_data_failures=2, recovery_count=3, duplicate_event_count=1,
            committed_capital_samples=(5.0, 10.0, 7.0),
            closed_trade_pnl=(2.0, -1.0, 3.0), hold_seconds=(60.0, 120.0, 180.0),
        )
        account = {"starting_equity": 50.0, "ending_equity": 54.0, "realized_pnl": 4.0,
                   "unrealized_pnl": 0.0, "fees": 0.1, "slippage": 0.2,
                   "maximum_drawdown": 1.5, "fills": 6}
        a = build_metrics(account=account, observation=observation)
        b = build_metrics(account=account, observation=observation)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a.win_rate, 2 / 3)
        self.assertAlmostEqual(a.expectancy, 4 / 3)
        self.assertAlmostEqual(a.profit_factor, 5.0)
        self.assertAlmostEqual(a.capital_utilization, 0.2)
        self.assertAlmostEqual(a.average_hold_time, 120.0)
        self.assertAlmostEqual(a.opportunity_capture_rate, 0.4)
        self.assertAlmostEqual(a.entry_acceptance_rate, 0.75)
        self.assertAlmostEqual(a.false_negative_rate, 0.2)
        self.assertEqual(a.rejected_by_pause, 2)
        self.assertEqual(a.market_data_failures, 2)

    def test_profit_factor_positive_profit_zero_loss_is_positive_infinity(self):
        observation = PaperExperimentObservation(closed_trade_pnl=(2.0, 3.0))
        account = {
            "starting_equity": 50.0, "ending_equity": 55.0, "realized_pnl": 5.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        metrics = build_metrics(account=account, observation=observation)
        self.assertTrue(math.isinf(metrics.profit_factor))
        self.assertGreater(metrics.profit_factor, 0.0)

    def test_profit_factor_profit_and_loss_uses_gross_profit_over_gross_loss(self):
        observation = PaperExperimentObservation(closed_trade_pnl=(4.0, 1.0, -2.0))
        account = {
            "starting_equity": 50.0, "ending_equity": 53.0, "realized_pnl": 3.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        metrics = build_metrics(account=account, observation=observation)
        self.assertAlmostEqual(metrics.profit_factor, 2.5)

    def test_profit_factor_zero_profit_zero_loss_is_deterministic_zero(self):
        observation = PaperExperimentObservation(closed_trade_pnl=())
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        metrics = build_metrics(account=account, observation=observation)
        self.assertEqual(metrics.profit_factor, 0.0)

    def test_false_negative_rate_uses_profitable_opportunity_denominator(self):
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        for missed, expected in ((0, 0.0), (2, 0.2), (10, 1.0)):
            observation = PaperExperimentObservation(
                profitable_opportunities=10, missed_profitable_opportunities=missed,
                actionable_outcomes=100, false_negatives=99,
            )
            self.assertAlmostEqual(build_metrics(account=account, observation=observation).false_negative_rate, expected)

    def test_false_negative_rate_zero_profitable_opportunities_is_deterministic_zero(self):
        observation = PaperExperimentObservation(profitable_opportunities=0, missed_profitable_opportunities=0)
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        self.assertEqual(build_metrics(account=account, observation=observation).false_negative_rate, 0.0)

    def test_capital_utilization_is_peak_committed_over_starting_equity(self):
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        low = build_metrics(
            account=account,
            observation=PaperExperimentObservation(committed_capital_samples=(5.0, 2.0)),
        )
        high = build_metrics(
            account=account,
            observation=PaperExperimentObservation(committed_capital_samples=(10.0, 2.0)),
        )
        self.assertAlmostEqual(low.capital_utilization, 0.10)
        self.assertAlmostEqual(high.capital_utilization, 0.20)

    def test_metric_changes_are_deterministic_when_semantic_inputs_change(self):
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        base = PaperExperimentObservation(
            profitable_opportunities=10, missed_profitable_opportunities=2,
            closed_trade_pnl=(4.0, -2.0), committed_capital_samples=(5.0,),
        )
        changed_denominator = PaperExperimentObservation(
            profitable_opportunities=20, missed_profitable_opportunities=2,
            closed_trade_pnl=(4.0, -2.0), committed_capital_samples=(5.0,),
        )
        changed_loss = PaperExperimentObservation(
            profitable_opportunities=10, missed_profitable_opportunities=2,
            closed_trade_pnl=(4.0, -4.0), committed_capital_samples=(5.0,),
        )
        changed_peak = PaperExperimentObservation(
            profitable_opportunities=10, missed_profitable_opportunities=2,
            closed_trade_pnl=(4.0, -2.0), committed_capital_samples=(10.0,),
        )
        base_metrics = build_metrics(account=account, observation=base)
        self.assertNotEqual(
            base_metrics.false_negative_rate,
            build_metrics(account=account, observation=changed_denominator).false_negative_rate,
        )
        self.assertNotEqual(
            base_metrics.profit_factor,
            build_metrics(account=account, observation=changed_loss).profit_factor,
        )
        self.assertNotEqual(
            base_metrics.capital_utilization,
            build_metrics(account=account, observation=changed_peak).capital_utilization,
        )

    def test_ab_comparison_is_neutral_and_structurally_compatible(self):
        observation = PaperExperimentObservation(closed_trade_pnl=(1.0, -0.5))
        account = {"starting_equity": 50.0, "ending_equity": 50.5, "realized_pnl": 0.5,
                   "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
                   "maximum_drawdown": 0.0, "fills": 2}
        metrics = build_metrics(account=account, observation=observation)
        comparison = PaperABComparison(metrics, metrics)
        self.assertTrue(comparison.structurally_equal())
        self.assertAlmostEqual(comparison.delta("ending_equity"), 0.0)

    def test_ab_profit_factor_delta_finite_vs_finite_is_improved_minus_baseline(self):
        observation_baseline = PaperExperimentObservation(closed_trade_pnl=(2.0, -1.0))
        observation_improved = PaperExperimentObservation(closed_trade_pnl=(4.0, -1.0))
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        baseline = build_metrics(account=account, observation=observation_baseline)
        improved = build_metrics(account=account, observation=observation_improved)
        self.assertEqual(PaperABComparison(baseline, improved).delta("profit_factor"), 2.0)

    def test_ab_profit_factor_delta_infinity_vs_finite_is_positive_infinity(self):
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        baseline = build_metrics(account=account, observation=PaperExperimentObservation(closed_trade_pnl=(2.0,)))
        improved = build_metrics(account=account, observation=PaperExperimentObservation(closed_trade_pnl=(2.0, -1.0)))
        self.assertEqual(PaperABComparison(baseline, improved).delta("profit_factor"), float("inf"))

    def test_ab_profit_factor_delta_finite_vs_infinity_is_positive_infinity(self):
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        baseline = build_metrics(account=account, observation=PaperExperimentObservation(closed_trade_pnl=(2.0, -1.0)))
        improved = build_metrics(account=account, observation=PaperExperimentObservation(closed_trade_pnl=(2.0,)))
        self.assertEqual(PaperABComparison(baseline, improved).delta("profit_factor"), float("inf"))

    def test_ab_profit_factor_delta_infinity_vs_infinity_is_deterministic_zero(self):
        account = {
            "starting_equity": 50.0, "ending_equity": 50.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
            "maximum_drawdown": 0.0,
        }
        metrics = build_metrics(account=account, observation=PaperExperimentObservation(closed_trade_pnl=(2.0,)))
        comparison = PaperABComparison(metrics, metrics)
        delta = comparison.delta("profit_factor")
        self.assertEqual(delta, 0.0)
        self.assertFalse(math.isnan(delta))
        self.assertEqual(delta, comparison.delta("profit_factor"))


class TestPaperCapitalCrashWindows(unittest.TestCase):
    def _new_bridge(self, journal: Path, ledger: PaperLedger | None = None):
        return PaperRunnerCapitalBridge(
            AllocationConfig(starting_capital=50.0, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=5.0),
            ledger or PaperLedger(starting_equity=50.0), journal_path=journal,
        )

    def test_reserve_stop_recover_preserves_identity_and_releases_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "capital.jsonl"
            live = self._new_bridge(journal)
            audit = live.allocation_for(symbol="BTCUSDT", rank=1, opportunity_score=90.0, required_symbol_minimum=0.0)
            self.assertTrue(audit.accepted)
            recovered = self._new_bridge(journal)
            self.assertEqual(recovered.pending_reserved, 5.0)
            self.assertEqual(recovered._allocation_state[audit.allocation_id], "RESERVED")
            self.assertTrue(recovered.release(audit.allocation_id, reason="TEST_EXIT"))
            self.assertFalse(recovered.release(audit.allocation_id, reason="DUPLICATE"))
            self.assertEqual(recovered.audit_state()["reserved_capital"], 0.0)
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([event["type"] for event in events], ["RESERVE", "RELEASE"])
            recovered_again = self._new_bridge(journal)
            self.assertEqual(recovered_again.audit_state(), recovered.audit_state())

    def test_reserve_bind_stop_recover_preserves_order_identity_and_releases_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "capital.jsonl"
            live = self._new_bridge(journal)
            audit = live.allocation_for(symbol="ETHUSDT", rank=2, opportunity_score=80.0, required_symbol_minimum=0.0)
            live.bind_order(audit.allocation_id, "ENTRY-ORDER-1")
            recovered = self._new_bridge(journal)
            self.assertEqual(recovered._allocation_state[audit.allocation_id], "BOUND")
            self.assertEqual(recovered._allocation_to_order[audit.allocation_id], "ENTRY-ORDER-1")
            self.assertEqual(recovered._order_to_allocation["ENTRY-ORDER-1"], audit.allocation_id)
            self.assertTrue(recovered.release(audit.allocation_id, reason="TEST_EXIT"))
            self.assertFalse(recovered.release(audit.allocation_id, reason="DUPLICATE"))
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([event["type"] for event in events], ["RESERVE", "BIND", "RELEASE"])
            recovered_again = self._new_bridge(journal)
            self.assertEqual(recovered_again.audit_state(), recovered.audit_state())


class TestPaperRunnerNetworkFailureMatrix(unittest.TestCase):
    def _runner(self, tmp: str, error: Exception) -> Paper8HRunner:
        config = Paper8HConfig(duration_hours=1.0, starting_capital=50.0, dynamic_universe=True,
                               output_dir=Path(tmp), capital_mode=CapitalMode.FIXED_ALLOCATION,
                               allocation_rate=0.10, top_n=2)
        runtime = PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=50.0))
        supervisor = PaperRuntimeSupervisor(runtime=runtime)
        runner = Paper8HRunner(config=config, stream=object(), supervisor=supervisor,
                               opportunity=_FailingOpportunity(error),
                               log=JsonlRunLog(Path(tmp) / "events.jsonl"))
        runner.log.open()
        return runner

    def _assert_fail_closed(self, error: Exception):
        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                runner = self._runner(tmp, error)
                try:
                    await runner._run_signal_cycle(None)
                    self.assertEqual(runner.capital.pending_reserved, 0.0)
                    self.assertEqual(runner.supervisor.active_orders, ())
                    records = [json.loads(line) for line in Path(tmp, "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
                    failure = next(record for record in records if record["event_type"] == "signal_cycle_failure")
                    self.assertTrue(failure["fail_closed"])
                    self.assertEqual(failure["rejection_reason"], "MARKET_DATA_FAILURE")
                finally:
                    runner.log.close()
        asyncio.run(run_case())

    def test_dns_failure_fail_closed(self): self._assert_fail_closed(OSError("DNS resolution failed"))
    def test_timeout_fail_closed(self): self._assert_fail_closed(TimeoutError("market request timed out"))
    def test_market_data_failure_fail_closed(self): self._assert_fail_closed(RuntimeError("market data unavailable"))
    def test_decision_context_failure_fail_closed(self): self._assert_fail_closed(ValueError("decision context unavailable"))


if __name__ == "__main__":
    unittest.main()
