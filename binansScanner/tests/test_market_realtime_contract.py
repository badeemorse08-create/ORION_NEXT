from __future__ import annotations

import unittest
from datetime import datetime, timezone

from models.market_event import MarketEvent, MarketEventType
from providers.market_stream import BinanceWebSocketMarketStream, MarketEventNormalizer, MarketStreamRunner, MarketStreamDisconnected
from services.market_event_router import TimeframeAwareMarketRouter


def trade_message(ts: int = 61000, trade_id: int = 1, symbol: str = "BTCUSDT") -> dict:
    return {"data": {"e": "trade", "E": ts, "T": ts, "s": symbol, "t": trade_id, "p": "100.5", "q": "2"}}


def candle_message(ts: int = 61000, closed: bool = True, interval: str = "1m") -> dict:
    return {"data": {"e": "kline", "E": ts, "s": "BTCUSDT", "k": {"t": ts - 60000, "T": ts, "s": "BTCUSDT", "i": interval, "o": "100", "h": "101", "l": "99", "c": "100.5", "v": "10", "x": closed}}}


class FakeSource:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = iter(batches)
        self.connected = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connected += 1

    def events(self):
        async def iterator():
            try:
                batch = next(self.batches)
            except StopIteration as exc:
                raise MarketStreamDisconnected("done") from exc
            for item in batch:
                yield item
        return iterator()

    async def close(self) -> None:
        self.closed += 1


class TestMarketEventContract(unittest.TestCase):
    def test_normalized_trade_contract(self) -> None:
        event = MarketEventNormalizer().normalize(trade_message())
        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertEqual(event.event_type, MarketEventType.TRADE)
        self.assertEqual(event.payload["price"], 100.5)
        self.assertEqual(event.source_event_id, "1")
        self.assertTrue(event.event_id)
        self.assertEqual(event.event_timestamp.tzinfo, timezone.utc)

    def test_source_event_id_is_stable_identity(self) -> None:
        source_event_id = "trade-123"
        left = MarketEvent(
            symbol="BTCUSDT",
            event_timestamp=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            source_timestamp=datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc),
            event_type=MarketEventType.TRADE,
            payload={"price": 100.0, "quantity": 1.0},
            source_event_id=source_event_id,
        )
        right = MarketEvent(
            symbol="BTCUSDT",
            event_timestamp=datetime(2025, 1, 1, 0, 2, tzinfo=timezone.utc),
            source_timestamp=None,
            event_type=MarketEventType.TRADE,
            payload={"price": 101.0, "quantity": 2.0},
            source_event_id=source_event_id,
        )
        self.assertEqual(left.event_id, right.event_id)

    def test_candle_close_preserves_timeframe_semantics(self) -> None:
        event = MarketEventNormalizer().normalize(candle_message(interval="4h"))
        self.assertEqual(event.event_type, MarketEventType.CANDLE_CLOSE)
        self.assertEqual(event.payload["timeframe"], "4h")
        self.assertTrue(event.payload["is_closed"])

    def test_websocket_subscription_preserves_required_timeframes(self) -> None:
        from enums import Timeframe
        stream = BinanceWebSocketMarketStream("BTCUSDT", [Timeframe.M1, Timeframe.M5, Timeframe.H1, Timeframe.H4, Timeframe.D1])
        for timeframe in ("1m", "5m", "1h", "4h", "1d"):
            self.assertIn(f"@kline_{timeframe}", stream.url)

    def test_payload_is_immutable(self) -> None:
        event = MarketEventNormalizer().normalize(trade_message())
        with self.assertRaises(TypeError):
            event.payload["price"] = 1.0

    def test_unsupported_event_is_rejected(self) -> None:
        raw = trade_message()
        raw["data"]["e"] = "unknown"
        with self.assertRaises(ValueError):
            MarketEventNormalizer().normalize(raw)


