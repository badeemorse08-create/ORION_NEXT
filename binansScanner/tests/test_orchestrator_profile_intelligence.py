"""Contract tests for Orchestrator/ProfileIntelligence integration."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock

import pandas as pd

from core.orchestrator import Orchestrator, OrchestratorConfig, PipelineError, PipelineStage
from core.profile_intelligence import ProfileIntelligence, ProfileIntelligenceResult, ProfileRecommendation
from enums import DataHealth, Timeframe
from models.market import MarketDataset, MarketMetadata, TimeframeData
from models.profile import (
    EMAAlignment,
    MarketCharacteristics,
    ProfileResult,
    ProfileStatistics,
    TimeframeProfile,
    TrendStrengthType,
    TrendType,
    MomentumState,
    MarketPhaseType,
    RiskLevel,
    VolatilityLevelType,
    VolumeStrength,
)


class TestOrchestratorProfileIntelligence(TestCase):
    def _dataset(self) -> MarketDataset:
        now = datetime.now(timezone.utc)
        dataframe = pd.DataFrame(
            {
                "open": [100.0, 100.5],
                "high": [101.0, 101.5],
                "low": [99.0, 100.0],
                "close": [100.5, 101.0],
                "volume": [10.0, 11.0],
            },
            index=pd.DatetimeIndex([now, now], name="timestamp"),
        )
        return MarketDataset(
            metadata=MarketMetadata(
                symbol="BTCUSDT",
                exchange="BINANCE",
                source="TEST",
                cache_version="1.0.0",
                downloaded_at=now,
                last_updated_at=now,
            ),
            timeframes={
                Timeframe.M1: TimeframeData(
                    timeframe=Timeframe.M1,
                    dataframe=dataframe,
                    data_health=DataHealth.ACCEPTABLE,
                    candles_count=2,
                    first_timestamp=now,
                    last_timestamp=now,
                )
            },
        )

    def _profile(self, *, risk_level: str = RiskLevel.LOW.value) -> ProfileResult:
        now = datetime.now(timezone.utc)
        characteristics = MarketCharacteristics(
            trend=TrendType.BULLISH.value,
            trend_strength=TrendStrengthType.STRONG.value,
            volatility_level=VolatilityLevelType.NORMAL.value,
            momentum=MomentumState.BUY.value,
            volume_strength=VolumeStrength.STRONG.value,
            ema_alignment=EMAAlignment.BULLISH.value,
            market_phase=MarketPhaseType.MARKUP.value,
            risk_level=risk_level,
            confidence=80.0,
            trend_score=80.0,
            momentum_score=80.0,
            volume_score=70.0,
            volatility_score=40.0,
        )
        timeframe = TimeframeProfile(
            timeframe=Timeframe.M1.value,
            characteristics=characteristics,
            candles_count=2,
            missing_candles=0,
            first_timestamp=now,
            last_timestamp=now,
        )
        return ProfileResult(
            symbol="BTCUSDT",
            market=characteristics,
            statistics=ProfileStatistics(
                health_score=90.0,
                confidence_limit=80.0,
                completion_ratio=1.0,
                total_candles=2,
                missing_candles=0,
                newest_candle=now,
                oldest_candle=now,
            ),
            timeframes=(timeframe,),
            is_tradeable=True,
        )

    def _orchestrator(
        self,
        profile_intelligence: object,
    ) -> tuple[Orchestrator, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock, MarketDataset]:
        provider = MagicMock()
        storage = MagicMock()
        indicator = MagicMock()
        analysis = MagicMock()
        profile_engine = MagicMock()
        score = MagicMock()
        decision = MagicMock()
        validation = MagicMock()
        dataset = self._dataset()

        provider.execute.return_value = dataset
        validation.validate_dataset.return_value = MagicMock()
        indicator.calculate_dataset.return_value = dataset
        analysis.analyze.return_value = MagicMock()
        score.calculate.return_value = MagicMock()
        decision_result = MagicMock()
        decision_result.decision = "WAIT"
        decision_result.confidence = 50.0
        decision_result.reasons = ["test"]
        decision.decide.return_value = decision_result

        orchestrator = Orchestrator(
            provider=provider,
            storage=storage,
            indicator_engine=indicator,
            analysis_engine=analysis,
            profile_engine=profile_engine,
            score_engine=score,
            decision_engine=decision,
            validation_engine=validation,
            config=OrchestratorConfig(ENABLE_TIMING=False),
            profile_intelligence=profile_intelligence,
        )
        return orchestrator, provider, storage, profile_engine, score, decision, dataset

    def test_real_profile_intelligence_runs_before_scoring(self) -> None:
        orchestrator, _, _, profile_engine, score, _, dataset = self._orchestrator(ProfileIntelligence())
        profile_engine.build_profile.return_value = self._profile()

        result = orchestrator.run_pipeline("BTCUSDT", ["1m"])

        self.assertIsInstance(result.profile, ProfileResult)
        self.assertIsInstance(result.profile_intelligence, ProfileIntelligenceResult)
        self.assertEqual(result.profile_intelligence.recommendation, ProfileRecommendation.BULLISH.value)
        self.assertEqual(result.profile_intelligence.confidence, 80.0)
        score.calculate.assert_called_once_with(result.analysis)
        self.assertEqual(result.statistics.current_stage, PipelineStage.FINISHED)
        self.assertIs(result.dataset, dataset)

    def test_injected_profile_intelligence_is_called_before_scoring(self) -> None:
        intelligence = MagicMock()
        expected = ProfileIntelligenceResult(
            recommendation=ProfileRecommendation.BULLISH.value,
            confidence=80.0,
            reasons=("aligned",),
            blocked=False,
        )
        intelligence.evaluate.return_value = expected
        orchestrator, _, _, profile_engine, score, _, dataset = self._orchestrator(intelligence)
        profile_engine.build_profile.return_value = self._profile()

        result = orchestrator.run_pipeline("BTCUSDT", ["1m"])

        intelligence.evaluate.assert_called_once_with(result.profile)
        self.assertEqual(result.profile_intelligence, expected)
        score.calculate.assert_called_once_with(result.analysis)
        self.assertEqual(result.statistics.current_stage, PipelineStage.FINISHED)
        self.assertIs(result.dataset, dataset)

    def test_blocked_profile_intelligence_stops_before_scoring(self) -> None:
        intelligence = MagicMock()
        blocked = ProfileIntelligenceResult(
            recommendation=ProfileRecommendation.BLOCKED.value,
            confidence=0.0,
            reasons=("invalid coverage",),
            blocked=True,
        )
        intelligence.evaluate.return_value = blocked
        orchestrator, _, _, profile_engine, score, _, _ = self._orchestrator(intelligence)
        profile_engine.build_profile.return_value = self._profile()

        with self.assertRaisesRegex(PipelineError, "invalid coverage"):
            orchestrator.run_pipeline("BTCUSDT", ["1m"])

        intelligence.evaluate.assert_called_once()
        score.calculate.assert_not_called()
        self.assertEqual(orchestrator.statistics().current_stage, PipelineStage.PROFILE)


if __name__ == "__main__":
    import unittest
    unittest.main()
