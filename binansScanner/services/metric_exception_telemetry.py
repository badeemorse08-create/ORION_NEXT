from __future__ import annotations

from typing import Any, Mapping, Sequence


def install_metric_exception_telemetry(opportunity_discovery_cls: type) -> None:
    """Instrument metric-construction exceptions without changing control flow."""
    if getattr(opportunity_discovery_cls, "_metric_exception_telemetry_installed", False):
        return

    original = opportunity_discovery_cls._build_history_metric

    def wrapped(self, source: Any, symbol: str, ticker: Mapping[str, Any], book: Mapping[str, Any] | None, history: Sequence[Any]):
        try:
            return original(self, source, symbol, ticker, book, history)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            events = getattr(self, "_metric_exception_events", None)
            if events is None:
                events = []
                setattr(self, "_metric_exception_events", events)
            events.append(
                {
                    "event_type": "startup_diagnostic_metric_exception",
                    "symbol": symbol,
                    "stage": "metric_construction",
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "history_length": len(history),
                    "metrics_history_window": getattr(source, "METRICS_HISTORY_WINDOW", None),
                    "min_history_candles": getattr(source, "MIN_HISTORY_CANDLES", None),
                    "ticker_present": ticker is not None,
                    "book_present": book is not None,
                }
            )
            raise

    @property
    def metric_exception_events(self):
        return tuple(dict(event) for event in getattr(self, "_metric_exception_events", ()))

    opportunity_discovery_cls._build_history_metric = wrapped
    opportunity_discovery_cls.metric_exception_events = metric_exception_events
    opportunity_discovery_cls._metric_exception_telemetry_installed = True
