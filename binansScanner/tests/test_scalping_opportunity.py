from __future__ import annotations

import ast
import math
import unittest

from models.capital_management import AllocationConfig, CapitalManager
from models.opportunity import MarketMetrics, OpportunityCandidate, OpportunityCandidateSet
from models.scalping_opportunity import EntryState, OpportunityClass, RejectionReason, ReplayEvaluation, ReplayEvent
from services.scalping_opportunity import ScalpingCandidatePoolManager, ScalpingConfig, ScalpingDecisionEngine, ScalpingEvidenceEngine, ScalpingReplayEvaluator


def candles(*, start: float, drift: float, volume: float, count: int = 48, range_size: float = 0.8):
    output = []
    price = start
    for index in range(count):
        open_price = price
        close = price + drift
        high = max(open_price, close) + range_size
        low = min(open_price, close) - range_size
        if index >= count - 2:
            high += range_size
            low -= range_size
        output.append((index, open_price, high, low, close, volume))
        price = close
    from models.scalping_opportunity import Candle
    return tuple(Candle(*row) for row in output)


def candidate(symbol: str = "BTCUSDT", score: float = 75.0) -> OpportunityCandidate:
    return OpportunityCandidate(symbol, score, 1, MarketMetrics(symbol, 100_000_000, 0.02, 5, True, 100), (), (("base", 0.75),), 0.5)


def replay_event(*, captured: bool = True, accepted: bool = True, return_pct: float = 0.02, hold_hours: float = 1.0, costs_pct: float = 0.10, utilization_pct: float = 25.0, profitable: bool = True) -> ReplayEvent:
    return ReplayEvent(captured, return_pct, accepted, hold_hours, costs_pct, utilization_pct, profitable)


