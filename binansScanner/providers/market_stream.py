from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Protocol

import websockets

from enums import Timeframe
from models.market_event import MarketEvent, MarketEventNormalizationError, MarketEventType


class MarketStreamError(RuntimeError):
    pass


class MarketStreamDisconnected(MarketStreamError):
    pass


class MarketStreamSource(Protocol):
    async def connect(self) -> None: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def close(self) -> None: ...


class MarketEventNormalizer:
    """Normalize Binance websocket envelopes into canonical events."""

    _TIMEFRAME_VALUES = {tf.value for tf in Timeframe}

    def normalize(self, raw: dict[str, Any]) -> MarketEvent:
        if not isinstance(raw, dict):
            raise MarketEventNormalizationError("Market stream message must be a mapping")
        data = raw.get("data", raw)
        if not isinstance(data, dict):
            raise MarketEventNormalizationError("Market stream data must be a mapping")
        event_name = str(data.get("e", "")).strip()
        symbol = str(data.get("s", "")).strip().upper()
        event_ms = self._finite_int(data.get("E"), "event timestamp")
        if not symbol or event_ms is None:
            raise MarketEventNormalizationError("Market event requires symbol and event timestamp")
        event_timestamp = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)

        if event_name == "trade":
            trade_ms = self._finite_int(data.get("T"), "trade timestamp")
            payload = {
                "price": self._finite_float(data.get("p"), "trade price"),
                "quantity": self._finite_float(data.get("q"), "trade quantity"),
                "trade_id": str(data.get("t")),
            }
            return MarketEvent(
                symbol=symbol,
                event_timestamp=event_timestamp,
                source_timestamp=(datetime.fromtimestamp(trade_ms / 1000.0, tz=timezone.utc) if trade_ms is not None else None),
                event_type=MarketEventType.TRADE,
                source_event_id=str(data.get("t")) if data.get("t") is not None else None,
                payload=payload,
            )

        if event_name in {"24hrMiniTicker", "24hrTicker"}:
            payload = {
                "price": self._finite_float(data.get("c"), "ticker price"),
                "open": self._finite_float(data.get("o"), "ticker open", required=False),
                "high": self._finite_float(data.get("h"), "ticker high", required=False),
                "low": self._finite_float(data.get("l"), "ticker low", required=False),
                "volume": self._finite_float(data.get("v"), "ticker volume", required=False),
            }
            return MarketEvent(
                symbol=symbol,
                event_timestamp=event_timestamp,
                event_type=MarketEventType.TICKER,
                source_event_id=str(data.get("u")) if data.get("u") is not None else None,
                payload={k: v for k, v in payload.items() if v is not None},
            )

        if event_name == "kline":
            kline = data.get("k")
            if not isinstance(kline, dict):
                raise MarketEventNormalizationError("Kline event requires kline payload")
            interval = str(kline.get("i", ""))
            if interval not in self._TIMEFRAME_VALUES:
                raise MarketEventNormalizationError(f"Unsupported kline interval: {interval!r}")
            start_ms = self._finite_int(kline.get("t"), "kline start timestamp")
            close_ms = self._finite_int(kline.get("T"), "kline close timestamp")
            if start_ms is None or close_ms is None:
                raise MarketEventNormalizationError("Kline event requires start and close timestamps")
            closed = bool(kline.get("x", False))
            payload = {
                "timeframe": interval,
                "open_time": datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).isoformat(),
                "close_time": datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc).isoformat(),
                "open": self._finite_float(kline.get("o"), "kline open"),
                "high": self._finite_float(kline.get("h"), "kline high"),
                "low": self._finite_float(kline.get("l"), "kline low"),
                "close": self._finite_float(kline.get("c"), "kline close"),
                "volume": self._finite_float(kline.get("v"), "kline volume"),
                "is_closed": closed,
            }
            return MarketEvent(
                symbol=symbol,
                event_timestamp=event_timestamp,
                source_timestamp=datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc),
                event_type=MarketEventType.CANDLE_CLOSE if closed else MarketEventType.CANDLE_UPDATE,
                source_event_id=f"{interval}:{start_ms}",
                payload=payload,
            )

        raise MarketEventNormalizationError(f"Unsupported market event type: {event_name!r}")

    @staticmethod
    def _finite_float(value: Any, label: str, required: bool = True) -> float | None:
        if value is None and not required:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketEventNormalizationError(f"Invalid {label}") from exc
        if not math.isfinite(result):
            raise MarketEventNormalizationError(f"Non-finite {label}")
        return result

    @staticmethod
    def _finite_int(value: Any, label: str) -> int | None:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketEventNormalizationError(f"Invalid {label}") from exc
        if result < 0:
            raise MarketEventNormalizationError(f"Invalid {label}")
        return result


