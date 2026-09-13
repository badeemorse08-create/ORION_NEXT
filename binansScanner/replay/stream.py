from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from models.market_event import MarketEvent, MarketEventType

from replay.clock import ReplayClock
from replay.dataset import HistoricalDataset


class HistoricalMarketEventStream:
    """Finite replay stream that exposes events progressively in simulation time."""

    def __init__(self, dataset: HistoricalDataset, clock: ReplayClock) -> None:
        self.dataset = dataset
        self.clock = clock
        self._index = 0
        self._connected = False
        self._closed = False
        self._last_released_timestamp: datetime | None = None

    @property
    def position(self) -> int:
        return self._index

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("historical replay stream is closed")
        self._connected = True

    def _raw(self, event: MarketEvent) -> dict[str, Any]:
        ts_ms = int(event.event_timestamp.astimezone(timezone.utc).timestamp() * 1000)
        source_ms = int((event.source_timestamp or event.event_timestamp).astimezone(timezone.utc).timestamp() * 1000)
        payload = dict(event.payload)
        if event.event_type is MarketEventType.TRADE:
            return {
                "e": "trade", "s": event.symbol, "E": ts_ms, "T": source_ms,
                "p": str(payload["price"]), "q": str(payload.get("quantity", 0.0)),
                "t": event.source_event_id or f"{event.symbol}-{ts_ms}",
            }
        if event.event_type is MarketEventType.TICKER:
            return {
                "e": "24hrTicker", "s": event.symbol, "E": ts_ms,
                "c": str(payload["price"]), "o": str(payload.get("open", payload["price"])),
                "h": str(payload.get("high", payload["price"])), "l": str(payload.get("low", payload["price"])),
                "v": str(payload.get("volume", 0.0)),
                "u": event.source_event_id or str(ts_ms),
            }
        timeframe = str(payload.get("timeframe", "1m"))
        open_time = datetime.fromisoformat(str(payload.get("open_time", event.event_timestamp.isoformat())).replace("Z", "+00:00"))
        close_time = datetime.fromisoformat(str(payload.get("close_time", event.event_timestamp.isoformat())).replace("Z", "+00:00"))
        return {
            "e": "kline", "s": event.symbol, "E": ts_ms,
            "k": {
                "i": timeframe,
                "t": int(open_time.astimezone(timezone.utc).timestamp() * 1000),
                "T": int(close_time.astimezone(timezone.utc).timestamp() * 1000),
                "o": str(payload["open"]), "h": str(payload["high"]),
                "l": str(payload["low"]), "c": str(payload["close"]),
                "v": str(payload.get("volume", 0.0)),
                "x": bool(payload.get("is_closed", event.event_type is MarketEventType.CANDLE_CLOSE)),
            },
        }

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if not self._connected:
            raise RuntimeError("historical replay stream is not connected")
        while self._index < len(self.dataset.events):
            event = self.dataset.events[self._index].to_market_event()
            if self._last_released_timestamp is not None and event.event_timestamp < self._last_released_timestamp:
                raise RuntimeError("historical replay event ordering violation")

            target_wall_delay = 0.0
            if self._last_released_timestamp is not None:
                simulation_gap = (event.event_timestamp - self._last_released_timestamp).total_seconds()
                target_wall_delay = self.clock.wall_delay_for(simulation_gap)
                if target_wall_delay > 0:
                    await asyncio.sleep(target_wall_delay)

            self.clock.advance_to(event.event_timestamp)
            self._last_released_timestamp = event.event_timestamp
            self._index += 1
            yield self._raw(event)
            await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True
        self._connected = False


__all__ = ["HistoricalMarketEventStream"]
