from __future__ import annotations

import unittest

from models.capital_management import AllocationConfig, CapitalManager
from models.opportunity import MarketMetrics
from models.scalping_opportunity import Candle, EntryState, OpportunityClass
from services.scalping_opportunity import ScalpingCandidatePoolManager, ScalpingConfig
from services.scalping_pipeline import ScalpingOpportunityPipeline
from services.opportunity_discovery import OpportunityConfig, OpportunityDiscovery, MarketUniverseDiscovery


class FakeUniverse:
    def exchange_info(self):
        symbols = []
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "AVAXUSDT"):
            symbols.append({"symbol": symbol, "baseAsset": symbol[:-4], "quoteAsset": "USDT", "status": "TRADING"})
        return {"symbols": symbols}


class FakeMetrics:
    def metrics_bulk(self, symbols):
        return {symbol: MarketMetrics(symbol, 100_000_000, 0.02, 5, True, 100) for symbol in symbols}


class FakeCandles:
    def candles(self, symbol, timeframe, limit):
        price = 100.0
        rows = []
        for i in range(limit):
            if timeframe == "15m":
                step = 1.0 if i >= limit - 3 else 0.2
                volume = 300.0 if i >= limit - 2 else 100.0
            elif timeframe == "1h":
                step, volume = 0.5, 100.0
            else:
                step, volume = 0.0, 100.0
            close = price + step
            rows.append(Candle(i, price, max(price, close) + 0.5, min(price, close) - 0.5, close, volume))
            price = close
        return tuple(rows)


class TimeoutCandles(FakeCandles):
    def candles(self, symbol, timeframe, limit):
        raise TimeoutError("startup discovery deadline exceeded")


class OrdinaryFailureCandles(FakeCandles):
    def candles(self, symbol, timeframe, limit):
        raise RuntimeError("ordinary candle data failure")


class ScalpingPipelineTests(unittest.TestCase):
    def _pipeline(self, candles):
        config = ScalpingConfig(active_top_n=2, broad_pool_top_n=6)
        discovery = OpportunityDiscovery(
            MarketUniverseDiscovery(FakeUniverse()),
            FakeMetrics(),
            OpportunityConfig(default_top_n=8),
            clock=lambda: 0.0,
        )
        return ScalpingOpportunityPipeline(
            discovery,
            candles,
            pool_manager=ScalpingCandidatePoolManager(config),
        )

    def test_full_universe_to_broad_then_active_pipeline(self):
        pipeline = self._pipeline(FakeCandles())
        result = pipeline.discover()
        self.assertEqual(len(result.broad_pool.candidates), 6)
        self.assertEqual(len(result.active_set.candidates), 2)
        self.assertGreater(len(result.broad_pool.candidates), len(result.active_set.candidates))
        self.assertEqual(result.broad_pool.symbols(), tuple(sorted(result.broad_pool.symbols(), key=lambda s: (-result.broad_pool.candidates[result.broad_pool.symbols().index(s)].opportunity_score, s))))
        for item in result.broad_pool.candidates:
            self.assertIn(item.opportunity_class, {x.value for x in OpportunityClass})
            self.assertIn(item.entry_state, {x.value for x in EntryState})
            self.assertIsNotNone(item.decision_trace)

    def test_explicit_broad_override_is_never_active_top_n(self):
        config = ScalpingConfig(active_top_n=1, broad_pool_top_n=4)
        discovery = OpportunityDiscovery(
            MarketUniverseDiscovery(FakeUniverse()),
            FakeMetrics(),
            OpportunityConfig(default_top_n=8),
            clock=lambda: 0.0,
        )
        pipeline = ScalpingOpportunityPipeline(discovery, FakeCandles(), pool_manager=ScalpingCandidatePoolManager(config))
        result = pipeline.discover(top_n=4)
        self.assertEqual(len(result.broad_pool.candidates), 4)
        self.assertEqual(len(result.active_set.candidates), 1)

    def test_capital_boundary_is_read_only_during_discovery(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50.0, fixed_allocation=10.0))
        self.assertEqual(manager.reserved_capital, 0.0)

    def test_candle_timeout_propagates_out_of_pipeline(self):
        pipeline = self._pipeline(TimeoutCandles())
        with self.assertRaises(TimeoutError):
            pipeline.discover()

    def test_ordinary_candle_exception_preserves_fail_safe_behavior(self):
        pipeline = self._pipeline(OrdinaryFailureCandles())
        result = pipeline.discover()
        self.assertEqual(len(result.broad_pool.candidates), 6)
        self.assertEqual(len(result.active_set.candidates), 2)
        self.assertTrue(all(item.entry_state in {EntryState.C.value, EntryState.D.value} for item in result.broad_pool.candidates))
        self.assertTrue(all(getattr(item.decision_trace, "entry_allowed", False) is False for item in result.broad_pool.candidates))


if __name__ == "__main__":
    unittest.main()
