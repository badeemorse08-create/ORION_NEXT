"""
===============================================================================
Badee Binance Scanner
Architecture : ORION
Module      : core.dependency_container
Version     : 1.8.0
Status      : ORION Canonical Composition Root
===============================================================================

Composition Root responsible solely for object creation, dependency wiring,
and lifecycle management.
===============================================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from providers.binance_provider import BinanceProvider
from providers.market_data_provider import MarketDataProvider
from storage.market_storage import MarketStorage
from storage.sqlite_market_storage import SQLiteMarketStorage
from repositories.market_repository import MarketRepository
from services.market_service import MarketService
from engines.indicator_engine import IndicatorEngine
from engines.analysis_engine import AnalysisEngine
from engines.profile_engine import ProfileEngine
from engines.score_engine import ScoreEngine
from engines.decision_engine import DecisionEngine
from engines.report_engine import ReportEngine
from engines.validation_engine import ValidationEngine
from engines.execution_engine import ExecutionEngine, PaperExecutionAdapter, ExecutionAdapter
from scheduler.scheduler_service import SchedulerService
from api.api_service import ApiService
from api.api_router import ApiRouter
from core.orchestrator import Orchestrator, OrchestratorConfig
from core.pipeline import Pipeline

base_logger = logging.getLogger(__name__)


class ContainerError(Exception):
    """Base exception for dependency-container failures."""


@dataclass(frozen=True)
class ContainerConfiguration:
    logger: Optional[logging.Logger] = None
    orchestrator_config: Optional[OrchestratorConfig] = None
    paper_trading_enabled: bool = True
    cache_enabled: bool = True
    database_path: str = "market_data.db"
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False


class LoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> tuple[str, dict[str, Any]]:
        context = self.extra or {}
        text = " | ".join(f"{k}={v}" for k, v in context.items() if v is not None)
        return (f"[{text}] {msg}" if text else msg), kwargs


class DependencyContainer:
    """ORION composition root; no business logic is executed here."""

    def __init__(self, config: Optional[ContainerConfiguration] = None) -> None:
        self._config = config or ContainerConfiguration()
        self._logger_instance = self._config.logger or base_logger
        self._logger = LoggerAdapter(self._logger_instance, {"component": "DependencyContainer", "operation": "init"})

        self._binance_provider_instance: Optional[BinanceProvider] = None
        self._market_data_provider_instance: Optional[MarketDataProvider] = None
        self._market_storage_instance: Optional[MarketStorage] = None
        self._market_repository_instance: Optional[MarketRepository] = None
        self._market_service_instance: Optional[MarketService] = None
        self._indicator_engine_instance: Optional[IndicatorEngine] = None
        self._analysis_engine_instance: Optional[AnalysisEngine] = None
        self._profile_engine_instance: Optional[ProfileEngine] = None
        self._score_engine_instance: Optional[ScoreEngine] = None
        self._decision_engine_instance: Optional[DecisionEngine] = None
        self._report_engine_instance: Optional[ReportEngine] = None
        self._validation_engine_instance: Optional[ValidationEngine] = None
        self._execution_adapter_instance: Optional[ExecutionAdapter] = None
        self._execution_engine_instance: Optional[ExecutionEngine] = None
        self._scheduler_service_instance: Optional[SchedulerService] = None
        self._api_service_instance: Optional[ApiService] = None
        self._api_router_instance: Optional[ApiRouter] = None
        self._orchestrator_instance: Optional[Orchestrator] = None
        self._pipeline_instance: Optional[Pipeline] = None

    def _create_binance_provider(self) -> BinanceProvider:
        return BinanceProvider(
            api_key=self._config.binance_api_key,
            api_secret=self._config.binance_api_secret,
            testnet=self._config.binance_testnet,
        )

    def _create_market_data_provider(self) -> MarketDataProvider:
        return MarketDataProvider(source=self.build_binance_provider(), logger=self._logger_instance)

    def _create_market_storage(self) -> SQLiteMarketStorage:
        return SQLiteMarketStorage(database_path=self._config.database_path, logger=self._logger_instance)

    def _create_market_repository(self) -> MarketRepository:
        return MarketRepository(
            market_provider=self.build_market_data_provider(),
            storage=self.build_market_storage(),
            logger=self._logger_instance,
        )

    def _create_market_service(self) -> MarketService:
        return MarketService(repository=self.build_market_repository(), logger=self._logger_instance)

    def _create_indicator_engine(self) -> IndicatorEngine:
        return IndicatorEngine()

    def _create_analysis_engine(self) -> AnalysisEngine:
        return AnalysisEngine()

    def _create_profile_engine(self) -> ProfileEngine:
        return ProfileEngine()

    def _create_score_engine(self) -> ScoreEngine:
        return ScoreEngine()

    def _create_decision_engine(self) -> DecisionEngine:
        return DecisionEngine()

    def _create_report_engine(self) -> ReportEngine:
        return ReportEngine()

    def _create_validation_engine(self) -> ValidationEngine:
        return ValidationEngine()

    def _create_execution_adapter(self) -> ExecutionAdapter:
        if not self._config.paper_trading_enabled:
            raise ContainerError("Paper trading is disabled; live execution is fail-closed in this Phase A build.")
        return PaperExecutionAdapter()

    def _create_scheduler_service(self) -> SchedulerService:
        return SchedulerService(logger=self._logger_instance)

    def _create_api_service(self) -> ApiService:
        return ApiService(
            scheduler=self.build_scheduler_service(),
            pipeline=self.build_pipeline(),
            logger=self._logger_instance,
        )

    def _create_api_router(self) -> ApiRouter:
        return ApiRouter(
            service=self.build_api_service(),
            logger=self._logger_instance,
        )

    def build_binance_provider(self) -> BinanceProvider:
        if self._binance_provider_instance is None:
            try:
                self._binance_provider_instance = self._create_binance_provider()
            except Exception as exc:
                raise ContainerError(f"Failed to build BinanceProvider: {exc}") from exc
        return self._binance_provider_instance

    def build_market_data_provider(self) -> MarketDataProvider:
        if self._market_data_provider_instance is None:
            try:
                self._market_data_provider_instance = self._create_market_data_provider()
            except Exception as exc:
                raise ContainerError(f"Failed to build MarketDataProvider: {exc}") from exc
        return self._market_data_provider_instance

    def build_market_storage(self) -> MarketStorage:
        if self._market_storage_instance is None:
            try:
                self._market_storage_instance = self._create_market_storage()
            except Exception as exc:
                raise ContainerError(f"Failed to build MarketStorage: {exc}") from exc
        return self._market_storage_instance

    def build_market_repository(self) -> MarketRepository:
        if self._market_repository_instance is None:
            try:
                self._market_repository_instance = self._create_market_repository()
            except Exception as exc:
                raise ContainerError(f"Failed to build MarketRepository: {exc}") from exc
        return self._market_repository_instance

    def build_market_service(self) -> MarketService:
        if self._market_service_instance is None:
            try:
                self._market_service_instance = self._create_market_service()
            except Exception as exc:
                raise ContainerError(f"Failed to build MarketService: {exc}") from exc
        return self._market_service_instance

    def build_indicator_engine(self) -> IndicatorEngine:
        if self._indicator_engine_instance is None:
            self._indicator_engine_instance = self._create_indicator_engine()
        return self._indicator_engine_instance

    def build_analysis_engine(self) -> AnalysisEngine:
        if self._analysis_engine_instance is None:
            self._analysis_engine_instance = self._create_analysis_engine()
        return self._analysis_engine_instance

    def build_profile_engine(self) -> ProfileEngine:
        if self._profile_engine_instance is None:
            self._profile_engine_instance = self._create_profile_engine()
        return self._profile_engine_instance

    def build_score_engine(self) -> ScoreEngine:
        if self._score_engine_instance is None:
            self._score_engine_instance = self._create_score_engine()
        return self._score_engine_instance

    def build_decision_engine(self) -> DecisionEngine:
        if self._decision_engine_instance is None:
            self._decision_engine_instance = self._create_decision_engine()
        return self._decision_engine_instance

    def build_report_engine(self) -> ReportEngine:
        if self._report_engine_instance is None:
            self._report_engine_instance = self._create_report_engine()
        return self._report_engine_instance

    def build_validation_engine(self) -> ValidationEngine:
        if self._validation_engine_instance is None:
            self._validation_engine_instance = self._create_validation_engine()
        return self._validation_engine_instance

    def build_execution_engine(self) -> ExecutionEngine:
        if self._execution_engine_instance is None:
            if self._execution_adapter_instance is None:
                self._execution_adapter_instance = self._create_execution_adapter()
            self._execution_engine_instance = ExecutionEngine(
                adapter=self._execution_adapter_instance,
                logger=self._logger_instance,
            )
        return self._execution_engine_instance

    def build_scheduler_service(self) -> SchedulerService:
        if self._scheduler_service_instance is None:
            self._scheduler_service_instance = self._create_scheduler_service()
        return self._scheduler_service_instance

    def build_api_service(self) -> ApiService:
        if self._api_service_instance is None:
            self._api_service_instance = self._create_api_service()
        return self._api_service_instance

    def build_api_router(self) -> ApiRouter:
        if self._api_router_instance is None:
            self._api_router_instance = self._create_api_router()
        return self._api_router_instance

    def build_orchestrator(self) -> Orchestrator:
        if self._orchestrator_instance is None:
            config = self._config.orchestrator_config or OrchestratorConfig()
            self._orchestrator_instance = Orchestrator(
                provider=self.build_market_data_provider(),
                storage=self.build_market_storage(),
                indicator_engine=self.build_indicator_engine(),
                analysis_engine=self.build_analysis_engine(),
                profile_engine=self.build_profile_engine(),
                score_engine=self.build_score_engine(),
                decision_engine=self.build_decision_engine(),
                validation_engine=self.build_validation_engine(),
                config=config,
            )
        return self._orchestrator_instance

    def build_pipeline(self) -> Pipeline:
        if self._pipeline_instance is None:
            self._pipeline_instance = Pipeline(
                orchestrator=self.build_orchestrator(),
                execution_engine=self.build_execution_engine(),
                report_engine=self.build_report_engine(),
                logger=self._logger_instance,
            )
        return self._pipeline_instance

    def reset(self) -> None:
        if self._scheduler_service_instance is not None:
            self._scheduler_service_instance.stop()

        self._binance_provider_instance = None
        self._market_data_provider_instance = None
        self._market_storage_instance = None
        self._market_repository_instance = None
        self._market_service_instance = None
        self._indicator_engine_instance = None
        self._analysis_engine_instance = None
        self._profile_engine_instance = None
        self._score_engine_instance = None
        self._decision_engine_instance = None
        self._report_engine_instance = None
        self._validation_engine_instance = None
        self._execution_adapter_instance = None
        self._execution_engine_instance = None
        self._scheduler_service_instance = None
        self._api_service_instance = None
        self._api_router_instance = None
        self._orchestrator_instance = None
        self._pipeline_instance = None

    def logger(self) -> logging.Logger:
        return self._logger_instance


# End Of File
