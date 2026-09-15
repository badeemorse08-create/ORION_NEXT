"""
ORION GAP-VG-002: executable single-scenario Core Intelligence evidence.

This test uses the real ValidationEngine, IndicatorEngine, AnalysisEngine,
ProfileEngine, ScoreEngine, and DecisionEngine. The deterministic fixture
contains no mocks or patches and proves the canonical graph plus the parallel
Profile boundary.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

from enums import DataHealth, Timeframe
from engines.analysis_engine import AnalysisEngine
from engines.decision_engine import DecisionEngine
from engines.indicator_engine import IndicatorEngine
from engines.profile_engine import ProfileEngine
from engines.score_engine import ScoreEngine
from engines.validation_engine import ValidationEngine, ValidationStatus
from models.analysis import AnalysisResult
from models.decision import DecisionResult
from models.market import MarketDataset, MarketMetadata, TimeframeData
from models.profile import ProfileResult
from models.score import ScoreResult


class TestCoreIntelligenceSingleScenario(unittest.TestCase):
    """Prove one deterministic dataset traverses the real Core graph."""

    @staticmethod
    def build_deterministic_dataset() -> MarketDataset:
        """Build deterministic OHLCV input without replacing any Core engine."""
        index = pd.date_range(
            "2025-01-01T00:00:00Z",
            periods=240,
            freq="D",
        )
        close = pd.Series(
            [100.0 + (float(i) * 0.5) for i in range(len(index))],
            index=index,
            dtype=float,
        )
        dataframe = pd.DataFrame(
            {
                "open": close - 0.25,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1000.0,
            },
            index=index,
        )
        metadata = MarketMetadata(
            symbol="ORIONTESTUSDT",
            exchange="TEST",
            source="deterministic-fixture",
            cache_version="gap-vg-002-v2",
            downloaded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            last_updated_at=datetime(2025, 8, 28, tzinfo=timezone.utc),
            is_valid=True,
        )
        timeframe_data = TimeframeData(
            timeframe=Timeframe.D1,
            dataframe=dataframe,
            data_health=DataHealth.EXCELLENT,
            candles_count=len(dataframe),
            first_timestamp=index[0].to_pydatetime(),
            last_timestamp=index[-1].to_pydatetime(),
        )
        dataset = MarketDataset(metadata=metadata)
        dataset.add_timeframe(timeframe_data)
        return dataset

    @staticmethod
    def profile_signature(profile: ProfileResult) -> tuple[object, ...]:
        """Return deterministic Profile evidence excluding generated_at."""
        timeframe = profile.timeframes[0]
        return (
            profile.symbol,
            profile.timeframe_count,
            profile.is_tradeable,
            profile.has_blocks,
            profile.market.trend,
            profile.market.ema_alignment,
            round(profile.market.confidence, 8),
            timeframe.timeframe,
            timeframe.candles_count,
        )

    @staticmethod
    def analysis_signature(analysis: AnalysisResult) -> tuple[object, ...]:
        """Return deterministic Analysis evidence."""
        return (
            analysis.market_state,
            analysis.strength,
            tuple(analysis.signals),
            tuple(analysis.warnings),
        )

    @staticmethod
    def score_signature(score: ScoreResult) -> tuple[object, ...]:
        """Return deterministic Score evidence."""
        return (
            score.score,
            score.category,
            tuple(score.factors),
            tuple(score.warnings),
        )

    @staticmethod
    def decision_signature(decision: DecisionResult) -> tuple[object, ...]:
        """Return deterministic Decision evidence."""
        return (
            decision.decision,
            decision.confidence,
            tuple(decision.reasons),
            tuple(decision.warnings),
        )

    def test_real_core_intelligence_single_scenario(self) -> None:
        """
        Prove the real execution graph:

            MarketDataset -> Validation -> Indicators -> Analysis -> Score -> Decision
                                  \
                                   -> Profile -> independent ProfileResult
        """
        dataset = self.build_deterministic_dataset()
        original_frame = dataset.get_timeframe(Timeframe.D1).dataframe.copy()

        validation_engine = ValidationEngine()
        indicator_engine = IndicatorEngine()
        analysis_engine = AnalysisEngine(default_timeframe=Timeframe.D1)
        profile_engine = ProfileEngine()
        score_engine = ScoreEngine()
        decision_engine = DecisionEngine()

        # Concrete production classes only: no mocks, patches, or substitutes.
        self.assertIs(type(validation_engine), ValidationEngine)
        self.assertIs(type(indicator_engine), IndicatorEngine)
        self.assertIs(type(analysis_engine), AnalysisEngine)
        self.assertIs(type(profile_engine), ProfileEngine)
        self.assertIs(type(score_engine), ScoreEngine)
        self.assertIs(type(decision_engine), DecisionEngine)

        # 1. MarketDataset -> Validation.
        validation = validation_engine.validate_dataset(dataset)
        self.assertEqual(validation.status, ValidationStatus.PASSED)
        self.assertTrue(validation.passed)
        self.assertGreater(validation.checks, 0)

        # Parallel branch: the real ProfileEngine consumes the same MarketDataset
        # and produces an independent ProfileResult without mutating market data.
        profile = profile_engine.build_profile(dataset)
        self.assertIsInstance(profile, ProfileResult)
        self.assertIsNot(profile, dataset)
        self.assertEqual(profile.symbol, dataset.symbol)
        self.assertEqual(profile.timeframe_count, 1)
        self.assertTrue(profile.is_tradeable)
        self.assertFalse(profile.has_blocks)
        self.assertGreater(profile.market.confidence, 0.0)
        self.assertEqual(profile.timeframes[0].timeframe, Timeframe.D1.value)
        self.assertEqual(profile.timeframes[0].candles_count, len(original_frame))
        self.assertIsNot(profile.market, dataset)
        self.assertTrue(
            dataset.get_timeframe(Timeframe.D1).dataframe[
                ["open", "high", "low", "close", "volume"]
            ].equals(original_frame)
        )

        # 2. Validation -> Indicators.
        prepared_dataset = indicator_engine.calculate_dataset(dataset)
        self.assertIs(prepared_dataset, dataset)
        prepared_frame = prepared_dataset.get_timeframe(Timeframe.D1).dataframe
        for column in AnalysisEngine.REQUIRED_INDICATORS:
            self.assertIn(column, prepared_frame.columns)
        self.assertFalse(
            prepared_frame.loc[
                prepared_frame.index[-1],
                list(AnalysisEngine.REQUIRED_INDICATORS),
            ].isna().any()
        )

        # 3. Indicators -> Analysis.
        analysis = analysis_engine.analyze(prepared_dataset)
        self.assertIsInstance(analysis, AnalysisResult)
        self.assertEqual(analysis.market_state, "BULLISH")
        self.assertGreater(analysis.strength, 0.0)
        self.assertIn("EMA_ALIGNMENT_BULLISH", analysis.signals)
        self.assertIn("MOMENTUM_POSITIVE", analysis.signals)
        self.assertIn("STRONG_TREND", analysis.signals)

        # 4. AnalysisResult -> ScoreResult. ProfileResult is intentionally not
        # passed here because ScoreEngine's canonical contract consumes AnalysisResult.
        score = score_engine.calculate(analysis)
        self.assertIsInstance(score, ScoreResult)
        self.assertGreaterEqual(score.score, -100.0)
        self.assertLessEqual(score.score, 100.0)
        self.assertIn("EMA_ALIGNMENT_BULLISH", score.factors)

        # 5. AnalysisResult + ScoreResult -> DecisionResult.
        decision = decision_engine.decide(analysis, score)
        self.assertIsInstance(decision, DecisionResult)
        self.assertEqual(decision.decision, "WAIT")
        self.assertEqual(decision.confidence, abs(score.score))
        self.assertIn("EMA_ALIGNMENT_BULLISH", decision.reasons)
        self.assertIn("MOMENTUM_POSITIVE", decision.reasons)
        self.assertIn("STRONG_TREND", decision.reasons)
        self.assertIsNotNone(analysis)
        self.assertIsNotNone(score)

        # Determinism: repeat the complete real computation on a fresh identical
        # dataset and require stable semantic outputs (excluding Profile.generated_at).
        dataset_2 = self.build_deterministic_dataset()
        validation_2 = ValidationEngine().validate_dataset(dataset_2)
        profile_2 = ProfileEngine().build_profile(dataset_2)
        IndicatorEngine().calculate_dataset(dataset_2)
        analysis_2 = AnalysisEngine(default_timeframe=Timeframe.D1).analyze(dataset_2)
        score_2 = ScoreEngine().calculate(analysis_2)
        decision_2 = DecisionEngine().decide(analysis_2, score_2)

        self.assertEqual(validation.status, validation_2.status)
        self.assertEqual(self.profile_signature(profile), self.profile_signature(profile_2))
        self.assertEqual(self.analysis_signature(analysis), self.analysis_signature(analysis_2))
        self.assertEqual(self.score_signature(score), self.score_signature(score_2))
        self.assertEqual(self.decision_signature(decision), self.decision_signature(decision_2))

        # The indicator stage may add derived columns, but canonical OHLCV values
        # remain unchanged and ProfileResult stays independent from MarketDataset.
        self.assertEqual(len(prepared_frame), len(original_frame))
        self.assertTrue(
            prepared_frame[["open", "high", "low", "close", "volume"]].equals(original_frame)
        )


if __name__ == "__main__":
    unittest.main()
