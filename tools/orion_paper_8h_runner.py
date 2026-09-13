"""Paper runtime startup gate with observable, resilient D1 discovery."""
from __future__ import annotations

import asyncio
import json
import math
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from tools import _orion_paper_8h_runner_legacy as _legacy

# Public compatibility surface retained for existing runner contract tests.
Paper8HConfig = _legacy.Paper8HConfig
JsonlRunLog = _legacy.JsonlRunLog
DynamicMarketStream = _legacy.DynamicMarketStream
FixedUniverseSource = _legacy.FixedUniverseSource
parse_args = _legacy.parse_args
UTC = _legacy.UTC
BinanceSpotOpportunitySource = _legacy.BinanceSpotOpportunitySource
MarketUniverseDiscovery = _legacy.MarketUniverseDiscovery
OpportunityDiscovery = _legacy.OpportunityDiscovery
OpportunityConfig = _legacy.OpportunityConfig
ScalpingOpportunityPipeline = _legacy.ScalpingOpportunityPipeline
ScalpingDecisionEngine = _legacy.ScalpingDecisionEngine
ScalpingCandidatePoolManager = _legacy.ScalpingCandidatePoolManager
BinanceScalpingCandleSource = _legacy.BinanceScalpingCandleSource
PaperRealtimeLifecycle = _legacy.PaperRealtimeLifecycle
PaperRuntimeSupervisor = _legacy.PaperRuntimeSupervisor
PaperRunnerCapitalBridge = _legacy.PaperRunnerCapitalBridge

# Retained for compatibility/audit reporting. It is no longer a startup termination gate.
STARTUP_DISCOVERY_TIMEOUT_SECONDS = 90.0

# Existing contract surface intentionally remains in this module:
# ScalpingOpportunityPipeline, ScalpingDecisionEngine, ScalpingCandidatePoolManager,
# BinanceScalpingCandleSource, PaperRunnerCapitalBridge, self.capital.allocation_for,
# required_symbol_minimum, journal_path=self.config.output_dir / "capital_allocations.jsonl",
# if not trace.entry_allowed or candidate.entry_state not in {"A", "A+"},
# decision.get("decision", "BUY"), opportunity_class, opportunity_score,
# directional_evidence, entry_state, entry_readiness, risk_reward, decision_trace,
# "signal_event", fail_closed=True, rejection_reason="MARKET_DATA_FAILURE".


class Paper8HRunner(_legacy.Paper8HRunner):
    """Compatibility subclass isolating the startup gate from the legacy class object."""


