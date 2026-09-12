from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

DJTB = "DJTBUSDT"
SYMBOLS = tuple(f"S{i:03d}USDT" for i in range(475)) + (DJTB,)
ELIGIBLE = SYMBOLS[:100]
LOW_VOLUME = SYMBOLS[100:-1]


def history_payload(rows: int = 32):
    base_ts = 1_700_000_000_000
    payload = []
    for index in range(rows):
        close = 100 + index + (0.5 if index % 2 else -0.5)
        payload.append([
            base_ts + index * 86_400_000,
            str(close - 0.5),
            str(close + 0.5),
            str(close - 1.0),
            str(close),
            "10",
        ])
    return payload


def exchange_info_rows():
    return {"symbols": [{"symbol": symbol, "status": "TRADING", "baseAsset": symbol[:-4], "quoteAsset": "USDT", "isSpotTradingAllowed": True, "filters": []} for symbol in SYMBOLS]}


def ticker(symbol: str, volume: float | None = None, change: float | None = None):
    index = int(symbol[1:4]) if symbol.startswith("S") else 999
    if volume is None:
        volume = 400_000_000.0 - (index * 1_500_000.0 if index < 100 else 0.0)
    if change is None:
        change = 1.0 if index < 50 else 25.0
    return {"symbol": symbol, "lastPrice": "100", "quoteVolume": str(volume), "priceChangePercent": str(change), "weightedAvgPrice": "100"}


def book(symbol: str): return {"symbol": symbol, "bidPrice": "99.99", "askPrice": "100.01"}


class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def read(self): return json.dumps(self.payload).encode("utf-8")


class ShortHistoryNetwork:
    def __init__(self, persistent_short: bool = False):
        self.persistent_short = persistent_short; self.history_calls: list[str] = []; self.targeted_calls: list[str] = []; self.lifecycle_calls = 0
    def __call__(self, request, timeout):
        url = request.full_url
        if "exchangeInfo" in url: return Response(exchange_info_rows())
        if "ticker/24hr" in url:
            if "symbol=" in url: self.targeted_calls.append(url); return Response(ticker(DJTB, volume=200_000_000.0, change=20.0))
            return Response([ticker(symbol) for symbol in ELIGIBLE] + [ticker(symbol, 500_000.0, 0.0) for symbol in LOW_VOLUME])
        if "ticker/bookTicker" in url:
            if "symbol=" in url: self.targeted_calls.append(url); return Response(book(DJTB))
            return Response([book(symbol) for symbol in SYMBOLS[:-1]])
        if "/klines?" in url:
            symbol = url.split("symbol=", 1)[1].split("&", 1)[0]; self.history_calls.append(symbol)
            if symbol == DJTB:
                if self.persistent_short or self.history_calls.count(DJTB) == 1: return Response(history_payload(3))
                return Response(history_payload(32))
            return Response(history_payload(32))
        raise AssertionError(f"first_divergence=unexpected endpoint {url}")


