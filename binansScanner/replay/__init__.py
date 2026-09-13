"""Isolated historical Paper Replay infrastructure."""

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BINANS_SCANNER = _REPO_ROOT / "binansScanner"
if str(_BINANS_SCANNER) not in sys.path:
    sys.path.insert(0, str(_BINANS_SCANNER))

from replay.audit import MovementAudit, build_movement_audit, correlate_opportunity_refreshes
from replay.clock import ReplayClock
from replay.dataset import HistoricalDataset, HistoricalDatasetManifest, HistoricalMarketEvent
from replay.runner import HistoricalPaperReplayRunner, ReplayConfig
from replay.source import HistoricalMarketDataSource
from replay.stream import HistoricalMarketEventStream

__all__ = [
    "HistoricalDataset",
    "HistoricalDatasetManifest",
    "HistoricalMarketEvent",
    "HistoricalMarketDataSource",
    "HistoricalMarketEventStream",
    "HistoricalPaperReplayRunner",
    "ReplayClock",
    "ReplayConfig",
    "MovementAudit",
    "build_movement_audit",
    "correlate_opportunity_refreshes",
]