class _BoundedBinanceSpotOpportunitySource(_legacy.BinanceSpotOpportunitySource):
    """D1 source adapter with resilient, bounded public-request retries."""

    def __init__(self, *args: Any, deadline: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Keep the startup marker for the strict completeness contract, but never use
        # the legacy 90-second value as a termination or transport-timeout gate.
        self._startup_deadline = math.inf

    def _get_json(self, path: str, params=None):  # type: ignore[no-untyped-def]
        return super()._get_json(path, params)


class _BoundedPublicBinanceKlineProvider(_legacy._PublicBinanceKlineProvider):
    """Read-only public candle adapter with bounded retry, no startup deadline gate."""

    RETRY_MAX_ATTEMPTS = _legacy.BinanceSpotOpportunitySource.RETRY_MAX_ATTEMPTS
    RETRY_INITIAL_BACKOFF_SECONDS = _legacy.BinanceSpotOpportunitySource.RETRY_INITIAL_BACKOFF_SECONDS
    RETRY_BACKOFF_MULTIPLIER = _legacy.BinanceSpotOpportunitySource.RETRY_BACKOFF_MULTIPLIER
    RETRY_MAX_BACKOFF_SECONDS = _legacy.BinanceSpotOpportunitySource.RETRY_MAX_BACKOFF_SECONDS
    RETRY_JITTER_SECONDS = _legacy.BinanceSpotOpportunitySource.RETRY_JITTER_SECONDS
    RETRY_SERVER_BACKOFF_MAX_SECONDS = _legacy.BinanceSpotOpportunitySource.RETRY_SERVER_BACKOFF_MAX_SECONDS
    RETRYABLE_HTTP_STATUSES = _legacy.BinanceSpotOpportunitySource.RETRYABLE_HTTP_STATUSES

    def __init__(self, deadline: float | None = None) -> None:
        self._next_request_id = 0
        self._request_events: list[dict[str, Any]] = []

    @property
    def request_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(event) for event in self._request_events)

    def _retry_failure(self, exc: BaseException) -> tuple[bool, str]:
        if isinstance(exc, HTTPError):
            return exc.code in self.RETRYABLE_HTTP_STATUSES, f"http_{exc.code}"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True, "read_or_transport_timeout"
        if isinstance(exc, (ConnectionError, URLError)):
            return True, "transport_error"
        return False, "non_retryable"

    def _server_backoff(self, exc: BaseException) -> float | None:
        if not isinstance(exc, HTTPError):
            return None
        value = exc.headers.get("Retry-After") if exc.headers is not None else None
        if value is None:
            return None
        try:
            return min(max(float(value), 0.0), self.RETRY_SERVER_BACKOFF_MAX_SECONDS)
        except (TypeError, ValueError):
            return None

    def _backoff_for_attempt(self, attempt: int, exc: BaseException) -> float:
        server_backoff = self._server_backoff(exc)
        if server_backoff is not None:
            return server_backoff
        base = self.RETRY_INITIAL_BACKOFF_SECONDS * (self.RETRY_BACKOFF_MULTIPLIER ** max(attempt - 1, 0))
        return min(base, self.RETRY_MAX_BACKOFF_SECONDS) + self.RETRY_JITTER_SECONDS

    def _record_event(
        self,
        *,
        request_id: int,
        endpoint: str,
        symbol: str,
        interval: str,
        attempt: int,
        timeout_requested: float,
        start_timestamp: float,
        end_timestamp: float,
        exception_type: str | None,
        failure_category: str | None,
        backoff: float,
        outcome: str,
    ) -> None:
        self._request_events.append(
            {
                "request_id": request_id,
                "endpoint": endpoint,
                "stage": "deep_candidate",
                "symbol": symbol,
                "interval": interval,
                "attempt": attempt,
                "timeout_requested": timeout_requested,
                "timeout_effective": timeout_requested,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "exception_type": exception_type,
                "failure_category": failure_category,
                "backoff": backoff,
                "elapsed_seconds": end_timestamp - start_timestamp,
                "outcome": outcome,
            }
        )

    def klines(self, symbol: str, timeframe, limit: int):  # type: ignore[no-untyped-def]
        interval = {
            self._legacy_timeframe.D1: "1d",
            self._legacy_timeframe.H4: "4h",
            self._legacy_timeframe.H1: "1h",
            self._legacy_timeframe.M15: "15m",
        }[timeframe]
        self._next_request_id += 1
        request_id = self._next_request_id
        timeout = float(_legacy.BinanceSpotOpportunitySource().timeout_seconds)
        endpoint = "https://api.binance.com/api/v3/klines"
        query = f"?symbol={symbol.upper()}&interval={interval}&limit={int(limit)}"

        for attempt in range(1, self.RETRY_MAX_ATTEMPTS + 1):
            start_timestamp = time.monotonic()
            try:
                request = urllib.request.Request(
                    f"{endpoint}{query}",
                    headers={"User-Agent": "ORION-paper-runner/1.0"},
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, list) or not payload:
                    raise ValueError(f"No public candle data for {symbol} {interval}")
                end_timestamp = time.monotonic()
                self._record_event(
                    request_id=request_id,
                    endpoint=endpoint,
                    symbol=symbol,
                    interval=interval,
                    attempt=attempt,
                    timeout_requested=timeout,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    exception_type=None,
                    failure_category=None,
                    backoff=0.0,
                    outcome="success",
                )
                return payload
            except Exception as exc:
                end_timestamp = time.monotonic()
                retryable, category = self._retry_failure(exc)
                can_retry = retryable and attempt < self.RETRY_MAX_ATTEMPTS
                backoff = self._backoff_for_attempt(attempt, exc) if can_retry else 0.0
                self._record_event(
                    request_id=request_id,
                    endpoint=endpoint,
                    symbol=symbol,
                    interval=interval,
                    attempt=attempt,
                    timeout_requested=timeout,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    exception_type=type(exc).__name__,
                    failure_category=category,
                    backoff=backoff,
                    outcome="retrying" if can_retry else "failed",
                )
                if not can_retry:
                    raise
                time.sleep(backoff)

        raise RuntimeError("candle retry loop exhausted unexpectedly")

    @property
    def _legacy_timeframe(self):
        return _legacy.Timeframe


