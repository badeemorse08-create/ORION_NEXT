"""Binance adapter for the D1 scalping evidence boundary."""
from __future__ import annotations

from typing import Sequence

from enums import Timeframe
from models.scalping_opportunity import Candle
from providers.binance_provider import BinanceProvider


class BinanceScalpingCandleSource:
    """Read-only market adapter; no execution, order, or position access."""

    _TIMEFRAMES = {
        "1d": Timeframe.D1,
        "4h": Timeframe.H4,
        "1h": Timeframe.H1,
        "15m": Timeframe.M15,
    }

    def __init__(self, provider: BinanceProvider) -> None:
        self.provider = provider

    def candles(self, symbol: str, timeframe: str, limit: int) -> Sequence[Candle]:
        if timeframe not in self._TIMEFRAMES:
            raise ValueError(f"unsupported scalping timeframe: {timeframe}")
        raw = self.provider.klines(symbol, self._TIMEFRAMES[timeframe], limit=limit)
        return tuple(
            Candle(
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
            for row in raw
            if len(row) >= 6
        )