class BinanceWebSocketMarketStream:
    """Continuous Binance public websocket source; market-data only."""

    BASE_URL = "wss://stream.binance.com:9443/stream"

    def __init__(self, symbols: Iterable[str] | str, timeframes: Iterable[Timeframe] = ()) -> None:
        symbol_values = (symbols,) if isinstance(symbols, str) else symbols
        normalized_symbols = tuple(sorted({str(s).strip().lower() for s in symbol_values if str(s).strip()}))
        if not normalized_symbols:
            raise ValueError("At least one symbol is required")
        self._symbols = normalized_symbols
        self._timeframes = tuple(timeframes)
        self._socket: Any = None
        self._connected = False

    @property
    def url(self) -> str:
        streams = [f"{symbol}@trade" for symbol in self._symbols]
        streams.extend(f"{symbol}@kline_{tf.value}" for symbol in self._symbols for tf in self._timeframes)
        return f"{self.BASE_URL}?streams={'/'.join(streams)}"

    async def connect(self) -> None:
        try:
            self._socket = await websockets.connect(
                self.url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
            self._connected = True
        except Exception as exc:
            self._connected = False
            raise MarketStreamDisconnected(str(exc)) from exc

    async def _iterate(self) -> AsyncIterator[dict[str, Any]]:
        if self._socket is None or not self._connected:
            raise MarketStreamDisconnected("Market stream is not connected")
        try:
            async for message in self._socket:
                try:
                    yield json.loads(message)
                except json.JSONDecodeError as exc:
                    raise MarketStreamDisconnected("Invalid websocket JSON") from exc
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
            self._connected = False
            raise MarketStreamDisconnected(str(exc)) from exc

    def events(self) -> AsyncIterator[dict[str, Any]]:
        return self._iterate()

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        self._connected = False
        if socket is not None:
            await socket.close()


@dataclass(slots=True)
class MarketStreamStats:
    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    normalized_failures: int = 0
    reconnects: int = 0
    disconnects: int = 0


class MarketStreamRunner:
    """Resilient event loop with deterministic duplicate and ordering policy."""

    def __init__(
        self,
        source: MarketStreamSource,
        normalizer: MarketEventNormalizer | None = None,
        on_event: Callable[[MarketEvent], Awaitable[None] | None] | None = None,
        reconnect_delays: tuple[float, ...] = (0.1, 0.5, 1.0, 2.0),
        dedupe_capacity: int = 4096,
    ) -> None:
        if source is None:
            raise ValueError("Market stream source is required")
        if dedupe_capacity <= 0:
            raise ValueError("dedupe_capacity must be positive")
        self.source = source
        self.normalizer = normalizer or MarketEventNormalizer()
        self.on_event = on_event
        self.reconnect_delays = reconnect_delays
        self.dedupe_capacity = dedupe_capacity
        self.stats = MarketStreamStats()
        self._seen: dict[str, None] = {}
        self._last_timestamp: dict[tuple[str, MarketEventType, str | None], datetime] = {}
        self._stop = False

    async def run(self, max_events: int | None = None) -> None:
        while not self._stop:
            try:
                await self.source.connect()
                async for raw in self.source.events():
                    self.stats.received += 1
                    event = self._normalize(raw)
                    if event is None:
                        continue
                    if event.event_id in self._seen:
                        self.stats.duplicates += 1
                        continue
                    timeframe = str(event.payload.get("timeframe")) if event.payload.get("timeframe") is not None else None
                    order_key = (event.symbol, event.event_type, timeframe)
                    last = self._last_timestamp.get(order_key)
                    if last is not None and event.event_timestamp < last:
                        self.stats.out_of_order += 1
                        continue
                    self._remember(event.event_id)
                    self._last_timestamp[order_key] = max(event.event_timestamp, last) if last else event.event_timestamp
                    self.stats.accepted += 1
                    if self.on_event is not None:
                        result = self.on_event(event)
                        if asyncio.iscoroutine(result):
                            await result
                    if max_events is not None and self.stats.accepted >= max_events:
                        self._stop = True
                        break
                if not self._stop:
                    self.stats.disconnects += 1
                    await self._reconnect()
            except (MarketStreamDisconnected, OSError, asyncio.TimeoutError):
                if self._stop:
                    break
                self.stats.disconnects += 1
                await self._reconnect()
            finally:
                if self._stop:
                    await self.source.close()

    def stop(self) -> None:
        self._stop = True

    def _normalize(self, raw: dict[str, Any]) -> MarketEvent | None:
        try:
            return self.normalizer.normalize(raw)
        except MarketEventNormalizationError:
            self.stats.normalized_failures += 1
            return None

    def _remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        if len(self._seen) > self.dedupe_capacity:
            self._seen.pop(next(iter(self._seen)))

    async def _reconnect(self) -> None:
        index = min(self.stats.reconnects, max(0, len(self.reconnect_delays) - 1))
        delay = self.reconnect_delays[index] if self.reconnect_delays else 0.0
        self.stats.reconnects += 1
        await self.source.close()
        if delay > 0:
            await asyncio.sleep(delay)
