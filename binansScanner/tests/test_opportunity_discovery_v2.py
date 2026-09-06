from __future__ import annotations

import math
import unittest

from models.opportunity import MarketMetrics
from providers.binance_opportunity_source import BinanceSpotOpportunitySource
from services.opportunity_discovery import (
    MarketEligibilityFilter,
    MarketUniverseDiscovery,
    OpportunityConfig,
    OpportunityDiscovery,
    OpportunityRanker,
    OpportunityScorer,
)


class Universe:
    def __init__(self, rows): self.rows = rows; self.calls = 0
    def exchange_info(self): self.calls += 1; return {"symbols": self.rows}


class BulkMetrics:
    def __init__(self, data): self.data = data; self.bulk_calls = 0; self.single_calls = 0
    def metrics_bulk(self, symbols): self.bulk_calls += 1; return {s: self.data[s] for s in symbols if s in self.data}
    def metrics(self, symbol): self.single_calls += 1; return self.data[symbol]


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value


class D1V3Tests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True},
            {"symbol":"ETHUSDT","baseAsset":"ETH","quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True},
        ]
        self.config = OpportunityConfig(min_quote_volume_24h=1e6, min_volatility=0.001, max_volatility=0.20, target_volatility=0.03, default_top_n=2)
        self.candidates = MarketUniverseDiscovery(Universe(self.rows)).discover()

    @staticmethod
    def metrics(symbol, *, volume=100e6, volatility=.03, spread=5, volume_quality=.8, trend=.5, persistence=.5, trend_direction=0.0, momentum=.5, momentum_direction=0.0, structure=.8, change=0.0):
        return MarketMetrics(symbol, volume, volatility, spread, True, 100.0, volume_quality, trend, momentum, structure, change, 100.0, trend_direction, persistence, momentum_direction)

    def test_true_volatility_is_distinct_from_24h_change(self):
        scorer = OpportunityScorer(self.config)
        calm = self.metrics("BTCUSDT", volatility=.01, change=8.0)
        volatile = self.metrics("BTCUSDT", volatility=.08, change=8.0)
        self.assertNotEqual(scorer.score_components(calm)[2][1], scorer.score_components(volatile)[2][1])
        self.assertEqual(scorer.score_components(calm)[0][1], scorer.score_components(volatile)[0][1])

    def test_trend_direction_and_persistence(self):
        scorer = OpportunityScorer(self.config)
        bullish = self.metrics("BTCUSDT", trend=.9, persistence=.9, trend_direction=1.0)
        weak = self.metrics("BTCUSDT", trend=.4, persistence=.4, trend_direction=1.0)
        bearish = self.metrics("BTCUSDT", trend=.9, persistence=.9, trend_direction=-1.0)
        self.assertGreater(scorer.score_components(bullish)[3][1], scorer.score_components(weak)[3][1])
        self.assertGreater(scorer.directional_evidence(bullish), scorer.directional_evidence(bearish))

    def test_momentum_independence(self):
        scorer = OpportunityScorer(self.config)
        low = self.metrics("BTCUSDT", momentum=.2, momentum_direction=1.0, change=8.0, trend=.7)
        high = self.metrics("BTCUSDT", momentum=.9, momentum_direction=1.0, change=8.0, trend=.7)
        self.assertGreater(scorer.score(high), scorer.score(low))
        same_change = self.metrics("BTCUSDT", momentum=.9, momentum_direction=1.0, change=-8.0, trend=.7)
        self.assertEqual(scorer.score(high), scorer.score(same_change))

    def test_24h_change_cannot_drive_trend_or_momentum(self):
        scorer = OpportunityScorer(self.config)
        base = self.metrics("BTCUSDT", trend=.8, persistence=.8, momentum=.7, trend_direction=1.0, momentum_direction=1.0, change=1.0)
        changed = self.metrics("BTCUSDT", trend=.8, persistence=.8, momentum=.7, trend_direction=1.0, momentum_direction=1.0, change=20.0)
        self.assertEqual(scorer.score(base), scorer.score(changed))
        self.assertEqual(scorer.directional_evidence(base), scorer.directional_evidence(changed))

    def test_bullish_and_bearish_directional_evidence_are_distinct(self):
        scorer = OpportunityScorer(self.config)
        bullish = self.metrics("BTCUSDT", trend=.95, persistence=.95, trend_direction=1.0, momentum=.9, momentum_direction=1.0)
        bearish = self.metrics("BTCUSDT", trend=.95, persistence=.95, trend_direction=-1.0, momentum=.9, momentum_direction=-1.0)
        self.assertGreater(scorer.directional_evidence(bullish), 0.0)
        self.assertLess(scorer.directional_evidence(bearish), 0.0)

    def test_high_volatility_without_trend_is_not_top_opportunity(self):
        metrics = {
            "BTCUSDT": self.metrics("BTCUSDT", volume=100e6, volatility=.19, trend=.1, persistence=.1, momentum=.1, structure=.3, volume_quality=.9),
            "ETHUSDT": self.metrics("ETHUSDT", volume=5e6, volatility=.03, trend=.9, persistence=.9, momentum=.8, structure=.9, volume_quality=.8),
        }
        result = OpportunityRanker(config=self.config).rank(self.candidates, metrics)
        self.assertEqual(result.symbols()[0], "ETHUSDT")

    def test_high_volume_alone_does_not_guarantee_top_rank(self):
        metrics = {
            "BTCUSDT": self.metrics("BTCUSDT", volume=1e9, volatility=.08, trend=.2, persistence=.2, momentum=.1, structure=.2, volume_quality=1.0, spread=40),
            "ETHUSDT": self.metrics("ETHUSDT", volume=5e6, volatility=.03, trend=.9, persistence=.9, momentum=.8, structure=.9, volume_quality=.2, spread=3),
        }
        result = OpportunityRanker(config=self.config).rank(self.candidates, metrics)
        self.assertEqual(result.symbols()[0], "ETHUSDT")

    def test_poor_market_quality_can_reject(self):
        candidate = self.candidates[0]
        filt = MarketEligibilityFilter(self.config)
        result = filt.evaluate(candidate, self.metrics(candidate.symbol, volume=100e3, spread=100))
        self.assertFalse(result.eligible)
        self.assertIn("LOW_VOLUME", result.reasons)
        self.assertIn("WIDE_SPREAD", result.reasons)

    def test_equal_24h_change_different_trend_structure_scores_differently(self):
        scorer = OpportunityScorer(self.config)
        trending = self.metrics("BTCUSDT", change=8.0, trend=.95, persistence=.95, trend_direction=1.0, momentum=.8, structure=.9)
        flat = self.metrics("ETHUSDT", change=8.0, trend=.2, persistence=.2, trend_direction=0.0, momentum=.5, structure=.5)
        self.assertNotEqual(scorer.score(trending), scorer.score(flat))

    def test_duplicate_feature_protection(self):
        scorer = OpportunityScorer(self.config)
        base = self.metrics("BTCUSDT", change=15.0, trend=.6, momentum=.6, volatility=.03)
        changed_only_24h = self.metrics("BTCUSDT", change=-15.0, trend=.6, momentum=.6, volatility=.03)
        self.assertEqual(scorer.score(base), scorer.score(changed_only_24h))
        names = tuple(name for name, _ in scorer.score_components(base))
        self.assertEqual(names, ("volume_quality", "liquidity", "volatility_regime", "trend_quality", "momentum", "structure_quality"))

    def test_score_is_deterministic_and_bounded(self):
        scorer = OpportunityScorer(self.config)
        metric = self.metrics("BTCUSDT")
        self.assertEqual(scorer.score(metric), scorer.score(metric))
        self.assertGreaterEqual(scorer.score(metric), 0.0)
        self.assertLessEqual(scorer.score(metric), 100.0)

    def test_hysteresis_preserves_incumbent_on_small_crossing(self):
        cfg = OpportunityConfig(min_quote_volume_24h=1e6, min_volatility=.001, max_volatility=.20, target_volatility=.03, default_top_n=2, hysteresis_score_delta=5.0)
        ranker = OpportunityRanker(config=cfg)
        first = ranker.rank(self.candidates, {"BTCUSDT": self.metrics("BTCUSDT", trend=.80), "ETHUSDT": self.metrics("ETHUSDT", trend=.75)})
        second = ranker.rank(self.candidates, {"BTCUSDT": self.metrics("BTCUSDT", trend=.76), "ETHUSDT": self.metrics("ETHUSDT", trend=.80)})
        self.assertEqual(first.symbols(), second.symbols())

    def test_full_universe_to_top_n_contract(self):
        source = BulkMetrics({
            "BTCUSDT": self.metrics("BTCUSDT", trend=.9, persistence=.9, trend_direction=1.0, momentum=.8),
            "ETHUSDT": self.metrics("ETHUSDT", trend=.7, persistence=.7, trend_direction=1.0, momentum=.7),
        })
        output = OpportunityDiscovery(MarketUniverseDiscovery(Universe(self.rows)), source, self.config, clock=Clock()).discover(top_n=2)
        self.assertEqual(len(output.candidates), 2)
        self.assertEqual(tuple(candidate.rank for candidate in output.candidates), (1, 2))
        self.assertTrue(all(-1.0 <= candidate.directional_evidence <= 1.0 for candidate in output.candidates))


