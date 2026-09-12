from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_PATH = _REPO_ROOT / "tools" / "orion_paper_8h_runner.py"
_SPEC = importlib.util.spec_from_file_location("orion_paper_8h_runner_metric_exception_persistence", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


class _DelegatingPipeline:
    """Test seam around the real Pipeline construction that delegates to real discovery."""

    def __init__(self, discovery, *args, **kwargs):
        self.discovery = discovery
        self.args = args
        self.kwargs = kwargs

    def discover(self):
        return self.discovery.discover(top_n=1)


def _ticker(symbol: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "lastPrice": "100",
        "quoteVolume": "200000000",
        "priceChangePercent": "1",
        "weightedAvgPrice": "100",
    }


def _book(symbol: str) -> dict[str, str]:
    return {"symbol": symbol, "bidPrice": "99.995", "askPrice": "100.005"}


def _history() -> tuple[list[object], ...]:
    return tuple([[i, "1", "2", "0", "100", "10"] for i in range(32)])


class PaperRunnerMetricExceptionPersistenceTests(unittest.TestCase):
    def test_real_create_persists_metric_exception_before_startup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            config = runner.Paper8HConfig(output_dir=output_dir, starting_capital=50.0, top_n=1)

            def fake_get_json(self, path, params=None):
                if path == "exchangeInfo" and params is None:
                    return {
                        "symbols": [{
                            "symbol": "DJTBUSDT",
                            "baseAsset": "DJTB",
                            "quoteAsset": "USDT",
                            "status": "TRADING",
                            "isSpotTradingAllowed": True,
                        }]
                    }
                if path == "ticker/24hr" and params is None:
                    return [_ticker("DJTBUSDT")]
                if path == "ticker/bookTicker" and params is None:
                    return [_book("DJTBUSDT")]
                raise AssertionError((path, params))

            def fake_history(self, symbol: str):
                self._test_history_calls = getattr(self, "_test_history_calls", 0) + 1
                return _history()

            def fake_features(self, rows):
                raise ValueError("DJTBUSDT metric feature construction sentinel")

            with patch.object(runner, "ScalpingOpportunityPipeline", _DelegatingPipeline), \
                patch.object(runner._BoundedBinanceSpotOpportunitySource, "_get_json", fake_get_json), \
                patch.object(runner._BoundedBinanceSpotOpportunitySource, "_fetch_history", fake_history), \
                patch.object(runner._BoundedBinanceSpotOpportunitySource, "_history_features", fake_features), \
                patch.object(runner, "PaperRealtimeLifecycle") as lifecycle:
                with self.assertRaisesRegex(RuntimeError, r"fresh discovery bootstrap incomplete: expected=1 received=0 missing=DJTBUSDT"):
                    runner.Paper8HRunner.create(config)

            lifecycle.assert_not_called()
            lines = [json.loads(line) for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            diagnostic = [line for line in lines if line.get("event_type") == "startup_diagnostic_metric_exception"]
            failures = [line for line in lines if line.get("event_type") == "startup_failure"]
            self.assertEqual(len(diagnostic), 1)
            self.assertEqual(len(failures), 1)
            self.assertLess(lines.index(diagnostic[0]), lines.index(failures[0]))
            event = diagnostic[0]
            self.assertEqual(event["symbol"], "DJTBUSDT")
            self.assertEqual(event["stage"], "metric_construction")
            self.assertEqual(event["exception_type"], "ValueError")
            self.assertEqual(event["exception_message"], "DJTBUSDT metric feature construction sentinel")
            self.assertEqual(event["history_length"], 32)
            self.assertEqual(event["metrics_history_window"], 31)
            self.assertEqual(event["min_history_candles"], 22)
            self.assertTrue(event["ticker_present"])
            self.assertTrue(event["book_present"])
            self.assertEqual(failures[0]["failure_kind"], "discovery_exception")
            self.assertIn("fresh discovery bootstrap incomplete", failures[0]["error"])


if __name__ == "__main__":
    unittest.main()
