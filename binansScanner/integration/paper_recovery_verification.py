"""Deterministic, independently testable paper recovery verification.

This module compares canonical runtime/capital state only. It does not create
trading decisions and does not own accounting or recovery authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class RecoveryVerification:
    runtime_replay_equal: bool
    runtime_repeat_recovery_equal: bool
    capital_replay_equal: bool
    paper_only: bool
    failure_reasons: tuple[str, ...]
    canonical_state: str
    recovered_state: str
    repeated_recovery_state: str
    capital_state: str
    recovered_capital_state: str
    repeated_recovered_capital_state: str

    @property
    def passed(self) -> bool:
        return not self.failure_reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_replay_equal": self.runtime_replay_equal,
            "runtime_repeat_recovery_equal": self.runtime_repeat_recovery_equal,
            "capital_replay_equal": self.capital_replay_equal,
            "paper_only": self.paper_only,
            "failure_reasons": self.failure_reasons,
            "canonical_state": self.canonical_state,
            "recovered_state": self.recovered_state,
            "repeated_recovery_state": self.repeated_recovery_state,
            "capital_state": self.capital_state,
            "recovered_capital_state": self.recovered_capital_state,
            "repeated_recovered_capital_state": self.repeated_recovered_capital_state,
        }


def _canonical(value: Any) -> str:
    """Return a deterministic, type-aware state summary for diagnostics."""
    if isinstance(value, Mapping):
        items = sorted((str(k), _canonical(v)) for k, v in value.items())
        return "{" + ", ".join(f"{k}={v}" for k, v in items) + "}"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(_canonical(v) for v in value) + "]"
    if isinstance(value, set):
        return "{" + ", ".join(sorted(_canonical(v) for v in value)) + "}"
    return f"{type(value).__name__}({value!r})"


def _first_mismatch(left: Any, right: Any, *, label: str) -> str | None:
    if left == right:
        return None
    if isinstance(left, tuple) and isinstance(right, tuple) and len(left) == len(right):
        for index, (left_value, right_value) in enumerate(zip(left, right)):
            if left_value != right_value:
                return f"{label}[{index}] differs: {_canonical(left_value)} != {_canonical(right_value)}"
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        keys = sorted(set(left) | set(right), key=str)
        for key in keys:
            if left.get(key) != right.get(key):
                return f"{label}.{key} differs: {_canonical(left.get(key))} != {_canonical(right.get(key))}"
    return f"{label} differs: {_canonical(left)} != {_canonical(right)}"


def verify_recovery(
    *,
    canonical_runtime: Any,
    recovered_runtime: Any,
    repeated_runtime: Any,
    canonical_capital: Mapping[str, Any],
    recovered_capital: Mapping[str, Any],
    repeated_capital: Mapping[str, Any],
    paper_only: bool,
) -> RecoveryVerification:
    """Evaluate all recovery invariants independently and deterministically."""
    runtime_replay_equal = canonical_runtime == recovered_runtime
    runtime_repeat_recovery_equal = recovered_runtime == repeated_runtime
    capital_replay_equal = canonical_capital == recovered_capital == repeated_capital
    failure_reasons: list[str] = []
    if not runtime_replay_equal:
        failure_reasons.append(_first_mismatch(canonical_runtime, recovered_runtime, label="runtime_replay") or "runtime replay mismatch")
    if not runtime_repeat_recovery_equal:
        failure_reasons.append(_first_mismatch(recovered_runtime, repeated_runtime, label="runtime_repeat_recovery") or "repeated recovery mismatch")
    if not capital_replay_equal:
        first = _first_mismatch(canonical_capital, recovered_capital, label="capital_replay")
        second = _first_mismatch(recovered_capital, repeated_capital, label="capital_repeat_recovery")
        failure_reasons.extend(reason for reason in (first, second) if reason is not None)
    if not paper_only:
        failure_reasons.append("paper_only=false")
    return RecoveryVerification(
        runtime_replay_equal=runtime_replay_equal,
        runtime_repeat_recovery_equal=runtime_repeat_recovery_equal,
        capital_replay_equal=capital_replay_equal,
        paper_only=paper_only,
        failure_reasons=tuple(failure_reasons),
        canonical_state=_canonical(canonical_runtime),
        recovered_state=_canonical(recovered_runtime),
        repeated_recovery_state=_canonical(repeated_runtime),
        capital_state=_canonical(canonical_capital),
        recovered_capital_state=_canonical(recovered_capital),
        repeated_recovered_capital_state=_canonical(repeated_capital),
    )


__all__ = ["RecoveryVerification", "verify_recovery"]