class ScalpingTests(unittest.TestCase):
    def setUp(self):
        self.config = ScalpingConfig(min_candles=32, active_top_n=2, broad_pool_top_n=8)
        self.engine = ScalpingEvidenceEngine(self.config)
        self.decision = ScalpingDecisionEngine(self.config)
        self.flat = candles(start=100, drift=0.0, volume=100)
        up_base = list(candles(start=100, drift=0.9, volume=100))
        last = up_base[-1]
        from models.scalping_opportunity import Candle
        up_base[-3:] = [
            Candle(last.timestamp - 2, last.close, last.close + 1.0, last.close - 0.2, last.close + 0.8, 100),
            Candle(last.timestamp - 1, last.close + 0.8, last.close + 4.0, last.close + 0.6, last.close + 3.5, 120),
            Candle(last.timestamp, last.close + 3.5, last.close + 9.0, last.close + 3.0, last.close + 8.5, 160),
        ]
        self.up = tuple(up_base)
        breakout_base = candles(start=100, drift=0.5, volume=100)
        self.breakout = breakout_base[:-2] + tuple(
            type(breakout_base[0])(
                item.timestamp, item.open, item.high + 10.0, item.low - 1.0,
                item.close + 6.0, item.volume * 5.0,
            ) for item in breakout_base[-2:]
        )
        self.candle_map = {"1d": self.flat, "4h": self.flat, "1h": self.up, "15m": self.breakout}

    def test_all_four_timeframes_are_evidence(self):
        result = self.engine.compute(self.candle_map)
        self.assertEqual(tuple(x.timeframe for x in result.evidence), ("1d", "4h", "1h", "15m"))

    def test_four_hour_is_not_hard_scalping_gate(self):
        self.assertEqual(self.engine.compute(self.candle_map).opportunity_class, OpportunityClass.BREAKOUT_ACCELERATION)

    def test_breakout_class_detected(self):
        result = self.engine.compute(self.candle_map)
        self.assertEqual(result.opportunity_class, OpportunityClass.BREAKOUT_ACCELERATION)
        self.assertGreater(result.opportunity_score, 0.0)

    def test_trend_continuation_class_detected(self):
        rising = candles(start=100, drift=0.5, volume=100)
        result = self.engine.compute({"1d": rising, "4h": rising, "1h": rising, "15m": rising})
        self.assertEqual(result.opportunity_class, OpportunityClass.TREND_CONTINUATION)

    def test_pullback_continuation_class_detected(self):
        rise = list(candles(start=100, drift=0.6, volume=100, count=42))
        last = rise[-1]
        from models.scalping_opportunity import Candle
        rise[-3:] = [Candle(last.timestamp - 2, last.close, last.close + 0.2, last.close - 1.0, last.close - 0.5, 100), Candle(last.timestamp - 1, last.close - 0.5, last.close + 0.2, last.close - 0.3, last.close + 0.1, 120), Candle(last.timestamp, last.close + 0.1, last.close + 1.0, last.close, last.close + 0.8, 130)]
        seq = tuple(rise)
        result = self.engine.compute({"1d": seq, "4h": seq, "1h": seq, "15m": seq})
        self.assertIn(result.opportunity_class, (OpportunityClass.PULLBACK_CONTINUATION, OpportunityClass.TREND_CONTINUATION))

    def test_directional_evidence_is_directional(self):
        bull = self.engine.compute({"1d": self.up, "4h": self.up, "1h": self.up, "15m": self.up})
        bear = tuple(type(c)(c.timestamp, c.open, c.high, c.low, c.close, c.volume) for c in candles(start=200, drift=-0.9, volume=100))
        bear_features = self.engine.compute({"1d": bear, "4h": bear, "1h": bear, "15m": bear})
        self.assertGreater(bull.directional_evidence, 0)
        self.assertLess(bear_features.directional_evidence, 0)

    def test_acceleration_is_separate_from_momentum(self):
        from models.scalping_opportunity import Candle
        steady = list(candles(start=100, drift=0.9, volume=100))
        accelerated = list(steady)
        anchor = accelerated[39]
        accelerated[39] = Candle(anchor.timestamp, anchor.open, anchor.high, anchor.low, anchor.close - 4.0, anchor.volume)
        steady_features = self.engine.compute({"1d": self.flat, "4h": self.flat, "1h": tuple(steady), "15m": self.flat}).evidence[2]
        accelerated_features = self.engine.compute({"1d": self.flat, "4h": self.flat, "1h": tuple(accelerated), "15m": self.flat}).evidence[2]
        self.assertEqual(steady_features.momentum_score, accelerated_features.momentum_score)
        self.assertNotEqual(steady_features.acceleration_score, accelerated_features.acceleration_score)

    def test_volume_expansion_is_distinct_feature(self):
        self.assertGreater(self.engine.compute(self.candle_map).evidence[-1].volume_expansion, 0.5)

    def test_range_expansion_is_distinct_feature(self):
        self.assertGreater(self.engine.compute(self.candle_map).evidence[-1].range_expansion, 0.5)

    def test_structure_feature_exists(self):
        evidence = self.engine.compute(self.candle_map).evidence[2]
        self.assertGreaterEqual(evidence.structure_score, 0.0)
        self.assertLessEqual(evidence.structure_score, 1.0)

    def test_supertrend_is_evidence_not_authority(self):
        baseline = self.engine.compute(self.candle_map, use_supertrend=False)
        with_st = self.engine.compute(self.candle_map, use_supertrend=True)
        self.assertIn(baseline.opportunity_class, list(OpportunityClass))
        self.assertIn(with_st.opportunity_class, list(OpportunityClass))
        self.assertNotEqual(with_st.supertrend_enabled, baseline.supertrend_enabled)

    def test_supertrend_ab_is_measurable(self):
        baseline = ReplayEvaluation((replay_event(captured=True, accepted=True, return_pct=0.02), replay_event(captured=False, accepted=False, return_pct=-0.01, profitable=False)), 1.0)
        improved = ReplayEvaluation((replay_event(captured=True, accepted=True, return_pct=0.025), replay_event(captured=False, accepted=False, return_pct=-0.01, profitable=False)), 1.0)
        result = ScalpingReplayEvaluator.compare_supertrend(baseline, improved)
        self.assertEqual(result.capture_delta, 0.0)
        self.assertGreaterEqual(result.expectancy_delta, 0.0)

    def test_high_quality_opportunity_does_not_auto_reject(self):
        result = self.decision.decide(candidate(score=95), self.candle_map)
        self.assertIn(result.entry_state, (EntryState.A_PLUS.value, EntryState.A.value, EntryState.B.value, EntryState.C.value))
        self.assertNotEqual(result.entry_state, EntryState.D.value)
        self.assertIsNotNone(result.decision_trace)

    def test_entry_readiness_is_separate_from_opportunity_quality(self):
        result = self.decision.decide(candidate(score=95), {"1d": self.up, "4h": self.up, "1h": self.up, "15m": self.flat})
        self.assertGreaterEqual(result.entry_readiness, 0.0)
        self.assertLess(result.entry_readiness, self.config.a_readiness)
        self.assertEqual(result.entry_state, EntryState.B.value)
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn("opportunity_good_entry_timing_not_ready", result.decision_trace.reasons)

    def test_positive_direction_allows_actionable_long(self):
        result = self.decision.decide(candidate(score=95), {"1d": self.up, "4h": self.up, "1h": self.up, "15m": self.up})
        self.assertGreater(result.directional_evidence, self.config.directional_long_min)
        self.assertTrue(result.decision_trace.directional_evidence > 0)
        self.assertIn(result.entry_state, (EntryState.A_PLUS.value, EntryState.A.value, EntryState.B.value))

    def test_negative_direction_blocks_actionable_long(self):
        bear = candles(start=200, drift=-0.9, volume=100)
        result = self.decision.decide(candidate(score=95), {"1d": bear, "4h": bear, "1h": bear, "15m": bear})
        self.assertLess(result.directional_evidence, -self.config.directional_long_min)
        self.assertNotIn(result.entry_state, (EntryState.A_PLUS.value, EntryState.A.value))
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn(RejectionReason.DIRECTIONAL_CONFLICT, result.decision_trace.rejection_reasons)

    def test_ambiguous_direction_blocks_entry(self):
        result = self.decision.decide(candidate(score=95), {"1d": self.flat, "4h": self.flat, "1h": self.flat, "15m": self.flat})
        self.assertLess(abs(result.directional_evidence), self.config.directional_long_min)
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn(RejectionReason.DIRECTIONAL_INSUFFICIENT, result.decision_trace.rejection_reasons)
        self.assertNotIn(result.entry_state, (EntryState.A_PLUS.value, EntryState.A.value))

    def test_directional_conflict_is_explicitly_traced(self):
        bear = candles(start=200, drift=-0.9, volume=100)
        trace = self.decision.decide(candidate(score=95), {"1d": bear, "4h": bear, "1h": bear, "15m": bear}).decision_trace
        self.assertIn("directional_evidence", trace.measured_features)
        self.assertLess(trace.directional_evidence, 0)
        self.assertIn("directional_conflict_long_entry_blocked", trace.reasons)

    def test_capital_manager_is_only_sizing_authority(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50.0, fixed_allocation=10.0))
        result = self.decision.decide(candidate(score=95), self.candle_map, capital_manager=manager)
        self.assertIsNotNone(result.decision_trace)
        self.assertEqual(manager.desired_allocation(), 10.0)

    def test_capital_rejection_is_traced(self):
        manager = CapitalManager(AllocationConfig(starting_capital=5.0, fixed_allocation=10.0))
        result = self.decision.decide(candidate(score=95), self.candle_map, capital_manager=manager)
        self.assertIn(RejectionReason.CAPITAL, result.decision_trace.rejection_reasons)
        self.assertEqual(result.entry_state, EntryState.D.value)

    def test_pause_rejection_is_traced(self):
        self.assertIn(RejectionReason.PAUSE, self.decision.decide(candidate(), self.candle_map, pause=True).decision_trace.rejection_reasons)

    def test_duplicate_position_rejection_is_traced(self):
        self.assertIn(RejectionReason.DUPLICATE_POSITION, self.decision.decide(candidate(), self.candle_map, active_symbols=("BTCUSDT",)).decision_trace.rejection_reasons)

    def test_market_failure_is_fail_closed_and_observable(self):
        result = self.decision.decide(candidate(), {"1d": self.flat})
        self.assertEqual(result.entry_state, EntryState.D.value)
        self.assertIn(RejectionReason.MARKET_DATA_FAILURE, result.decision_trace.rejection_reasons)

    def test_candidate_pool_default_is_broader_than_active(self):
        self.assertGreater(self.config.broad_pool_top_n, self.config.active_top_n)

    def test_candidate_pool_hysteresis_retains_incumbent(self):
        config = ScalpingConfig(active_top_n=1, broad_pool_top_n=3, hysteresis_score_delta=2.0)
        manager = ScalpingCandidatePoolManager(config)
        broad = OpportunityCandidateSet((candidate("BTCUSDT", 80), candidate("ETHUSDT", 75), candidate("SOLUSDT", 70)), 3)
        first = manager.select(broad, broad.candidates)
        second_input = OpportunityCandidateSet((candidate("BTCUSDT", 79), candidate("ETHUSDT", 80), candidate("SOLUSDT", 70)), 3)
        second = manager.select(second_input, second_input.candidates)
        self.assertEqual(first.active_set.symbols(), ("BTCUSDT",))
        self.assertEqual(second.active_set.symbols(), ("BTCUSDT",))

    def test_candidate_pool_separates_broad_and_active_deterministically(self):
        config = ScalpingConfig(active_top_n=2, broad_pool_top_n=4)
        manager = ScalpingCandidatePoolManager(config)
        broad = OpportunityCandidateSet((candidate("BTCUSDT", 80), candidate("ETHUSDT", 70), candidate("ADAUSDT", 60), candidate("SOLUSDT", 50)), 4)
        result = manager.select(broad, broad.candidates)
        self.assertGreater(len(result.broad_pool.candidates), len(result.active_set.candidates))
        self.assertEqual(result.broad_pool.symbols(), ("BTCUSDT", "ETHUSDT", "ADAUSDT", "SOLUSDT"))
        self.assertEqual(result.active_set.symbols(), ("BTCUSDT", "ETHUSDT"))

    def test_decision_trace_contains_all_required_answers(self):
        trace = self.decision.decide(candidate(score=95), self.candle_map).decision_trace
        self.assertTrue(trace.discovered)
        self.assertTrue(trace.eligible)
        self.assertGreater(len(trace.measured_features), 5)
        self.assertIsInstance(trace.opportunity_class, OpportunityClass)
        self.assertGreaterEqual(trace.opportunity_score, 0.0)
        self.assertLessEqual(trace.opportunity_score, 100.0)
        self.assertIsNotNone(trace.entry_state)

    def test_replay_metrics_are_semantically_grounded(self):
        events = (
            replay_event(captured=True, accepted=True, return_pct=0.02, hold_hours=2.0, costs_pct=0.20, utilization_pct=40.0, profitable=True),
            replay_event(captured=True, accepted=False, return_pct=0.05, hold_hours=4.0, costs_pct=0.00, utilization_pct=10.0, profitable=True),
            replay_event(captured=False, accepted=False, return_pct=-0.01, hold_hours=0.0, costs_pct=0.00, utilization_pct=0.0, profitable=False),
        )
        metrics = ScalpingReplayEvaluator.metrics(ReplayEvaluation(events, 2.0))
        self.assertAlmostEqual(metrics.opportunity_capture_rate, 2 / 3)
        self.assertAlmostEqual(metrics.entry_acceptance_rate, 1 / 2)
        self.assertAlmostEqual(metrics.trades_per_day, 0.5)
        self.assertAlmostEqual(metrics.average_hold_time, 2.0)
        self.assertAlmostEqual(metrics.fees_slippage_impact, 0.20)
        self.assertAlmostEqual(metrics.capital_utilization, 50.0 / 3.0)
        self.assertAlmostEqual(metrics.false_negative_rate, 0.5)
        self.assertTrue(math.isfinite(metrics.expectancy))

    def test_replay_trades_per_day_changes_with_time_basis(self):
        events = (replay_event(), replay_event(accepted=False, return_pct=-0.01, profitable=False))
        short = ScalpingReplayEvaluator.metrics(ReplayEvaluation(events, 1.0))
        long = ScalpingReplayEvaluator.metrics(ReplayEvaluation(events, 4.0))
        self.assertNotEqual(short.trades_per_day, long.trades_per_day)
        self.assertAlmostEqual(short.trades_per_day, 1.0)
        self.assertAlmostEqual(long.trades_per_day, 0.25)

    def test_replay_costs_change_fees_slippage_metric(self):
        low = ScalpingReplayEvaluator.metrics(ReplayEvaluation((replay_event(costs_pct=0.10),), 1.0))
        high = ScalpingReplayEvaluator.metrics(ReplayEvaluation((replay_event(costs_pct=0.40),), 1.0))
        self.assertLess(low.fees_slippage_impact, high.fees_slippage_impact)

    def test_replay_hold_durations_change_average_hold_time(self):
        short = ScalpingReplayEvaluator.metrics(ReplayEvaluation((replay_event(hold_hours=1.0),), 1.0))
        long = ScalpingReplayEvaluator.metrics(ReplayEvaluation((replay_event(hold_hours=5.0),), 1.0))
        self.assertLess(short.average_hold_time, long.average_hold_time)

    def test_replay_missed_profitable_opportunities_change_false_negative(self):
        none = ScalpingReplayEvaluator.metrics(ReplayEvaluation((replay_event(accepted=True, profitable=True),), 1.0))
        missed = ScalpingReplayEvaluator.metrics(ReplayEvaluation((replay_event(accepted=False, profitable=True),), 1.0))
        self.assertLess(none.false_negative_rate, missed.false_negative_rate)
        self.assertEqual(missed.false_negative_rate, 1.0)

    def test_replay_profit_factor_is_deterministic_for_undefined_loss_side(self):
        metrics = ScalpingReplayEvaluator.metrics(ReplayEvaluation((replay_event(return_pct=0.02),), 1.0))
        self.assertTrue(math.isinf(metrics.profit_factor))
        self.assertGreater(metrics.profit_factor, 0)

    def test_replay_metrics_same_inputs_are_deterministic(self):
        evaluation = ReplayEvaluation((replay_event(), replay_event(return_pct=-0.01, accepted=False, profitable=False)), 2.0)
        self.assertEqual(ScalpingReplayEvaluator.metrics(evaluation), ScalpingReplayEvaluator.metrics(evaluation))

    def test_ab_comparison_is_deterministic(self):
        baseline = ReplayEvaluation((replay_event(), replay_event(return_pct=-0.01, accepted=False, profitable=False)), 1.0)
        improved = ReplayEvaluation((replay_event(return_pct=0.025, costs_pct=0.05), replay_event(return_pct=0.01, costs_pct=0.05)), 1.0)
        self.assertEqual(ScalpingReplayEvaluator.compare(baseline, improved), ScalpingReplayEvaluator.compare(baseline, improved))

    def test_no_live_or_execution_imports(self):
        source = __import__("inspect").getsource(__import__("services.scalping_opportunity", fromlist=["x"]))
        tree = ast.parse(source)
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        text = "\n".join(ast.unparse(node) for node in imports).lower()
        self.assertFalse(any(token in text for token in ("execution", "live", "paper")))


if __name__ == "__main__":
    unittest.main()
