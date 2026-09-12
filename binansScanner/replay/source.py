from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Mapping

from enums import Timeframe
from providers.binance_opportunity_source import BinanceSpotOpportunitySource

from replay.clock import ReplayClock
from replay.dataset import HistoricalDataset


class HistoricalMarketDataSource(BinanceSpotOpportunitySource):
    """Offline MarketMetrics/candle source backed only by preloaded data."""

    _INTERVALS = {
        Timeframe.D1: "1d",
        Timeframe.H4: "4h",
        Timeframe.H1: "1h",
        Timeframe.M15: "15m",
    }

    def __init__(self, dataset: HistoricalDataset, clock: ReplayClock) -> None:
        super().__init__(ttl_seconds=0.0, timeout_seconds=10.0, clock=clock.monotonic)
        self.dataset = dataset
        self.clock = clock

    @property
    def live_accessed(self) -> bool:
        return False

    def _snapshot(self) -> Mapping[str, Any]:
        return self.dataset.metadata_at(self.clock.simulation_timestamp)

    def exchange_info(self) -> Mapping[str, Any]:
        return self._snapshot().get("exchange_info", {"symbols": []})

    def _historical_24h_ticker(self, symbol: str) -> Mapping[str, Any] | None:
        """Derive point-in-time 24h ticker metadata from preloaded 5m candles only."""
        rows = self.dataset.candles_at(symbol, "5m", self.clock.simulation_timestamp)
        if not rows:
            return None
        cutoff = self.clock.simulation_timestamp.astimezone(timezone.utc)
        window_start = cutoff - timedelta(hours=24)
        window_start_ms = int(window_start.timestamp() * 1000)
        window = [row for row in rows if len(row) > 6 and int(row[6]) > window_start_ms]
        if len(window) < 2:
            return None
        first = window[0]
        last = window[-1]
        first_price = float(first[1])
        last_price = float(last[4])
        base_volume = sum(float(row[5]) for row in window)
        quote_volume = sum(float(row[4]) * float(row[5]) for row in window)
        if first_price <= 0 or base_volume <= 0 or quote_volume <= 0:
            return None
        weighted_average = quote_volume / base_volume
        return {
            "symbol": symbol.upper(),
            "lastPrice": f"{last_price:.16g}",
            "priceChangePercent": f"{((last_price / first_price) - 1.0) * 100.0:.16g}",
            "weightedAvgPrice": f"{weighted_average:.16g}",
            "quoteVolume": f"{quote_volume:.16g}",
        }

    def _historical_tickers(self) -> list[Mapping[str, Any]]:
        return [
            ticker
            for symbol in self.dataset.manifest.symbols
            for ticker in (self._historical_24h_ticker(symbol),)
            if ticker is not None
        ]

    def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        params = params or {}
        snapshot = self._snapshot()
        if path == "exchangeInfo":
            return snapshot.get("exchange_info", {"symbols": []})
        if path == "ticker/24hr":
            rows = self._historical_tickers()
            symbol = params.get("symbol")
            if symbol is None:
                return rows
            wanted = str(symbol).upper()
            return [row for row in rows if str(row.get("symbol", "")).upper() == wanted]
        if path == "ticker/bookTicker":
            # Historical order-book top-of-book is not part of this candle dataset.
            # Canonical discovery already falls back to historical candle features when
            # book metadata is absent, so returning no fabricated book state preserves the
            # no-lookahead / no-synthetic-order-book contract.
            return []
        if path == "klines":
            symbol = str(params.get("symbol", "")).upper()
            interval = str(params.get("interval", "1d"))
            limit = int(params.get("limit", 32))
            rows = self.dataset.candles_at(symbol, interval, self.clock.simulation_timestamp)
            return list(rows[-limit:])
        raise RuntimeError(f"historical replay reached unsupported market-data path: {path}")

    def klines(self, symbol: str, timeframe: Timeframe, limit: int):
        interval = self._INTERVALS[timeframe]
        return self._get_json(
            "klines",
            {"symbol": symbol.upper(), "interval": interval, "limit": int(limit)},
        )

    def _cached(self, key: str, loader):
        # Discovery snapshots must be resolved against the current simulation time.
        return loader()

    def mark(self, timestamp: datetime) -> None:
        self.clock.advance_to(timestamp)


__all__ = ["HistoricalMarketDataSource"]
