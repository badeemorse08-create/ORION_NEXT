from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from providers.binance_opportunity_source import BinanceSpotOpportunitySource

SYMBOL = "AAAUSDT"


def _history_payload():
    base_ts = 1_700_000_000_000
    return [
        [base_ts + index * 86_400_000, str(100 + index - 0.5), str(100 + index + 0.5), str(100 + index - 1.0), str(100 + index), "10"]
        for index in range(32)
    ]


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _FakePipeline:
    def __init__(self, discovery, *args, **kwargs):
        self.discovery = discovery

    def discover(self):
        metrics = self.discovery._metrics_source.metrics_bulk((SYMBOL,))
        if SYMBOL not in metrics:
            raise AssertionError("bootstrap did not produce the mandatory survivor metric")
        return SimpleNamespace(candidates=(SimpleNamespace(symbol=SYMBOL),))


class _FakeSupervisor:
    def __init__(self, runtime):
        self.runtime = runtime


class ResilientBootstrapRunnerIntegrationTests(unittest.TestCase):
    @staticmethod
    def _config(runner_module, path: Path):
        return runner_module.Paper8HConfig(output_dir=path, starting_capital=50.0, top_n=1, dynamic_universe=True)

    @staticmethod
    def _fake_urlopen(*, timeout_once: bool):
        state = {"history_attempts": 0}

        def fake_urlopen(request, timeout):
            url = request.full_url
            if "ticker/24hr" in url:
                return _Response([{"symbol": SYMBOL, "lastPrice": "100", "quoteVolume": "200000000", "priceChangePercent": "1", "weightedAvgPrice": "100"}])
            if "ticker/bookTicker" in url:
                return _Response([{"symbol": SYMBOL, "bidPrice": "99.99", "askPrice": "100.01"}])
            if "/klines?" in url:
                state["history_attempts"] += 1
                if timeout_once and state["history_attempts"] == 1:
                    raise socket.timeout("read timed out")
                if not timeout_once:
                    raise socket.timeout("persistent read timeout")
                return _Response(_history_payload())
            raise AssertionError(url)

        return fake_urlopen, state

    def test_transient_history_timeout_retries_and_allows_runtime_initialization(self):
        import tools.orion_paper_8h_runner as runner_module

        fake_urlopen, state = self._fake_urlopen(timeout_once=True)
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(runner_module, Path(tmp) / "run")
            runtime = SimpleNamespace(ledger=Mock())
            lifecycle_factory = Mock(return_value=runtime)
            stream_factory = Mock(return_value=object())
            supervisor_factory = Mock(side_effect=lambda runtime: _FakeSupervisor(runtime))
            with patch("providers.binance_opportunity_source.urlopen", side_effect=fake_urlopen), patch(
                "providers.binance_opportunity_source.time.sleep"
            ) as sleep, patch.object(runner_module, "ScalpingOpportunityPipeline", _FakePipeline), patch.object(
                runner_module, "DynamicMarketStream", stream_factory
            ), patch.object(runner_module, "PaperRuntimeSupervisor", supervisor_factory), patch.object(
                runner_module, "PaperRealtimeLifecycle", lifecycle_factory
            ):
                result = runner_module.Paper8HRunner.create(config)

            self.assertIsNotNone(result)
            self.assertEqual(state["history_attempts"], 2)
            sleep.assert_called_once_with(0.5)
            records = [json.loads(line) for line in (Path(tmp) / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            phases = [record["startup_phase"] for record in records if record["event_type"] == "startup_phase"]
            self.assertEqual(phases, ["market_discovery", "runtime_initialization", "running"])
            lifecycle_factory.assert_called_once()
            stream_factory.assert_called_once_with((SYMBOL,))

    def test_persistent_history_failure_exhausts_retry_and_blocks_runtime(self):
        import tools.orion_paper_8h_runner as runner_module

        fake_urlopen, state = self._fake_urlopen(timeout_once=False)
        lifecycle_factory = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(runner_module, Path(tmp) / "run")
            with patch("providers.binance_opportunity_source.urlopen", side_effect=fake_urlopen), patch(
                "providers.binance_opportunity_source.time.sleep"
            ) as sleep, patch.object(runner_module, "ScalpingOpportunityPipeline", _FakePipeline), patch.object(
                runner_module, "PaperRealtimeLifecycle", lifecycle_factory
            ):
                with self.assertRaises(TimeoutError):
                    runner_module.Paper8HRunner.create(config)

            self.assertEqual(state["history_attempts"], BinanceSpotOpportunitySource.RETRY_MAX_ATTEMPTS)
            self.assertEqual(sleep.call_count, BinanceSpotOpportunitySource.RETRY_MAX_ATTEMPTS - 1)
            lifecycle_factory.assert_not_called()
            records = [json.loads(line) for line in (Path(tmp) / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            failures = [record for record in records if record["event_type"] == "startup_failure"]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["startup_phase"], "failed")
            self.assertEqual(failures[0]["failure_kind"], "discovery_timeout")


if __name__ == "__main__":
    unittest.main()
