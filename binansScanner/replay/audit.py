from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from models.market_event import MarketEvent


@dataclass(frozen=True, slots=True)
class MovementAudit:
    symbol: str
    timestamp: datetime
    previous_price: float | None
    price: float
    movement_pct: float | None


def build_movement_audit(events: Iterable[MarketEvent], *, threshold_pct: float = 5.0) -> tuple[MovementAudit, ...]:
    if threshold_pct < 0:
        raise ValueError("threshold_pct must be non-negative")
    previous: dict[str, float] = {}
    audits: list[MovementAudit] = []
    for event in events:
        value = event.payload.get("price")
        if value is None:
            continue
        price = float(value)
        prior = previous.get(event.symbol)
        movement = None if prior in (None, 0.0) else ((price / prior) - 1.0) * 100.0
        previous[event.symbol] = price
        if movement is not None and abs(movement) >= threshold_pct:
            audits.append(MovementAudit(event.symbol, event.event_timestamp, prior, price, movement))
    return tuple(audits)


def correlate_opportunity_refreshes(
    movement_audits: Iterable[MovementAudit],
    refresh_events: Iterable[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    refreshes = tuple(refresh_events)
    result: list[dict[str, object]] = []
    for movement in movement_audits:
        matching = [
            refresh
            for refresh in refreshes
            if str(refresh.get("symbol", "")) == movement.symbol
            or movement.symbol in tuple(refresh.get("top_n_symbols", ()) or ())
        ]
        result.append(
            {
                "symbol": movement.symbol,
                "timestamp": movement.timestamp.isoformat(),
                "movement_pct": movement.movement_pct,
                "opportunity_refresh_observed": bool(matching),
                "refresh_count": len(matching),
                "refreshes": matching,
            }
        )
    return tuple(result)


__all__ = ["MovementAudit", "build_movement_audit", "correlate_opportunity_refreshes"]