class TestMarketEventRouter(unittest.IsolatedAsyncioTestCase):
    async def test_trade_does_not_route_to_candle_intelligence(self) -> None:
        router = TimeframeAwareMarketRouter()
        seen: list[str] = []
        router.subscribe_candle_intelligence("1m", lambda event: seen.append(event.event_type.value))
        routed = await router.route(MarketEventNormalizer().normalize(trade_message()))
        self.assertEqual(routed, 0)
        self.assertEqual(seen, [])

    async def test_only_closed_candle_routes_to_timeframe_consumer(self) -> None:
        router = TimeframeAwareMarketRouter()
        seen: list[str] = []
        router.subscribe_candle_intelligence("1m", lambda event: seen.append(event.event_type.value))
        await router.route(MarketEventNormalizer().normalize(candle_message(closed=False)))
        await router.route(MarketEventNormalizer().normalize(candle_message(ts=121000, closed=True)))
        self.assertEqual(seen, ["candle_close"])


class TestMarketStreamResilience(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_event_is_suppressed_across_reconnect(self) -> None:
        source = FakeSource([[trade_message(ts=61000, trade_id=1)], [trade_message(ts=61000, trade_id=1), trade_message(ts=62000, trade_id=2)]])
        seen: list[str] = []
        runner = MarketStreamRunner(source, on_event=lambda event: seen.append(event.source_event_id or ""), reconnect_delays=(0,))
        await runner.run(max_events=2)
        self.assertEqual(seen, ["1", "2"])
        self.assertEqual(runner.stats.duplicates, 1)
        self.assertGreaterEqual(runner.stats.reconnects, 1)

    async def test_out_of_order_event_is_dropped(self) -> None:
        source = FakeSource([[trade_message(ts=62000, trade_id=2), trade_message(ts=61000, trade_id=1), trade_message(ts=63000, trade_id=3)]])
        seen: list[str] = []
        runner = MarketStreamRunner(source, on_event=lambda event: seen.append(event.source_event_id or ""), reconnect_delays=(0,))
        await runner.run(max_events=2)
        self.assertEqual(seen, ["2", "3"])
        self.assertEqual(runner.stats.out_of_order, 1)

    async def test_ordering_is_isolated_by_event_stream(self) -> None:
        source = FakeSource([[trade_message(ts=62000, trade_id=2), candle_message(ts=61000, closed=True), trade_message(ts=63000, trade_id=3)]])
        seen: list[MarketEventType] = []
        runner = MarketStreamRunner(source, on_event=lambda event: seen.append(event.event_type), reconnect_delays=(0,))
        await runner.run(max_events=3)
        self.assertEqual(seen, [MarketEventType.TRADE, MarketEventType.CANDLE_CLOSE, MarketEventType.TRADE])
        self.assertEqual(runner.stats.out_of_order, 0)

    async def test_disconnect_reconnects(self) -> None:
        source = FakeSource([[trade_message(ts=61000, trade_id=1)], [trade_message(ts=62000, trade_id=2)]])
        seen: list[str] = []
        runner = MarketStreamRunner(source, on_event=lambda event: seen.append(event.source_event_id or ""), reconnect_delays=(0,))
        await runner.run(max_events=2)
        self.assertEqual(seen, ["1", "2"])
        self.assertGreaterEqual(source.connected, 2)
        self.assertGreaterEqual(runner.stats.reconnects, 1)
        self.assertGreaterEqual(runner.stats.disconnects, 1)

    async def test_non_finite_market_value_is_rejected_without_delivery(self) -> None:
        raw = trade_message(ts=61000)
        raw["data"]["p"] = "nan"
        source = FakeSource([[raw, trade_message(ts=62000, trade_id=2)]])
        seen: list[MarketEvent] = []
        runner = MarketStreamRunner(source, on_event=lambda event: seen.append(event), reconnect_delays=(0,))
        await runner.run(max_events=1)
        self.assertEqual([event.source_event_id for event in seen], ["2"])
        self.assertEqual(runner.stats.normalized_failures, 1)


if __name__ == "__main__":
    unittest.main()