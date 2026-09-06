"""
ORION GAP-VG-002: executable single-scenario Core Intelligence evidence.

This test deliberately uses the real ValidationEngine, IndicatorEngine,
AnalysisEngine, ProfileEngine, ScoreEngine, and DecisionEngine.  The fixture
is deterministic and contains no mocks or patches for Core Intelligence.
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
    """Prove one deterministic dataset traverses the real Core chain."""

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
            cache_version="gap-vg-002-v1",
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

    def test_real_core_intelligence_single_scenario(self) -> None:
        """MarketDataset -> Validation -> Indicators -> Analysis -> Profile -> Score -> Decision."""
        dataset = self.build_deterministic_dataset()
        original_frame = dataset.get_timeframe(Timeframe.D1).dataframe.copy()

        validation_engine = ValidationEngine()
        indicator_engine = IndicatorEngine()
        analysis_engine = AnalysisEngine(default_timeframe=Timeframe.D1)
        profile_engine = ProfileEngine()
        score_engine = ScoreEngine()
        decision_engine = DecisionEngine()

        # 1. MarketDataset -> Validation (real ValidationEngine)
        validation = validation_engine.validate_dataset(dataset)
        self.assertEqual(validation.status, ValidationStatus.PASSED)
        self.assertTrue(validation.passed)
        self.assertGreater(validation.checks, 0)

        # 2. Validation -> Indicators (real IndicatorEngine)
        prepared_dataset = indicator_engine.calculate_dataset(dataset)
        self.assertIs(prepared_dataset, dataset)
        prepared_frame = prepared_dataset.get_timeframe(Timeframe.D1).dataframe
        for column in AnalysisEngine.REQUIRED_INDICATORS:
            self.assertIn(column, prepared_frame.columns)
        self.assertFalse(prepared_frame.loc[prepared_frame.index[-1], list(AnalysisEngine.REQUIRED_INDICATORS)].isna().any())

        # 3. Indicators -> Analysis (real AnalysisEngine)
        analysis = analysis_engine.analyze(prepared_dataset)
        self.assertIsInstance(analysis, AnalysisResult)
        self.assertEqual(analysis.market_state, "BULLISH")
        self.assertGreater(analysis.strength, 0.0)
        self.assertIn("EMA_ALIGNMENT_BULLISH", analysis.signals)

        # 4. Analysis -> Profile (real ProfileEngine)
        profile = profile_engine.build_profile(prepared_dataset)
        self.assertIsInstance(profile, ProfileResult)
        self.assertIsNot(profile, prepared_dataset)
        self.assertEqual(profile.symbol, prepared_dataset.symbol)
        self.assertEqual(profile.timeframe_count, 1)
        self.assertTrue(profile.is_tradeable)
        self.assertFalse(profile.has_blocks)
        self.assertGreater(profile.market.confidence, 0.0)
        self.assertEqual(profile.timeframes[0].timeframe, Timeframe.D1.value)
        self.assertIsNot(profile.market, prepared_dataset)

        # 5. Profile/Analysis evidence -> Score (real ScoreEngine)
        score = score_engine.calculate(analysis)
        self.assertIsInstance(score, ScoreResult)
        self.assertGreaterEqual(score.score, -100.0)
        self.assertLessEqual(score.score, 100.0)
        self.assertIn("EMA_ALIGNMENT_BULLISH", score.factors)

        # 6. Analysis + Score -> Decision (real DecisionEngine)
        decision = decision_engine.decide(analysis, score)
        self.assertIsInstance(decision, DecisionResult)
        self.assertEqual(decision.decision, "FAVORABLE")
        self.assertGreater(decision.confidence, 0.0)
        self.assertIn("EMA_ALIGNMENT_BULLISH", decision.reasons)

        # The indicator stage is the only stage above that mutates its explicit
        # MarketDataset input; Profile/Analysis/Score/Decision consume contracts.
        self.assertEqual(len(prepared_frame), len(original_frame))
        self.assertTrue(
            prepared_frame[["open", "high", "low", "close", "volume"]].equals(original_frame)
        )


if __name__ == "__main__":
    unittest.main()
