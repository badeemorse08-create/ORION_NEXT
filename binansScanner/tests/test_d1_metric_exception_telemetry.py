from __future__ import annotations

import math
import unittest

from models.opportunity import UniverseCandidate
from providers.binance_opportunity_source import BinanceSpotOpportunitySource
from services.opportunity_discovery import OpportunityConfig, OpportunityDiscovery


class _Source(BinanceSpotOpportunitySource):
    def __init__(self):
        super().__init__(ttl_seconds=0.0, timeout_seconds=1.0)
        self._startup_deadline = math.inf
        self.history_calls: list[str] = []

    def _get_json(self, path, params=None):
        if path == "ticker/24hr" and params is None:
            return [{
                "symbol": "DJTBUSDT",
                "lastPrice": "100",
                "quoteVolume": "200000000",
                "priceChangePercent": "1",
                "weightedAvgPrice": "100",
            }]
        if path == "ticker/bookTicker" and params is None:
            return [{"symbol": "DJTBUSDT", "bidPrice": "99.995", "askPrice": "100.005"}]
        raise AssertionError((path, params))

    def _fetch_history(self, symbol: str):
        self.history_calls.append(symbol)
        return tuple([[i, "1", "2", "0", "100", "10"] for i in range(32)])

    def _history_features(self, rows):
        raise ValueError("DJTBUSDT metric feature construction sentinel")


class _Universe:
    def discover(self):
        return (UniverseCandidate("DJTBUSDT", "DJTB", "USDT"),)


class MetricExceptionTelemetryTests(unittest.TestCase):
    def test_metric_exception_is_recorded_and_bootstrap_remains_failed_closed(self):
        source = _Source()
        discovery = OpportunityDiscovery(_Universe(), source, OpportunityConfig(default_top_n=1))

        with self.assertRaisesRegex(RuntimeError, r"missing=DJTBUSDT"):
            discovery.discover(top_n=1)

        self.assertEqual(source.history_calls, ["DJTBUSDT"])
        self.assertEqual(discovery.last_bootstrap.received_symbols, ())
        self.assertEqual(discovery.last_bootstrap.missing_symbols, ("DJTBUSDT",))

        events = discovery.metric_exception_events
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event_type"], "startup_diagnostic_metric_exception")
        self.assertEqual(event["symbol"], "DJTBUSDT")
        self.assertEqual(event["stage"], "metric_construction")
        self.assertEqual(event["exception_type"], "ValueError")
        self.assertEqual(event["exception_message"], "DJTBUSDT metric feature construction sentinel")
        self.assertEqual(event["history_length"], 32)
        self.assertEqual(event["metrics_history_window"], 31)
        self.assertEqual(event["min_history_candles"], 22)
        self.assertTrue(event["ticker_present"])
        self.assertTrue(event["book_present"])


if __name__ == "__main__":
    unittest.main()
