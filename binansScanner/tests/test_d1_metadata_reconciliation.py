from __future__ import annotations

import math
import socket
import unittest
from unittest.mock import patch

from models.opportunity import MarketMetrics
from providers.binance_opportunity_source import BinanceSpotOpportunitySource
from services.opportunity_discovery import MarketEligibilityFilter, MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery


def _history(rows: int = 32):
    return [[i, "1", "2", "0", str(100 + (i % 3) * 2), "10"] for i in range(rows)]


def _ticker(symbol: str, volume: float = 200_000_000.0):
    return {"symbol": symbol, "lastPrice": "100", "quoteVolume": str(volume), "priceChangePercent": "1", "weightedAvgPrice": "100"}


def _book(symbol: str, spread_bps: float = 1.0):
    half = spread_bps / 20_000.0
    return {"symbol": symbol, "bidPrice": str(100.0 * (1.0 - half)), "askPrice": str(100.0 * (1.0 + half))}


def _candidate(symbol: str):
    return type("Candidate", (), {"symbol": symbol, "base_asset": symbol[:-4], "quote_asset": "USDT"})()


def _metrics(symbol: str) -> MarketMetrics:
    return MarketMetrics(symbol, 200_000_000.0, 0.03, 1.0, True, 100.0, 1.0, 0.8, 0.7, 0.9, 1.0, 100.0, 0.5, 0.8, 0.4)


class MockTargetedSource(BinanceSpotOpportunitySource):
    def __init__(self, *, persistent_missing=False, low_volume=False, wide_spread=False, bulk_book_missing=False):
        super().__init__(ttl_seconds=30.0, timeout_seconds=1.0)
        self._startup_deadline = math.inf
        self.calls = []
        self.history_calls = []
        self.persistent_missing = persistent_missing
        self.low_volume = low_volume
        self.wide_spread = wide_spread
        self.bulk_book_missing = bulk_book_missing

    def _get_json(self, path, params=None):
        params = None if params is None else dict(params)
        self.calls.append((path, params))
        symbol = None if params is None else str(params.get("symbol"))
        if path == "ticker/24hr":
            if symbol:
                if self.persistent_missing:
                    raise RuntimeError("targeted ticker unavailable")
                return _ticker(symbol, 500_000.0 if self.low_volume else 200_000_000.0)
            rows = [_ticker("AAABUSDT")]
            if self.bulk_book_missing:
                rows.append(_ticker("DJTBUSDT"))
            return rows
        if path == "ticker/bookTicker":
            if symbol:
                if self.persistent_missing:
                    raise RuntimeError("targeted book unavailable")
                return _book(symbol, 100.0 if self.wide_spread else 1.0)
            return [_book("AAABUSDT")] if self.bulk_book_missing else [_book("AAABUSDT")]
        raise AssertionError((path, params))

    def _fetch_history(self, symbol: str):
        self.history_calls.append(symbol)
        return tuple(_history())


def _discovery(source, candidates):
    discovery = OpportunityDiscovery(MarketUniverseDiscovery(type("U", (), {"exchange_info": lambda self: {"symbols": []}})()), source, OpportunityConfig(default_top_n=1))
    discovery._universe = type("Universe", (), {"discover": lambda self: candidates})()
    return discovery


