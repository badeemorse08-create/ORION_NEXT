"""ORION canonical intelligence orchestrator.

The orchestrator coordinates domain stages using their real result contracts.
It does not embed downstream results in MarketDataset and does not depend on
ExecutionEngine or ReportEngine internals.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from core.execution_plan_builder import ExecutionPlanBuilder
from core.profile_intelligence import ProfileIntelligence, ProfileIntelligenceResult
from engines.analysis_engine import AnalysisEngine
from engines.decision_engine import DecisionEngine
from engines.indicator_engine import IndicatorEngine
from engines.profile_engine import ProfileEngine
from engines.score_engine import ScoreEngine
from engines.validation_engine import ValidationEngine
from models.analysis import AnalysisResult
from models.decision import DecisionResult
from models.execution import ExecutionPlan
from models.market import MarketDataset
from models.profile import ProfileResult
from models.score import ScoreResult

base_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorConfig:
    ENGINE_VERSION: str = "5.0.0"
    PIPELINE_VERSION: str = "2.0.0"
    ENABLE_TIMING: bool = True
    ENABLE_LOGGING: bool = True


class PipelineStage(str, Enum):
    INITIALIZE = "INITIALIZE"
    DOWNLOAD = "DOWNLOAD"
    VALIDATION = "VALIDATION"
    STORE = "STORE"
    INDICATORS = "INDICATORS"
    ANALYSIS = "ANALYSIS"
    PROFILE = "PROFILE"
    SCORE = "SCORE"
    DECISION = "DECISION"
    FINISHED = "FINISHED"


class MarketProviderProtocol(Protocol):
    def execute(self, symbol: str, timeframes: list[str]) -> MarketDataset: ...


class MarketStorageProtocol(Protocol):
    def execute(self, dataset: MarketDataset) -> None: ...


@dataclass(slots=True)
class PipelineStatistics:
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    elapsed_ms: float = 0.0
    current_stage: PipelineStage = PipelineStage.INITIALIZE
    completed_stage_count: int = 0
    success: bool = False
    error_message: Optional[str] = None


@dataclass(slots=True)
class OrchestratorResult:
    dataset: Optional[MarketDataset] = None
    validation: Optional[Any] = None
    analysis: Optional[AnalysisResult] = None
    profile: Optional[ProfileResult] = None
    profile_intelligence: Optional[ProfileIntelligenceResult] = None
    score: Optional[ScoreResult] = None
    decision: Optional[DecisionResult] = None
    execution_plan: Optional[ExecutionPlan] = None
    statistics: PipelineStatistics = field(default_factory=PipelineStatistics)


class OrchestratorError(Exception):
    pass


class PipelineError(OrchestratorError):
    pass


class LoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> tuple[str, dict[str, Any]]:
        context = self.extra or {}
        text = " | ".join(f"{k}={v}" for k, v in context.items() if v is not None)
        return (f"[{text}] {msg}" if text else msg), kwargs


class Orchestrator:
    """Coordinates provider, market validation, persistence and intelligence contracts."""

    def __init__(
        self,
        provider: MarketProviderProtocol,
        storage: MarketStorageProtocol,
        indicator_engine: IndicatorEngine,
        analysis_engine: AnalysisEngine,
        profile_engine: ProfileEngine,
        score_engine: ScoreEngine,
        decision_engine: DecisionEngine,
        validation_engine: ValidationEngine,
        config: OrchestratorConfig,
        execution_plan_builder: Optional[ExecutionPlanBuilder] = None,
        profile_intelligence: Optional[ProfileIntelligence] = None,
    ) -> None:
        dependencies = {
            "provider": provider,
            "storage": storage,
            "indicator_engine": indicator_engine,
            "analysis_engine": analysis_engine,
            "profile_engine": profile_engine,
            "score_engine": score_engine,
            "decision_engine": decision_engine,
            "validation_engine": validation_engine,
            "config": config,
        }
        for name, value in dependencies.items():
            if value is None:
                raise OrchestratorError(f"{name} dependency is required.")

        self._provider = provider
        self._storage = storage
        self._indicator_engine = indicator_engine
        self._analysis_engine = analysis_engine
        self._profile_engine = profile_engine
        self._profile_intelligence = profile_intelligence or ProfileIntelligence()
        self._score_engine = score_engine
        self._decision_engine = decision_engine
        self._validation_engine = validation_engine
        self._config = config
        self._execution_plan_builder = execution_plan_builder or ExecutionPlanBuilder()
        self._last_result: Optional[OrchestratorResult] = None
        self._current_stage = PipelineStage.INITIALIZE
        self._logger = LoggerAdapter(base_logger, {"symbol": "NONE", "stage": "INITIALIZE", "operation": "init"})

    def run(self, symbol: str, timeframes: list[str]) -> OrchestratorResult:
        return self.run_pipeline(symbol, timeframes)

    def run_pipeline(self, symbol: str, timeframes: list[str]) -> OrchestratorResult:
        started = datetime.now(timezone.utc)
        perf = time.perf_counter()
        completed = 0
        dataset = None
        validation = None
        analysis = None
        profile = None
        profile_intelligence = None
        score = None
        decision = None
        execution_plan = None
        error_message = None
        success = False
        self._logger.extra.update({"symbol": symbol, "operation": "run_pipeline"})

        try:
            self._change_stage(PipelineStage.INITIALIZE)
            self._validate_input(symbol, timeframes)
            completed += 1

            self._change_stage(PipelineStage.DOWNLOAD)
            dataset = self._provider.execute(symbol=symbol, timeframes=timeframes)
            self._require_dataset(dataset)
            completed += 1

            # Validation is deliberately completed before persistence.  Invalid
            # provider output must never become durable market state.
            self._change_stage(PipelineStage.VALIDATION)
            validation = self._validation_engine.validate_dataset(dataset)
            completed += 1

            self._change_stage(PipelineStage.STORE)
            self._storage.execute(dataset)
            completed += 1

            self._change_stage(PipelineStage.INDICATORS)
            dataset = self._indicator_engine.calculate_dataset(dataset)
            completed += 1

            self._change_stage(PipelineStage.ANALYSIS)
            analysis = self._analysis_engine.analyze(dataset)
            completed += 1

            self._change_stage(PipelineStage.PROFILE)
            profile = self._profile_engine.build_profile(dataset)
            if not profile.is_tradeable:
                blocked = "; ".join(profile.blocks) or "Profile intelligence is not tradeable."
                raise PipelineError(
                    f"Profile intelligence blocked before Score/Decision: {blocked}"
                )
            if isinstance(profile, ProfileResult):
                # ProfileIntelligence is an interpretation/diagnostic boundary;
                # ProfileEngine remains the canonical authority for the existing
                # is_tradeable gate.  A blocked interpretation is retained on the
                # canonical result but must not invalidate a tradeable profile
                # merely because optional intelligence coverage is incomplete.
                profile_intelligence = self._profile_intelligence.evaluate(profile)
            completed += 1

            self._change_stage(PipelineStage.SCORE)
            score = self._score_engine.calculate(analysis)
            completed += 1

            self._change_stage(PipelineStage.DECISION)
            decision = self._decision_engine.decide(analysis, score)
            execution_plan = self._execution_plan_builder.build(dataset, decision)
            completed += 1

            self._change_stage(PipelineStage.FINISHED)
            success = True

        except Exception as exc:
            error_message = str(exc)
            self._logger.error(f"Pipeline failed at [{self._current_stage.value}]: {exc}")
            if isinstance(exc, PipelineError):
                raise
            raise PipelineError(
                f"Pipeline error for symbol {symbol} at stage {self._current_stage.value}: {exc}"
            ) from exc
        finally:
            finished = datetime.now(timezone.utc)
            elapsed = (time.perf_counter() - perf) * 1000.0 if self._config.ENABLE_TIMING else 0.0
            stats = PipelineStatistics(
                started_at=started,
                finished_at=finished,
                elapsed_ms=elapsed,
                current_stage=self._current_stage,
                completed_stage_count=completed,
                success=success,
                error_message=error_message,
            )
            self._last_result = OrchestratorResult(
                dataset=dataset,
                validation=validation,
                analysis=analysis,
                profile=profile,
                profile_intelligence=profile_intelligence,
                score=score,
                decision=decision,
                execution_plan=execution_plan,
                statistics=stats,
            )
            self._logger.extra.update({"stage": self._current_stage.value, "elapsed_ms": f"{elapsed:.2f}ms"})

        return self._last_result

    def reset(self) -> None:
        self._last_result = None
        self._current_stage = PipelineStage.INITIALIZE
        self._logger.extra.update({"stage": self._current_stage.value})

    def statistics(self) -> Optional[PipelineStatistics]:
        return self._last_result.statistics if self._last_result else None

    def last_result(self) -> Optional[OrchestratorResult]:
        return self._last_result

    @staticmethod
    def _validate_input(symbol: str, timeframes: list[str]) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise PipelineError(f"Invalid symbol: {symbol!r}")
        if not isinstance(timeframes, list) or not timeframes:
            raise PipelineError("At least one timeframe is required.")

    @staticmethod
    def _require_dataset(dataset: Optional[MarketDataset]) -> None:
        if not isinstance(dataset, MarketDataset):
            raise PipelineError("Provider returned an invalid MarketDataset.")
        if not dataset.timeframes:
            raise PipelineError("Provider returned a MarketDataset without timeframes.")

    def _change_stage(self, stage: PipelineStage) -> None:
        self._current_stage = stage
        self._logger.extra["stage"] = stage.value


# End Of File
