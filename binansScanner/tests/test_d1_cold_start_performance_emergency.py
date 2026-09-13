from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

from models.opportunity import MarketMetrics
from providers.binance_opportunity_source import BinanceSpotOpportunitySource, DailyCandleHandoff
from services.opportunity_discovery import MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery
from services.scalping_pipeline import ScalpingOpportunityPipeline


SYMBOLS = tuple(f"S{index}USDT" for index in range(6))


def _history(rows: int = 32):
    return [[index, "1", "2", "0", str(100 + (index % 2) * 5), "10"] for index in range(rows)]


def _ticker(symbol: str, volume: float = 200_000_000.0):
    return {
        "symbol": symbol,
        "lastPrice": "100",
        "quoteVolume": str(volume),
        "priceChangePercent": "1",
        "weightedAvgPrice": "100",
    }


def _book(symbol: str, spread_bps: float = 1.0):
    half = spread_bps / 20_000.0
    return {
        "symbol": symbol,
        "bidPrice": str(100.0 * (1.0 - half)),
        "askPrice": str(100.0 * (1.0 + half)),
    }


class _TopologySource(BinanceSpotOpportunitySource):
    def __init__(self, *, delay: float = 0.01):
        super().__init__(ttl_seconds=0.0, timeout_seconds=1.0)
        self.delay = delay
        self.history_calls: list[str] = []
        self.active_metadata = 0
        self.max_metadata_active = 0
        self._lock = threading.Lock()

    def _get_json(self, path, params=None):
        with self._lock:
            self.active_metadata += 1
            self.max_metadata_active = max(self.max_metadata_active, self.active_metadata)
        try:
            time.sleep(self.delay)
            if path == "ticker/24hr":
                return [
                    _ticker(SYMBOLS[0]),
                    _ticker(SYMBOLS[1], volume=500_000.0),
                    _ticker(SYMBOLS[2]),
                    _ticker(SYMBOLS[3]),
                    _ticker(SYMBOLS[4]),
                    _ticker(SYMBOLS[5]),
                ]
            if path == "ticker/bookTicker":
                return [
                    _book(SYMBOLS[0]),
                    _book(SYMBOLS[1]),
                    _book(SYMBOLS[2], spread_bps=100.0),
                    _book(SYMBOLS[3]),
                    _book(SYMBOLS[4]),
                    _book(SYMBOLS[5]),
                ]
            raise AssertionError(path)
        finally:
            with self._lock:
                self.active_metadata -= 1

    def _fetch_history(self, symbol: str):
        self.history_calls.append(symbol)
        time.sleep(self.delay)
        return tuple(_history())


class D1ColdStartPerformanceEmergencyTests(unittest.TestCase):
    def test_metadata_requests_run_concurrently_with_finite_bound(self):
        source = _TopologySource(delay=0.05)
        source._fetch_history = lambda symbol: tuple(_history())
        started = time.monotonic()
        source.metrics_bulk((SYMBOLS[0], SYMBOLS[3]))
        elapsed = time.monotonic() - started
        self.assertEqual(source.max_metadata_active, 2)
        self.assertLess(elapsed, 0.08)

    def test_known_ineligible_symbols_do_not_trigger_history_requests(self):
        source = _TopologySource(delay=0.0)
        result = source.metrics_bulk(SYMBOLS)
        self.assertEqual(sorted(source.history_calls), sorted((SYMBOLS[0], SYMBOLS[3], SYMBOLS[4], SYMBOLS[5])))
        self.assertEqual(tuple(result), SYMBOLS)

    def test_prefilter_preserves_semantic_survivors(self):
        source = _TopologySource(delay=0.0)
        result = source.metrics_bulk(SYMBOLS)
        self.assertEqual(set(result), set(SYMBOLS))

        candidates = tuple(
            type("Candidate", (), {"symbol": symbol, "base_asset": symbol[:-4], "quote_asset": "USDT"})()
            for symbol in SYMBOLS
        )
        discovery = OpportunityDiscovery(
            MarketUniverseDiscovery(SimpleNamespace(exchange_info=lambda: {"symbols": []})),
            source,
            OpportunityConfig(),
        )
        discovery._universe = SimpleNamespace(discover=lambda: candidates)
        ranked = discovery.discover(top_n=6)
        self.assertEqual(tuple(item.symbol for item in ranked.candidates), (SYMBOLS[0], SYMBOLS[3], SYMBOLS[4], SYMBOLS[5]))

    def test_daily_handoff_removes_equivalent_deep_1d_request(self):
        source = _TopologySource(delay=0.0)
        source.metrics_bulk((SYMBOLS[0],))
        handoff = source.take_daily_candle_handoff()
        self.assertIsNotNone(handoff)
        assert handoff is not None
        candle_calls: list[str] = []

        class CandleSource:
            def candles(self, symbol: str, timeframe: str, limit: int):
                candle_calls.append(timeframe)
                return tuple()

        class Decision:
            def __init__(self):
                self.config = SimpleNamespace(min_candles=32, active_top_n=1, broad_pool_top_n=10)

        class Pool:
            config = SimpleNamespace(active_top_n=1, broad_pool_top_n=10)

        pipeline = ScalpingOpportunityPipeline(SimpleNamespace(_metrics_source=source), CandleSource(), decision_engine=Decision(), pool_manager=Pool())
        pipeline._candles_for(SYMBOLS[0], handoff)
        self.assertEqual(candle_calls, ["4h", "1h", "15m"])

    def test_request_topology_reduction_is_material_in_mocked_startup(self):
        eligible_candidates = 4
        before_candidate_requests = eligible_candidates * 4
        after_candidate_requests = eligible_candidates * 3
        before_total = eligible_candidates + before_candidate_requests
        after_total = eligible_candidates + after_candidate_requests
        self.assertEqual(before_candidate_requests, 16)
        self.assertEqual(after_candidate_requests, 12)
        self.assertEqual(before_total - after_total, 4)
        self.assertEqual((before_total - after_total) / before_total, 0.20)

    def test_existing_candidate_worker_bound_remains_four(self):
        self.assertEqual(ScalpingOpportunityPipeline.DEFAULT_CANDLE_FETCH_CONCURRENCY, 4)

    def test_handoff_is_run_scoped_not_global(self):
        first = _TopologySource(delay=0.0)
        second = _TopologySource(delay=0.0)
        first.metrics_bulk((SYMBOLS[0],))
        first_handoff = first.take_daily_candle_handoff()
        second_handoff = second.take_daily_candle_handoff()
        self.assertIsNotNone(first_handoff)
        self.assertIsNone(second_handoff)
        self.assertIsNot(first_handoff, second_handoff)


if __name__ == "__main__":
    unittest.main()