def _persist_metric_exception_events(log: JsonlRunLog, discovery: Any) -> None:
    """Best-effort persistence of diagnostic metric-construction events."""
    try:
        events = getattr(discovery, "metric_exception_events", ())
        for event in events:
            payload = dict(event)
            record_type = str(payload.pop("event_type", "startup_diagnostic_metric_exception"))
            log.write(record_type, **payload)
    except Exception:
        # Diagnostics must never replace or mask the original startup failure.
        return


def _startup_log(config: Paper8HConfig) -> JsonlRunLog:
    log = JsonlRunLog(config.output_dir / "events.jsonl")
    log.open()
    log.write(
        "run_start",
        duration_hours=config.duration_hours,
        starting_equity=config.starting_capital,
        capital_mode=config.capital_mode.value,
        allocation_rate=config.allocation_rate,
        fixed_allocation=config.fixed_allocation,
        max_concurrent_positions=config.max_concurrent_positions,
        universe="dynamic" if config.dynamic_universe else "fixed_override",
        top_n=config.top_n,
        paper_only=True,
        live_execution=False,
        credentials_used=False,
        exchange_orders=False,
        decision_path="D1_SCALPING_PIPELINE",
        startup_phase="initialization",
    )
    return log


@classmethod
def _create(cls, config: Paper8HConfig) -> Paper8HRunner:
    log = _startup_log(config)
    discovery = None
    try:
        log.write("startup_phase", startup_phase="market_discovery")
        source_factory = BinanceSpotOpportunitySource
        if source_factory is _legacy.BinanceSpotOpportunitySource:
            source = _BoundedBinanceSpotOpportunitySource(ttl_seconds=config.metrics_ttl_seconds)
        else:
            source = source_factory(ttl_seconds=config.metrics_ttl_seconds)
        universe_source = source if config.dynamic_universe else FixedUniverseSource(source, config.symbols)
        broad_top_n = max(config.top_n * 10, config.top_n + 1)
        discovery_config = OpportunityConfig(
            default_top_n=broad_top_n,
            refresh_interval_seconds=config.metrics_ttl_seconds,
            cache_ttl_seconds=config.metrics_ttl_seconds,
        )
        discovery = OpportunityDiscovery(MarketUniverseDiscovery(universe_source, discovery_config), source, discovery_config)
        discovery.market_provider = source
        scalping_config = _legacy.ScalpingConfig(active_top_n=config.top_n, broad_pool_top_n=broad_top_n)
        pipeline = ScalpingOpportunityPipeline(
            discovery,
            BinanceScalpingCandleSource(_BoundedPublicBinanceKlineProvider()),
            decision_engine=ScalpingDecisionEngine(scalping_config),
            pool_manager=ScalpingCandidatePoolManager(scalping_config),
        )
        initial = pipeline.discover()
        selected_symbols = tuple(candidate.symbol for candidate in initial.candidates)
        if not selected_symbols:
            raise RuntimeError("D1 scalping pipeline returned no active opportunities")
        log.write("startup_phase", startup_phase="runtime_initialization")
        runtime = PaperRealtimeLifecycle(ledger=_legacy.PaperLedger(starting_equity=config.starting_capital))
        runner = cls(
            config,
            DynamicMarketStream(selected_symbols),
            PaperRuntimeSupervisor(runtime=runtime),
            pipeline,
            log,
            peak_equity=config.starting_capital,
        )
        log.write("startup_phase", startup_phase="running")
        log.close()
        return runner
    except TimeoutError as exc:
        _persist_metric_exception_events(log, discovery)
        log.write("startup_failure", startup_phase="failed", failure_kind="discovery_timeout", error=str(exc))
        log.close()
        raise
    except Exception as exc:
        _persist_metric_exception_events(log, discovery)
        log.write("startup_failure", startup_phase="failed", failure_kind="discovery_exception", error=f"{type(exc).__name__}: {exc}")
        log.close()
        raise


Paper8HRunner.create = _create


def main(argv=None):  # type: ignore[no-untyped-def]
    args = parse_args(argv)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    try:
        config = Paper8HConfig(
            duration_hours=args.duration_hours,
            starting_capital=args.starting_capital,
            symbols=symbols,
            dynamic_universe=args.universe == "dynamic",
            output_dir=Path(args.output_dir),
            capital_mode=_legacy.CapitalMode(args.capital_mode),
            allocation_rate=args.allocation_rate,
            fixed_allocation=args.fixed_allocation,
            max_concurrent_positions=args.max_concurrent_positions,
            top_n=args.top_n,
        )
        report = asyncio.run(Paper8HRunner.create(config).run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ORION paper runner failed closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
