import unittest
from datetime import timedelta
from pathlib import Path

from integration.paper_experiment import PaperExperimentConfig, PaperExperimentObservation
from integration.paper_experiment_report import report_from_runner
from models.capital_management import CapitalMode


class TestPaperExperimentReport(unittest.TestCase):
    def test_report_contains_complete_contract_without_recomputing_accounting(self):
        config = PaperExperimentConfig(
            experiment_id="AB-TEST-001", starting_capital=50.0,
            capital_mode=CapitalMode.FIXED_ALLOCATION, allocation_rate=0.10,
            fixed_allocation=None, broad_pool_size=100, active_top_n=10,
            run_duration=timedelta(hours=1), universe_mode="dynamic",
            output_dir=Path("runs/paper"),
        )
        runner_report = {"starting_equity": 50.0, "ending_equity": 51.0, "realized_pnl": 1.0,
                         "unrealized_pnl": 0.0, "fees": 0.1, "slippage": 0.05,
                         "maximum_drawdown": 0.5, "fills": 2}
        observation = PaperExperimentObservation(
            opportunity_evaluated=4, opportunity_accepted=2, entry_evaluated=2,
            entry_accepted=1, profitable_opportunities=1, missed_profitable_opportunities=0,
            strategy_rejections=1, capital_rejections=0, pause_rejections=0,
            duplicate_rejections=0, market_data_failures=1, recovery_count=1,
            duplicate_event_count=0, committed_capital_samples=(5.0,),
            closed_trade_pnl=(1.0,), hold_seconds=(120.0,),
        )
        payload = report_from_runner(config=config, runner_report=runner_report,
                                    observation=observation, recovery_verified=True,
                                    no_live_execution=True).as_dict()
        self.assertEqual(payload["experiment"]["starting_capital"], 50.0)
        self.assertEqual(payload["experiment"]["active_top_n"], 10)
        self.assertEqual(payload["metrics"]["fills"], 2)
        self.assertEqual(payload["metrics"]["market_data_failures"], 1)
        self.assertEqual(payload["metrics"]["profit_factor"], float("inf"))
        self.assertEqual(payload["metrics"]["false_negative_rate"], 0.0)
        self.assertTrue(payload["verification"]["recovery_verified"])
        self.assertTrue(payload["verification"]["no_live_execution"])


if __name__ == "__main__":
    unittest.main()
