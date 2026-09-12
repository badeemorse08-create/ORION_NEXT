from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from integration.paper_realtime_lifecycle import PaperRealtimeLifecycle
from integration.paper_runtime_supervisor import PaperRuntimeSupervisor
from providers.market_stream import MarketEventNormalizer
from tools._orion_paper_8h_runner_legacy import JsonlRunLog, Paper8HConfig, Paper8HRunner
from models.market_event import MarketEventType
from models.paper_capital import PaperLedger
from replay.clock import ReplayClock
from replay.dataset import HistoricalDataset
from replay.source import HistoricalMarketDataSource
from replay.stream import HistoricalMarketEventStream
from services.binance_scalping_source import BinanceScalpingCandleSource
from services.opportunity_discovery import MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery
from services.scalping_opportunity import ScalpingCandidatePoolManager, ScalpingConfig, ScalpingDecisionEngine
from services.scalping_pipeline import ScalpingOpportunityPipeline


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    campaign: str = "7D"
    acceleration_factor: float = 600.0
    end_policy: str = "CLOSE_AT_END"
    active_top_n: int = 10
    broad_pool_top_n: int = 100

    def __post_init__(self) -> None:
        if self.campaign not in {"7D", "30D", "90D", "365D"}:
            raise ValueError("unsupported replay campaign")
        if self.acceleration_factor <= 0:
            raise ValueError("acceleration_factor must be positive")
        if self.end_policy != "CLOSE_AT_END":
            raise ValueError("only CLOSE_AT_END is implemented in this package")
        if self.active_top_n <= 0 or self.broad_pool_top_n <= self.active_top_n:
            raise ValueError("invalid replay pool sizes")


