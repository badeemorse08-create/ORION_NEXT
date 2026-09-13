import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_PATH = _REPO_ROOT / "tools" / "orion_paper_8h_runner.py"
_SPEC = importlib.util.spec_from_file_location("orion_paper_8h_runner_scalping_integration", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner_module
_SPEC.loader.exec_module(runner_module)


class _InitialSet:
    def __init__(self, symbols):
        self.candidates = tuple(type("Candidate", (), {"symbol": symbol})() for symbol in symbols)

    def symbols(self):
        return tuple(candidate.symbol for candidate in self.candidates)


class _PipelineSpy:
    def __init__(self, *args, **kwargs):
        self.discover_calls = 0

    def discover(self, *args, **kwargs):
        self.discover_calls += 1
        return _InitialSet(("BTCUSDT", "ETHUSDT"))


class _DiscoverySpy:
    def __init__(self, *args, **kwargs):
        self.market_provider = kwargs.get("source")


class _MarketDiscoverySpy:
    def __init__(self, *args, **kwargs):
        pass


class _SourceSpy:
    def __init__(self, *args, **kwargs):
        pass

    def exchange_info(self):
        return {"symbols": []}


class _StreamSpy:
    def __init__(self, symbols):
        self.symbols = tuple(symbols)


class _SupervisorSpy:
    def __init__(self, *args, **kwargs):
        self.runtime = type("Runtime", (), {"ledger": object()})()


class _CapitalBridgeSpy:
    def __init__(self, *args, **kwargs):
        pass


class TestPaperRunnerScalpingIntegration(unittest.TestCase):
    def test_runner_create_wires_the_approved_scalping_pipeline(self):
        config = runner_module.Paper8HConfig(top_n=2)
        with patch.object(runner_module, "BinanceSpotOpportunitySource", _SourceSpy), \
             patch.object(runner_module, "MarketUniverseDiscovery", _MarketDiscoverySpy), \
             patch.object(runner_module, "OpportunityDiscovery", _DiscoverySpy), \
             patch.object(runner_module, "ScalpingOpportunityPipeline", _PipelineSpy), \
             patch.object(runner_module, "DynamicMarketStream", _StreamSpy), \
             patch.object(runner_module, "PaperRealtimeLifecycle"), \
             patch.object(runner_module, "PaperRuntimeSupervisor", _SupervisorSpy), \
             patch.object(runner_module, "PaperRunnerCapitalBridge", _CapitalBridgeSpy):
            runner = runner_module.Paper8HRunner.create(config)
        self.assertIsInstance(runner.opportunity, _PipelineSpy)
        self.assertEqual(runner.stream.symbols, ("BTCUSDT", "ETHUSDT"))

    def test_runner_has_no_legacy_direct_decision_path(self):
        source = _RUNNER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("evaluate_decision", source)
        self.assertNotIn("def canonical_decision", source)
        self.assertIn("ScalpingOpportunityPipeline", source)
        self.assertIn("ScalpingDecisionEngine", source)
        self.assertIn("ScalpingCandidatePoolManager", source)
        self.assertIn("BinanceScalpingCandleSource", source)

    def test_entry_requires_d1_entry_allowed_and_a_or_a_plus(self):
        source = _RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn('if not trace.entry_allowed or candidate.entry_state not in {"A", "A+"}', source)
        self.assertIn('decision.get("decision", "BUY")', source)

    def test_runner_preserves_d1_decision_trace_in_observability(self):
        source = _RUNNER_PATH.read_text(encoding="utf-8")
        for field in ("opportunity_class", "opportunity_score", "directional_evidence", "entry_state", "entry_readiness", "risk_reward", "decision_trace"):
            self.assertIn(field, source)
        self.assertIn('"signal_event"', source)

    def test_market_data_failure_is_fail_closed_without_legacy_fallback(self):
        source = _RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn('fail_closed=True', source)
        self.assertIn('rejection_reason="MARKET_DATA_FAILURE"', source)
        self.assertNotIn("canonical_decision", source)

    def test_capital_manager_remains_allocation_boundary(self):
        source = _RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("self.capital.allocation_for", source)
        self.assertIn("required_symbol_minimum", source)
        self.assertNotIn("max_notional_pct", source)


if __name__ == "__main__":
    unittest.main()
