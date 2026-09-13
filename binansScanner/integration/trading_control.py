"""Persistent fail-closed control for new trading entries.

The control boundary is deliberately narrower than lifecycle management: PAUSED
blocks new entry submission/reservation while existing positions and pending
orders remain governed by D4/D5.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class TradingState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


@dataclass(frozen=True, slots=True)
class TradingControlEvent:
    event: str
    state: TradingState
    timestamp: str
    source: str
    reason: str


class TradingControlStore:
    """Atomic persistent state with PAUSED as the read-failure default."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.event_log_path = self.path.with_name(self.path.name + ".events.jsonl")
        self._lock = threading.RLock()
        self._events: list[TradingControlEvent] = []

    @property
    def state(self) -> TradingState:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                return TradingState(str(payload["state"]))
            except (OSError, ValueError, KeyError, TypeError):
                return TradingState.PAUSED

    @property
    def events(self) -> tuple[TradingControlEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def initialize(self, *, state: TradingState = TradingState.RUNNING, source: str = "startup", reason: str = "initialization") -> TradingState:
        """Create a canonical state only when no state file exists."""
        with self._lock:
            if self.path.exists():
                return self.state
            self._write(state, source=source, reason=reason, event="TRADING_RESUMED" if state is TradingState.RUNNING else "TRADING_PAUSED")
            return state

    def pause(self, *, source: str = "user", reason: str = "pause new entries") -> TradingState:
        return self._transition(TradingState.PAUSED, source=source, reason=reason, event="TRADING_PAUSED")

    def resume(self, *, source: str = "user", reason: str = "resume trading") -> TradingState:
        return self._transition(TradingState.RUNNING, source=source, reason=reason, event="TRADING_RESUMED")

    def _transition(self, state: TradingState, *, source: str, reason: str, event: str) -> TradingState:
        with self._lock:
            if self.state is state:
                return state
            self._write(state, source=source, reason=reason, event=event)
            return state

    def require_entry_allowed(self, *, source: str = "runtime", reason: str = "entry submission") -> None:
        """Atomically check persisted state immediately before entry creation."""
        with self._lock:
            state = self.state
            if state is TradingState.PAUSED:
                self._write_event("ENTRY_BLOCKED_BY_PAUSE", state, source=source, reason=reason)
                raise PermissionError("new entries are paused")

    def _write(self, state: TradingState, *, source: str, reason: str, event: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"state": state.value, "updated_at": datetime.now(timezone.utc).isoformat(), "version": 1}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._write_event(event, state, source=source, reason=reason)

    def _write_event(self, event: str, state: TradingState, *, source: str, reason: str) -> None:
        item = TradingControlEvent(event, state, datetime.now(timezone.utc).isoformat(), source, reason)
        self._events.append(item)
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": item.event, "state": item.state.value, "timestamp": item.timestamp, "source": item.source, "reason": item.reason}, sort_keys=True) + "\n")


__all__ = ["TradingState", "TradingControlEvent", "TradingControlStore"]
