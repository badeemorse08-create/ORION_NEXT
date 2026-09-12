from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from models.market_event import MarketEvent, MarketEventType
from models.signal_snapshot import MaterialChangePolicy, SignalIdentity, build_next_snapshot
from replay.clock import ReplayClock
from replay.dataset import HistoricalDataset
from replay.runner import HistoricalPaperReplayRunner, ReplayConfig
from replay.source import HistoricalMarketDataSource
from replay.verification import ReplayVerifier

from tests.fixtures.historical_replay_fixture import START, SYMBOLS, build_fixture_dataset


class TestHistoricalPaperReplay(unittest.TestCase):
    def test_fixture_manifest_is_reproducible(self):
        left = build_fixture_dataset()
        right = build_fixture_dataset()
        self.assertEqual(left.manifest.integrity_sha256, right.manifest.integrity_sha256)
        self.assertEqual(tuple(event.event_id for event in left.events), tuple(event.event_id for event in right.events))

    def test_fixture_round_trip_preserves_manifest_and_data(self):
        dataset = build_fixture_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dataset"
            dataset.write_directory(root)
            loaded = HistoricalDataset.from_directory(root)
        self.assertEqual(dataset.manifest.integrity_sha256, loaded.manifest.integrity_sha256)
        self.assertEqual(tuple(event.event_id for event in dataset.events), tuple(event.event_id for event in loaded.events))
        self.assertEqual(dataset.candles, loaded.candles)

    def test_progressive_clock_blocks_future_metadata_and_candles(self):
        dataset = build_fixture_dataset()
        before = dataset.start - timedelta(seconds=2)
        clock = ReplayClock(before)
        source = HistoricalMarketDataSource(dataset, clock)
        self.assertEqual(source.exchange_info(), {"symbols": []})
        clock.advance_to(dataset.start)
        self.assertEqual(len(source.exchange_info()["symbols"]), len(SYMBOLS))
        future_row = (
            int((dataset.end + timedelta(days=1)).timestamp() * 1000),
            "1", "1", "1", "1", "1",
            int((dataset.end + timedelta(days=1, hours=1)).timestamp() * 1000),
        )
        augmented = dict(dataset.candles)
        augmented[(SYMBOLS[0], "1d")] = (*augmented[(SYMBOLS[0], "1d")], future_row)
        future_dataset = HistoricalDataset(dataset.manifest, dataset.events, dataset.metadata_snapshots, augmented)
        future_clock = ReplayClock(dataset.start)
        future_source = HistoricalMarketDataSource(future_dataset, future_clock)
        visible = future_source.dataset.candles_at(SYMBOLS[0], "1d", dataset.start)
        self.assertNotIn(future_row, visible)

    def test_stream_releases_events_in_simulation_order(self):
        dataset = build_fixture_dataset()
        clock = ReplayClock(dataset.start, acceleration_factor=1e9)
        stream = __import__("replay.stream", fromlist=["HistoricalMarketEventStream"]).HistoricalMarketEventStream(dataset, clock)

        async def collect():
            await stream.connect()
            timestamps = []
            async for raw in stream.events():
                timestamps.append(raw["E"])
            await stream.close()
            return timestamps

        timestamps = asyncio.run(collect())
        self.assertEqual(timestamps, sorted(timestamps))

    def test_same_timestamp_order_is_deterministic(self):
        dataset = build_fixture_dataset()
        ordered = tuple((event.timestamp, event.symbol, event.event_type.value, event.source_event_id) for event in dataset.events)
        self.assertEqual(ordered, tuple(sorted(ordered)))

    def test_event_identity_and_duplicate_protection_are_stable(self):
        event = build_fixture_dataset().events[0].to_market_event()
        duplicate = MarketEvent(
            symbol=event.symbol,
            event_timestamp=event.event_timestamp,
            event_type=event.event_type,
            payload=dict(event.payload),
            source_event_id=event.source_event_id,
        )
        self.assertEqual(event.event_id, duplicate.event_id)
        dataset = build_fixture_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            runner = HistoricalPaperReplayRunner.build(
                dataset,
                Path(tmp),
                replay_config=ReplayConfig(active_top_n=1, broad_pool_top_n=5, acceleration_factor=1e9),
            )
            self.assertEqual(runner.supervisor.process_market_event(event), ())
            self.assertEqual(runner.supervisor.process_market_event(duplicate), ())
            self.assertEqual(runner.supervisor.health.duplicate_events, 1)

    def test_historical_source_never_calls_live_transport(self):
        dataset = build_fixture_dataset()
        source = HistoricalMarketDataSource(dataset, ReplayClock(dataset.start))
        source.exchange_info()
        source._get_json("ticker/24hr")
        source._get_json("ticker/bookTicker")
        source._get_json("klines", {"symbol": SYMBOLS[0], "interval": "1d", "limit": 32})
        self.assertFalse(source.live_accessed)
        with self.assertRaises(RuntimeError):
            source._get_json("unknown")

    def test_real_paper_runtime_is_constructed_only_after_startup_discovery(self):
        dataset = build_fixture_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            runner = HistoricalPaperReplayRunner.build(
                dataset,
                Path(tmp),
                replay_config=ReplayConfig(active_top_n=1, broad_pool_top_n=5, acceleration_factor=1e9),
                starting_capital=200.0,
            )
            self.assertTrue(runner.opportunity.discovery._cached_output is not None)
            self.assertTrue(runner.supervisor.no_live_path())

    def test_replay_processes_progressively_and_produces_end_evidence(self):
        dataset = build_fixture_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            config = ReplayConfig(active_top_n=1, broad_pool_top_n=5, acceleration_factor=1e9)
            runner = HistoricalPaperReplayRunner.build(dataset, Path(tmp), replay_config=config, starting_capital=200.0)
            report = asyncio.run(runner.run_replay(dataset, replay_config=config))
            self.assertGreater(report["processed_event_count"], 0)
            self.assertEqual(report["out_of_order_count"], 0)