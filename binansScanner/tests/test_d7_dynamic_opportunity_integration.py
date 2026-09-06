from __future__ import annotations

import pathlib
import sys
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BINANS_SCANNER = _REPO_ROOT / "binansScanner"
for path in (_REPO_ROOT, _BINANS_SCANNER):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from models.opportunity import MarketMetrics
from services.opportunity_discovery import MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery
from tools.orion_paper_8h_runner import FixedUniverseSource


class FakeBinanceSource:
    def __init__(self):
        self.rows = [
            {"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True},
            {"symbol":"ETHUSDT","baseAsset":"ETH","quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True},
            {"symbol":"SOLUSDT","baseAsset":"SOL","quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True},
        ]
        self.bulk_calls = 0
    def exchange_info(self): return {"symbols": list(self.rows)}
    def metrics_bulk(self, symbols):
        self.bulk_calls += 1
        return {s: MarketMetrics(s, 100_000_000.0 - i*10_000_000, .03, 5.0, True, 100.0) for i,s in enumerate(symbols)}


class TestD7DynamicOpportunityIntegration(unittest.TestCase):
    def test_dynamic_universe_consumes_all_spot_symbols_before_top_n(self):
        source = FakeBinanceSource()
        cfg = OpportunityConfig(default_top_n=2, min_quote_volume_24h=1_000_000)
        discovery = OpportunityDiscovery(MarketUniverseDiscovery(source, cfg), source, cfg)
        result = discovery.discover(top_n=2)
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.symbols(), ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(source.bulk_calls, 1)

    def test_top_n_is_the_only_downstream_opportunity_set(self):
        source = FakeBinanceSource()
        cfg = OpportunityConfig(default_top_n=1, min_quote_volume_24h=1_000_000)
        result = OpportunityDiscovery(MarketUniverseDiscovery(source, cfg), source, cfg).discover(top_n=1)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.top_n, 1)

    def test_refresh_recomputes_after_d1_refresh_interval(self):
        source = FakeBinanceSource(); now = [0.0]
        cfg = OpportunityConfig(default_top_n=2, refresh_interval_seconds=30.0, min_quote_volume_24h=1_000_000)
        discovery = OpportunityDiscovery(MarketUniverseDiscovery(source, cfg), source, cfg, clock=lambda: now[0])
        first = discovery.discover(top_n=2)
        source.rows = [source.rows[1], source.rows[2], source.rows[0]]
        second = discovery.discover(top_n=2)
        self.assertEqual(first.symbols(), second.symbols())
        now[0] = 31.0
        third = discovery.discover(top_n=2)
        self.assertEqual(third.symbols(), ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(source.bulk_calls, 2)

    def test_fixed_symbols_require_explicit_filter_and_do_not_change_d1_contract(self):
        source = FakeBinanceSource()
        filtered = FixedUniverseSource(source, ("ETHUSDT",))
        rows = filtered.exchange_info()["symbols"]
        self.assertEqual([row["symbol"] for row in rows], ["ETHUSDT"])
        self.assertEqual(tuple(filtered.metrics_bulk(("BTCUSDT", "ETHUSDT"))), ("ETHUSDT",))

    def test_removed_candidate_does_not_imply_position_exit_policy(self):
        source = FakeBinanceSource(); cfg = OpportunityConfig(default_top_n=1, min_quote_volume_24h=1_000_000)
        result = OpportunityDiscovery(MarketUniverseDiscovery(source, cfg), source, cfg).discover(top_n=1)
        self.assertEqual(result.symbols(), ("BTCUSDT",))
        self.assertNotIn("SELL", [c.symbol for c in result.candidates])


if __name__ == "__main__": unittest.main()