class D2HistoryRecoveryTests(unittest.TestCase):
    def test_provider_short_history_retries_without_caching_invalid_payload(self):
        from providers.binance_opportunity_source import BinanceSpotOpportunitySource
        network = ShortHistoryNetwork()
        with patch("providers.binance_opportunity_source.urlopen", side_effect=network):
            source = BinanceSpotOpportunitySource(ttl_seconds=300.0); history = source._fetch_history(DJTB)
        self.assertEqual(len(history), 32); self.assertGreaterEqual(len(history), source.MIN_HISTORY_CANDLES); self.assertEqual(network.history_calls, [DJTB, DJTB]); self.assertEqual(source._cache[f"klines_{DJTB}"].value, history)
        recovery = [event for event in source.reconciliation_events if event["symbol"] == DJTB]
        self.assertEqual([event["history_outcome"] for event in recovery], ["retrying", "recovered"])

    def test_provider_persistent_short_history_exhausts_bounded_recovery(self):
        from providers.binance_opportunity_source import BinanceSpotOpportunitySource
        network = ShortHistoryNetwork(persistent_short=True)
        with patch("providers.binance_opportunity_source.urlopen", side_effect=network):
            source = BinanceSpotOpportunitySource(ttl_seconds=300.0)
            with self.assertRaisesRegex(ValueError, "insufficient price history"): source._fetch_history(DJTB)
        self.assertEqual(network.history_calls, [DJTB, DJTB]); self.assertNotIn(f"klines_{DJTB}", source._cache)
        recovery = [event for event in source.reconciliation_events if event["symbol"] == DJTB]
        self.assertEqual(recovery[-1]["history_outcome"], "exhausted"); self.assertEqual(recovery[-1]["final_disposition"], "unresolved")

    def test_provider_empty_history_is_recoverable_but_bounded(self):
        from providers.binance_opportunity_source import BinanceSpotOpportunitySource
        class EmptyThenValid(ShortHistoryNetwork):
            def __call__(self, request, timeout):
                url = request.full_url
                if "/klines?" in url:
                    self.history_calls.append(DJTB)
                    if len(self.history_calls) == 1: return Response([])
                    return Response(history_payload(32))
                return super().__call__(request, timeout)
        network = EmptyThenValid()
        with patch("providers.binance_opportunity_source.urlopen", side_effect=network):
            source = BinanceSpotOpportunitySource(ttl_seconds=300.0); history = source._fetch_history(DJTB)
        self.assertEqual(len(history), 32); self.assertGreaterEqual(len(history), source.MIN_HISTORY_CANDLES); self.assertEqual(network.history_calls, [DJTB, DJTB])

    def test_provider_malformed_history_fails_without_recovery_loop(self):
        from providers.binance_opportunity_source import BinanceSpotOpportunitySource
        class Malformed(ShortHistoryNetwork):
            def __call__(self, request, timeout):
                url = request.full_url
                if "/klines?" in url:
                    self.history_calls.append(DJTB); payload = history_payload(22); payload[0][4] = "not-a-number"; return Response(payload)
                return super().__call__(request, timeout)
        network = Malformed()
        with patch("providers.binance_opportunity_source.urlopen", side_effect=network):
            source = BinanceSpotOpportunitySource(ttl_seconds=300.0)
            with self.assertRaisesRegex(ValueError, "invalid price history candle"): source._fetch_history(DJTB)
        self.assertEqual(network.history_calls, [DJTB]); self.assertNotIn(f"klines_{DJTB}", source._cache)

    def test_real_paper8h_create_recovers_short_history_before_metric_construction(self):
        import tools.orion_paper_8h_runner as runner_module
        from services.opportunity_discovery import OpportunityDiscovery
        network = ShortHistoryNetwork(); lifecycle = Mock(); original_builder = OpportunityDiscovery._build_history_metric; built_lengths: list[int] = []
        def traced_builder(self, source, symbol, ticker_value, book_value, history):
            built_lengths.append(len(history)); return original_builder(self, source, symbol, ticker_value, book_value, history)
        with tempfile.TemporaryDirectory() as tmp:
            config = runner_module.Paper8HConfig(duration_hours=0.01, starting_capital=50.0, dynamic_universe=True, top_n=1, output_dir=Path(tmp) / "run")
            with patch("providers.binance_opportunity_source.urlopen", side_effect=network), patch("tools.orion_paper_8h_runner.urllib.request.urlopen", side_effect=network), patch.object(runner_module, "DynamicMarketStream", return_value=Mock()), patch.object(runner_module, "PaperRealtimeLifecycle", return_value=lifecycle) as lifecycle_factory, patch.object(OpportunityDiscovery, "_build_history_metric", new=traced_builder):
                runner = runner_module.Paper8HRunner.create(config)
        discovery = runner.opportunity.discovery
        self.assertEqual(discovery.last_bootstrap.missing_symbols, ()); self.assertEqual(len(discovery.last_bootstrap.expected_symbols), 476); self.assertEqual(len(discovery.last_bootstrap.received_symbols), 476); self.assertIn(32, built_lengths); self.assertNotIn(3, built_lengths); self.assertEqual(network.history_calls.count(DJTB), 5); lifecycle_factory.assert_called_once()

    def test_real_paper8h_create_blocks_runtime_after_persistent_short_history(self):
        import tools.orion_paper_8h_runner as runner_module
        network = ShortHistoryNetwork(persistent_short=True); lifecycle_factory = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            config = runner_module.Paper8HConfig(duration_hours=0.01, starting_capital=50.0, dynamic_universe=True, top_n=1, output_dir=Path(tmp) / "run")
            with patch("providers.binance_opportunity_source.urlopen", side_effect=network), patch("tools.orion_paper_8h_runner.urllib.request.urlopen", side_effect=network), patch.object(runner_module, "DynamicMarketStream", return_value=Mock()), patch.object(runner_module, "PaperRealtimeLifecycle", lifecycle_factory):
                with self.assertRaisesRegex(RuntimeError, "missing=DJTBUSDT"): runner_module.Paper8HRunner.create(config)
        self.assertEqual(network.history_calls.count(DJTB), 4); lifecycle_factory.assert_not_called()


if __name__ == "__main__": unittest.main()