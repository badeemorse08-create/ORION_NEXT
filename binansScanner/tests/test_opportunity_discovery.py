from __future__ import annotations

import math
import unittest

from models.opportunity import MarketMetrics
from services.opportunity_discovery import (
    MarketEligibilityFilter,
    MarketUniverseDiscovery,
    OpportunityConfig,
    OpportunityDiscovery,
    OpportunityRanker,
    OpportunityScorer,
)


class FakeUniverseSource:
    def __init__(self, symbols):
        self._symbols = symbols

    def exchange_info(self):
        return {"symbols": list(self._symbols)}


class FakeMetricsSource:
    def __init__(self, metrics):
        self._metrics = metrics

    def metrics(self, symbol):
        return self._metrics[symbol]


class TestUniverseDiscovery(unittest.TestCase):
    def test_discovery_is_deterministic_and_filters_non_trading_symbols(self):
        source = FakeUniverseSource(
            [
                {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "USDCUSDT", "baseAsset": "USDC", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "BADUSDT", "baseAsset": "BAD", "quoteAsset": "USDT", "status": "BREAK"},
                {"symbol": "BTCEUR", "baseAsset": "BTC", "quoteAsset": "EUR", "status": "TRADING"},
            ]
        )
        result = MarketUniverseDiscovery(source).discover()
        self.assertEqual(tuple(candidate.symbol for candidate in result), ("BTCUSDT", "ETHUSDT"))

    def test_duplicate_exchange_rows_are_canonicalized(self):
        source = FakeUniverseSource(
            [
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
            ]
        )
        result = MarketUniverseDiscovery(source).discover()
        self.assertEqual(len(result), 1)


class TestEligibility(unittest.TestCase):
    def setUp(self):
        self.config = OpportunityConfig(
            min_quote_volume_24h=1_000_000,
            min_volatility=0.01,
            max_volatility=0.10,
            max_spread_bps=25,
        )
        self.filter = MarketEligibilityFilter(self.config)
        self.candidate = MarketUniverseDiscovery(
            FakeUniverseSource([
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"}
            ])
        ).discover()[0]

    def test_low_volume_is_rejected(self):
        result = self.filter.evaluate(
            self.candidate,
            MarketMetrics("BTCUSDT", 999_999, 0.03, 5),
        )
        self.assertFalse(result.eligible)
        self.assertIn("LOW_VOLUME", result.reasons)

    def test_wide_spread_is_rejected(self):
        result = self.filter.evaluate(
            self.candidate,
            MarketMetrics("BTCUSDT", 2_000_000, 0.03, 26),
        )
        self.assertFalse(result.eligible)
        self.assertIn("WIDE_SPREAD", result.reasons)

    def test_invalid_metrics_fail_closed(self):
        result = self.filter.evaluate(
            self.candidate,
            MarketMetrics("BTCUSDT", math.nan, 0.03, 5),
        )
        self.assertFalse(result.eligible)
        self.assertIn("INVALID_VOLUME", result.reasons)

    def test_spread_optional_when_unavailable(self):
        result = self.filter.evaluate(
            self.candidate,
            MarketMetrics("BTCUSDT", 2_000_000, 0.03, None),
        )
        self.assertTrue(result.eligible)


class TestScoringAndRanking(unittest.TestCase):
    def setUp(self):
        self.config = OpportunityConfig(
            min_quote_volume_24h=1_000_000,
            min_volatility=0.01,
            max_volatility=0.10,
            max_spread_bps=25,
            volume_reference_24h=100_000_000,
            target_volatility=0.03,
            default_top_n=2,
        )
        self.candidates = MarketUniverseDiscovery(
            FakeUniverseSource(
                [
                    {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING"},
                    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                    {"symbol": "SOLUSDT", "baseAsset": "SOL", "quoteAsset": "USDT", "status": "TRADING"},
                ]
            )
        ).discover()
        self.metrics = {
            "BTCUSDT": MarketMetrics("BTCUSDT", 100_000_000, 0.03, 5),
            "ETHUSDT": MarketMetrics("ETHUSDT", 10_000_000, 0.04, 10),
            "SOLUSDT": MarketMetrics("SOLUSDT", 50_000_000, 0.03, 5),
        }

    def test_score_is_bounded_and_deterministic(self):
        scorer = OpportunityScorer(self.config)
        value_a = scorer.score(self.metrics["BTCUSDT"])
        value_b = scorer.score(self.metrics["BTCUSDT"])
        self.assertEqual(value_a, value_b)
        self.assertGreaterEqual(value_a, 0)
        self.assertLessEqual(value_a, 100)

    def test_ties_break_by_symbol(self):
        ranker = OpportunityRanker(config=self.config)
        metrics = {
            "AAAUSDT": MarketMetrics("AAAUSDT", 50_000_000, 0.03, 5),
            "BBBUSD": MarketMetrics("BBBUSD", 50_000_000, 0.03, 5),
        }
        candidates = (
            type(self.candidates[0])(symbol="BBBUSD", base_asset="BBB", quote_asset="USD"),
            type(self.candidates[0])(symbol="AAAUSDT", base_asset="AAA", quote_asset="USDT"),
        )
        result = ranker.rank(candidates, metrics, top_n=2)
        self.assertEqual(result.symbols(), ("AAAUSDT", "BBBUSD"))
        self.assertEqual(tuple(candidate.rank for candidate in result.candidates), (1, 2))

    def test_top_n_is_enforced(self):
        ranker = OpportunityRanker(config=self.config)
        result = ranker.rank(self.candidates, self.metrics, top_n=2)
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.top_n, 2)


class TestOpportunityDiscoveryE2E(unittest.TestCase):
    def test_universe_filter_score_rank_top_n(self):
        universe = MarketUniverseDiscovery(
            FakeUniverseSource(
                [
                    {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING"},
                    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                    {"symbol": "LOWUSDT", "baseAsset": "LOW", "quoteAsset": "USDT", "status": "TRADING"},
                ]
            )
        )
        metrics = FakeMetricsSource(
            {
                "BTCUSDT": MarketMetrics("BTCUSDT", 200_000_000, 0.03, 3),
                "ETHUSDT": MarketMetrics("ETHUSDT", 20_000_000, 0.04, 8),
                "LOWUSDT": MarketMetrics("LOWUSDT", 10_000, 0.04, 8),
            }
        )
        config = OpportunityConfig(default_top_n=2)
        output = OpportunityDiscovery(universe, metrics, config).discover()
        self.assertEqual(output.symbols(), ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(tuple(candidate.rank for candidate in output.candidates), (1, 2))
        self.assertTrue(all(candidate.opportunity_score >= 0 for candidate in output.candidates))
        self.assertEqual(len(output.candidates), 2)


if __name__ == "__main__":
    unittest.main()
