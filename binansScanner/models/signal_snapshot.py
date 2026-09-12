"""Immutable signal snapshot, versioning, and validity contracts.

This module is intentionally isolated from execution. It records signal identity,
point-in-time snapshot state, explicit expiry, and deterministic material-change
classification. It does not cancel/replace orders or manage positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Optional


class SignalValidity(str, Enum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


class MaterialChangeReason(str, Enum):
    ENTRY_PRICE_CHANGED = "ENTRY_PRICE_CHANGED"
    DIRECTION_CHANGED = "DIRECTION_CHANGED"
    DECISION_CHANGED = "DECISION_CHANGED"
    MARKET_CONTEXT_CHANGED = "MARKET_CONTEXT_CHANGED"
    CONFIDENCE_THRESHOLD_CROSSED = "CONFIDENCE_THRESHOLD_CROSSED"
    QUALITY_THRESHOLD_CROSSED = "QUALITY_THRESHOLD_CROSSED"
    VALIDITY_EXPIRED = "VALIDITY_EXPIRED"


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: float, field: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported entry_plan value type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class SignalIdentity:
    """Stable identity for a logical signal stream."""

    symbol: str
    strategy: str
    intent: str
    identity_key: str = ""

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip()
        strategy = str(self.strategy).strip()
        intent = str(self.intent).strip()
        if not symbol or not strategy or not intent:
            raise ValueError("symbol, strategy, and intent must be non-empty")
        canonical = "|".join((symbol.upper(), strategy, intent))
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.identity_key and self.identity_key != expected:
            raise ValueError("identity_key does not match signal identity")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "identity_key", expected)


@dataclass(frozen=True, slots=True)
class MaterialChangePolicy:
    """Explicit policy; no hidden thresholds are applied."""

    entry_price_change_pct: float = 0.10
    confidence_threshold: Optional[float] = None
    quality_threshold: Optional[float] = None
    entry_price_key: str = "entry_price"

    def __post_init__(self) -> None:
        pct = _finite(self.entry_price_change_pct, "entry_price_change_pct")
        if pct <= 0.0:
            raise ValueError("entry_price_change_pct must be greater than zero")
        if self.confidence_threshold is not None:
            _finite(self.confidence_threshold, "confidence_threshold")
        if self.quality_threshold is not None:
            _finite(self.quality_threshold, "quality_threshold")
        if not str(self.entry_price_key).strip():
            raise ValueError("entry_price_key must be non-empty")


@dataclass(frozen=True, slots=True)
class SignalSnapshot:
    """Immutable point-in-time signal state consumed by downstream validity logic."""

    identity: SignalIdentity
    version: int
    direction: str
    decision: str
    confidence: float
    entry_plan: Mapping[str, Any]
    generated_at: datetime
    valid_until: datetime
    market_context_fingerprint: Optional[str] = None
    quality: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SignalIdentity):
            raise TypeError("identity must be SignalIdentity")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("version must be a positive integer")
        direction = str(self.direction).strip()
        decision = str(self.decision).strip()
        if not direction or not decision:
            raise ValueError("direction and decision must be non-empty")
        confidence = _finite(self.confidence, "confidence")
        generated_at = _utc(self.generated_at, "generated_at")
        valid_until = _utc(self.valid_until, "valid_until")
        if valid_until <= generated_at:
            raise ValueError("valid_until must be strictly after generated_at")
        if not isinstance(self.entry_plan, Mapping):
            raise TypeError("entry_plan must be a mapping")
        canonical_plan = _canonical(self.entry_plan)
        if self.market_context_fingerprint is not None:
            fingerprint = str(self.market_context_fingerprint).strip()
            if not fingerprint:
                raise ValueError("market_context_fingerprint must be non-empty when provided")
            object.__setattr__(self, "market_context_fingerprint", fingerprint)
        if self.quality is not None:
            object.__setattr__(self, "quality", _finite(self.quality, "quality"))
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "entry_plan", MappingProxyType(canonical_plan))

    @property
    def signal_id(self) -> str:
        return self.identity.identity_key

    @property
    def expired(self) -> bool:
        return self.is_expired(datetime.now(timezone.utc))

    def is_expired(self, at: datetime) -> bool:
        return _utc(at, "at") >= self.valid_until

    def validity_at(self, at: datetime) -> SignalValidity:
        return SignalValidity.EXPIRED if self.is_expired(at) else SignalValidity.ACTIVE

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.identity.symbol,
            "strategy": self.identity.strategy,
            "intent": self.identity.intent,
            "version": self.version,
            "direction": self.direction,
            "decision": self.decision,
            "confidence": self.confidence,
            "entry_plan": dict(self.entry_plan),
            "generated_at": self.generated_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "market_context_fingerprint": self.market_context_fingerprint,
            "quality": self.quality,
        }


@dataclass(frozen=True, slots=True)
class SignalVersionRelation:
    previous: Optional[SignalSnapshot]
    current: SignalSnapshot
    material_change: bool
    reasons: tuple[MaterialChangeReason, ...]

    @property
    def previous_version(self) -> Optional[int]:
        return None if self.previous is None else self.previous.version

    @property
    def current_version(self) -> int:
        return self.current.version

    @property
    def previous_validity(self) -> Optional[SignalValidity]:
        if self.previous is None:
            return None
        return SignalValidity.STALE if self.material_change else SignalValidity.EXPIRED if self.previous.expired else SignalValidity.ACTIVE


def _threshold_crossed(old: Optional[float], new: Optional[float], threshold: Optional[float]) -> bool:
    if threshold is None or old is None or new is None:
        return False
    return (old < threshold <= new) or (old >= threshold > new)


def _entry_price(snapshot: SignalSnapshot, policy: MaterialChangePolicy) -> Optional[float]:
    value = snapshot.entry_plan.get(policy.entry_price_key)
    if value is None:
        return None
    return _finite(value, f"entry_plan[{policy.entry_price_key!r}]")


def material_change_reasons(
    previous: SignalSnapshot,
    current: SignalSnapshot,
    policy: MaterialChangePolicy,
) -> tuple[MaterialChangeReason, ...]:
    if previous.identity != current.identity:
        raise ValueError("material comparison requires identical signal identity")
    reasons: list[MaterialChangeReason] = []
    old_price = _entry_price(previous, policy)
    new_price = _entry_price(current, policy)
    if old_price is not None and new_price is not None:
        denominator = abs(old_price)
        if denominator == 0.0:
            if new_price != 0.0:
                reasons.append(MaterialChangeReason.ENTRY_PRICE_CHANGED)
        elif abs(new_price - old_price) / denominator >= policy.entry_price_change_pct:
            reasons.append(MaterialChangeReason.ENTRY_PRICE_CHANGED)
    if previous.direction != current.direction:
        reasons.append(MaterialChangeReason.DIRECTION_CHANGED)
    if previous.decision != current.decision:
        reasons.append(MaterialChangeReason.DECISION_CHANGED)
    if (
        previous.market_context_fingerprint is not None
        and current.market_context_fingerprint is not None
        and previous.market_context_fingerprint != current.market_context_fingerprint
    ):
        reasons.append(MaterialChangeReason.MARKET_CONTEXT_CHANGED)
    if _threshold_crossed(previous.confidence, current.confidence, policy.confidence_threshold):
        reasons.append(MaterialChangeReason.CONFIDENCE_THRESHOLD_CROSSED)
    if _threshold_crossed(previous.quality, current.quality, policy.quality_threshold):
        reasons.append(MaterialChangeReason.QUALITY_THRESHOLD_CROSSED)
    if previous.is_expired(current.generated_at):
        reasons.append(MaterialChangeReason.VALIDITY_EXPIRED)
    return tuple(reasons)


def build_next_snapshot(
    *,
    previous: Optional[SignalSnapshot],
    identity: SignalIdentity,
    direction: str,
    decision: str,
    confidence: float,
    entry_plan: Mapping[str, Any],
    generated_at: datetime,
    valid_until: datetime,
    policy: MaterialChangePolicy,
    market_context_fingerprint: Optional[str] = None,
    quality: Optional[float] = None,
) -> SignalVersionRelation:
    if previous is not None and previous.identity != identity:
        raise ValueError("previous snapshot belongs to a different signal identity")
    version = 1 if previous is None else previous.version + 1
    current = SignalSnapshot(
        identity=identity,
        version=version,
        direction=direction,
        decision=decision,
        confidence=confidence,
        entry_plan=entry_plan,
        generated_at=generated_at,
        valid_until=valid_until,
        market_context_fingerprint=market_context_fingerprint,
        quality=quality,
    )
    reasons = () if previous is None else material_change_reasons(previous, current, policy)
    return SignalVersionRelation(
        previous=previous,
        current=current,
        material_change=bool(reasons),
        reasons=reasons,
    )


def evaluate_snapshot(
    snapshot: SignalSnapshot,
    *,
    previous: Optional[SignalSnapshot] = None,
    at: Optional[datetime] = None,
    policy: Optional[MaterialChangePolicy] = None,
) -> SignalValidity:
    when = datetime.now(timezone.utc) if at is None else _utc(at, "at")
    if previous is not None:
        if policy is None:
            raise ValueError("policy is required when previous is provided")
        if previous.identity != snapshot.identity:
            raise ValueError("previous snapshot belongs to a different signal identity")
        if material_change_reasons(previous, snapshot, policy):
            return SignalValidity.STALE
    return snapshot.validity_at(when)


__all__ = [
    "MaterialChangePolicy",
    "MaterialChangeReason",
    "SignalIdentity",
    "SignalSnapshot",
    "SignalValidity",
    "SignalVersionRelation",
    "build_next_snapshot",
    "evaluate_snapshot",
    "material_change_reasons",
]
