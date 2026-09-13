from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from models.opportunity import MarketMetrics
from services.opportunity_discovery import MarketUniverseDiscovery, OpportunityConfig, OpportunityDiscovery

SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT")


def _metric(symbol: str, quality: float = 0.9) -> MarketMetrics:
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
    def __init__(self, symbols=SYMBOLS):
        self.symbols = tuple(symbols)

    def exchange_info(self):
        return {
            "symbols": [
                {"symbol": symbol, "baseAsset": symbol[:-4], "quoteAsset": "USDT", "status": "TRADING"}
                for symbol in self.symbols
            ]
        }


class _StartupSource:
    def __init__(self, metrics, *, startup=True):
        self.metrics_data = dict(metrics)
        self.calls = 0
        if startup:
            self._startup_deadline = float("inf")

    def metrics_bulk(self, symbols):
        self.calls += 1
        return dict(self.metrics_data)


class ColdStartCorrectiveContractTests(unittest.TestCase):
    def _discovery(self, source, *, universe=None):
        return OpportunityDiscovery(
            MarketUniverseDiscovery(universe or _Universe()),
            source,
            OpportunityConfig(refresh_interval_seconds=300.0),
        )

    def test_partial_bootstrap_is_rejected_before_ranking_with_explicit_inventory(self):
        source = _StartupSource({SYMBOLS[0]: _metric(SYMBOLS[0])})
        discovery = self._discovery(source)
        rank = Mock()
        discovery._ranker.rank = rank

        with self.assertRaisesRegex(RuntimeError, "fresh discovery bootstrap incomplete"):
            discovery.discover(top_n=2)

        rank.assert_not_called()
        report = discovery.last_bootstrap
        self.assertEqual(report.expected_symbols, SYMBOLS)
        self.assertEqual(report.received_symbols, (SYMBOLS[0],))
        self.assertEqual(report.missing_symbols, SYMBOLS[1:])
        self.assertEqual(report.source_status, "incomplete")
        self.assertEqual(report.deadline_state, "active")

    def test_filtered_or_extra_source_output_cannot_hide_missing_expected_symbol(self):
        source = _StartupSource(
            {SYMBOLS[0]: _metric(SYMBOLS[0]), SYMBOLS[2]: _metric(SYMBOLS[2]), "ZZZUSDT": _metric("ZZZUSDT")}
        )
        discovery = self._discovery(source)
        with self.assertRaisesRegex(RuntimeError, "missing=BBBUSDT"):
            discovery.discover(top_n=2)
        self.assertEqual(discovery.last_bootstrap.missing_symbols, ("BBBUSDT",))

    def test_startup_mode_never_uses_previous_cached_output(self):
        source = _StartupSource({symbol: _metric(symbol) for symbol in SYMBOLS})
        discovery = self._discovery(source)
        discovery.discover(top_n=2)
        source.metrics_data.pop(SYMBOLS[1])

        with self.assertRaisesRegex(RuntimeError, "fresh discovery bootstrap incomplete"):
            discovery.discover(top_n=2)

        self.assertEqual(source.calls, 2)

    def test_complete_bootstrap_is_recorded_before_ranking(self):
        source = _StartupSource({symbol: _metric(symbol) for symbol in SYMBOLS})
        discovery = self._discovery(source)
        result = discovery.discover(top_n=2)

        self.assertEqual(len(result.candidates), 2)
        report = discovery.last_bootstrap
        self.assertEqual(report.expected_symbols, SYMBOLS)
        self.assertEqual(report.received_symbols, SYMBOLS)
        self.assertEqual(report.missing_symbols, ())
        self.assertEqual(report.source_status, "complete")

    def test_ordinary_source_exception_preserves_original_exception_and_records_status(self):
        class BrokenSource(_StartupSource):
            def metrics_bulk(self, symbols):
                raise ValueError("ordinary source failure")

        discovery = self._discovery(BrokenSource({}))
        with self.assertRaisesRegex(ValueError, "ordinary source failure"):
            discovery.discover(top_n=2)
        self.assertEqual(discovery.last_bootstrap.source_status, "exception")

    def test_timeout_source_exception_preserves_timeout_and_records_status(self):
        class TimeoutSource(_StartupSource):
            def metrics_bulk(self, symbols):
                raise TimeoutError("paper startup discovery deadline exceeded")

        discovery = self._discovery(TimeoutSource({}))
        with self.assertRaises(TimeoutError):
            discovery.discover(top_n=2)
        self.assertEqual(discovery.last_bootstrap.source_status, "timeout")

    def test_source_instances_do_not_share_bootstrap_or_cached_state(self):
        first = _StartupSource({symbol: _metric(symbol, 1.0) for symbol in SYMBOLS})
        second = _StartupSource({symbol: _metric(symbol, 0.2) for symbol in SYMBOLS})
        first_discovery = self._discovery(first)
        second_discovery = self._discovery(second)

        first_discovery.discover(top_n=2)
        self.assertIsNone(second_discovery.last_bootstrap)
        second_discovery.discover(top_n=2)
        self.assertIsNot(first_discovery.last_bootstrap, second_discovery.last_bootstrap)
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    def test_startup_timeout_constant_remains_90_seconds(self):
        import tools.orion_paper_8h_runner as runner_module
        self.assertEqual(runner_module.STARTUP_DISCOVERY_TIMEOUT_SECONDS, 90.0)

    def test_incomplete_bootstrap_prevents_runner_runtime_construction(self):
        import tools.orion_paper_8h_runner as runner_module

        class FakeSource:
            def __init__(self, *args, **kwargs):
                self._startup_deadline = kwargs.get("deadline", float("inf"))

            def exchange_info(self):
                return _Universe().exchange_info()

            def metrics_bulk(self, symbols):
                return {SYMBOLS[0]: _metric(SYMBOLS[0])}

        runtime_factory = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            config = runner_module.Paper8HConfig(output_dir=Path(tmp), dynamic_universe=True)
            with patch.object(runner_module, "BinanceSpotOpportunitySource", FakeSource), patch.object(
                runner_module, "PaperRealtimeLifecycle", runtime_factory
            ):
                with self.assertRaisesRegex(RuntimeError, "fresh discovery bootstrap incomplete"):
                    runner_module.Paper8HRunner.create(config)

            runtime_factory.assert_not_called()
            events = Path(tmp) / "events.jsonl"
            records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            failures = [record for record in records if record.get("event_type") == "startup_failure"]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["startup_phase"], "failed")


if __name__ == "__main__":
    unittest.main()
