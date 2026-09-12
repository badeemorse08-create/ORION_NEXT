from __future__ import annotations

import unittest

from models.opportunity import MarketMetrics, OpportunityCandidate
from models.scalping_opportunity import DecisionTrace, EntryState, OpportunityClass, RejectionReason, TimeframeEvidence
from services.scalping_opportunity import ScalpingConfig, ScalpingDecisionEngine
from services.scalping_pipeline import FastRecall, ScalpingOpportunityPipeline


class FastRecallTests(unittest.TestCase):
    @staticmethod
    def candidate(symbol: str, score: float, **metrics) -> OpportunityCandidate:
        return OpportunityCandidate(symbol, score, 0, MarketMetrics(symbol, 100_000_000.0, 0.02, 2.0, True, 100.0, **metrics), ())

    def test_strong_mover_is_recalled_below_composite_cutoff(self):
        result = FastRecall(lane_top_n=1, composite_top_n=1).recall([self.candidate("LEADER", 95), self.candidate("HOT", 10, price_change_pct_24h=42)], broad_limit=2)
        self.assertIn("HOT", tuple(x.symbol for x in result.candidates))
        self.assertIn("high_mover", dict(result.provenance)["HOT"])

    def test_short_term_acceleration_lane(self):
        result = FastRecall(lane_top_n=1, composite_top_n=1).recall([self.candidate("QUIET", 95), self.candidate("ACCEL", 5, short_term_acceleration=9)], broad_limit=2)
        self.assertIn("short_term_acceleration", dict(result.provenance)["ACCEL"])

    def test_volume_expansion_lane(self):
        result = FastRecall(lane_top_n=1, composite_top_n=1).recall([self.candidate("QUIET", 95), self.candidate("VOLUME", 5, volume_expansion=8)], broad_limit=2)
        self.assertIn("volume_expansion", dict(result.provenance)["VOLUME"])

    def test_breakout_range_expansion_lane(self):
        result = FastRecall(lane_top_n=1, composite_top_n=1).recall([self.candidate("QUIET", 95), self.candidate("BREAK", 5, range_expansion=8)], broad_limit=2)
        self.assertIn("breakout_range_expansion", dict(result.provenance)["BREAK"])

    def test_duplicate_symbol_is_recalled_once_with_multiple_lanes(self):
        result = FastRecall(lane_top_n=1, composite_top_n=1).recall([self.candidate("HOT", 5, price_change_pct_24h=50, short_term_acceleration=10, volume_expansion=10, range_expansion=10)], broad_limit=10)
        self.assertEqual(tuple(x.symbol for x in result.candidates), ("HOT",))
        self.assertEqual(len(dict(result.provenance)["HOT"]), 5)

    def test_recall_provenance_is_deterministic_and_counts_are_observable(self):
        recall = FastRecall(lane_top_n=1, composite_top_n=1)
        candidates = [self.candidate("A", 80, price_change_pct_24h=20), self.candidate("B", 60)]
        first = recall.recall(candidates, broad_limit=2)
        second = recall.recall(candidates, broad_limit=2)
        self.assertEqual(first.provenance, second.provenance)
        self.assertEqual(first.counts, second.counts)

    def test_31_day_context_is_not_required_for_recall(self):
        result = FastRecall(lane_top_n=1, composite_top_n=1).recall([self.candidate("HOT", 1, price_change_pct_24h=44)], broad_limit=10)
        self.assertEqual(result.candidates[0].symbol, "HOT")


class ClassificationIntegrityTests(unittest.TestCase):
    @staticmethod
    def evidence(**overrides):
        base = {k: dict(regime_score=.2, trend_score=.2, trend_direction=.1, momentum_score=.2, momentum_direction=.1, acceleration_score=.1, volume_expansion=.1, range_expansion=.1, structure_score=.2, supertrend_evidence=0.0, atr=1.0) for k in ("1d", "4h", "1h", "15m")}
        for tf, values in overrides.items():
            base[tf].update(values)
        return tuple(TimeframeEvidence(tf, **base[tf]) for tf in ("1d", "4h", "1h", "15m"))

    def setUp(self):
        self.pipeline = ScalpingOpportunityPipeline.__new__(ScalpingOpportunityPipeline)
        self.pipeline.decision_engine = ScalpingDecisionEngine(ScalpingConfig())

    def candidate(self, state=EntryState.A, allowed=False, score=75.0, readiness=.7, directional=.3, evidence=None):
        trace = DecisionTrace(True, True, ("trend", "momentum"), OpportunityClass.UNCLASSIFIED, score, directional, state, allowed, (), ("unclassified",))
        return OpportunityCandidate("TESTUSDT", score, 1, MarketMetrics("TESTUSDT", 100_000_000, .02), (), (), directional, "UNCLASSIFIED", state.value, readiness, None, evidence or self.evidence(), trace)

    def test_classifiable_trend_continuation(self):
        evidence = self.evidence(**{"1d": {"regime_score": .6}, "4h": {"trend_score": .6}, "1h": {"trend_score": .6, "momentum_score": .5}})
        result = self.pipeline._classification_integrity(self.candidate(evidence=evidence))
        self.assertEqual(result.opportunity_class, OpportunityClass.TREND_CONTINUATION.value)

    def test_classifiable_breakout(self):
        evidence = self.evidence(**{"1h": {"acceleration_score": .5}, "15m": {"volume_expansion": .6, "range_expansion": .6}})
        result = self.pipeline._classification_integrity(self.candidate(evidence=evidence))
        self.assertEqual(result.opportunity_class, OpportunityClass.BREAKOUT_ACCELERATION.value)

    def test_classifiable_pullback(self):
        evidence = self.evidence(**{"1h": {"trend_score": .6}, "15m": {"momentum_score": .4, "structure_score": .5, "acceleration_score": .3}})
        result = self.pipeline._classification_integrity(self.candidate(evidence=evidence))
        self.assertEqual(result.opportunity_class, OpportunityClass.PULLBACK_CONTINUATION.value)

    def test_high_quality_non_classifiable_is_unclassified(self):
        result = self.pipeline._classification_integrity(self.candidate(score=95, readiness=.95, directional=.5))
        self.assertEqual(result.opportunity_class, OpportunityClass.UNCLASSIFIED.value)
        self.assertEqual(result.entry_state, EntryState.C.value)
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn(RejectionReason.CLASSIFICATION_INSUFFICIENT, result.decision_trace.rejection_reasons)

    def test_c_can_never_be_actionable(self):
        result = self.pipeline._classification_integrity(self.candidate(state=EntryState.C, allowed=True))
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn(RejectionReason.ENTRY_STATE_CONFLICT, result.decision_trace.rejection_reasons)

    def test_d_can_never_be_actionable(self):
        result = self.pipeline._classification_integrity(self.candidate(state=EntryState.D, allowed=True))
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn(RejectionReason.ENTRY_STATE_CONFLICT, result.decision_trace.rejection_reasons)

    def test_repeated_classification_is_deterministic(self):
        candidate = self.candidate()
        self.assertEqual(self.pipeline._classification_integrity(candidate), self.pipeline._classification_integrity(candidate))


if __name__ == "__main__":
    unittest.main()
