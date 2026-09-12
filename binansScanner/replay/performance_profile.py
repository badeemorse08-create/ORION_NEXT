from __future__ import annotations

import asyncio
import json
import math
import resource
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models.market_event import MarketEventType
from replay.dataset import HistoricalDataset, HistoricalDatasetManifest
from replay.runner import HistoricalPaperReplayRunner, ReplayConfig
from tools._orion_paper_8h_runner_legacy import JsonlRunLog
from services.opportunity_discovery import OpportunityDiscovery
from services.scalping_opportunity import ScalpingDecisionEngine
from services.scalping_pipeline import FastRecall, ScalpingOpportunityPipeline

PROFILE_HOURS = 24
HEARTBEAT_SIM_SECONDS = 30 * 60


def _rss_mb() -> float:
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _timed(collector: dict[str, list[float]], name: str, original):
    def wrapped(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            collector.setdefault(name, []).append(time.perf_counter() - started)
    return wrapped


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _stats(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "average_seconds": sum(values) / len(values) if values else 0.0,
        "p95_seconds": _p95(values),
        "total_seconds": sum(values),
    }


def _profile_dataset(full: HistoricalDataset) -> HistoricalDataset:
    start = full.start
    end = start + timedelta(hours=PROFILE_HOURS)
    events = tuple(event for event in full.events if event.timestamp <= end)
    metadata = tuple(item for item in full.metadata_snapshots if item[0] <= end)
    if not metadata:
        metadata = (full.metadata_snapshots[0],)
    manifest = HistoricalDatasetManifest(
        period=f"{start.isoformat()}/{end.isoformat()}",
        source=full.manifest.source,
        symbols=full.manifest.symbols,
        event_types=full.manifest.event_types,
        timeframes=full.manifest.timeframes,
        timestamp_convention=full.manifest.timestamp_convention,
        ordering_convention=full.manifest.ordering_convention,
        dataset_version=full.manifest.dataset_version,
        integrity_sha256=full.manifest.integrity_sha256,
    )
    profile = HistoricalDataset(manifest, events, metadata, full.candles)
    profile.validate()
    return profile


def run_profile(dataset_root: Path, output_root: Path) -> dict:
    full = HistoricalDataset.from_directory(dataset_root)
    profile_dataset = _profile_dataset(full)
    timings: dict[str, list[float]] = defaultdict(list)
    heartbeats: list[dict] = []
    event_latencies: list[float] = []
    cycle_latencies: list[float] = []

    originals = {
        "discovery": OpportunityDiscovery.discover,
        "recall": FastRecall.recall,
        "evaluate": ScalpingOpportunityPipeline._evaluate_candidate,
        "decision": ScalpingDecisionEngine.decide,
        "pipeline": ScalpingOpportunityPipeline.discover,
        "dataset_lookup": HistoricalDataset.candles_at,
    }
    OpportunityDiscovery.discover = _timed(timings, "opportunity_discovery", originals["discovery"])
    FastRecall.recall = _timed(timings, "fast_recall", originals["recall"])
    ScalpingOpportunityPipeline._evaluate_candidate = _timed(timings, "scoring_evaluation", originals["evaluate"])
    ScalpingDecisionEngine.decide = _timed(timings, "scoring_decision", originals["decision"])
    ScalpingOpportunityPipeline.discover = _timed(timings, "canonical_pipeline_discover", originals["pipeline"])
    HistoricalDataset.candles_at = _timed(timings, "dataset_lookup", originals["dataset_lookup"])

    runner_cls = HistoricalPaperReplayRunner
    runner_handler_original = getattr(runner_cls, "_on_market_event")
    log_original = JsonlRunLog.write

    build_started = time.perf_counter()
    runner = runner_cls.build(
        profile_dataset,
        output_root / "run",
        replay_config=ReplayConfig(campaign="7D", acceleration_factor=600.0, end_policy="CLOSE_AT_END", active_top_n=10, broad_pool_top_n=20),
        starting_capital=200.0,
    )
    startup_wall = time.perf_counter() - build_started

    def profiled_handler(self, event):
        started = time.perf_counter()
        try:
            return runner_handler_original(self, event)
        finally:
            elapsed = time.perf_counter() - started
            event_latencies.append(elapsed)
            if getattr(event, "event_type", None) is MarketEventType.CANDLE_CLOSE:
                cycle_latencies.append(elapsed)

    def profiled_log(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return log_original(self, *args, **kwargs)
        finally:
            timings["logging_serialization"].append(time.perf_counter() - started)

    runner_cls._on_market_event = profiled_handler
    JsonlRunLog.write = profiled_log

    original_stream_events = runner.stream.events
    profile_start = profile_dataset.start
    profile_end = profile_dataset.end
    profile_t0: float | None = None
    last_heartbeat = profile_start - timedelta(seconds=HEARTBEAT_SIM_SECONDS)
    heartbeat_count = 0

    async def profiled_stream_events():
        nonlocal profile_t0, last_heartbeat, heartbeat_count
        async for raw in original_stream_events():
            if profile_t0 is None:
                profile_t0 = time.perf_counter()
            timestamp = datetime.fromtimestamp(int(raw["E"]) / 1000.0, tz=timezone.utc)
            if timestamp > profile_end:
                break
            yield raw
            if timestamp - last_heartbeat >= timedelta(seconds=HEARTBEAT_SIM_SECONDS):
                heartbeat_count += 1
                elapsed = time.perf_counter() - profile_t0
                processed = len(event_latencies)
                progress = max(0.0, min(1.0, (timestamp - profile_start).total_seconds() / (profile_end - profile_start).total_seconds()))
                rate = processed / elapsed if elapsed > 0 else 0.0
                estimated_total = elapsed / progress if progress > 0 else 0.0
                heartbeats.append({
                    "checkpoint": heartbeat_count,
                    "simulation_timestamp": timestamp.isoformat(),
                    "processed_event_count": processed,
                    "decision_cycle_count": len(cycle_latencies),
                    "elapsed_wall_seconds": elapsed,
                    "events_per_second": rate,
                    "simulated_seconds_elapsed": (timestamp - profile_start).total_seconds(),
                    "simulated_progress_percent": progress * 100.0,
                    "estimated_remaining_wall_seconds": max(0.0, estimated_total - elapsed),
                    "memory_maxrss_mb": _rss_mb(),
                })
                last_heartbeat = timestamp

    runner.stream.events = profiled_stream_events

    replay_started = time.perf_counter()
    try:
        report = asyncio.run(runner.run_replay(profile_dataset, replay_config=ReplayConfig(campaign="7D", acceleration_factor=600.0, end_policy="CLOSE_AT_END", active_top_n=10, broad_pool_top_n=20)))
        replay_wall = time.perf_counter() - replay_started
    finally:
        runner_cls._on_market_event = runner_handler_original
        JsonlRunLog.write = log_original
        OpportunityDiscovery.discover = originals["discovery"]
        FastRecall.recall = originals["recall"]
        ScalpingOpportunityPipeline._evaluate_candidate = originals["evaluate"]
        ScalpingDecisionEngine.decide = originals["decision"]
        ScalpingOpportunityPipeline.discover = originals["pipeline"]
        HistoricalDataset.candles_at = originals["dataset_lookup"]

    if not heartbeats and event_latencies:
        heartbeats.append({
            "checkpoint": 1,
            "simulation_timestamp": profile_end.isoformat(),
            "processed_event_count": len(event_latencies),
            "decision_cycle_count": len(cycle_latencies),
            "elapsed_wall_seconds": replay_wall,
            "events_per_second": len(event_latencies) / replay_wall if replay_wall else 0.0,
            "simulated_seconds_elapsed": (profile_end - profile_start).total_seconds(),
            "simulated_progress_percent": 100.0,
            "estimated_remaining_wall_seconds": 0.0,
            "memory_maxrss_mb": _rss_mb(),
        })

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "heartbeats.jsonl").write_text("".join(json.dumps(h, sort_keys=True) + "\n" for h in heartbeats), encoding="utf-8")

    events = len(event_latencies)
    cycles = len(cycle_latencies)
    profile_seconds = (profile_end - profile_start).total_seconds()
    est_7d_replay = replay_wall * 7.0
    est_7d_total = startup_wall + est_7d_replay
    source_hash = full.manifest.integrity_sha256

    component_totals = {name: sum(values) for name, values in timings.items()}
    measured_components = {
        key: value for key, value in component_totals.items()
        if key in {"opportunity_discovery", "fast_recall", "scoring_evaluation", "scoring_decision", "dataset_lookup", "logging_serialization"}
    }
    bottleneck = max(measured_components.items(), key=lambda item: item[1]) if measured_components else ("unknown", 0.0)

    final = {
        "profile_type": "controlled real historical replay performance profile",
        "profile_window_hours": PROFILE_HOURS,
        "exact_commit_sha": runner._code_sha(),
        "dataset_identity": {
            "source_integrity_sha256": source_hash,
            "dataset_version": full.manifest.dataset_version,
            "source_period": full.manifest.period,
            "profile_period": profile_dataset.manifest.period,
            "symbols": list(full.manifest.symbols),
            "universe_size": len(full.manifest.symbols),
            "underlying_dataset_contents_unchanged": True,
        },
        "profiling_interval": {
            "simulation_start": profile_start.isoformat(),
            "simulation_end": profile_end.isoformat(),
            "wall_clock_seconds": replay_wall,
        },
        "event_count": events,
        "decision_cycle_count": cycles,
        "throughput": {
            "events_per_second": events / replay_wall if replay_wall else 0.0,
            "decision_cycles_per_second": cycles / replay_wall if replay_wall else 0.0,
            "simulation_speedup_ratio": profile_seconds / replay_wall if replay_wall else 0.0,
        },
        "latency": {
            "per_event": _stats(event_latencies),
            "per_candle_close_decision_cycle": _stats(cycle_latencies),
            "opportunity_discovery": _stats(timings["opportunity_discovery"]),
            "fast_recall": _stats(timings["fast_recall"]),
            "scoring_evaluation": _stats(timings["scoring_evaluation"]),
            "scoring_decision": _stats(timings["scoring_decision"]),
            "dataset_lookup_access": _stats(timings["dataset_lookup"]),
            "logging_serialization": _stats(timings["logging_serialization"]),
            "canonical_pipeline_discover": _stats(timings["canonical_pipeline_discover"]),
        },
        "memory": {
            "peak_rss_mb": _rss_mb(),
            "growth_mb_from_first_checkpoint": (heartbeats[-1]["memory_maxrss_mb"] - heartbeats[0]["memory_maxrss_mb"]) if heartbeats else 0.0,
            "heartbeats": [{"simulation_timestamp": h["simulation_timestamp"], "processed_events": h["processed_event_count"], "maxrss_mb": h["memory_maxrss_mb"]} for h in heartbeats],
        },
        "heartbeat_progress": heartbeats,
        "progress_timestamps": [h["simulation_timestamp"] for h in heartbeats],
        "bottleneck_diagnosis": {
            "dominant_measured_component": bottleneck[0],
            "dominant_component_total_seconds": bottleneck[1],
            "share_of_replay_wall_clock": bottleneck[1] / replay_wall if replay_wall else 0.0,
            "component_totals_seconds": measured_components,
            "basis": "measured cumulative instrumentation totals from canonical replay path",
        },
        "estimated_full_7d_completion": {
            "replay_wall_seconds": est_7d_replay,
            "startup_wall_seconds": startup_wall,
            "total_wall_seconds": est_7d_total,
            "total_wall_minutes": est_7d_total / 60.0,
            "scaling_basis": "24h controlled profile scaled 7x plus one-time canonical startup",
        },
        "canonical_report": report,
        "safety": {
            "paper_only": True,
            "production_strategy_modified": False,
            "live_market_data_during_replay": False,
            "synthetic_market_data": False,
            "decision_cycles_skipped": False,
            "fast_recall_bypassed": False,
            "scoring_bypassed": False,
        },
        "reproducibility": {
            "profile_hours": PROFILE_HOURS,
            "acceleration_factor": 600.0,
            "active_top_n": 10,
            "broad_pool_top_n": 20,
            "heartbeat_sim_seconds": HEARTBEAT_SIM_SECONDS,
            "instrumentation": "runtime wrappers only; canonical methods executed unchanged",
        },
    }
    (output_root / "performance_profile.json").write_text(json.dumps(final, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return final


def main() -> None:
    dataset_root = Path("real_replay_profile/dataset")
    output_root = Path("real_replay_profile/profile")
    if not (dataset_root / "manifest.json").exists():
        raise FileNotFoundError(f"Campaign A dataset not found: {dataset_root}")
    result = run_profile(dataset_root, output_root)
    print("PROFILE_COMPLETE", result["exact_commit_sha"])
    print("DATASET_HASH", result["dataset_identity"]["source_integrity_sha256"])
    print("PROFILE_EVENTS", result["event_count"])
    print("PROFILE_DECISION_CYCLES", result["decision_cycle_count"])
    print("EVENTS_PER_SECOND", result["throughput"]["events_per_second"])
    print("DECISION_CYCLES_PER_SECOND", result["throughput"]["decision_cycles_per_second"])
    print("REPLAY_WALL_SECONDS", result["profiling_interval"]["wall_clock_seconds"])
    print("DOMINANT_COMPONENT", result["bottleneck_diagnosis"]["dominant_measured_component"])
    print("ESTIMATED_7D_TOTAL_WALL_MINUTES", result["estimated_full_7d_completion"]["total_wall_minutes"])


if __name__ == "__main__":
    main()