class SourceTests(unittest.TestCase):
    def test_true_market_volatility_and_feature_extraction(self):
        source = BinanceSpotOpportunitySource(clock=lambda: 0)
        prices = [100, 101, 103, 102, 104, 106, 105, 107, 109, 108, 110, 112, 111, 113, 115, 114, 116, 118, 117, 119, 121, 120, 122, 124, 123, 125, 127, 126, 128, 130, 129]
        history = [[0, 0, 0, 0, str(price)] for price in prices]
        volatility, trend_quality, trend_direction, persistence, momentum_quality, momentum_direction = source._history_features(history)
        self.assertGreater(volatility, 0.0)
        self.assertGreater(trend_quality, 0.0)
        self.assertGreater(trend_direction, 0.0)
        self.assertGreaterEqual(persistence, 0.0)
        self.assertGreaterEqual(momentum_quality, 0.0)
        self.assertGreater(momentum_direction, 0.0)

    def test_bulk_endpoint_contract_and_history(self):
        calls = []
        source = BinanceSpotOpportunitySource(ttl_seconds=30, clock=lambda: 0)
        payloads = {
            "exchangeInfo": {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "isSpotTradingAllowed": True}]},
            "ticker/24hr": [{"symbol": "BTCUSDT", "lastPrice": "130", "quoteVolume": "200000000", "priceChangePercent": "3", "weightedAvgPrice": "129"}],
            "ticker/bookTicker": [{"symbol": "BTCUSDT", "bidPrice": "129.9", "askPrice": "130.1"}],
            "klines": [[0, 0, 0, 0, str(100 + i), 0] for i in range(31)],
        }
        source._get_json = lambda path, params=None: (calls.append((path, params)) or payloads[path])
        result = source.metrics_bulk(["BTCUSDT"])
        self.assertIn("BTCUSDT", result)
        self.assertGreater(result["BTCUSDT"].volatility, 0.0)
        self.assertGreater(result["BTCUSDT"].trend_quality, 0.0)
        self.assertGreater(result["BTCUSDT"].trend_direction, 0.0)
        self.assertEqual(result["BTCUSDT"].price_change_pct_24h, 3.0)
        self.assertEqual([path for path, _ in calls], ["ticker/24hr", "ticker/bookTicker", "klines"])

    def test_malformed_rows_fail_closed(self):
        source = BinanceSpotOpportunitySource(clock=lambda: 0)
        source._get_json = lambda path, params=None: [] if path != "ticker/24hr" else [{"symbol": "BTCUSDT", "lastPrice": "bad", "quoteVolume": "1", "priceChangePercent": "1", "weightedAvgPrice": "1"}]
        self.assertEqual(source.metrics_bulk(["BTCUSDT"]), {})


if __name__ == "__main__": unittest.main()