class D1MetadataReconciliationTests(unittest.TestCase):
    def test_bulk_ticker_omission_recovered_by_targeted_ticker(self):
        source = MockTargetedSource()
        discovery = _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT")))
        discovery.discover(top_n=1)
        self.assertEqual([p[1]["symbol"] for p in source.calls if p[0] == "ticker/24hr" and p[1]], ["DJTBUSDT"])
        self.assertEqual(source.history_calls.count("DJTBUSDT"), 1)

    def test_bulk_book_omission_recovered_by_targeted_book(self):
        source = MockTargetedSource(bulk_book_missing=True)
        discovery = _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT")))
        discovery.discover(top_n=1)
        self.assertEqual([p[1]["symbol"] for p in source.calls if p[0] == "ticker/bookTicker" and p[1]], ["DJTBUSDT"])
        self.assertEqual(source.history_calls.count("DJTBUSDT"), 1)

    def test_targeted_low_volume_rejection_prevents_history(self):
        source = MockTargetedSource(low_volume=True)
        _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT"))).discover(top_n=1)
        self.assertNotIn("DJTBUSDT", source.history_calls)

    def test_targeted_wide_spread_rejection_prevents_history(self):
        source = MockTargetedSource(wide_spread=True)
        _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT"))).discover(top_n=1)
        self.assertNotIn("DJTBUSDT", source.history_calls)

    def test_targeted_metadata_timeout_retries(self):
        source = BinanceSpotOpportunitySource(ttl_seconds=0.0, timeout_seconds=1.0)
        attempts = {"ticker": 0}
        def fake_urlopen(request, timeout):
            url = request.full_url
            if "ticker/24hr?symbol=DJTBUSDT" in url:
                attempts["ticker"] += 1
                if attempts["ticker"] == 1:
                    raise socket.timeout("read timed out")
                return _Response(_ticker("DJTBUSDT"))
            raise AssertionError(url)
        with patch("providers.binance_opportunity_source.urlopen", side_effect=fake_urlopen), patch("providers.binance_opportunity_source.time.sleep") as sleep:
            source._get_json("ticker/24hr", {"symbol": "DJTBUSDT"})
        self.assertEqual(attempts["ticker"], 2)
        sleep.assert_called_once_with(0.5)

    def test_targeted_request_count_is_bounded(self):
        source = MockTargetedSource(persistent_missing=True)
        with self.assertRaises(RuntimeError):
            _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT"), _candidate("ZZZUSDT"))).discover(top_n=1)
        targeted = [c for c in source.calls if c[1] is not None]
        self.assertEqual(len(targeted), 8)
        self.assertEqual({c[1]["symbol"] for c in targeted}, {"DJTBUSDT", "ZZZUSDT"})

    def test_reconciliation_rounds_are_bounded_at_two(self):
        source = MockTargetedSource(persistent_missing=True)
        discovery = _discovery(source, (_candidate("DJTBUSDT"),))
        with self.assertRaises(RuntimeError):
            discovery.discover(top_n=1)
        events = [e for e in discovery.last_reconciliation_events if e["symbol"] == "DJTBUSDT"]
        self.assertEqual([e["reconciliation_attempt"] for e in events], [1, 2])

    def test_no_infinite_refresh_loop(self):
        source = MockTargetedSource(persistent_missing=True)
        with self.assertRaises(RuntimeError):
            _discovery(source, (_candidate("DJTBUSDT"),)).discover(top_n=1)
        self.assertEqual(len([c for c in source.calls if c[1] is not None]), 4)

    def test_deterministic_ordering_is_preserved(self):
        candidates = tuple(_candidate(s) for s in ("ZZZUSDT", "DJTBUSDT", "AAABUSDT"))
        snapshots = []
        for _ in range(2):
            source = MockTargetedSource(persistent_missing=True)
            discovery = _discovery(source, candidates)
            with self.assertRaises(RuntimeError):
                discovery.discover(top_n=1)
            snapshots.append([(e["symbol"], e["reconciliation_attempt"]) for e in discovery.last_reconciliation_events])
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0], [("DJTBUSDT", 1), ("DJTBUSDT", 2), ("ZZZUSDT", 1), ("ZZZUSDT", 2)])

    def test_djtb_incident_475_of_476_recovers_deterministically(self):
        symbols = tuple(f"S{i:03d}USDT" for i in range(475)) + ("DJTBUSDT",)
        class IncidentSource(MockTargetedSource):
            def _get_json(self, path, params=None):
                if params is None and path in {"ticker/24hr", "ticker/bookTicker"}:
                    self.calls.append((path, None))
                    maker = _ticker if path == "ticker/24hr" else _book
                    return [maker(symbol) for symbol in symbols if symbol != "DJTBUSDT"]
                return super()._get_json(path, params)
        source = IncidentSource()
        discovery = _discovery(source, tuple(_candidate(s) for s in symbols))
        discovery.discover(top_n=1)
        self.assertEqual(len(discovery.last_bootstrap.expected_symbols), 476)
        self.assertEqual(len(discovery.last_bootstrap.received_symbols), 476)
        self.assertEqual(discovery.last_bootstrap.missing_symbols, ())
        targeted = [c for c in source.calls if c[1] and c[1]["symbol"] == "DJTBUSDT"]
        self.assertEqual({c[0] for c in targeted}, {"ticker/24hr", "ticker/bookTicker"})
        self.assertEqual(source.history_calls.count("DJTBUSDT"), 1)

    def test_persistent_djtb_absence_fails_closed(self):
        source = MockTargetedSource(persistent_missing=True)
        discovery = _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT")))
        with self.assertRaises(RuntimeError):
            discovery.discover(top_n=1)
        self.assertEqual(discovery.last_bootstrap.missing_symbols, ("DJTBUSDT",))
        self.assertEqual(discovery.last_bootstrap.source_status, "incomplete")

    def test_runtime_not_created_while_unresolved(self):
        source = MockTargetedSource(persistent_missing=True)
        discovery = _discovery(source, (_candidate("DJTBUSDT"),))
        ranked = False
        def fail_if_ranked(*args, **kwargs):
            nonlocal ranked
            ranked = True
            raise AssertionError("ranking must not run")
        discovery._ranker.rank = fail_if_ranked
        with self.assertRaises(RuntimeError):
            discovery.discover(top_n=1)
        self.assertFalse(ranked)

    def test_existing_d1_prefilter_semantics_remain_unchanged(self):
        filt = MarketEligibilityFilter(OpportunityConfig())
        candidate = _candidate("DJTBUSDT")
        low = _metrics("DJTBUSDT")
        object.__setattr__(low, "quote_volume_24h", 500_000.0)
        wide = _metrics("DJTBUSDT")
        object.__setattr__(wide, "spread_bps", 100.0)
        self.assertIn("LOW_VOLUME", filt.evaluate(candidate, low).reasons)
        self.assertIn("WIDE_SPREAD", filt.evaluate(candidate, wide).reasons)

    def test_1d_handoff_remains_duplicate_free(self):
        source = MockTargetedSource()
        _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT"))).discover(top_n=1)
        handoff = source.take_daily_candle_handoff()
        self.assertIsNotNone(handoff)
        self.assertIn("DJTBUSDT", handoff.candles)
        self.assertEqual(len(handoff.candles["DJTBUSDT"]), 32)
        self.assertIsNone(source.take_daily_candle_handoff())

    def test_targeted_observability_identifies_exact_loss_location(self):
        source = MockTargetedSource()
        discovery = _discovery(source, (_candidate("AAABUSDT"), _candidate("DJTBUSDT")))
        discovery.discover(top_n=1)
        event = next(e for e in discovery.last_reconciliation_events if e["symbol"] == "DJTBUSDT")
        self.assertEqual((event["bulk_ticker_state"], event["bulk_book_state"]), ("absent", "absent"))
        self.assertEqual((event["targeted_ticker_state"], event["targeted_book_state"]), ("present", "present"))
        self.assertEqual(event["reconciliation_attempt"], 1)
        self.assertTrue(event["needs_history"])
        self.assertEqual(event["history_outcome"], "success")
        self.assertEqual(event["final_disposition"], "eligible")


class _Response:
    def __init__(self, payload):
        import json
        self.body = json.dumps(payload).encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def read(self):
        return self.body