class HistoricalPaperReplayRunner(Paper8HRunner):
    """Offline replay orchestration that reuses canonical discovery/scalping/paper contracts."""

    @classmethod
    def build(
        cls,
        dataset: HistoricalDataset,
        output_dir: Path,
        *,
        replay_config: ReplayConfig | None = None,
        starting_capital: float = 200.0,
    ) -> "HistoricalPaperReplayRunner":
        replay_config = replay_config or ReplayConfig()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        clock = ReplayClock(dataset.start, replay_config.acceleration_factor)
        source = HistoricalMarketDataSource(dataset, clock)
        discovery_config = OpportunityConfig(
            default_top_n=replay_config.broad_pool_top_n,
            refresh_interval_seconds=30.0,
            cache_ttl_seconds=0.0,
        )
        discovery = OpportunityDiscovery(
            MarketUniverseDiscovery(source, discovery_config),
            source,
            discovery_config,
            clock=clock.monotonic,
        )
        discovery.market_provider = source
        scalping_config = ScalpingConfig(
            active_top_n=replay_config.active_top_n,
            broad_pool_top_n=replay_config.broad_pool_top_n,
        )
        pipeline = ScalpingOpportunityPipeline(
            discovery,
            BinanceScalpingCandleSource(source),
            decision_engine=ScalpingDecisionEngine(scalping_config),
            pool_manager=ScalpingCandidatePoolManager(scalping_config),
        )

        # Startup is a hard gate: no Paper runtime is constructed until the canonical
        # discovery + Fast Recall + deep evaluation path has completed successfully.
        initial = pipeline.discover()
        selected_symbols = tuple(candidate.symbol for candidate in initial.candidates)
        if not selected_symbols:
            raise RuntimeError("historical replay startup produced no active opportunities")

        stream = HistoricalMarketEventStream(dataset, clock)
        lifecycle = PaperRealtimeLifecycle(ledger=PaperLedger(starting_equity=starting_capital))
        supervisor = PaperRuntimeSupervisor(
            runtime=lifecycle,
            control_path=output_dir / "replay_trading_control.json",
        )
        paper_config = Paper8HConfig(
            duration_hours=1.0,
            starting_capital=starting_capital,
            dynamic_universe=True,
            output_dir=output_dir,
            top_n=replay_config.active_top_n,
        )
        return cls(
            paper_config,
            stream,
            supervisor,
            pipeline,
            JsonlRunLog(output_dir / "events.jsonl"),
            peak_equity=starting_capital,
        )

    async def run_replay(self, dataset: HistoricalDataset, *, replay_config: ReplayConfig | None = None) -> dict:
        replay_config = replay_config or ReplayConfig()
        clock = self.stream.clock
        normalizer = MarketEventNormalizer()
        self.log.open()
        self.log.write(
            "replay_start",
            code_sha=self._code_sha(),
            dataset_version=dataset.manifest.dataset_version,
            dataset_hash=dataset.manifest.integrity_sha256,
            period=dataset.manifest.period,
            campaign=replay_config.campaign,
            simulation_start=dataset.start.isoformat(),
            simulation_end=dataset.end.isoformat(),
            acceleration_factor=replay_config.acceleration_factor,
            end_of_run_policy=replay_config.end_policy,
            paper_only=True,
            live_execution=False,
            credentials_used=False,
            exchange_orders=False,
        )

        seen: set[str] = set()
        last_timestamp: datetime | None = None
        processed = duplicates = out_of_order = decision_cycles = 0
        simulation_end = dataset.end
        try:
            await self.stream.connect()
            async for raw in self.stream.events():
                event = normalizer.normalize(raw)
                if event.event_id in seen:
                    duplicates += 1
                    self.log.write(
                        "replay_duplicate",
                        event_id=event.event_id,
                        symbol=event.symbol,
                        simulation_timestamp=event.event_timestamp.isoformat(),
                    )
                    continue
                if last_timestamp is not None and event.event_timestamp < last_timestamp:
                    out_of_order += 1
                    raise RuntimeError("historical replay event ordering violation")
                seen.add(event.event_id)
                last_timestamp = event.event_timestamp
                if event.event_type is MarketEventType.CANDLE_CLOSE:
                    decision_cycles += 1
                await self._on_market_event(event)
                processed += 1

            for position in self.supervisor.active_positions:
                price = self.last_prices.get(position.symbol)
                if price is None:
                    raise RuntimeError(f"no terminal mark for open position {position.symbol}")
                self.supervisor.runtime.exit_position(
                    symbol=position.symbol,
                    price=price,
                    now=simulation_end,
                )
                self.log.write(
                    "replay_end_position_close",
                    symbol=position.symbol,
                    price=price,
                    simulation_timestamp=simulation_end.isoformat(),
                    policy=replay_config.end_policy,
                )

            account = self.supervisor.replay_state()[3]
            report = {
                "code_sha": self._code_sha(),
                "dataset_hash": dataset.manifest.integrity_sha256,
                "simulation_start": dataset.start.isoformat(),
                "simulation_end": dataset.end.isoformat(),
                "wall_clock_end": datetime.now(timezone.utc).isoformat(),
                "acceleration_factor": replay_config.acceleration_factor,
                "processed_event_count": processed,
                "duplicate_event_count": duplicates,
                "out_of_order_count": out_of_order,
                "decision_cycles": decision_cycles,
                "orders": len(self.supervisor.runtime.orders.events),
                "fills": len([event for event in self.supervisor.runtime.orders.events if event.event_type == "ORDER_FILLED"]),
                "positions": len(self.supervisor.runtime.positions.events),
                "capital_state": {
                    "cash": account.wallet.cash,
                    "starting_equity": account.wallet.starting_equity,
                },
                "runtime_health": self.supervisor.health.healthy,
                "paper_only": self.supervisor.health.paper_only,
                "lookahead_verification": self._lookahead_verification(dataset),
                "end_of_run_policy": replay_config.end_policy,
            }
            self.log.write("replay_end", **report)
            return report
        finally:
            await self.stream.close()
            self.log.close()

    @staticmethod
    def _lookahead_verification(dataset: HistoricalDataset) -> bool:
        return all(event.timestamp <= dataset.end for event in dataset.events)

    @staticmethod
    def _code_sha() -> str:
        try:
            return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            return "unknown"


__all__ = ["HistoricalPaperReplayRunner", "ReplayConfig"]
