from __future__ import annotations

import json
import socket
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from models.opportunity import MarketMetrics
from providers.binance_opportunity_source import BinanceSpotOpportunitySource
from services.opportunity_discovery import MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery


SYMBOL = "AAAUSDT"


def _history_payload(rows: int = 32):
    base_ts = 1_700_000_000_000
    return [[base_ts + index * 86_400_000,
             str(100 + index - 0.5),
             str(100 + index + 0.5),
             str(100 + index - 1.0),
             str(100 + index),
             "10"] for index in range(rows)]


def _metric(symbol: str = SYMBOL) -> MarketMetrics:
    return MarketMetrics(symbol=symbol, quote_volume_24h=200_000_000.0, volatility=0.03, spread_bps=1.0, tradable=True, last_price=100.0, volume_quality=0.9, trend_quality=0.9, momentum_quality=0.9, structure_quality=0.9, trend_persistence=0.9, trend_direction=0.9, momentum_direction=0.9)


class _Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def read(self): return json.dumps(self.payload).encode("utf-8")


class _Universe:
    def exchange_info(self):
        return {"symbols": [{"symbol": SYMBOL, "baseAsset": "AAA", "quoteAsset": "USDT", "status": "TRADING"}]}


class _StartupSource:
    def __init__(self, *, metrics=None, failure=None):
        self._startup_deadline = float("inf"); self.metrics_data = metrics if metrics is not None else {SYMBOL: _metric()}; self.failure = failure
    def metrics_bulk(self, symbols):
        if self.failure is not None: raise self.failure
        return dict(self.metrics_data)


