"""Bulk Binance Spot market source for dynamic opportunity discovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from models.opportunity import MarketMetrics


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: Any


@dataclass(frozen=True, slots=True)
class DailyCandleHandoff:
    """Discovery-scoped daily candles available for equivalent deep evaluation."""

    limit: int
    candles: Mapping[str, tuple[Sequence[Any], ...]]


class BinanceSpotOpportunitySource:
    """Discover spot symbols and derive distinct market-quality features."""

    BASE_URL = "https://api.binance.com/api/v3"
    HISTORY_INTERVAL = "1d"
    HISTORY_LIMIT = 32
    METRICS_HISTORY_WINDOW = 31
    MIN_HISTORY_CANDLES = 22
    HISTORY_RECOVERY_ATTEMPTS = 2
    DISCOVERY_CONCURRENCY = 4
    METADATA_CONCURRENCY = 2
    EARLY_MIN_QUOTE_VOLUME_24H = 1_000_000.0
    EARLY_MAX_SPREAD_BPS = 50.0
    RETRY_MAX_ATTEMPTS = 4
    RETRY_INITIAL_BACKOFF_SECONDS = 0.5
    RETRY_BACKOFF_MULTIPLIER = 2.0
    RETRY_MAX_BACKOFF_SECONDS = 2.0
    RETRY_JITTER_SECONDS = 0.0
    RETRY_SERVER_BACKOFF_MAX_SECONDS = 10.0
    RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
    RECONCILIATION_MAX_ATTEMPTS = 2

    def __init__(self, ttl_seconds: float = 30.0, timeout_seconds: float = 10.0, clock=time.monotonic) -> None:
        if ttl_seconds < 0 or timeout_seconds <= 0:
            raise ValueError("invalid cache/timeout configuration")
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_request_id = 0
        self._request_events: list[dict[str, Any]] = []
        self._reconciliation_events: list[dict[str, Any]] = []
        self._last_daily_candle_handoff: DailyCandleHandoff | None = None

    @property
    def request_events(self) -> tuple[dict[str, Any], ...]:
        """Return immutable-observer access to recorded public request attempts."""
        with self._request_lock:
            return tuple(dict(event) for event in self._request_events)

    @property
    def reconciliation_events(self) -> tuple[dict[str, Any], ...]:
        with self._request_lock:
            return tuple(dict(event) for event in self._reconciliation_events)

    def _new_request_id(self) -> int:
        with self._request_lock:
            self._next_request_id += 1
            return self._next_request_id

    @staticmethod
    def _request_stage(path: str) -> str:
        if path == "exchangeInfo":
            return "universe"
        if path in {"ticker/24hr", "ticker/bookTicker"}:
            return "metadata"
        if path == "klines":
            return "history"
        return "public_market_data"

    @classmethod
    def _retry_failure(cls, exc: BaseException) -> tuple[bool, str]:
        if isinstance(exc, HTTPError):
            return exc.code in cls.RETRYABLE_HTTP_STATUSES, f"http_{exc.code}"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True, "read_or_transport_timeout"
        if isinstance(exc, (ConnectionError, URLError)):
            return True, "transport_error"
        return False, "non_retryable"

    @classmethod
    def _server_backoff(cls, exc: BaseException) -> float | None:
        if not isinstance(exc, HTTPError):
            return None
        value = exc.headers.get("Retry-After") if exc.headers is not None else None
        if value is None:
            return None
        try:
            return min(max(float(value), 0.0), cls.RETRY_SERVER_BACKOFF_MAX_SECONDS)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _backoff_for_attempt(cls, attempt: int, exc: BaseException) -> float:
        server_backoff = cls._server_backoff(exc)
        if server_backoff is not None:
            return server_backoff
        base = cls.RETRY_INITIAL_BACKOFF_SECONDS * (cls.RETRY_BACKOFF_MULTIPLIER ** max(attempt - 1, 0))
        return min(base, cls.RETRY_MAX_BACKOFF_SECONDS) + cls.RETRY_JITTER_SECONDS

    def _record_request_event(
        self,
        *,
        request_id: int,
        endpoint: str,
        stage: str,
        symbol: str | None,
        attempt: int,
        timeout_requested: float,
        timeout_effective: float,
        start_timestamp: float,
        end_timestamp: float,
        exception_type: str | None,
        failure_category: str | None,
        backoff: float,
        outcome: str,
    ) -> None:
        event = {
            "request_id": request_id,
            "endpoint": endpoint,
            "stage": stage,
            "symbol": symbol,
            "attempt": attempt,
            "timeout_requested": timeout_requested,
            "timeout_effective": timeout_effective,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "exception_type": exception_type,
            "failure_category": failure_category,
            "backoff": backoff,
            "elapsed_seconds": end_timestamp - start_timestamp,
            "outcome": outcome,
        }
        with self._request_lock:
            self._request_events.append(event)
            if len(self._request_events) > 512:
                del self._request_events[:-512]

    def _request_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params or {})}" if params else ""
        request_id = self._new_request_id()
        endpoint = f"{self.BASE_URL}/{path}"
        stage = self._request_stage(path)
        symbol = str(params.get("symbol")).upper() if params and params.get("symbol") is not None else None
        timeout_requested = float(self.timeout_seconds)

        for attempt in range(1, self.RETRY_MAX_ATTEMPTS + 1):
            start_timestamp = time.monotonic()
            timeout_effective = timeout_requested
            try:
                request = Request(f"{endpoint}{query}", headers={"Accept": "application/json"})
                with urlopen(request, timeout=timeout_effective) as response:
                    payload = json.load(response)
                end_timestamp = time.monotonic()
                self._record_request_event(
                    request_id=request_id,
                    endpoint=endpoint,
                    stage=stage,
                    symbol=symbol,
                    attempt=attempt,
                    timeout_requested=timeout_requested,
                    timeout_effective=timeout_effective,
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
                self._record_request_event(
                    request_id=request_id,
                    endpoint=endpoint,
                    stage=stage,
                    symbol=symbol,
                    attempt=attempt,
                    timeout_requested=timeout_requested,
                    timeout_effective=timeout_effective,
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

        raise RuntimeError("request retry loop exhausted unexpectedly")

    def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request_json(path, params)

    def _cached(self, key: str, loader):
        now = self._clock()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > now:
                return entry.value
        value = loader()
        with self._cache_lock:
            current = self._cache.get(key)
            if current is not None and current.expires_at > now:
                return current.value
            self._cache[key] = _CacheEntry(now + self.ttl_seconds, value)
        return value

    def exchange_info(self) -> Mapping[str, Any]:
        return self._cached("exchange_info", lambda: self._get_json("exchangeInfo"))

    def take_daily_candle_handoff(self) -> DailyCandleHandoff | None:
        """Consume the latest discovery-scoped daily candle dataset exactly once."""
        handoff = self._last_daily_candle_handoff
        self._last_daily_candle_handoff = None
        return handoff

    @staticmethod
    def _ema(values: Sequence[float], period: int) -> float:
        if len(values) < period:
            raise ValueError("insufficient history for EMA")
        alpha = 2.0 / (period + 1.0)
        ema = statistics.fmean(values[:period])
        for value in values[period:]:
            ema = alpha * value + (1.0 - alpha) * ema
        return ema

    @classmethod
    def _history_features(cls, rows: Sequence[Any]) -> tuple[float, float, float, float, float, float]:
        closes = [float(row[4]) for row in rows if isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) > 4]
        if len(closes) < cls.MIN_HISTORY_CANDLES:
            raise ValueError("insufficient price history")
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i] > 0 and closes[i - 1] > 0]
        if len(returns) < cls.MIN_HISTORY_CANDLES - 1:
            raise ValueError("insufficient return history")

        volatility = statistics.stdev(returns)
        ema_fast = cls._ema(closes[-21:], 7)
        ema_slow = cls._ema(closes[-21:], 21)
        trend_direction = max(-1.0, min(1.0, (ema_fast / ema_slow - 1.0) / 0.05)) if ema_slow > 0 else 0.0
        recent_returns = returns[-7:]
        positive_fraction = sum(value > 0 for value in recent_returns) / len(recent_returns)
        negative_fraction = sum(value < 0 for value in recent_returns) / len(recent_returns)
        trend_persistence = max(positive_fraction, negative_fraction)
        trend_quality = min(1.0, abs(trend_direction)) * trend_persistence

        roc_3 = closes[-1] / closes[-4] - 1.0
        roc_7 = closes[-1] / closes[-8] - 1.0
        momentum_raw = roc_3 - (roc_7 / 7.0 * 3.0)
        momentum_direction = max(-1.0, min(1.0, momentum_raw / 0.03))
        momentum_quality = min(1.0, abs(momentum_direction))
        return volatility, trend_quality, trend_direction, trend_persistence, momentum_quality, momentum_direction

    @classmethod
    def _validate_history_payload(cls, history: Any) -> tuple[Sequence[Any], ...]:
        if not isinstance(history, list):
            raise ValueError("invalid price history payload")
        if not history or len(history) < cls.MIN_HISTORY_CANDLES:
            raise ValueError("insufficient price history")

        for index, row in enumerate(history):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 6:
                raise ValueError(f"invalid price history candle at index {index}")
            try:
                timestamp = float(row[0])
                open_price = float(row[1])
                high_price = float(row[2])
                low_price = float(row[3])
                close_price = float(row[4])
                volume = float(row[5])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid price history candle at index {index}") from exc
            values = (timestamp, open_price, high_price, low_price, close_price, volume)
            if not all(math.isfinite(value) for value in values):
                raise ValueError(f"invalid price history candle at index {index}")
            if timestamp <= 0 or volume < 0 or min(open_price, high_price, low_price, close_price) <= 0:
                raise ValueError(f"invalid price history candle at index {index}")
            if high_price < max(open_price, close_price, low_price) or low_price > min(open_price, close_price, high_price):
                raise ValueError(f"invalid price history candle at index {index}")

        return tuple(history)

    @classmethod
    def _history_recovery_allowed(cls, exc: ValueError) -> bool:
        return str(exc) in {"insufficient price history", "insufficient return history"}

    def _record_history_recovery(
        self,
        *,
        symbol: str,
        attempt: int,
        history_outcome: str,
        final_disposition: str,
        history_length: int | None,
        exception_type: str | None = None,
        exception_message: str | None = None,
    ) -> None:
        with self._request_lock:
            self._reconciliation_events.append(
                {
                    "attempt": attempt,
                    "symbol": symbol,
                    "metadata_state": "history_incomplete",
                    "metadata": None,
                    "needs_history": True,
                    "history_outcome": history_outcome,
                    "final_disposition": final_disposition,
                    "history_length": history_length,
                    "exception_type": exception_type,
                    "exception_message": exception_message,
                }
            )

    def _fetch_history(self, symbol: str) -> tuple[Sequence[Any], ...]:
        """Fetch fresh valid history; short history is recoverable, malformed data is fatal."""
        key = f"klines_{symbol}"
        for attempt in range(1, self.HISTORY_RECOVERY_ATTEMPTS + 1):
            history: Any = None
            try:
                history = self._get_json(
                    "klines",
                    {"symbol": symbol, "interval": self.HISTORY_INTERVAL, "limit": self.HISTORY_LIMIT},
                )
                validated = self._validate_history_payload(history)
                with self._cache_lock:
                    now = self._clock()
                    self._cache[key] = _CacheEntry(now + self.ttl_seconds, validated)
                if attempt > 1:
                    self._record_history_recovery(
                        symbol=symbol,
                        attempt=attempt,
                        history_outcome="recovered",
                        final_disposition="eligible",
                        history_length=len(validated),
                    )
                return validated
            except ValueError as exc:
                history_length = len(history) if isinstance(history, list) else None
                if not self._history_recovery_allowed(exc) or attempt >= self.HISTORY_RECOVERY_ATTEMPTS:
                    self._record_history_recovery(
                        symbol=symbol,
                        attempt=attempt,
                        history_outcome="exhausted" if self._history_recovery_allowed(exc) else "invalid",
                        final_disposition="unresolved",
                        history_length=history_length,
                        exception_type=type(exc).__name__,
                        exception_message=str(exc),
                    )
                    raise
                self._record_history_recovery(
                    symbol=symbol,
                    attempt=attempt,
                    history_outcome="retrying",
                    final_disposition="unresolved",
                    history_length=history_length,
                    exception_type=type(exc).__name__,
                    exception_message=str(exc),
                )
        raise RuntimeError("history recovery loop exhausted unexpectedly")

    @classmethod
    def _needs_history(cls, ticker: Mapping[str, Any] | None, book: Mapping[str, Any] | None) -> bool:
        if ticker is None:
            return True
        try:
            quote_volume = float(ticker["quoteVolume"])
        except (KeyError, TypeError, ValueError):
            return True
        if not math.isfinite(quote_volume):
            return True
        if quote_volume < cls.EARLY_MIN_QUOTE_VOLUME_24H:
            return False
        if book is None:
            return True
        try:
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
        except (KeyError, TypeError, ValueError):
            return True
        if bid <= 0 or ask < bid:
            return True
        spread_bps = ((ask - bid) / ((ask + bid) / 2.0)) * 10_000.0
        return not math.isfinite(spread_bps) or spread_bps <= cls.EARLY_MAX_SPREAD_BPS

    @classmethod
    def _metadata_only_metric(cls, symbol: str, ticker: Mapping[str, Any] | None, book: Mapping[str, Any] | None) -> MarketMetrics | None:
        if ticker is None:
            return None
        try:
            quote_volume = float(ticker["quoteVolume"])
            last_price = float(ticker["lastPrice"])
            change_pct = float(ticker["priceChangePercent"])
            weighted_avg = float(ticker["weightedAvgPrice"])
        except (KeyError, TypeError, ValueError):
            return None
        spread_bps = None
        if book is not None:
            try:
                bid = float(book["bidPrice"])
                ask = float(book["askPrice"])
                if bid > 0 and ask >= bid:
                    spread_bps = ((ask - bid) / ((ask + bid) / 2.0)) * 10_000.0
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return None
        safe_volume = quote_volume if math.isfinite(quote_volume) else 0.0
        volume_quality = min(math.log1p(max(safe_volume, 0.0)) / math.log1p(100_000_000.0), 1.0)
        return MarketMetrics(
            symbol=symbol,
            quote_volume_24h=quote_volume,
            volatility=0.0,
            spread_bps=spread_bps,
            tradable=True,
            last_price=last_price,
            volume_quality=volume_quality,
            trend_quality=None,
            momentum_quality=None,
            structure_quality=None,
            price_change_pct_24h=change_pct,
            weighted_avg_price_24h=weighted_avg,
            trend_direction=None,
            trend_persistence=None,
            momentum_direction=None,
        )

    @staticmethod
    def _metadata_summary(ticker: Mapping[str, Any] | None, book: Mapping[str, Any] | None) -> dict[str, Any]:
        if ticker is None:
            return {"ticker": None, "book_ticker": None}
        summary: dict[str, Any] = {
            "quote_volume_24h": ticker.get("quoteVolume"),
            "last_price": ticker.get("lastPrice"),
            "price_change_percent_24h": ticker.get("priceChangePercent"),
            "weighted_avg_price_24h": ticker.get("weightedAvgPrice"),
        }
        if book is not None:
            summary["bid_price"] = book.get("bidPrice")
            summary["ask_price"] = book.get("askPrice")
        return summary

    def _record_reconciliation(
        self,
        *,
        attempt: int,
        symbol: str,
        metadata_state: str,
        ticker: Mapping[str, Any] | None,
        book: Mapping[str, Any] | None,
        needs_history: bool | None,
        history_outcome: str,
        final_disposition: str,
    ) -> None:
        with self._request_lock:
            self._reconciliation_events.append(
                {
                    "attempt": attempt,
                    "symbol": symbol,
                    "metadata_state": metadata_state,
                    "metadata": self._metadata_summary(ticker, book),
                    "needs_history": needs_history,
                    "history_outcome": history_outcome,
                    "final_disposition": final_disposition,
                }
            )

    def reconcile_missing_symbols(self, symbols: Sequence[str]) -> Mapping[str, MarketMetrics]:
        """Reconcile missing startup symbols using fresh same-run metadata only."""
        wanted = tuple(sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()}))
        if not wanted:
            return {}

        resolved: dict[str, MarketMetrics] = {}
        pending = wanted
        existing_handoff = self._last_daily_candle_handoff
        reusable_daily: dict[str, tuple[Sequence[Any], ...]] = {}
        if existing_handoff is not None and existing_handoff.limit == self.HISTORY_LIMIT:
            reusable_daily.update(existing_handoff.candles)

        for attempt in range(1, self.RECONCILIATION_MAX_ATTEMPTS + 1):
            if not pending:
                break
            try:
                with ThreadPoolExecutor(max_workers=self.METADATA_CONCURRENCY, thread_name_prefix="orion-reconciliation-metadata") as executor:
                    ticker_future = executor.submit(self._get_json, "ticker/24hr")
                    book_future = executor.submit(self._get_json, "ticker/bookTicker")
                    tickers = ticker_future.result()
                    books = book_future.result()
            except Exception:
                for symbol in pending:
                    self._record_reconciliation(
                        attempt=attempt,
                        symbol=symbol,
                        metadata_state="acquisition_exception",
                        ticker=None,
                        book=None,
                        needs_history=None,
                        history_outcome="not_attempted",
                        final_disposition="unresolved",
                    )
                raise

            ticker_by_symbol = {
                str(row.get("symbol", "")).upper(): row
                for row in tickers
                if isinstance(row, Mapping)
            }
            book_by_symbol = {
                str(row.get("symbol", "")).upper(): row
                for row in books
                if isinstance(row, Mapping)
            }
            history_symbols: list[str] = []
            metadata_states: dict[str, tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None]] = {}
            for symbol in pending:
                ticker = ticker_by_symbol.get(symbol)
                book = book_by_symbol.get(symbol)
                if ticker is None:
                    metadata_states[symbol] = ("ticker_missing", ticker, book)
                elif book is None:
                    metadata_states[symbol] = ("book_missing", ticker, book)
                else:
                    metadata_states[symbol] = ("complete", ticker, book)
                needs_history = self._needs_history(ticker, book)
                if not needs_history:
                    metric = self._metadata_only_metric(symbol, ticker, book)
                    if metric is not None:
                        resolved[symbol] = metric
                        self._record_reconciliation(
                            attempt=attempt,
                            symbol=symbol,
                            metadata_state=metadata_states[symbol][0],
                            ticker=ticker,
                            book=book,
                            needs_history=False,
                            history_outcome="not_required",
                            final_disposition="definitely_ineligible",
                        )
                        continue
                history_symbols.append(symbol)

            histories: dict[str, tuple[Sequence[Any], ...]] = {}
            with ThreadPoolExecutor(max_workers=self.DISCOVERY_CONCURRENCY, thread_name_prefix="orion-reconciliation-history") as executor:
                future_to_symbol = {executor.submit(self._fetch_history, symbol): symbol for symbol in history_symbols}
                for future in as_completed(future_to_symbol):
                    symbol = future_to_symbol[future]
                    try:
                        histories[symbol] = future.result()
                    except TimeoutError:
                        for pending_future in future_to_symbol:
                            pending_future.cancel()
                        raise
                    except Exception:
                        continue

            next_pending: list[str] = []
            for symbol in pending:
                if symbol in resolved:
                    continue
                ticker = ticker_by_symbol.get(symbol)
                book = book_by_symbol.get(symbol)
                history = histories.get(symbol)
                if ticker is None or history is None:
                    next_pending.append(symbol)
                    self._record_reconciliation(
                        attempt=attempt,
                        symbol=symbol,
                        metadata_state=metadata_states[symbol][0],
                        ticker=ticker,
                        book=book,
                        needs_history=True,
                        history_outcome="missing_or_failed",
                        final_disposition="unresolved",
                    )
                    continue
                try:
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
                    metric_history = history[-self.METRICS_HISTORY_WINDOW:]
                    volatility, trend_quality, trend_direction, trend_persistence, momentum_quality, momentum_direction = self._history_features(metric_history)
                    structure_deviation = abs(last_price / weighted_avg - 1.0) if weighted_avg > 0 else math.inf
                    structure_quality = max(0.0, 1.0 - min(structure_deviation / 0.05, 1.0)) if math.isfinite(structure_deviation) else 0.0
                    volume_quality = min(math.log1p(max(quote_volume, 0.0)) / math.log1p(100_000_000.0), 1.0)
                    resolved[symbol] = MarketMetrics(
                        symbol=symbol,
                        quote_volume_24h=quote_volume,
                        volatility=volatility,
                        spread_bps=spread_bps,
                        tradable=True,
                        last_price=last_price,
                        volume_quality=volume_quality,
                        trend_quality=trend_quality,
                        momentum_quality=momentum_quality,
                        structure_quality=structure_quality,
                        price_change_pct_24h=change_pct,
                        weighted_avg_price_24h=weighted_avg,
                        trend_direction=trend_direction,
                        trend_persistence=trend_persistence,
                        momentum_direction=momentum_direction,
                    )
                    if len(history) == self.HISTORY_LIMIT:
                        reusable_daily[symbol] = history
                    self._record_reconciliation(
                        attempt=attempt,
                        symbol=symbol,
                        metadata_state=metadata_states[symbol][0],
                        ticker=ticker,
                        book=book,
                        needs_history=True,
                        history_outcome="success",
                        final_disposition="eligible",
                    )
                except (KeyError, TypeError, ValueError, statistics.StatisticsError, ZeroDivisionError):
                    next_pending.append(symbol)
                    self._record_reconciliation(
                        attempt=attempt,
                        symbol=symbol,
                        metadata_state=metadata_states[symbol][0],
                        ticker=ticker,
                        book=book,
                        needs_history=True,
                        history_outcome="invalid",
                        final_disposition="unresolved",
                    )
            self._last_daily_candle_handoff = DailyCandleHandoff(limit=self.HISTORY_LIMIT, candles=dict(reusable_daily))
            pending = tuple(next_pending)

        for symbol in pending:
            self._record_reconciliation(
                attempt=self.RECONCILIATION_MAX_ATTEMPTS,
                symbol=symbol,
                metadata_state="unresolved_after_reconciliation",
                ticker=None,
                book=None,
                needs_history=True,
                history_outcome="exhausted",
                final_disposition="unresolved",
            )
        return resolved

    def metrics_bulk(self, symbols: Sequence[str]) -> Mapping[str, MarketMetrics]:
        wanted = tuple(sorted({s.upper() for s in symbols}))
        self._last_daily_candle_handoff = None
        if not wanted:
            return {}

        with ThreadPoolExecutor(max_workers=self.METADATA_CONCURRENCY, thread_name_prefix="orion-discovery-metadata") as executor:
            ticker_future = executor.submit(self._cached, "ticker_24h", lambda: self._get_json("ticker/24hr"))
            book_future = executor.submit(self._cached, "book_ticker", lambda: self._get_json("ticker/bookTicker"))
            tickers = ticker_future.result()
            books = book_future.result()
        ticker_by_symbol = {str(row.get("symbol", "")).upper(): row for row in tickers if isinstance(row, Mapping)}
        book_by_symbol = {str(row.get("symbol", "")).upper(): row for row in books if isinstance(row, Mapping)}
        history_symbols = tuple(
            symbol for symbol in wanted
            if self._needs_history(ticker_by_symbol.get(symbol), book_by_symbol.get(symbol))
        )

        histories: dict[str, tuple[Sequence[Any], ...]] = {}
        with ThreadPoolExecutor(max_workers=self.DISCOVERY_CONCURRENCY, thread_name_prefix="orion-discovery-history") as executor:
            future_to_symbol = {
                executor.submit(self._fetch_history, symbol): symbol
                for symbol in history_symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    histories[symbol] = future.result()
                except TimeoutError:
                    for pending in future_to_symbol:
                        pending.cancel()
                    raise
                except Exception:
                    continue

        result: dict[str, MarketMetrics] = {}
        reusable_daily: dict[str, tuple[Sequence[Any], ...]] = {}
        for symbol in wanted:
            ticker = ticker_by_symbol.get(symbol)
            if not self._needs_history(ticker, book_by_symbol.get(symbol)):
                metadata_metric = self._metadata_only_metric(symbol, ticker, book_by_symbol.get(symbol))
                if metadata_metric is not None:
                    result[symbol] = metadata_metric
                    continue
            history = histories.get(symbol)
            if ticker is None or history is None:
                continue
            try:
                last_price = float(ticker["lastPrice"])
                quote_volume = float(ticker["quoteVolume"])
                change_pct = float(ticker["priceChangePercent"])
                weighted_avg = float(ticker["weightedAvgPrice"])
                book = book_by_symbol.get(symbol)
                spread_bps = None
                if book is not None:
                    bid = float(book["bidPrice"])
                    ask = float(book["askPrice"])
                    if bid > 0 and ask >= bid:
                        spread_bps = ((ask - bid) / ((ask + bid) / 2.0)) * 10_000.0

                metric_history = history[-self.METRICS_HISTORY_WINDOW:]
                volatility, trend_quality, trend_direction, trend_persistence, momentum_quality, momentum_direction = self._history_features(metric_history)
                structure_deviation = abs(last_price / weighted_avg - 1.0) if weighted_avg > 0 else math.inf
                structure_quality = max(0.0, 1.0 - min(structure_deviation / 0.05, 1.0)) if math.isfinite(structure_deviation) else 0.0
                volume_quality = min(math.log1p(max(quote_volume, 0.0)) / math.log1p(100_000_000.0), 1.0)

                result[symbol] = MarketMetrics(
                    symbol=symbol,
                    quote_volume_24h=quote_volume,
                    volatility=volatility,
                    spread_bps=spread_bps,
                    tradable=True,
                    last_price=last_price,
                    volume_quality=volume_quality,
                    trend_quality=trend_quality,
                    momentum_quality=momentum_quality,
                    structure_quality=structure_quality,
                    price_change_pct_24h=change_pct,
                    weighted_avg_price_24h=weighted_avg,
                    trend_direction=trend_direction,
                    trend_persistence=trend_persistence,
                    momentum_direction=momentum_direction,
                )
                if len(history) == self.HISTORY_LIMIT:
                    reusable_daily[symbol] = history
            except (KeyError, TypeError, ValueError, statistics.StatisticsError, ZeroDivisionError):
                continue

        self._last_daily_candle_handoff = DailyCandleHandoff(
            limit=self.HISTORY_LIMIT,
            candles=dict(reusable_daily),
        )
        return result
