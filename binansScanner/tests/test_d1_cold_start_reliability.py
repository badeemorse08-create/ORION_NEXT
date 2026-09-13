from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from models.opportunity import MarketMetrics
from services.opportunity_discovery import MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery
from providers.binance_opportunity_source import BinanceSpotOpportunitySource


SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")


def _history_payload(rows: int = 32, quality: float = 1.0):
    base_ts = 1_700_000_000_000
    payload = []
    for index in range(rows):
        close = 100.0 + index * quality
        payload.append(
            [
                base_ts + index * 86_400_000,
                f"{close - 0.5:.8f}",
                f"{close + 0.5:.8f}",
                f"{close - 1.0:.8f}",
                f"{close:.8f}",
                "10",
            ]
        )
    return payload


def _metric(symbol: str, quality: float) -> MarketMetrics:
    return MarketMetrics(
        symbol=symbol,
        quote_volume_24h=200_000_000.0,
        volatility=0.03,
        spread_bps=1.0,
        tradable=True,
        last_price=100.0,
        volume_quality=quality,
        trend_quality=quality,
        momentum_quality=quality,
        structure_quality=quality,
        trend_persistence=quality,
        trend_direction=quality,
        momentum_direction=quality,
    )


class _Universe:
    def __init__(self, symbols: tuple[str, ...]):
        self._symbols = symbols

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "baseAsset": symbol[:-4],
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                }
                for symbol in self._symbols
            ]
        }


class _StartupSource:
    def __init__(self, metrics: dict[str, MarketMetrics]):
        self._startup_deadline = float("inf")
        self.metrics = metrics

    def exchange_info(self):
        return _Universe(tuple(self.metrics)).exchange_info()

    def metrics_bulk(self, symbols):
        return {symbol: self.metrics[symbol] for symbol in symbols if symbol in self.metrics}


class _ChangingBinanceSource(BinanceSpotOpportunitySource):
    def __init__(self, quality: float):
        super().__init__(ttl_seconds=300.0, timeout_seconds=1.0)
        self.quality = quality
        self.calls = 0

    def _get_json(self, path, params=None):
        if path == "ticker/24hr":
            return [
                {
                    "symbol": symbol,
                    "lastPrice": "100",
                    "quoteVolume": "200000000",
                    "priceChangePercent": "1",
                    "weightedAvgPrice": "100",
                }
                for symbol in SYMBOLS
            ]
        if path == "ticker/bookTicker":
            return [{"symbol": symbol, "bidPrice": "99.99", "askPrice": "100.01"} for symbol in SYMBOLS]
        if path == "klines":
            self.calls += 1
            return _history_payload(32, self.quality)
        raise AssertionError(path)


class _InstrumentedHistorySource(BinanceSpotOpportunitySource):
    def __init__(self, delay: float = 0.02):
        super().__init__(ttl_seconds=0.0, timeout_seconds=1.0)
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.started_at = 0.0
        self.finished_at = 0.0

    def _get_json(self, path, params=None):
        if path == "ticker/24hr":
            return [
                {
                    "symbol": symbol,
                    "lastPrice": "100",
                    "quoteVolume": "200000000",
                    "priceChangePercent": "1",
                    "weightedAvgPrice": "100",
                }
                for symbol in SYMBOLS
            ]
        if path == "ticker/bookTicker":
            return [{"symbol": symbol, "bidPrice": "99.99", "askPrice": "100.01"} for symbol in SYMBOLS]
        raise AssertionError(path)

    def _fetch_history(self, symbol):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.calls == 1:
                self.started_at = time.monotonic()
        try:
            time.sleep(self.delay)
            return tuple(_history_payload())
        finally:
            with self.lock:
                self.active -= 1
                if self.active == 0:
                    self.finished_at = time.monotonic()


class D1ColdStartReliabilityTests(unittest.TestCase):
    def test_incomplete_fresh_bootstrap_fails_closed_before_runtime(self):
        metrics = {SYMBOLS[0]: _metric(SYMBOLS[0], 0.9)}
        source = _StartupSource(metrics)
        discovery = OpportunityDiscovery(
            MarketUniverseDiscovery(_Universe(SYMBOLS)),
            source,
            OpportunityConfig(refresh_interval_seconds=30.0),
        )
        with self.assertRaisesRegex(RuntimeError, "fresh discovery bootstrap incomplete"):
            discovery.discover(top_n=1)

    def test_timeout_error_from_fresh_bootstrap_is_not_downgraded(self):
        class TimeoutSource(_StartupSource):
            def metrics_bulk(self, symbols):
                raise TimeoutError("paper startup discovery deadline exceeded")

        source = TimeoutSource({symbol: _metric(symbol, 0.9) for symbol in SYMBOLS})
        discovery = OpportunityDiscovery(MarketUniverseDiscovery(_Universe(SYMBOLS)), source)
        with self.assertRaises(TimeoutError):
            discovery.discover(top_n=2)

    def test_no_cross_run_historical_cache(self):
        first_run = _ChangingBinanceSource(quality=1.0)
        second_run = _ChangingBinanceSource(quality=0.1)
        first = first_run.metrics_bulk(SYMBOLS)
        second = second_run.metrics_bulk(SYMBOLS)
        self.assertEqual(first_run.calls, len(SYMBOLS))
        self.assertEqual(second_run.calls, len(SYMBOLS))
        self.assertNotEqual(first["AAAUSDT"].volatility, second["AAAUSDT"].volatility)
        self.assertIsNot(first_run._cache, second_run._cache)

    def test_fresh_source_has_no_historical_data_from_previous_instance(self):
        prior = _ChangingBinanceSource(quality=1.0)
        prior.metrics_bulk(("AAAUSDT",))
        fresh = _ChangingBinanceSource(quality=0.5)
        fresh.metrics_bulk(("AAAUSDT",))
        self.assertEqual(fresh.calls, 1)

    def test_bounded_concurrency_and_mocked_cold_start_latency_are_observable(self):
        source = _InstrumentedHistorySource(delay=0.02)
        started = time.monotonic()
        source.metrics_bulk(SYMBOLS)
        elapsed = time.monotonic() - started
        self.assertEqual(source.calls, len(SYMBOLS))
        self.assertGreaterEqual(source.max_active, 2)
        self.assertLessEqual(source.max_active, source.DISCOVERY_CONCURRENCY)
        self.assertGreater(source.finished_at - source.started_at, 0.0)
        self.assertLess(elapsed, len(SYMBOLS) * source.delay * 0.9)

    def test_canonical_symbol_order_is_stable_after_parallel_acquisition(self):
        source = _InstrumentedHistorySource(delay=0.01)
        result = source.metrics_bulk(tuple(reversed(SYMBOLS)))
        self.assertEqual(tuple(result), tuple(sorted(SYMBOLS)))


if __name__ == "__main__":
    unittest.main()
