from __future__ import annotations

import unittest
from types import SimpleNamespace

from models.opportunity import MarketMetrics
from providers.binance_opportunity_source import BinanceSpotOpportunitySource
from services.opportunity_discovery import MarketEligibilityFilter, MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery


SYMBOLS = ("LOWUSDT", "WIDEUSDT", "GOODUSDT", "MISSINGUSDT", "BADBOOKUSDT", "NANVOLUSDT")


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


class _BootstrapSource(BinanceSpotOpportunitySource):
    def __init__(self):
        super().__init__(ttl_seconds=0.0, timeout_seconds=1.0)
        self.history_calls: list[str] = []

    def _get_json(self, path, params=None):
        if path == "ticker/24hr":
            return [
                _ticker("LOWUSDT", 500_000.0),
                _ticker("WIDEUSDT"),
                _ticker("GOODUSDT"),
                _ticker("BADBOOKUSDT"),
                _ticker("NANVOLUSDT", float("nan")),
            ]
        if path == "ticker/bookTicker":
            return [
                _book("LOWUSDT"),
                _book("WIDEUSDT", 100.0),
                _book("GOODUSDT"),
                {"symbol": "BADBOOKUSDT", "bidPrice": "0", "askPrice": "100"},
                _book("NANVOLUSDT"),
            ]
        raise AssertionError(path)

    def _fetch_history(self, symbol: str):
        self.history_calls.append(symbol)
        return tuple(_history())


class D1BootstrapReductionTests(unittest.TestCase):
    def test_volume_and_spread_confirmed_rejections_skip_history(self):
        source = _BootstrapSource()
        result = source.metrics_bulk(SYMBOLS)
        self.assertNotIn("LOWUSDT", source.history_calls)
        self.assertNotIn("WIDEUSDT", source.history_calls)
        self.assertIn("GOODUSDT", source.history_calls)
        self.assertIn("BADBOOKUSDT", source.history_calls)
        self.assertIn("NANVOLUSDT", source.history_calls)
        self.assertIn("MISSINGUSDT", source.history_calls)
        self.assertEqual(set(result), set(SYMBOLS) - {"MISSINGUSDT"})

    def test_missing_or_ambiguous_metadata_is_fail_open_to_history(self):
        source = _BootstrapSource()
        source._get_json = lambda path, params=None: (
            [_ticker("GOODUSDT")] if path == "ticker/24hr" else [{"symbol": "BADBOOKUSDT", "bidPrice": "0", "askPrice": "100"}]
        )
        source.metrics_bulk(("GOODUSDT", "BADBOOKUSDT"))
        self.assertEqual(sorted(source.history_calls), ["BADBOOKUSDT", "GOODUSDT"])

    def test_non_finite_volume_is_not_treated_as_confirmed_rejection(self):
        source = _BootstrapSource()
        source.metrics_bulk(("NANVOLUSDT",))
        self.assertEqual(source.history_calls, ["NANVOLUSDT"])

    def test_metadata_only_rejection_remains_represented(self):
        source = _BootstrapSource()
        result = source.metrics_bulk(("LOWUSDT", "WIDEUSDT"))
        self.assertEqual(tuple(result), ("LOWUSDT", "WIDEUSDT"))
        self.assertIsInstance(result["LOWUSDT"], MarketMetrics)
        self.assertIsInstance(result["WIDEUSDT"], MarketMetrics)

    def test_prefilter_survivor_set_matches_existing_d1_eligibility(self):
        source = _BootstrapSource()
        candidates = tuple(
            type("Candidate", (), {"symbol": symbol, "base_asset": symbol[:-4], "quote_asset": "USDT"})()
            for symbol in ("LOWUSDT", "WIDEUSDT", "GOODUSDT")
        )
        discovery = OpportunityDiscovery(
            MarketUniverseDiscovery(SimpleNamespace(exchange_info=lambda: {"symbols": []})),
            source,
            OpportunityConfig(),
        )
        discovery._universe = SimpleNamespace(discover=lambda: candidates)
        ranked = discovery.discover(top_n=3)
        actual = tuple(item.symbol for item in ranked.candidates)

        full_metrics = source.metrics_bulk(tuple(candidate.symbol for candidate in candidates))
        eligibility = MarketEligibilityFilter(OpportunityConfig())
        expected_rejected = {
            symbol
            for symbol, metric in full_metrics.items()
            if not eligibility.evaluate(next(candidate for candidate in candidates if candidate.symbol == symbol), metric).eligible
        }
        self.assertEqual(expected_rejected, {"LOWUSDT", "WIDEUSDT"})
        self.assertEqual(set(actual), {"GOODUSDT"})

    def test_history_request_reduction_is_exact_for_mocked_universe(self):
        source = _BootstrapSource()
        source.metrics_bulk(SYMBOLS)
        self.assertEqual(len(SYMBOLS), 6)
        self.assertEqual(len(source.history_calls), 4)
        self.assertEqual(sorted(source.history_calls), ["BADBOOKUSDT", "GOODUSDT", "MISSINGUSDT", "NANVOLUSDT"])

    def test_existing_constants_are_unchanged(self):
        self.assertEqual(BinanceSpotOpportunitySource.HISTORY_LIMIT, 32)
        self.assertEqual(BinanceSpotOpportunitySource.METRICS_HISTORY_WINDOW, 31)
        self.assertEqual(BinanceSpotOpportunitySource.MIN_HISTORY_CANDLES, 22)
        self.assertEqual(BinanceSpotOpportunitySource.DISCOVERY_CONCURRENCY, 4)


if __name__ == "__main__":
    unittest.main()
