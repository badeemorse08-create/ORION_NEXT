from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from models.market_event import MarketEventType
from replay.dataset import HistoricalDataset, HistoricalDatasetManifest, HistoricalMarketEvent


SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT")
START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _candle_rows(symbol_index: int, timeframe: str, count: int = 40):
    step = {"5m": 300_000, "1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000, "15m": 900_000}[timeframe]
    rows = []
    if timeframe == "5m":
        base_ms = int((START - timedelta(minutes=5 * (count + 12))).timestamp() * 1000)
    else:
        base_ms = int((START - timedelta(milliseconds=step * count * 2)).timestamp() * 1000)
    slopes = (0.75, -0.65, 0.95, -0.55, 0.45)
    slope = slopes[symbol_index % len(slopes)]
    for index in range(count):
        close = 100.0 + symbol_index * 2.0 + index * slope + ((-1) ** index) * 0.25
        open_price = close - 0.15
        high = max(open_price, close) + 0.20
        low = min(open_price, close) - 0.20
        timestamp = base_ms + index * step
        rows.append(
            (
                timestamp,
                f"{open_price:.8f}",
                f"{high:.8f}",
                f"{low:.8f}",
                f"{close:.8f}",
                "1000",
                timestamp + step - 1,
            )
        )
    return tuple(rows)


def _five_minute_rows(symbol_index: int, count: int = 300):
    return _candle_rows(symbol_index, "5m", count=count)


def build_fixture_dataset() -> HistoricalDataset:
    metadata_snapshot = {
        "exchange_info": {
            "symbols": [
                {
                    "symbol": symbol,
                    "baseAsset": symbol[:-4],
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                }
                for symbol in SYMBOLS
            ]
        },
        "ticker_24h": [
            {
                "symbol": symbol,
                "quoteVolume": str(10_000_000 + index * 100_000),
                "lastPrice": str(100 + index),
                "priceChangePercent": str(1.0 + index * 0.1),
                "weightedAvgPrice": str(100 + index * 2),
            }
            for index, symbol in enumerate(SYMBOLS)
        ],
        "book_ticker": [
            {"symbol": symbol, "bidPrice": str(99.99 + index), "askPrice": str(100.01 + index)}
            for index, symbol in enumerate(SYMBOLS)
        ],
    }

    events: list[HistoricalMarketEvent] = []
    for day in range(6):
        timestamp = START + timedelta(days=day)
        for symbol_index, symbol in enumerate(SYMBOLS):
            price = 100.0 + symbol_index * 2.0 + day * (0.75 - symbol_index * 0.05)
            events.append(
                HistoricalMarketEvent(
                    timestamp=timestamp,
                    symbol=symbol,
                    event_type=MarketEventType.CANDLE_CLOSE,
                    source_event_id=f"1d:{symbol}:{day}",
                    payload={
                        "timeframe": "1d",
                        "open_time": timestamp.isoformat(),
                        "close_time": (timestamp + timedelta(days=1) - timedelta(milliseconds=1)).isoformat(),
                        "open": price,
                        "high": price + 0.5,
                        "low": price - 0.5,
                        "close": price + 0.2,
                        "volume": 1000.0,
                        "is_closed": True,
                    },
                )
            )

    candles = {
        (symbol, timeframe): _candle_rows(symbol_index, timeframe)
        for symbol_index, symbol in enumerate(SYMBOLS)
        for timeframe in ("1d", "4h", "1h", "15m")
    }
    candles.update({
        (symbol, "5m"): _five_minute_rows(symbol_index)
        for symbol_index, symbol in enumerate(SYMBOLS)
    })
    metadata = ((START - timedelta(seconds=1), metadata_snapshot),)
    digest_payload = {
        "events": [
            {
                "timestamp": event.timestamp.isoformat(),
                "symbol": event.symbol,
                "event_type": event.event_type.value,
                "payload": event.payload,
                "source_event_id": event.source_event_id,
            }
            for event in sorted(events, key=lambda item: (item.timestamp, item.symbol, item.source_event_id or ""))
        ],
        "metadata": [{"timestamp": ts.isoformat(), "snapshot": snapshot} for ts, snapshot in metadata],
        "candles": {f"{symbol}|{timeframe}": rows for (symbol, timeframe), rows in sorted(candles.items())},
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    manifest = HistoricalDatasetManifest(
        period="7D",
        source="deterministic-replay-fixture",
        symbols=SYMBOLS,
        event_types=("candle_close",),
        timeframes=("1d", "4h", "1h", "15m", "5m"),
        timestamp_convention="ISO-8601 UTC",
        ordering_convention="timestamp,symbol,event_type,source_event_id",
        dataset_version="fixture-v2",
        integrity_sha256=digest,
    )
    return HistoricalDataset(
        manifest=manifest,
        events=tuple(sorted(events, key=lambda item: (item.timestamp, item.symbol, item.source_event_id or ""))),
        metadata_snapshots=metadata,
        candles=candles,
    )