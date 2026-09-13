"""Assembly boundary for deterministic paper A/B experiment reports.

Metric semantics are defined by ``PaperExperimentMetrics``. This adapter only
assembles those metrics from canonical runner/accounting output and never
recomputes or owns accounting state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from integration.paper_experiment import PaperExperimentConfig, PaperExperimentMetrics, PaperExperimentObservation, build_metrics


@dataclass(frozen=True, slots=True)
class PaperExperimentReport:
    config: PaperExperimentConfig
    metrics: PaperExperimentMetrics
    recovery_verified: bool
    no_live_execution: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment": {
                "experiment_id": self.config.experiment_id,
                "starting_capital": self.config.starting_capital,
                "capital_mode": self.config.capital_mode.value,
                "allocation_rate": self.config.allocation_rate,
                "fixed_allocation": self.config.fixed_allocation,
                "broad_pool_size": self.config.broad_pool_size,
                "active_top_n": self.config.active_top_n,
                "run_duration_seconds": self.config.run_duration.total_seconds(),
                "universe_mode": self.config.universe_mode,
                "output_dir": str(self.config.output_dir),
            },
            "metrics": self.metrics.as_dict(),
            "verification": {"recovery_verified": self.recovery_verified, "no_live_execution": self.no_live_execution},
        }


def report_from_runner(*, config: PaperExperimentConfig, runner_report: Mapping[str, Any], observation: PaperExperimentObservation, recovery_verified: bool, no_live_execution: bool) -> PaperExperimentReport:
    """Adapt canonical runner/accounting output without re-owning accounting."""
    account = {
        "starting_equity": runner_report["starting_equity"],
        "ending_equity": runner_report["ending_equity"],
        "realized_pnl": runner_report["realized_pnl"],
        "unrealized_pnl": runner_report["unrealized_pnl"],
        "fees": runner_report["fees"],
        "slippage": runner_report["slippage"],
        "maximum_drawdown": runner_report.get("maximum_drawdown", runner_report.get("max_drawdown", 0.0)),
        "fills": runner_report.get("fills", 0),
    }
    return PaperExperimentReport(config=config, metrics=build_metrics(account=account, observation=observation), recovery_verified=recovery_verified, no_live_execution=no_live_execution)


__all__ = ["PaperExperimentReport", "report_from_runner"]
