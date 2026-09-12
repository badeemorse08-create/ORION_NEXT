"""Dynamic market-universe discovery and deterministic opportunity selection."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping, Protocol, Sequence

from models.opportunity import (
    EligibilityResult,
    MarketMetrics,
    OpportunityCandidate,
    OpportunityCandidateSet,
    UniverseCandidate,
)


class UniverseSource(Protocol):
    def exchange_info(self) -> Mapping[str, Any]: ...


class MetricsSource(Protocol):
    def metrics(self, symbol: str) -> MarketMetrics: ...


class BulkMetricsSource(Protocol):
    def metrics_bulk(self, symbols: Sequence[str]) -> Mapping[str, MarketMetrics]: ...


@dataclass(frozen=True, slots=True)
class DiscoveryBootstrapStatus:
    """Observable completeness state for one fresh discovery acquisition."""

    expected_symbols: tuple[str, ...]
    received_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    source_status: str
    deadline_state: str


@dataclass(frozen=True, slots=True)
class OpportunityConfig:
    quote_assets: tuple[str, ...] = ("USDT",)
    excluded_base_assets: tuple[str, ...] = (
        "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP",
        "USDE", "USDD", "PYUSD", "FRAX", "UST", "USD1", "RLUSD",
        "EURC", "EURS", "EUR",
    )
    min_quote_volume_24h: float = 1_000_000.0
    min_volatility: float = 0.001
    max_volatility: float = 0.20
    max_spread_bps: float = 50.0
    volume_reference_24h: float = 100_000_000.0
    target_volatility: float = 0.03
    volume_weight: float = 0.20
    liquidity_weight: float = 0.15
    volatility_weight: float = 0.15
    trend_weight: float = 0.25
    momentum_weight: float = 0.15
    structure_weight: float = 0.10
    default_top_n: int = 10
    refresh_interval_seconds: float = 30.0
    hysteresis_score_delta: float = 0.50
    cache_ttl_seconds: float = 30.0
    require_last_price: bool = False

    def __post_init__(self) -> None:
        if not self.quote_assets:
            raise ValueError("At least one quote asset is required")
        if self.min_quote_volume_24h < 0:
            raise ValueError("min_quote_volume_24h must be non-negative")
        if not 0 <= self.min_volatility <= self.max_volatility:
            raise ValueError("volatility bounds are invalid")
        if self.max_spread_bps <= 0:
            raise ValueError("max_spread_bps must be positive")
        if self.volume_reference_24h <= 0:
            raise ValueError("volume_reference_24h must be positive")
        if not self.min_volatility <= self.target_volatility <= self.max_volatility:
            raise ValueError("target_volatility must be within eligibility bounds")
        weights = (self.volume_weight, self.liquidity_weight, self.volatility_weight, self.trend_weight, self.momentum_weight, self.structure_weight)
        if any(weight < 0 for weight in weights) or not math.isclose(sum(weights), 1.0, abs_tol=1e-12):
            raise ValueError("opportunity weights must be non-negative and sum to 1")
        if self.default_top_n <= 0:
            raise ValueError("default_top_n must be positive")
        if self.refresh_interval_seconds < 0 or self.cache_ttl_seconds < 0:
            raise ValueError("refresh/cache TTL must be non-negative")
        if self.hysteresis_score_delta < 0:
            raise ValueError("hysteresis_score_delta must be non-negative")


class MarketUniverseDiscovery:
    def __init__(self, source: UniverseSource, config: OpportunityConfig | None = None) -> None:
        self._source = source
        self._config = config or OpportunityConfig()

    def discover(self) -> tuple[UniverseCandidate, ...]:
        payload = self._source.exchange_info()
        symbols = payload.get("symbols", [])
        by_symbol: dict[str, UniverseCandidate] = {}
        for item in symbols:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            base = str(item.get("baseAsset", "")).strip().upper()
            quote = str(item.get("quoteAsset", "")).strip().upper()
            status = str(item.get("status", "")).strip().upper()
            if not symbol or not base or not quote or status != "TRADING":
                continue
            if quote not in self._config.quote_assets or base in self._config.excluded_base_assets:
                continue
            if item.get("isSpotTradingAllowed") is False:
                continue
            by_symbol[symbol] = UniverseCandidate(symbol, base, quote)
        return tuple(sorted(by_symbol.values(), key=lambda c: c.symbol))


class MarketEligibilityFilter:
    def __init__(self, config: OpportunityConfig | None = None) -> None:
        self._config = config or OpportunityConfig()

    def evaluate(self, candidate: UniverseCandidate, metrics: MarketMetrics) -> EligibilityResult:
        reasons: list[str] = []
        if candidate.symbol != metrics.symbol:
            reasons.append("SYMBOL_MISMATCH")
        if not metrics.tradable:
            reasons.append("NOT_TRADABLE")
        if self._config.require_last_price and metrics.last_price is None:
            reasons.append("MISSING_PRICE")
        elif metrics.last_price is not None and (not math.isfinite(metrics.last_price) or metrics.last_price <= 0):
            reasons.append("INVALID_PRICE")
        if not math.isfinite(metrics.quote_volume_24h) or metrics.quote_volume_24h < 0:
            reasons.append("INVALID_VOLUME")
        elif metrics.quote_volume_24h < self._config.min_quote_volume_24h:
            reasons.append("LOW_VOLUME")
        if not math.isfinite(metrics.volatility):
            reasons.append("INVALID_VOLATILITY")
        elif metrics.volatility < self._config.min_volatility:
            reasons.append("LOW_VOLATILITY")
        elif metrics.volatility > self._config.max_volatility:
            reasons.append("HIGH_VOLATILITY")
        if metrics.spread_bps is not None:
            if not math.isfinite(metrics.spread_bps) or metrics.spread_bps < 0:
                reasons.append("INVALID_SPREAD")
            elif metrics.spread_bps > self._config.max_spread_bps:
                reasons.append("WIDE_SPREAD")
        for label, value, low, high in (
            ("VOLUME_QUALITY", metrics.volume_quality, 0.0, 1.0),
            ("TREND_QUALITY", metrics.trend_quality, 0.0, 1.0),
            ("TREND_PERSISTENCE", metrics.trend_persistence, 0.0, 1.0),
            ("MOMENTUM_QUALITY", metrics.momentum_quality, 0.0, 1.0),
            ("STRUCTURE_QUALITY", metrics.structure_quality, 0.0, 1.0),
            ("TREND_DIRECTION", metrics.trend_direction, -1.0, 1.0),
            ("MOMENTUM_DIRECTION", metrics.momentum_direction, -1.0, 1.0),
        ):
            if value is not None and (not math.isfinite(value) or not low <= value <= high):
                reasons.append(f"INVALID_{label}")
        return EligibilityResult(candidate.symbol, not reasons, tuple(reasons))


class OpportunityScorer:
    def __init__(self, config: OpportunityConfig | None = None) -> None:
        self._config = config or OpportunityConfig()

    @staticmethod
    def _neutral(value: float | None) -> float:
        return 0.5 if value is None else max(0.0, min(value, 1.0))

    @staticmethod
    def _direction(value: float | None) -> float:
        return 0.0 if value is None else max(-1.0, min(value, 1.0))

    def score_components(self, metrics: MarketMetrics) -> tuple[tuple[str, float], ...]:
        volume_component = self._neutral(metrics.volume_quality) if metrics.volume_quality is not None else min(math.log1p(max(metrics.quote_volume_24h, 0.0)) / math.log1p(self._config.volume_reference_24h), 1.0)
        distance = abs(metrics.volatility - self._config.target_volatility)
        span = max(self._config.target_volatility - self._config.min_volatility, self._config.max_volatility - self._config.target_volatility, 1e-12)
        volatility_component = max(0.0, 1.0 - distance / span)
        if metrics.spread_bps is None:
            liquidity_component = 0.5
        elif not math.isfinite(metrics.spread_bps) or metrics.spread_bps < 0:
            liquidity_component = 0.0
        else:
            liquidity_component = max(0.0, 1.0 - min(metrics.spread_bps / self._config.max_spread_bps, 1.0))
        trend_component = 0.7 * self._neutral(metrics.trend_quality) + 0.3 * self._neutral(metrics.trend_persistence)
        return (("volume_quality", round(volume_component, 8)), ("liquidity", round(liquidity_component, 8)), ("volatility_regime", round(volatility_component, 8)), ("trend_quality", round(trend_component, 8)), ("momentum", round(self._neutral(metrics.momentum_quality), 8)), ("structure_quality", round(self._neutral(metrics.structure_quality), 8)))

    def directional_evidence(self, metrics: MarketMetrics) -> float:
        weighted = 0.65 * self._direction(metrics.trend_direction) * self._neutral(metrics.trend_quality) + 0.35 * self._direction(metrics.momentum_direction) * self._neutral(metrics.momentum_quality)
        return round(max(-1.0, min(weighted, 1.0)), 8)

    def score(self, metrics: MarketMetrics) -> float:
        components = dict(self.score_components(metrics))
        total = self._config.volume_weight * components["volume_quality"] + self._config.liquidity_weight * components["liquidity"] + self._config.volatility_weight * components["volatility_regime"] + self._config.trend_weight * components["trend_quality"] + self._config.momentum_weight * components["momentum"] + self._config.structure_weight * components["structure_quality"]
        return round(100.0 * max(0.0, min(total, 1.0)), 8)


class OpportunityRanker:
    def __init__(self, scorer: OpportunityScorer | None = None, config: OpportunityConfig | None = None) -> None:
        self._config = config or OpportunityConfig()
        self._scorer = scorer or OpportunityScorer(self._config)
        self._previous_rank: dict[str, int] = {}

    def rank(self, candidates: tuple[UniverseCandidate, ...], metrics: Mapping[str, MarketMetrics], top_n: int | None = None) -> OpportunityCandidateSet:
        requested_top_n = top_n if top_n is not None else self._config.default_top_n
        if requested_top_n <= 0:
            raise ValueError("top_n must be positive")
        eligibility = MarketEligibilityFilter(self._config)
        ranked: list[OpportunityCandidate] = []
        for candidate in candidates:
            metric = metrics.get(candidate.symbol)
            if metric is None:
                continue
            decision = eligibility.evaluate(candidate, metric)
            if not decision.eligible:
                continue
            ranked.append(OpportunityCandidate(candidate.symbol, self._scorer.score(metric), 0, metric, decision.reasons, self._scorer.score_components(metric), self._scorer.directional_evidence(metric)))
        ranked.sort(key=lambda item: (-item.opportunity_score, item.symbol))
        if self._previous_rank and self._config.hysteresis_score_delta > 0:
            for index in range(len(ranked) - 1):
                left, right = ranked[index], ranked[index + 1]
                if abs(left.opportunity_score - right.opportunity_score) <= self._config.hysteresis_score_delta:
                    lp = self._previous_rank.get(left.symbol)
                    rp = self._previous_rank.get(right.symbol)
                    if rp is not None and (lp is None or rp < lp):
                        ranked[index], ranked[index + 1] = right, left
        selected = ranked[:requested_top_n]
        result = tuple(OpportunityCandidate(item.symbol, item.opportunity_score, index, item.metrics, item.eligibility_reasons, item.score_components, item.directional_evidence) for index, item in enumerate(selected, start=1))
        self._previous_rank = {item.symbol: item.rank for item in result}
        return OpportunityCandidateSet(result, requested_top_n, None)


class OpportunityDiscovery:
    """Universe -> bulk metadata -> bounded targeted symbol revalidation -> history -> rank."""

    def __init__(self, universe: MarketUniverseDiscovery, metrics_source: MetricsSource, config: OpportunityConfig | None = None, clock: Callable[[], float] = __import__("time").monotonic) -> None:
        self._universe = universe
        self._metrics_source = metrics_source
        self._config = config or OpportunityConfig()
        self._ranker = OpportunityRanker(config=self._config)
        self._clock = clock
        self._cached_output: OpportunityCandidateSet | None = None
        self._cached_at = -math.inf
        self._last_bootstrap: DiscoveryBootstrapStatus | None = None
        self._last_reconciliation_events: tuple[dict[str, Any], ...] = ()

    @property
    def last_bootstrap(self) -> DiscoveryBootstrapStatus | None:
        return self._last_bootstrap

    @property
    def last_reconciliation_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(event) for event in self._last_reconciliation_events)

    def _record_bootstrap(self, expected_symbols: tuple[str, ...], received_symbols: tuple[str, ...], source_status: str) -> None:
        deadline = getattr(self._metrics_source, "_startup_deadline", None)
        deadline_state = "not_configured" if deadline is None else ("expired" if deadline <= self._clock() else "active")
        missing_symbols = tuple(symbol for symbol in expected_symbols if symbol not in received_symbols)
        self._last_bootstrap = DiscoveryBootstrapStatus(expected_symbols, received_symbols, missing_symbols, source_status, deadline_state)

    @staticmethod
    def _single_payload(payload: Any) -> Mapping[str, Any] | None:
        if isinstance(payload, Mapping):
            return payload
        if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], Mapping):
            return payload[0]
        return None

    @staticmethod
    def _metadata_summary(ticker: Mapping[str, Any] | None, book: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "quote_volume_24h": None if ticker is None else ticker.get("quoteVolume"),
            "last_price": None if ticker is None else ticker.get("lastPrice"),
            "price_change_percent_24h": None if ticker is None else ticker.get("priceChangePercent"),
            "weighted_avg_price_24h": None if ticker is None else ticker.get("weightedAvgPrice"),
            "bid_price": None if book is None else book.get("bidPrice"),
            "ask_price": None if book is None else book.get("askPrice"),
        }

    def _build_history_metric(self, source: Any, symbol: str, ticker: Mapping[str, Any], book: Mapping[str, Any] | None, history: Sequence[Any]) -> MarketMetrics:
        last_price = float(ticker["lastPrice"])
        quote_volume = float(ticker["quoteVolume"])
        change_pct = float(ticker["priceChangePercent"])
        weighted_avg = float(ticker["weightedAvgPrice"])
        spread_bps = None
        if book is not None:
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            if bid > 0 and ask >= bid:
                spread_bps = ((ask - bid) / ((ask + bid) / 2.0)) * 10_000.0
        window = history[-source.METRICS_HISTORY_WINDOW:]
        volatility, trend_quality, trend_direction, trend_persistence, momentum_quality, momentum_direction = source._history_features(window)
        structure_deviation = abs(last_price / weighted_avg - 1.0) if weighted_avg > 0 else math.inf
        structure_quality = max(0.0, 1.0 - min(structure_deviation / 0.05, 1.0)) if math.isfinite(structure_deviation) else 0.0
        volume_quality = min(math.log1p(max(quote_volume, 0.0)) / math.log1p(100_000_000.0), 1.0)
        return MarketMetrics(symbol=symbol, quote_volume_24h=quote_volume, volatility=volatility, spread_bps=spread_bps, tradable=True, last_price=last_price, volume_quality=volume_quality, trend_quality=trend_quality, momentum_quality=momentum_quality, structure_quality=structure_quality, price_change_pct_24h=change_pct, weighted_avg_price_24h=weighted_avg, trend_direction=trend_direction, trend_persistence=trend_persistence, momentum_direction=momentum_direction)

    def _startup_metrics(self, candidates: tuple[UniverseCandidate, ...]) -> Mapping[str, MarketMetrics]:
        source = self._metrics_source
        symbols = tuple(sorted(c.symbol for c in candidates))
        self._last_reconciliation_events = ()
        bulk_get = getattr(source, "_get_json", None)
        if not callable(bulk_get):
            return self._collect_metrics_legacy(candidates)

        tickers = bulk_get("ticker/24hr")
        books = bulk_get("ticker/bookTicker")
        ticker_rows = tickers if isinstance(tickers, list) else [tickers]
        book_rows = books if isinstance(books, list) else [books]
        ticker_by_symbol = {str(row.get("symbol", "")).upper(): row for row in ticker_rows if isinstance(row, Mapping)}
        book_by_symbol = {str(row.get("symbol", "")).upper(): row for row in book_rows if isinstance(row, Mapping)}

        result: dict[str, MarketMetrics] = {}
        revalidation_candidates = tuple(symbol for symbol in symbols if symbol not in ticker_by_symbol or symbol not in book_by_symbol)
        possible_history = tuple(symbol for symbol in symbols if symbol not in revalidation_candidates and source._needs_history(ticker_by_symbol.get(symbol), book_by_symbol.get(symbol)))

        def acquire_history(symbol: str) -> tuple[str, tuple[Sequence[Any], ...] | None]:
            try:
                return symbol, tuple(source._fetch_history(symbol))
            except Exception:
                return symbol, None

        histories: dict[str, tuple[Sequence[Any], ...]] = {}
        if possible_history:
            with ThreadPoolExecutor(max_workers=getattr(source, "DISCOVERY_CONCURRENCY", 4), thread_name_prefix="orion-startup-history") as executor:
                future_to_symbol = {executor.submit(acquire_history, symbol): symbol for symbol in possible_history}
                for future in as_completed(future_to_symbol):
                    symbol, history = future.result()
                    if history is not None:
                        histories[symbol] = history

        reusable_daily: dict[str, tuple[Sequence[Any], ...]] = {}
        for symbol in symbols:
            ticker = ticker_by_symbol.get(symbol)
            book = book_by_symbol.get(symbol)
            if ticker is not None and symbol not in revalidation_candidates and not source._needs_history(ticker, book):
                metric = source._metadata_only_metric(symbol, ticker, book)
                if metric is not None:
                    result[symbol] = metric
            elif ticker is not None and symbol not in revalidation_candidates and symbol in histories:
                try:
                    result[symbol] = self._build_history_metric(source, symbol, ticker, book, histories[symbol])
                    if len(histories[symbol]) == source.HISTORY_LIMIT:
                        reusable_daily[symbol] = histories[symbol]
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    pass

        pending = list(revalidation_candidates)
        events: list[dict[str, Any]] = []
        max_rounds = getattr(source, "RECONCILIATION_MAX_ATTEMPTS", 2)
        for attempt in range(1, max_rounds + 1):
            if not pending:
                break
            next_pending: list[str] = []

            def targeted(symbol: str) -> tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None, str, str]:
                ticker = book = None
                try:
                    ticker = self._single_payload(bulk_get("ticker/24hr", {"symbol": symbol}))
                    ticker_state = "present" if ticker is not None else "absent"
                except Exception:
                    ticker_state = "error"
                try:
                    book = self._single_payload(bulk_get("ticker/bookTicker", {"symbol": symbol}))
                    book_state = "present" if book is not None else "absent"
                except Exception:
                    book_state = "error"
                return symbol, ticker, book, ticker_state, book_state

            with ThreadPoolExecutor(max_workers=getattr(source, "METADATA_CONCURRENCY", 2), thread_name_prefix="orion-targeted-metadata") as executor:
                future_to_symbol = {executor.submit(targeted, symbol): symbol for symbol in pending}
                for future in as_completed(future_to_symbol):
                    symbol, ticker, book, ticker_state, book_state = future.result()
                    bulk_ticker_state = "present" if symbol in ticker_by_symbol else "absent"
                    bulk_book_state = "present" if symbol in book_by_symbol else "absent"
                    needs_history = True
                    history_outcome = "not_attempted"
                    final_disposition = "unresolved"
                    if ticker is not None:
                        if not source._needs_history(ticker, book):
                            metadata_metric = source._metadata_only_metric(symbol, ticker, book)
                            if metadata_metric is not None:
                                result[symbol] = metadata_metric
                                needs_history = False
                                history_outcome = "not_required"
                                final_disposition = "definitely_ineligible"
                        else:
                            try:
                                history = tuple(source._fetch_history(symbol))
                                result[symbol] = self._build_history_metric(source, symbol, ticker, book, history)
                                history_outcome = "success"
                                final_disposition = "eligible"
                                if len(history) == source.HISTORY_LIMIT:
                                    reusable_daily[symbol] = history
                            except Exception:
                                history_outcome = "failed"
                    if final_disposition == "unresolved":
                        next_pending.append(symbol)
                    events.append({
                        "symbol": symbol,
                        "bulk_ticker_state": bulk_ticker_state,
                        "bulk_book_state": bulk_book_state,
                        "targeted_ticker_state": ticker_state,
                        "targeted_book_state": book_state,
                        "reconciliation_attempt": attempt,
                        "metadata": self._metadata_summary(ticker, book),
                        "needs_history": needs_history,
                        "history_outcome": history_outcome,
                        "final_disposition": final_disposition,
                    })
            pending = sorted(next_pending)

        if pending:
            pending_set = set(pending)
            for event in events:
                if event["symbol"] in pending_set and event["reconciliation_attempt"] == max_rounds:
                    event["history_outcome"] = "exhausted"
                    event["final_disposition"] = "unresolved"

        self._last_reconciliation_events = tuple(sorted(events, key=lambda event: (event["symbol"], event["reconciliation_attempt"])))
        try:
            from providers.binance_opportunity_source import DailyCandleHandoff
            source._last_daily_candle_handoff = DailyCandleHandoff(limit=source.HISTORY_LIMIT, candles=dict(reusable_daily))
        except (ImportError, AttributeError):
            pass
        return result

    def _collect_metrics_legacy(self, candidates: tuple[UniverseCandidate, ...]) -> Mapping[str, MarketMetrics]:
        bulk = getattr(self._metrics_source, "metrics_bulk", None)
        if callable(bulk):
            raw = bulk(tuple(c.symbol for c in candidates))
            if not isinstance(raw, Mapping):
                raise TypeError("metrics source must return a mapping")
            return {symbol: raw[symbol] for symbol in (c.symbol for c in candidates) if symbol in raw}
        return {candidate.symbol: self._metrics_source.metrics(candidate.symbol) for candidate in candidates}

    def _collect_metrics(self, candidates: tuple[UniverseCandidate, ...]) -> Mapping[str, MarketMetrics]:
        symbols = tuple(c.symbol for c in candidates)
        startup_mode = getattr(self._metrics_source, "_startup_deadline", None) is not None
        try:
            result = self._startup_metrics(candidates) if startup_mode else self._collect_metrics_legacy(candidates)
        except TimeoutError:
            self._record_bootstrap(symbols, (), "timeout")
            raise
        except Exception:
            self._record_bootstrap(symbols, (), "exception")
            raise
        received = tuple(symbol for symbol in symbols if symbol in result)
        missing = tuple(symbol for symbol in symbols if symbol not in result)
        self._record_bootstrap(symbols, received, "complete" if not missing else "incomplete")
        if startup_mode and missing:
            raise RuntimeError(f"fresh discovery bootstrap incomplete: expected={len(symbols)} received={len(received)} missing={','.join(missing)}")
        return result

    def discover(self, top_n: int | None = None) -> OpportunityCandidateSet:
        candidates = self._universe.discover()
        now = self._clock()
        requested_top_n = top_n if top_n is not None else self._config.default_top_n
        startup_mode = getattr(self._metrics_source, "_startup_deadline", None) is not None
        if not startup_mode and self._cached_output is not None and now - self._cached_at < self._config.refresh_interval_seconds and self._cached_output.top_n == requested_top_n:
            return self._cached_output
        metrics = self._collect_metrics(candidates)
        output = self._ranker.rank(candidates, metrics, requested_top_n)
        self._cached_output = output
        self._cached_at = now
        return output