class ResilientBootstrapTests(unittest.TestCase):
    def test_read_timeout_is_retried_and_request_event_is_recorded(self):
        source = BinanceSpotOpportunitySource(timeout_seconds=10.0); responses = [socket.timeout("read timed out"), _Response({"ok": True})]; sleeps = []
        def fake_urlopen(request, timeout):
            self.assertEqual(timeout, 10.0); value = responses.pop(0)
            if isinstance(value, BaseException): raise value
            return value
        with patch("providers.binance_opportunity_source.urlopen", side_effect=fake_urlopen), patch("providers.binance_opportunity_source.time.sleep", side_effect=sleeps.append):
            result = source._get_json("ticker/24hr")
        self.assertEqual(result, {"ok": True}); self.assertEqual(sleeps, [0.5]); self.assertEqual(len(source.request_events), 2)
        self.assertEqual(source.request_events[0]["request_id"], source.request_events[1]["request_id"])
        for key in ("endpoint","stage","symbol","attempt","timeout_requested","timeout_effective","start_timestamp","end_timestamp","exception_type","elapsed_seconds"): self.assertIn(key, source.request_events[0])
        self.assertEqual(source.request_events[0]["attempt"], 1); self.assertEqual(source.request_events[0]["exception_type"], "TimeoutError"); self.assertEqual(source.request_events[0]["failure_category"], "read_or_transport_timeout"); self.assertEqual(source.request_events[0]["outcome"], "retrying"); self.assertEqual(source.request_events[1]["outcome"], "success"); self.assertEqual(source.request_events[0]["stage"], "metadata")

    def test_connection_failure_is_retried(self):
        source = BinanceSpotOpportunitySource(timeout_seconds=10.0)
        with patch("providers.binance_opportunity_source.urlopen", side_effect=[ConnectionResetError("reset"), ConnectionRefusedError("refused"), _Response({"ok": True})]), patch("providers.binance_opportunity_source.time.sleep") as sleep:
            self.assertEqual(source._get_json("exchangeInfo"), {"ok": True})
        self.assertEqual(sleep.call_count, 2); self.assertEqual([event["failure_category"] for event in source.request_events[:2]], ["transport_error", "transport_error"])

    def test_http_429_uses_server_retry_after(self):
        source = BinanceSpotOpportunitySource(timeout_seconds=10.0); error = HTTPError("https://api.binance.com/api/v3/klines", 429, "rate", {"Retry-After": "3"}, None)
        with patch("providers.binance_opportunity_source.urlopen", side_effect=[error, _Response({"ok": True})]), patch("providers.binance_opportunity_source.time.sleep") as sleep:
            self.assertEqual(source._get_json("klines", {"symbol": SYMBOL}), {"ok": True})
        sleep.assert_called_once_with(3.0); self.assertEqual(source.request_events[0]["failure_category"], "http_429")

    def test_http_5xx_is_retried(self):
        source = BinanceSpotOpportunitySource(timeout_seconds=10.0); error = HTTPError("https://api.binance.com/api/v3/klines", 503, "busy", {}, None)
        with patch("providers.binance_opportunity_source.urlopen", side_effect=[error, _Response({"ok": True})]), patch("providers.binance_opportunity_source.time.sleep") as sleep:
            self.assertEqual(source._get_json("klines", {"symbol": SYMBOL}), {"ok": True})
        sleep.assert_called_once_with(0.5); self.assertEqual(source.request_events[0]["failure_category"], "http_503")

    def test_permanent_4xx_is_not_retried(self):
        source = BinanceSpotOpportunitySource(timeout_seconds=10.0); error = HTTPError("https://api.binance.com/api/v3/klines", 400, "bad", {}, None)
        with patch("providers.binance_opportunity_source.urlopen", side_effect=error) as open_mock, patch("providers.binance_opportunity_source.time.sleep") as sleep:
            with self.assertRaises(HTTPError): source._get_json("klines", {"symbol": SYMBOL})
        self.assertEqual(open_mock.call_count, 1); sleep.assert_not_called(); self.assertEqual(source.request_events[0]["outcome"], "failed")

    def test_malformed_payload_is_not_retried(self):
        source = BinanceSpotOpportunitySource(timeout_seconds=10.0)
        with patch("providers.binance_opportunity_source.urlopen", return_value=_Response(object())) as open_mock:
            with patch("providers.binance_opportunity_source.json.load", side_effect=ValueError("bad json")):
                with self.assertRaises(ValueError): source._get_json("ticker/24hr")
        self.assertEqual(open_mock.call_count, 1); self.assertEqual(source.request_events[0]["outcome"], "failed"); self.assertEqual(source.request_events[0]["failure_category"], "non_retryable")

    def test_retry_count_and_backoff_are_bounded(self):
        source = BinanceSpotOpportunitySource(timeout_seconds=10.0)
        with patch("providers.binance_opportunity_source.urlopen", side_effect=socket.timeout("read timeout")) as open_mock, patch("providers.binance_opportunity_source.time.sleep") as sleep:
            with self.assertRaises(TimeoutError): source._get_json("ticker/24hr")
        self.assertEqual(open_mock.call_count, source.RETRY_MAX_ATTEMPTS); self.assertEqual(sleep.call_count, source.RETRY_MAX_ATTEMPTS - 1); self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5,1.0,2.0]); self.assertLessEqual(max(call.args[0] for call in sleep.call_args_list), source.RETRY_MAX_BACKOFF_SECONDS)

    def test_retry_policy_is_explicit_and_finite(self):
        self.assertEqual(BinanceSpotOpportunitySource.RETRY_MAX_ATTEMPTS, 4); self.assertEqual(BinanceSpotOpportunitySource.RETRY_INITIAL_BACKOFF_SECONDS, 0.5); self.assertEqual(BinanceSpotOpportunitySource.RETRY_BACKOFF_MULTIPLIER, 2.0); self.assertEqual(BinanceSpotOpportunitySource.RETRY_MAX_BACKOFF_SECONDS, 2.0); self.assertEqual(BinanceSpotOpportunitySource.RETRY_JITTER_SECONDS, 0.0); self.assertEqual(BinanceSpotOpportunitySource.RETRY_SERVER_BACKOFF_MAX_SECONDS, 10.0)

    def test_metadata_concurrency_is_two_in_the_real_source(self):
        source = BinanceSpotOpportunitySource(ttl_seconds=0.0, timeout_seconds=1.0); active = 0; max_active = 0; lock = threading.Lock(); barrier = threading.Barrier(2)
        def fake_urlopen(request, timeout):
            nonlocal active, max_active
            with lock: active += 1; max_active = max(max_active, active)
            try:
                barrier.wait(timeout=1.0)
                if request.full_url.endswith("ticker/24hr"): return _Response([{"symbol": SYMBOL, "lastPrice": "100", "quoteVolume": "500000", "priceChangePercent": "1", "weightedAvgPrice": "100"}])
                if request.full_url.endswith("ticker/bookTicker"): return _Response([{"symbol": SYMBOL, "bidPrice": "99.99", "askPrice": "100.01"}])
                raise AssertionError(request.full_url)
            finally:
                with lock: active -= 1
        with patch("providers.binance_opportunity_source.urlopen", side_effect=fake_urlopen): result = source.metrics_bulk((SYMBOL,))
        self.assertIn(SYMBOL, result); self.assertEqual(max_active, 2); self.assertEqual(source.METADATA_CONCURRENCY, 2); self.assertEqual(source.DISCOVERY_CONCURRENCY, 4)

    def test_transient_history_timeout_then_success_allows_bootstrap(self):
        class Source(BinanceSpotOpportunitySource):
            def _get_json(self, path, params=None):
                if path == "ticker/24hr": return [{"symbol": SYMBOL, "lastPrice": "100", "quoteVolume": "200000000", "priceChangePercent": "1", "weightedAvgPrice": "100"}]
                if path == "ticker/bookTicker": return [{"symbol": SYMBOL, "bidPrice": "99.99", "askPrice": "100.01"}]
                return super()._get_json(path, params)
        source = Source(ttl_seconds=0.0, timeout_seconds=10.0); payload = _history_payload()
        with patch("providers.binance_opportunity_source.urlopen", side_effect=[socket.timeout("read timed out"), _Response(payload)]), patch("providers.binance_opportunity_source.time.sleep") as sleep:
            metrics = source.metrics_bulk((SYMBOL,))
        self.assertIn(SYMBOL, metrics); sleep.assert_called_once_with(0.5); history_events = [event for event in source.request_events if event["stage"] == "history"]; self.assertEqual([event["outcome"] for event in history_events], ["retrying", "success"])

    def test_survivor_timeout_after_retry_exhaustion_fails_closed(self):
        source = _StartupSource(failure=TimeoutError("persistent startup timeout")); discovery = OpportunityDiscovery(MarketUniverseDiscovery(_Universe()), source, OpportunityConfig()); discovery._ranker.rank = Mock()
        with self.assertRaises(TimeoutError): discovery.discover(top_n=1)
        discovery._ranker.rank.assert_not_called()

    def test_incomplete_bootstrap_never_reaches_ranking(self):
        source = _StartupSource(metrics={}); discovery = OpportunityDiscovery(MarketUniverseDiscovery(_Universe()), source, OpportunityConfig()); discovery._ranker.rank = Mock()
        with self.assertRaisesRegex(RuntimeError, "fresh discovery bootstrap incomplete"): discovery.discover(top_n=1)
        discovery._ranker.rank.assert_not_called()

    def test_startup_source_does_not_use_90_second_deadline_as_transport_gate(self):
        import tools.orion_paper_8h_runner as runner_module
        source = runner_module._BoundedBinanceSpotOpportunitySource(ttl_seconds=0.0, deadline=1.0)
        self.assertEqual(runner_module.STARTUP_DISCOVERY_TIMEOUT_SECONDS, 90.0); self.assertEqual(source._startup_deadline, float("inf"))
        with patch("providers.binance_opportunity_source.urlopen", return_value=_Response({"ok": True})) as open_mock: self.assertEqual(source._get_json("exchangeInfo"), {"ok": True})
        self.assertEqual(open_mock.call_args.kwargs["timeout"], 10.0)

    def test_deep_candidate_timeout_retries_with_transport_timeout(self):
        import tools.orion_paper_8h_runner as runner_module
        from enums import Timeframe
        provider = runner_module._BoundedPublicBinanceKlineProvider(); payload = _history_payload(32)
        with patch("tools.orion_paper_8h_runner.urllib.request.urlopen", side_effect=[socket.timeout("read timed out"), _Response(payload)]), patch("tools.orion_paper_8h_runner.time.sleep") as sleep:
            self.assertEqual(provider.klines(SYMBOL, Timeframe.H4, 32), payload)
        self.assertEqual(sleep.call_count, 1); self.assertEqual(provider.request_events[0]["failure_category"], "read_or_transport_timeout"); self.assertEqual(provider.request_events[-1]["outcome"], "success"); self.assertEqual(provider.request_events[0]["timeout_effective"], 10.0)


if __name__ == "__main__": unittest.main()