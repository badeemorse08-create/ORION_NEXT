from __future__ import annotations

import unittest
from unittest.mock import Mock

from models.capital_management import AllocationConfig, CapitalManager
from models.opportunity import MarketMetrics, OpportunityCandidate
from models.scalping_opportunity import (
    DecisionTrace,
    EntryState,
    OpportunityClass,
    RejectionReason,
    RiskReward,
    TimeframeEvidence,
)
from services.scalping_opportunity import ScalpingConfig, ScalpingDecisionEngine, ScalpingFeatures


def candidate(symbol: str, discovery_score: float) -> OpportunityCandidate:
    return OpportunityCandidate(
        symbol,
        discovery_score,
        1,
        MarketMetrics(symbol, 100_000_000.0, 0.02, 2.0, True, 100.0),
        (),
        (("discovery_quality", discovery_score),),
    )


def class_specific_evidence() -> tuple[TimeframeEvidence, ...]:
    return (
        TimeframeEvidence("1d", .60, .40, .70, .40, .70, .30, .40, .40, .50, 1.0, 1.0),
        TimeframeEvidence("4h", .60, .50, .70, .45, .70, .35, .45, .45, .55, 1.0, 1.0),
        TimeframeEvidence("1h", .60, .60, .75, .60, .75, .60, .60, .60, .65, 1.0, 1.0),
        TimeframeEvidence("15m", .60, .45, .65, .60, .75, .65, .75, .75, .70, 1.0, 1.0),
    )


def features(score: float, *, timing: float = .80, directional: float = .80) -> ScalpingFeatures:
    rr = RiskReward(100.0, 98.0, 104.0, 2.0, 4.0, 2.0, True)
    return ScalpingFeatures(
        class_specific_evidence(),
        directional,
        OpportunityClass.BREAKOUT_ACCELERATION,
        score,
        timing,
        rr,
        False,
    )


class D1EntryStateScoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ScalpingDecisionEngine(ScalpingConfig())

    def _engine_with(self, computed: ScalpingFeatures) -> ScalpingDecisionEngine:
        self.engine.features.compute = Mock(return_value=computed)
        return self.engine

    def test_c_never_allows_entry(self) -> None:
        result = self._engine_with(features(60.0)).decide(candidate("ONTUSDT", 91.21), {})
        self.assertEqual(result.entry_state, EntryState.C.value)
        self.assertFalse(result.decision_trace.entry_allowed)
        self.assertIn(RejectionReason.ENTRY_STATE_CONFLICT, result.decision_trace.rejection_reasons)

    def test_d_never_allows_entry(self) -> None:
        manager = CapitalManager(AllocationConfig(starting_capital=5.0, fixed_allocation=10.0))
        result = self._engine_with(features(85.0)).decide(candidate("MSTRUSDT", 89.15), {}, capital_manager=manager)
        self.assertEqual(result.entry_state, EntryState.D.value)
        self.assertFalse(result.decision_trace.entry_allowed)

    def test_valid_class_specific_opportunity_reaches_a_plus(self) -> None:
        result = self._engine_with(features(85.0)).decide(candidate("MSTRUSDT", 89.15), {})
        self.assertEqual(result.opportunity_class, OpportunityClass.BREAKOUT_ACCELERATION.value)
        self.assertEqual(result.entry_state, EntryState.A_PLUS.value)
        self.assertTrue(result.decision_trace.entry_allowed)
        self.assertEqual(result.decision_trace.opportunity_score, 85.0)

    def test_discovery_score_does_not_authorize_entry(self) -> None:
        low_scalping = self._engine_with(features(60.0)).decide(candidate("ONTUSDT", 99.0), {})
        high_scalping = self._engine_with(features(85.0)).decide(candidate("ONTUSDT", 99.0), {})
        self.assertEqual(low_scalping.entry_state, EntryState.C.value)
        self.assertFalse(low_scalping.decision_trace.entry_allowed)
        self.assertEqual(high_scalping.entry_state, EntryState.A_PLUS.value)
        self.assertTrue(high_scalping.decision_trace.entry_allowed)

    def test_discovery_and_scalping_score_contracts_are_separate(self) -> None:
        high_discovery = self._engine_with(features(85.0)).decide(candidate("MSTRUSDT", 89.15), {})
        low_discovery = self._engine_with(features(85.0)).decide(candidate("MSTRUSDT", 46.0), {})
        self.assertEqual(high_discovery.decision_trace.opportunity_score, 85.0)
        self.assertEqual(low_discovery.decision_trace.opportunity_score, 85.0)
        self.assertEqual(high_discovery.entry_state, low_discovery.entry_state)
        self.assertEqual(high_discovery.decision_trace.entry_allowed, low_discovery.decision_trace.entry_allowed)
        self.assertEqual(high_discovery.opportunity_score, 89.15)
        self.assertEqual(low_discovery.opportunity_score, 46.0)

    def test_internal_score_scale_mismatch_does_not_reject_valid_entry(self) -> None:
        result = self._engine_with(features(85.0)).decide(candidate("MSTRUSDT", 46.0), {})
        self.assertEqual(result.entry_state, EntryState.A_PLUS.value)
        self.assertTrue(result.decision_trace.entry_allowed)
        self.assertEqual(result.decision_trace.opportunity_score, 85.0)

    def test_repeated_identical_input_has_identical_decision_trace(self) -> None:
        engine = self._engine_with(features(85.0))
        first = engine.decide(candidate("ONTUSDT", 91.21), {}).decision_trace
        second = engine.decide(candidate("ONTUSDT", 91.21), {}).decision_trace
        self.assertEqual(first, second)

    def test_decision_trace_contract_normalizes_c_and_d(self) -> None:
        for state in (EntryState.C, EntryState.D):
            trace = DecisionTrace(
                True, True, (), OpportunityClass.BREAKOUT_ACCELERATION, 85.0, .8,
                state, True, (), (),
            )
            self.assertFalse(trace.entry_allowed)
            self.assertIn(RejectionReason.ENTRY_STATE_CONFLICT, trace.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
