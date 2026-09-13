from __future__ import annotations

import unittest

from models.opportunity import MarketMetrics, OpportunityCandidate
from models.scalping_opportunity import (
    DecisionTrace,
    EntryState,
    OpportunityClass,
    RiskReward,
    TimeframeEvidence,
)
from services.scalping_opportunity import (
    OpportunityThroughputMetrics,
    ScalpingConfig,
    ScalpingDecisionEngine,
    ScalpingEvidenceEngine,
    ScalpingFeatures,
    measure_opportunity_throughput,
)
from services.scalping_pipeline import FastRecall


class StubFeatureEngine:
    def __init__(self, features: ScalpingFeatures) -> None:
        self._features = features

    def compute(self, candle_map, *, use_supertrend=False):
        return self._features


class D1ThroughputEvidenceBandTests(unittest.TestCase):
    @staticmethod
    def candidate(symbol: str, discovery_score: float) -> OpportunityCandidate:
        return OpportunityCandidate(symbol, discovery_score, 1, MarketMetrics(symbol, 100_000_000.0, 0.08, 8.0, True, 100.0), ())

    @staticmethod
    def evidence(value: float = 1.0) -> dict[str, TimeframeEvidence]:
        return {tf: TimeframeEvidence(tf, value, value, value, value, value, value, value, value, value, value, 1.0) for tf in ("1d", "4h", "1h", "15m")}

    @staticmethod
    def features(cls: OpportunityClass, score: float, timing: float = 0.9, direction: float = 0.8) -> ScalpingFeatures:
        return ScalpingFeatures(tuple(D1ThroughputEvidenceBandTests.evidence().values()), direction, cls, score, timing, RiskReward(100.0, 98.0, 104.0, 2.0, 4.0, 2.0, True), False)

    def test_class_specific_breakout_evidence_can_reach_a_plus(self):
        engine = ScalpingEvidenceEngine(ScalpingConfig())
        score = engine._class_score(self.evidence(), OpportunityClass.BREAKOUT_ACCELERATION, 0.8)
        self.assertGreaterEqual(score, 80.0)
        self.assertEqual(score, 98.4)

    def test_class_specific_trend_evidence_can_reach_a_plus(self):
        engine = ScalpingEvidenceEngine(ScalpingConfig())
        score = engine._class_score(self.evidence(), OpportunityClass.TREND_CONTINUATION, 0.8)
        self.assertGreaterEqual(score, 80.0)

    def test_class_specific_pullback_evidence_can_reach_a_plus(self):
        engine = ScalpingEvidenceEngine(ScalpingConfig())
        score = engine._class_score(self.evidence(), OpportunityClass.PULLBACK_CONTINUATION, 0.8)
        self.assertGreaterEqual(score, 80.0)

    def test_high_discovery_score_does_not_authorize_weak_entry_evidence(self):
        engine = ScalpingDecisionEngine(ScalpingConfig())
        engine.features = StubFeatureEngine(self.features(OpportunityClass.BREAKOUT_ACCELERATION, 50.0))
        result = engine.decide(self.candidate("ONTUSDT", 91.21), {"synthetic": ()})
        self.assertEqual(result.entry_state, EntryState.C.value)
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn("discovery_quality_does_not_authorize_entry", result.decision_trace.reasons)

    def test_moderate_discovery_with_strong_class_specific_entry_evidence_is_actionable(self):
        engine = ScalpingDecisionEngine(ScalpingConfig())
        engine.features = StubFeatureEngine(self.features(OpportunityClass.BREAKOUT_ACCELERATION, 85.0))
        result = engine.decide(self.candidate("MSTRUSDT", 46.0), {"synthetic": ()})
        self.assertEqual(result.entry_state, EntryState.A_PLUS.value)
        self.assertTrue(result.decision_trace.entry_allowed)

    def test_unclassified_strong_direction_and_rr_remains_safe_and_explainable(self):
        engine = ScalpingDecisionEngine(ScalpingConfig())
        engine.features = StubFeatureEngine(self.features(OpportunityClass.UNCLASSIFIED, 95.0))
        result = engine.decide(self.candidate("ONTUSDT", 91.21), {"synthetic": ()})
        self.assertEqual(result.entry_state, EntryState.C.value)
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn("classification_insufficient_evidence", result.decision_trace.reasons)

    def test_repeated_identical_input_produces_identical_trace(self):
        engine = ScalpingDecisionEngine(ScalpingConfig())
        engine.features = StubFeatureEngine(self.features(OpportunityClass.TREND_CONTINUATION, 85.0))
        candidate = self.candidate("TRENDUSDT", 60.0)
        self.assertEqual(engine.decide(candidate, {"synthetic": ()}).decision_trace, engine.decide(candidate, {"synthetic": ()}).decision_trace)

    def test_high_risk_hot_symbol_remains_visible_to_recall(self):
        result = FastRecall(lane_top_n=1, composite_top_n=1).recall([self.candidate("NEWVOLUSDT", 5.0)], broad_limit=10)
        self.assertEqual(tuple(c.symbol for c in result.candidates), ("NEWVOLUSDT",))
        self.assertIn("composite_opportunity", dict(result.provenance)["NEWVOLUSDT"])

    def test_throughput_metrics_are_deterministic_across_market_regimes(self):
        allowed = []
        for symbol in ("B1", "T1", "P1"):
            trace = DecisionTrace(True, True, (), OpportunityClass.BREAKOUT_ACCELERATION, 85.0, 0.8, EntryState.A_PLUS, True, (), ("class_evidence_band:breakout_acceleration",))
            allowed.append(OpportunityCandidate(symbol, 70.0, 1, MarketMetrics(symbol, 100_000_000.0, .03), (), decision_trace=trace))
        regimes = {"B1": "breakout_burst", "T1": "broad_trending", "P1": "post_breakout_pullback"}
        metrics = measure_opportunity_throughput(allowed, expected_strong_symbols=("B1", "T1", "P1"), market_regimes=regimes)
        self.assertIsInstance(metrics, OpportunityThroughputMetrics)
        self.assertEqual(metrics.opportunity_recall_rate, 1.0)
        self.assertEqual(metrics.false_negative_rate, 0.0)
        self.assertEqual(metrics.entries_per_market_regime, (("breakout_burst", 1), ("broad_trending", 1), ("post_breakout_pullback", 1)))
        self.assertEqual(metrics, measure_opportunity_throughput(allowed, expected_strong_symbols=("B1", "T1", "P1"), market_regimes=regimes))

    def test_quiet_market_has_no_forced_trade_quota(self):
        metrics = measure_opportunity_throughput((), expected_strong_symbols=(), market_regimes={})
        self.assertEqual(metrics.opportunity_recall_rate, 0.0)
        self.assertEqual(metrics.actionable_opportunity_rate, 0.0)
        self.assertEqual(metrics.false_negative_rate, 0.0)
        self.assertEqual(metrics.entries_per_market_regime, ())


if __name__ == "__main__":
    unittest.main()
