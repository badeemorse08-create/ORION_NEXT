from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
import asyncio
from dataclasses import dataclass

from models.market_event import MarketEvent, MarketEventType


MarketConsumer = Callable[[MarketEvent], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class RouteKey:
    event_type: MarketEventType
    timeframe: str | None = None


class MarketEventRouter:
    """Route normalized events without invoking the intelligence pipeline on every tick."""

    def __init__(self) -> None:
        self._consumers: dict[RouteKey, list[MarketConsumer]] = defaultdict(list)

    def subscribe(
        self,
        event_type: MarketEventType,
        consumer: MarketConsumer,
        timeframe: str | None = None,
    ) -> None:
        if not callable(consumer):
            raise TypeError("consumer must be callable")
        self._consumers[RouteKey(event_type, timeframe)].append(consumer)

    async def route(self, event: MarketEvent) -> int:
        consumers = list(self._consumers.get(RouteKey(event.event_type, None), ()))
        timeframe = event.payload.get("timeframe")
        if timeframe is not None:
            consumers.extend(self._consumers.get(RouteKey(event.event_type, str(timeframe)), ()))
        for consumer in consumers:
            result = consumer(event)
            if asyncio.iscoroutine(result):
                await result
        return len(consumers)

    def has_consumer(self, event_type: MarketEventType, timeframe: str | None = None) -> bool:
        return bool(self._consumers.get(RouteKey(event_type, timeframe)))


class TimeframeAwareMarketRouter(MarketEventRouter):
    """Explicit boundary: candle closes may feed timeframe intelligence; ticks do not."""

    def subscribe_candle_intelligence(self, timeframe: str, consumer: MarketConsumer) -> None:
        self.subscribe(MarketEventType.CANDLE_CLOSE, consumer, timeframe=timeframe)
