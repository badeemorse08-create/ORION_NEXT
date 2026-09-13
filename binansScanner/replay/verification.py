from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from models.market_event import MarketEvent
from integration.paper_runtime_supervisor import PaperRuntimeSupervisor


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    event_ids_equal: bool
    replay_state_equal: bool
    capital_state_equal: bool
    deterministic: bool


class ReplayVerifier:
    """Offline verification helpers; never provides market data to production code."""

    @staticmethod
    def compare_event_sequences(left: Sequence[MarketEvent], right: Sequence[MarketEvent]) -> bool:
        return tuple(event.event_id for event in left) == tuple(event.event_id for event in right)

    @staticmethod
    def compare_supervisors(left: PaperRuntimeSupervisor, right: PaperRuntimeSupervisor) -> ReplayComparison:
        left_state = left.replay_state()
        right_state = right.replay_state()
        left_event_ids = tuple(event.event_id for event in left.runtime.orders.events)
        right_event_ids = tuple(event.event_id for event in right.runtime.orders.events)
        left_capital = left_state[3]
        right_capital = right_state[3]
        event_ids_equal = left_event_ids == right_event_ids
        replay_state_equal = left_state == right_state
        capital_state_equal = left_capital == right_capital
        return ReplayComparison(
            event_ids_equal=event_ids_equal,
            replay_state_equal=replay_state_equal,
            capital_state_equal=capital_state_equal,
            deterministic=event_ids_equal and replay_state_equal and capital_state_equal and left.no_live_path() and right.no_live_path(),
        )

    @staticmethod
    def recovery_from_checkpoint(
        supervisor: PaperRuntimeSupervisor,
        events: Sequence[MarketEvent],
        checkpoint_index: int,
        processor: Callable[[PaperRuntimeSupervisor, MarketEvent], None],
    ) -> PaperRuntimeSupervisor:
        if checkpoint_index < 0 or checkpoint_index > len(events):
            raise ValueError("checkpoint_index outside event sequence")
        for event in events[:checkpoint_index]:
            processor(supervisor, event)
        recovered = supervisor.recover()
        for event in events[checkpoint_index:]:
            processor(recovered, event)
        return recovered


__all__ = ["ReplayComparison", "ReplayVerifier"]
