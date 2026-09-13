from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_PATH = _REPO_ROOT / "tools" / "orion_paper_8h_runner.py"
_SPEC = importlib.util.spec_from_file_location("orion_paper_8h_runner_startup_tests", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


class _FakePipeline:
    def __init__(self, *args, **kwargs):
        pass

    def discover(self):
        return type("Result", (), {"candidates": [type("Candidate", (), {"symbol": "BTCUSDT"})()]})()


class _FailingPipeline:
    def __init__(self, *args, **kwargs):
        pass

    def discover(self):
        raise RuntimeError("discovery failed")


class _TimeoutPipeline:
    def __init__(self, *args, **kwargs):
        pass

    def discover(self):
        raise TimeoutError("paper startup discovery timeout")


class PaperRunnerStartupTests(unittest.TestCase):
    def _config(self, directory: Path) -> runner.Paper8HConfig:
        return runner.Paper8HConfig(output_dir=directory, starting_capital=50.0, top_n=10)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_REPO_ROOT / "binansScanner" / "runs", ignore_errors=True)
        super().tearDownClass()

    def test_output_directory_and_run_start_exist_before_discovery(self):
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp) / "run")
            log = runner._startup_log(config)
            events = Path(tmp) / "run" / "events.jsonl"
            self.assertTrue(events.exists())
            first = json.loads(events.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["event_type"], "run_start")
            self.assertEqual(first["startup_phase"], "initialization")
            self.assertTrue(first["paper_only"])
            self.assertFalse(first["live_execution"])
            log.close()

    def test_bounded_source_accepts_legacy_deadline_argument_without_enforcing_it(self):
        source = runner._BoundedBinanceSpotOpportunitySource(deadline=time.monotonic() - 1)
        self.assertEqual(source._startup_deadline, float("inf"))
        with patch("providers.binance_opportunity_source.urlopen", return_value=type("Response", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *args: False,
            "read": lambda self: b"{}",
        })()):
            self.assertEqual(source._get_json("exchangeInfo"), {})

    def test_bounded_source_uses_transport_timeout_not_remaining_startup_deadline(self):
        import providers.binance_opportunity_source as source_module

        observed = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            observed["timeout"] = timeout
            return Response()

        source = runner._BoundedBinanceSpotOpportunitySource(deadline=time.monotonic() + 0.5)
        with patch.object(source_module, "urlopen", fake_urlopen):
            source._get_json("exchangeInfo")
        self.assertEqual(observed["timeout"], 10.0)

    def test_discovery_exception_writes_startup_failure(self):
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp) / "run")
            with patch.object(runner, "ScalpingOpportunityPipeline", _FailingPipeline):
                with self.assertRaises(RuntimeError):
                    runner.Paper8HRunner.create(config)
            lines = [json.loads(line) for line in (Path(tmp) / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            failure = lines[-1]
            self.assertEqual(failure["event_type"], "startup_failure")
            self.assertEqual(failure["startup_phase"], "failed")
            self.assertEqual(failure["failure_kind"], "discovery_exception")

    def test_pipeline_timeout_reaches_create_startup_failure(self):
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp) / "run")
            with patch.object(runner, "ScalpingOpportunityPipeline", _TimeoutPipeline):
                with self.assertRaises(TimeoutError):
                    runner.Paper8HRunner.create(config)
            lines = [json.loads(line) for line in (Path(tmp) / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            failure = lines[-1]
            self.assertEqual(failure["event_type"], "startup_failure")
            self.assertEqual(failure["startup_phase"], "failed")
            self.assertEqual(failure["failure_kind"], "discovery_timeout")

    def test_90_second_constant_does_not_terminate_valid_discovery(self):
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp) / "run")
            with patch.object(runner, "STARTUP_DISCOVERY_TIMEOUT_SECONDS", -1.0), patch.object(
                runner, "ScalpingOpportunityPipeline", _FakePipeline
            ), patch.object(runner.Paper8HRunner, "__post_init__", lambda self: None):
                result = runner.Paper8HRunner.create(config)
            self.assertEqual(result.config.output_dir, Path(tmp) / "run")
            lines = [json.loads(line) for line in (Path(tmp) / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            phases = [line.get("startup_phase") for line in lines if line["event_type"] == "startup_phase"]
            self.assertEqual(phases, ["market_discovery", "runtime_initialization", "running"])

    def test_successful_discovery_records_runtime_phases(self):
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp) / "run")
            with patch.object(runner, "ScalpingOpportunityPipeline", _FakePipeline):
                with patch.object(runner.Paper8HRunner, "__post_init__", lambda self: None):
                    result = runner.Paper8HRunner.create(config)
            lines = [json.loads(line) for line in (Path(tmp) / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            phases = [line.get("startup_phase") for line in lines if line["event_type"] == "startup_phase"]
            self.assertEqual(phases, ["market_discovery", "runtime_initialization", "running"])
            self.assertEqual(result.config.output_dir, Path(tmp) / "run")

    def test_failed_discovery_does_not_initialize_stream_runtime(self):
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp) / "run")
            with patch.object(runner, "ScalpingOpportunityPipeline", _FailingPipeline), patch.object(runner, "PaperRealtimeLifecycle") as lifecycle:
                with self.assertRaises(RuntimeError):
                    runner.Paper8HRunner.create(config)
            lifecycle.assert_not_called()

    def test_startup_failure_directory_is_retained(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "failed-run"
            config = self._config(output)
            with patch.object(runner, "ScalpingOpportunityPipeline", _FailingPipeline):
                with self.assertRaises(RuntimeError):
                    runner.Paper8HRunner.create(config)
            self.assertTrue(output.is_dir())
            self.assertTrue((output / "events.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
