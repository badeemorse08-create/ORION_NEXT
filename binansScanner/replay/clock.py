from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time


@dataclass(slots=True)
class ReplayClock:
    """Simulation clock independent from wall-clock execution time."""

    start: datetime
    acceleration_factor: float = 1.0
    _current: datetime | None = None
    _wall_start: float = 0.0

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("ReplayClock.start must be timezone-aware")
        if self.acceleration_factor <= 0:
            raise ValueError("acceleration_factor must be positive")
        self.start = self.start.astimezone(timezone.utc)
        self._current = self.start
        self._wall_start = time.monotonic()

    @property
    def simulation_timestamp(self) -> datetime:
        assert self._current is not None
        return self._current

    @property
    def wall_clock_timestamp(self) -> datetime:
        return datetime.now(timezone.utc)

    @property
    def elapsed_simulation_seconds(self) -> float:
        return max(0.0, (self.simulation_timestamp - self.start).total_seconds())

    @property
    def elapsed_wall_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._wall_start)

    def monotonic(self) -> float:
        """Return simulation elapsed seconds for deterministic cache/refresh logic."""
        return self.elapsed_simulation_seconds

    def advance_to(self, timestamp: datetime) -> None:
        if timestamp.tzinfo is None:
            raise ValueError("simulation timestamp must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)
        if timestamp < self.simulation_timestamp:
            raise ValueError("simulation clock cannot move backwards")
        self._current = timestamp

    def advance_by_wall_seconds(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        self.advance_to(self.simulation_timestamp + timedelta(seconds=seconds * self.acceleration_factor))

    def wall_delay_for(self, simulation_delta_seconds: float) -> float:
        if simulation_delta_seconds < 0:
            raise ValueError("simulation_delta_seconds must be non-negative")
        return simulation_delta_seconds / self.acceleration_factor
