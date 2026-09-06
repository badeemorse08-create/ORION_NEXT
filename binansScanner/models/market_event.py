from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


class MarketEventType(str, Enum):
    TRADE = "trade"
    TICKER = "ticker"
    CANDLE_UPDATE = "candle_update"
    CANDLE_CLOSE = "candle_close"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    symbol: str
    event_timestamp: datetime
    event_type: MarketEventType
    payload: Mapping[str, Any]
    source_timestamp: datetime | None = None
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper() if isinstance(self.symbol, str) else ""
        if not symbol:
            raise ValueError("MarketEvent.symbol must be a non-empty string")
        if self.event_timestamp.tzinfo is None:
            raise ValueError("MarketEvent.event_timestamp must be timezone-aware")
        if self.source_timestamp is not None and self.source_timestamp.tzinfo is None:
            raise ValueError("MarketEvent.source_timestamp must be timezone-aware")
        if not isinstance(self.event_type, MarketEventType):
            raise ValueError("MarketEvent.event_type must be MarketEventType")
        if not isinstance(self.payload, Mapping):
            raise ValueError("MarketEvent.payload must be a mapping")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def event_id(self) -> str:
        """Stable identity used for duplicate suppression across reconnects."""
        canonical = json.dumps(
            {
                "symbol": self.symbol,
                "event_type": self.event_type.value,
                "event_timestamp": self.event_timestamp.astimezone(timezone.utc).isoformat(),
                "source_timestamp": (
                    self.source_timestamp.astimezone(timezone.utc).isoformat()
                    if self.source_timestamp is not None else None
                ),
                "source_event_id": self.source_event_id,
                "payload": self.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MarketEventNormalizationError(ValueError):
    pass
