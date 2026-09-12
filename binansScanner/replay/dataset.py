from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from models.market_event import MarketEvent, MarketEventType


@dataclass(frozen=True, slots=True)
class HistoricalMarketEvent:
    timestamp: datetime
    symbol: str
    event_type: MarketEventType
    payload: Mapping[str, Any]
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("historical event timestamp must be timezone-aware")
        if not self.symbol.strip():
            raise ValueError("historical event symbol must be non-empty")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def event_id(self) -> str:
        """Stable dataset identity; source IDs survive disk serialization exactly."""
        if self.source_event_id:
            return str(self.source_event_id)
        canonical = json.dumps(
            {
                "symbol": self.symbol,
                "event_type": self.event_type.value,
                "timestamp": self.timestamp.isoformat(),
                "payload": self.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_market_event(self) -> MarketEvent:
        return MarketEvent(
            symbol=self.symbol,
            event_timestamp=self.timestamp,
            event_type=self.event_type,
            payload=self.payload,
            source_timestamp=self.timestamp,
            source_event_id=self.event_id,
        )


@dataclass(frozen=True, slots=True)
class HistoricalDatasetManifest:
    period: str
    source: str
    symbols: tuple[str, ...]
    event_types: tuple[str, ...]
    timeframes: tuple[str, ...]
    timestamp_convention: str
    ordering_convention: str
    dataset_version: str
    integrity_sha256: str


@dataclass(frozen=True, slots=True)
class HistoricalDataset:
    """Immutable preloaded dataset; visibility is controlled by ReplayClock."""

    manifest: HistoricalDatasetManifest
    events: tuple[HistoricalMarketEvent, ...]
    metadata_snapshots: tuple[tuple[datetime, Mapping[str, Any]], ...]
    candles: Mapping[tuple[str, str], tuple[tuple[Any, ...], ...]]

    @classmethod
    def from_directory(cls, root: Path) -> "HistoricalDataset":
        root = Path(root)
        manifest_data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        events = tuple(cls._read_events(root / "events.jsonl"))
        metadata = tuple(cls._read_metadata(root / "metadata.jsonl"))
        candles = cls._read_candles(root / "candles.jsonl")
        events = cls._sort_events(events)
        metadata = tuple(sorted(metadata, key=lambda item: item[0]))
        digest = cls._digest_events_metadata_candles(events, metadata, candles)
        expected = str(manifest_data.get("integrity_sha256", ""))
        if expected != digest:
            raise ValueError(f"dataset integrity mismatch: expected {expected}, actual {digest}")
        manifest = HistoricalDatasetManifest(
            period=str(manifest_data["period"]),
            source=str(manifest_data["source"]),
            symbols=tuple(sorted({str(s).upper() for s in manifest_data["symbols"]})),
            event_types=tuple(sorted(str(s) for s in manifest_data["event_types"])),
            timeframes=tuple(sorted(str(s) for s in manifest_data["timeframes"])),
            timestamp_convention=str(manifest_data["timestamp_convention"]),
            ordering_convention=str(manifest_data["ordering_convention"]),
            dataset_version=str(manifest_data["dataset_version"]),
            integrity_sha256=digest,
        )
        dataset = cls(manifest, events, metadata, candles)
        dataset.validate()
        return dataset

    def write_directory(self, root: Path) -> None:
        """Persist the immutable dataset files plus an integrity manifest."""
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        events = self._sort_events(self.events)
        metadata = tuple(sorted(self.metadata_snapshots, key=lambda item: item[0]))
        candles = {key: tuple(rows) for key, rows in sorted(self.candles.items())}
        digest = self._digest_events_metadata_candles(events, metadata, candles)
        manifest = HistoricalDatasetManifest(
            period=self.manifest.period,
            source=self.manifest.source,
            symbols=tuple(sorted(set(self.manifest.symbols))),
            event_types=tuple(sorted(set(self.manifest.event_types))),
            timeframes=tuple(sorted(set(self.manifest.timeframes))),
            timestamp_convention=self.manifest.timestamp_convention,
            ordering_convention=self.manifest.ordering_convention,
            dataset_version=self.manifest.dataset_version,
            integrity_sha256=digest,
        )
        (root / "events.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "timestamp": event.timestamp.isoformat(),
                        "symbol": event.symbol,
                        "event_type": event.event_type.value,
                        "payload": event.payload,
                        "source_event_id": event.event_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )
        (root / "metadata.jsonl").write_text(
            "".join(
                json.dumps(
                    {"timestamp": timestamp.isoformat(), "snapshot": snapshot},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ) + "\n"
                for timestamp, snapshot in metadata
            ),
            encoding="utf-8",
        )
        (root / "candles.jsonl").write_text(
            "".join(
                json.dumps(
                    {"symbol": symbol, "timeframe": timeframe, "row": row},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ) + "\n"
                for (symbol, timeframe), rows in candles.items()
                for row in rows
            ),
            encoding="utf-8",
        )
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "period": manifest.period,
                    "source": manifest.source,
                    "symbols": list(manifest.symbols),
                    "event_types": list(manifest.event_types),
                    "timeframes": list(manifest.timeframes),
                    "timestamp_convention": manifest.timestamp_convention,
                    "ordering_convention": manifest.ordering_convention,
                    "dataset_version": manifest.dataset_version,
                    "integrity_sha256": manifest.integrity_sha256,
                },
                indent=2,
                sort_keys=True,
                default=str,
            ) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _read_events(path: Path) -> Iterable[HistoricalMarketEvent]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            yield HistoricalMarketEvent(
                timestamp=timestamp,
                symbol=str(row["symbol"]),
                event_type=MarketEventType(str(row["event_type"])),
                payload=dict(row.get("payload", {})),
                source_event_id=row.get("source_event_id"),
            )

    @staticmethod
    def _read_metadata(path: Path) -> Iterable[tuple[datetime, Mapping[str, Any]]]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            snapshot = row.get("snapshot")
            if not isinstance(snapshot, Mapping):
                raise ValueError("metadata snapshot must be a mapping")
            yield timestamp, dict(snapshot)

    @staticmethod
    def _read_candles(path: Path) -> Mapping[tuple[str, str], tuple[tuple[Any, ...], ...]]:
        result: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (str(row["symbol"]).upper(), str(row["timeframe"]))
            candle = tuple(row["row"])
            result.setdefault(key, []).append(candle)
        return {key: tuple(sorted(rows, key=lambda item: int(item[0]))) for key, rows in result.items()}

    @staticmethod
    def _sort_events(events: Iterable[HistoricalMarketEvent]) -> tuple[HistoricalMarketEvent, ...]:
        materialized = list(events)
        materialized.sort(
            key=lambda event: (
                event.timestamp,
                event.symbol,
                event.event_type.value,
                event.event_id,
            )
        )
        return tuple(materialized)

    @staticmethod
    def _digest_events_metadata_candles(
        events: Iterable[HistoricalMarketEvent],
        metadata: Iterable[tuple[datetime, Mapping[str, Any]]],
        candles: Mapping[tuple[str, str], tuple[tuple[Any, ...], ...]],
    ) -> str:
        payload = {
            "events": [
                {
                    "timestamp": e.timestamp.isoformat(),
                    "symbol": e.symbol,
                    "event_type": e.event_type.value,
                    "payload": e.payload,
                    "source_event_id": e.event_id,
                }
                for e in events
            ],
            "metadata": [{"timestamp": ts.isoformat(), "snapshot": snapshot} for ts, snapshot in metadata],
            "candles": {f"{symbol}|{timeframe}": rows for (symbol, timeframe), rows in sorted(candles.items())},
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def start(self) -> datetime:
        candidates = [*(event.timestamp for event in self.events), *(ts for ts, _ in self.metadata_snapshots)]
        if not candidates:
            raise ValueError("historical dataset is empty")
        return min(candidates)

    @property
    def end(self) -> datetime:
        candidates = [*(event.timestamp for event in self.events), *(ts for ts, _ in self.metadata_snapshots)]
        if not candidates:
            raise ValueError("historical dataset is empty")
        return max(candidates)

    def metadata_at(self, timestamp: datetime) -> Mapping[str, Any]:
        timestamp = timestamp.astimezone(timezone.utc)
        visible: Mapping[str, Any] = {}
        for snapshot_timestamp, snapshot in self.metadata_snapshots:
            if snapshot_timestamp > timestamp:
                break
            visible = snapshot
        return visible

    def candles_at(self, symbol: str, timeframe: str, timestamp: datetime) -> tuple[tuple[Any, ...], ...]:
        cutoff_ms = int(timestamp.astimezone(timezone.utc).timestamp() * 1000)
        rows = self.candles.get((symbol.strip().upper(), timeframe), ())
        return tuple(row for row in rows if len(row) > 6 and int(row[6]) <= cutoff_ms)

    def validate(self) -> None:
        if not self.events and not self.metadata_snapshots:
            raise ValueError("historical dataset is empty")
        for index in range(1, len(self.events)):
            if self.events[index].timestamp < self.events[index - 1].timestamp:
                raise ValueError("events are not deterministically ordered")
        for index in range(1, len(self.metadata_snapshots)):
            if self.metadata_snapshots[index][0] < self.metadata_snapshots[index - 1][0]:
                raise ValueError("metadata snapshots are not deterministically ordered")
        symbols = set(self.manifest.symbols)
        if symbols and any(event.symbol not in symbols for event in self.events):
            raise ValueError("event symbol is outside manifest symbols")
        for (symbol, _), rows in self.candles.items():
            if symbols and symbol not in symbols:
                raise ValueError("candle symbol is outside manifest symbols")
            for row in rows:
                if len(row) < 6:
                    raise ValueError("historical candle row is incomplete")
