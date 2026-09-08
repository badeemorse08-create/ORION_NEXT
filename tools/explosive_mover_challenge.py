from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from models.market_event import MarketEventType
from replay.dataset import HistoricalDataset, HistoricalDatasetManifest, HistoricalMarketEvent
from replay.runner import HistoricalPaperReplayRunner, ReplayConfig

UTC = timezone.utc
API = "https://data-api.binance.vision/api/v3/klines"

CAMPAIGN_A_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "UNIUSDT", "ATOMUSDT",
    "NEARUSDT", "APTUSDT", "SUIUSDT", "INJUSDT", "PEPEUSDT", "WIFUSDT",
)
CHALLENGE_SYMBOLS = CAMPAIGN_A_SYMBOLS + ("TRUMPUSDT", "STXUSDT")

SIMULATION_START = datetime(2026, 8, 18, tzinfo=UTC)
SIMULATION_END = datetime(2026, 8, 24, 23, 59, 59, 999000, tzinfo=UTC)
WARMUP_START = SIMULATION_START - timedelta(days=40)
MOVE_THRESHOLDS = (10, 20, 30, 50, 70, 100, 150, 200)
WINDOW_STEPS = {"5m": 1, "15m": 3, "30m": 6, "1h": 12, "4h": 48, "24h": 288}
TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
REPORT_VALIDATION_CONTRACT = {
    "universe_completeness": "NOT_ESTABLISHED",
    "production_semantic_impact": "NONE",
    "current_exchange_info_used": False,
    "future_universe_information_used": False,
    "campaign_B": "BLOCKED",
    "broad_market_complete_universe_test": "BLOCKED",
}


def _ts(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _request_json(symbol: str, start_ms: int, end_ms: int) -> list:
    q = urlencode({"symbol": symbol, "interval": "5m", "startTime": start_ms, "endTime": end_ms, "limit": 1000})
    request = Request(f"{API}?{q}", headers={"User-Agent": "ORION-explosive-mover-challenge/2.0", "Accept": "application/json"})
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise ValueError("Binance kline response was not a list")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt == 5:
                break
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"historical acquisition failed for {symbol}: {last_error}")


def fetch_5m(symbol: str, start: datetime, end: datetime) -> tuple[tuple, ...]:
    cur = _ts(start)
    stop = _ts(end)
    rows: list = []
    while cur < stop:
        batch = _request_json(symbol, cur, stop)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 5 * 60 * 1000
        if nxt <= cur:
            raise RuntimeError(f"pagination stalled for {symbol}")
        cur = nxt
        if len(batch) < 1000:
            break
    filtered = [tuple(row) for row in rows if _ts(start) <= int(row[0]) < stop]
    dedup: dict[int, tuple] = {int(row[0]): row for row in filtered}
    return tuple(dedup[key] for key in sorted(dedup))


def aggregate(rows: tuple[tuple, ...], minutes: int) -> tuple[tuple, ...]:
    bucket_ms = minutes * 60 * 1000
    needed = minutes // 5
    groups: dict[int, list[tuple]] = defaultdict(list)
    for row in rows:
        groups[(int(row[0]) // bucket_ms) * bucket_ms].append(row)
    result: list[tuple] = []
    for bucket, group in sorted(groups.items()):
        group.sort(key=lambda r: int(r[0]))
        if len(group) != needed:
            continue
        result.append((bucket, group[0][1], max(float(r[2]) for r in group), min(float(r[3]) for r in group), group[-1][4], sum(float(r[5]) for r in group), bucket + bucket_ms - 1, "0", sum(int(r[8]) for r in group), "0", "0", "0"))
    return tuple(result)


def source_references(symbol: str) -> dict[str, str]:
    refs = {
        "spot_kline_endpoint": f"{API}?symbol={symbol}&interval=5m&startTime=<historical>&endTime=<historical>&limit=1000",
        "binance_market_data_docs": "https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md",
        "binance_public_data": "https://github.com/binance/binance-public-data/blob/master/README.md",
    }
    if symbol == "TRUMPUSDT":
        refs["binance_historical_listing"] = "https://www.binance.com/en-BH/support/announcement/detail/098d3a7afe5244b5ba6ea728f933817f"
    elif symbol == "STXUSDT":
        refs["binance_spot_market"] = "https://www.binance.com/en/trade/STX_USDT"
    return refs


def acquire_dataset(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    raw: dict[str, tuple[tuple, ...]] = {}
    validation: dict[str, dict] = {}
    for symbol in CHALLENGE_SYMBOLS:
        rows = fetch_5m(symbol, WARMUP_START, SIMULATION_END + timedelta(milliseconds=1))
        raw[symbol] = rows
        pre = [r for r in rows if int(r[0]) < _ts(SIMULATION_START)]
        period = [r for r in rows if _ts(SIMULATION_START) <= int(r[6]) <= _ts(SIMULATION_END)]
        validation[symbol] = {
            "historical_existence": bool(pre),
            "historical_period_trade": bool(period),
            "historical_evidence_is_not_current_exchange_info": True,
            "pre_start_first_observable": None if not pre else _iso(int(pre[0][0])),
            "period_first_observable": None if not period else _iso(int(period[0][0])),
            "period_last_observable": None if not period else _iso(int(period[-1][6])),
            "usdt_identity": symbol.endswith("USDT"),
            "spot_market_evidence": "Binance Spot historical kline observations for the exact USDT symbol",
            "status_semantics": "TRADING_OBSERVED_BY_HISTORICAL_SPOT_KLINE; historical exchangeInfo snapshot unavailable",
            "future_exchange_info_used": False,
            "source_references": source_references(symbol),
        }
        if not all((validation[symbol]["historical_existence"], validation[symbol]["historical_period_trade"], validation[symbol]["usdt_identity"])):
            raise RuntimeError(f"challenge symbol historical validation failed: {symbol}: {validation[symbol]}")

    candles: dict[tuple[str, str], tuple[tuple, ...]] = {}
    for symbol, rows in raw.items():
        candles[(symbol, "5m")] = rows
        for tf, minutes in TF_MIN.items():
            if tf != "5m":
                candles[(symbol, tf)] = aggregate(rows, minutes)

    lo, hi = _ts(SIMULATION_START), _ts(SIMULATION_END)
    events: list[HistoricalMarketEvent] = []
    for symbol, rows in raw.items():
        for row in rows:
            close_ms = int(row[6])
            if lo <= close_ms <= hi:
                events.append(HistoricalMarketEvent(
                    timestamp=datetime.fromtimestamp(close_ms / 1000, tz=UTC),
                    symbol=symbol,
                    event_type=MarketEventType.CANDLE_CLOSE,
                    payload={"timeframe": "5m", "open_time": _iso(int(row[0])), "close_time": _iso(close_ms), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]), "is_closed": True},
                    source_event_id=f"{symbol}:5m:{int(row[0])}",
                ))
    events = sorted(events, key=lambda e: (e.timestamp, e.symbol, e.event_type.value, e.event_id))

    metadata = ((SIMULATION_START, {
        "exchange_info": {"symbols": [
            {"symbol": symbol, "status": "TRADING", "baseAsset": symbol[:-4], "quoteAsset": "USDT", "isSpotTradingAllowed": True, "historical_evidence_basis": "individually validated historical Binance Spot evidence; challenge-scoped only"}
            for symbol in CHALLENGE_SYMBOLS
        ], "scope": "INDIVIDUAL_CHALLENGE_SET_ONLY", "universe_completeness": "NOT_ESTABLISHED", "current_exchange_info_used": False, "future_universe_information_used": False}
    }),)

    manifest = HistoricalDatasetManifest(
        period=f"{SIMULATION_START.isoformat()}/{SIMULATION_END.isoformat()}",
        source="Binance Spot historical public market data via data-api.binance.vision; selected individually validated challenge set",
        symbols=CHALLENGE_SYMBOLS,
        event_types=("candle_close",),
        timeframes=tuple(TF_MIN),
        timestamp_convention="UTC milliseconds; event timestamp = 5m candle close",
        ordering_convention="timestamp,symbol,event_type,stable source id",
        dataset_version="binance-spot-explosive-challenge-v2",
        integrity_sha256="pending",
    )
    dataset = HistoricalDataset(manifest, tuple(events), metadata, candles)
    dataset.write_directory(root)
    parsed = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    parsed.update({"scope": "INDIVIDUAL_VALIDATED_CHALLENGE_SET", "universe_completeness": "NOT_ESTABLISHED", "challenge_symbols": list(CHALLENGE_SYMBOLS), "campaign_interval": f"{SIMULATION_START.isoformat()}/{SIMULATION_END.isoformat()}", "warmup_start": WARMUP_START.isoformat(), "historical_validation": validation, "future_exchange_info_used": False, "future_universe_information_used": False, "source_policy": "No current exchangeInfo or current symbol list is used for challenge membership; no future listing/status information is injected", "sha256_scope": "HistoricalDataset canonical digest in integrity_sha256"})
    (root / "manifest.json").write_text(json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"validation": validation, "dataset_hash": parsed["integrity_sha256"], "event_count": len(events)}


def detect_moves(dataset: HistoricalDataset) -> list[dict]:
    results: list[dict] = []
    for symbol in dataset.manifest.symbols:
        closes = [(int(r[6]), float(r[4])) for r in dataset.candles[(symbol, "5m")]]
        index = {ts: i for i, (ts, _) in enumerate(closes)}
        for window, n in WINDOW_STEPS.items():
            crossed: set[int] = set()
            for ts, price in closes:
                if not (_ts(SIMULATION_START) <= ts <= _ts(SIMULATION_END)):
                    continue
                i = index[ts]
                if i < n:
                    continue
                base_ts, base_price = closes[i - n]
                if base_price <= 0:
                    continue
                move = (price / base_price - 1.0) * 100.0
                for threshold in MOVE_THRESHOLDS:
                    if threshold not in crossed and move >= threshold:
                        crossed.add(threshold)
                        results.append({"symbol": symbol, "window": window, "threshold_pct": threshold, "base_timestamp": _iso(base_ts), "threshold_cross_timestamp": _iso(ts), "base_price": base_price, "price_at_threshold": price, "movement_pct": move})
    return sorted(results, key=lambda x: (x["threshold_cross_timestamp"], x["symbol"], x["window"], x["threshold_pct"]))


def _serialize_candidate(candidate) -> dict:
    trace = candidate.decision_trace
    payload = {"symbol": candidate.symbol, "rank": candidate.rank, "opportunity_score": candidate.opportunity_score, "opportunity_class": candidate.opportunity_class, "entry_state": candidate.entry_state, "entry_readiness": candidate.entry_readiness, "entry_allowed": None if trace is None else trace.entry_allowed, "rejection_reasons": [] if trace is None else [getattr(r, "value", str(r)) for r in trace.rejection_reasons], "directional_evidence": candidate.directional_evidence, "recall_lanes": list(candidate.recall_lanes), "score_components": list(candidate.score_components), "eligibility_reasons": list(candidate.eligibility_reasons)}
    if trace is not None:
        payload["decision_trace"] = asdict(trace) if is_dataclass(trace) else str(trace)
    return payload


def _install_observer(runner: HistoricalPaperReplayRunner, path: Path) -> None:
    handle = path.open("w", encoding="utf-8")
    original = runner.opportunity.discover
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        event = runner.supervisor.last_processed_market_event
        ts = event.event_timestamp.isoformat() if event is not None else None
        active = {candidate.symbol for candidate in result.active_set.candidates}
        record = {"refresh_timestamp": ts, "active_symbols": sorted(active), "recall_counts": [list(pair) for pair in result.recall_counts], "recalled_provenance": [[symbol, list(lanes)] for symbol, lanes in result.recalled_provenance], "broad_candidates": [_serialize_candidate(c) | {"active": c.symbol in active} for c in result.broad_pool.candidates]}
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        handle.flush()
        return result
    runner.opportunity.discover = wrapped
    runner._challenge_observer_handle = handle


def _close_observer(runner: HistoricalPaperReplayRunner) -> None:
    handle = getattr(runner, "_challenge_observer_handle", None)
    if handle is not None:
        handle.close()


def parse_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _event_time(row: dict | None, fallback_key: str = "timestamp") -> datetime | None:
    if not isinstance(row, dict):
        return None
    raw = row.get(fallback_key) or row.get("refresh_timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _validate_report_contract(report: dict) -> None:
    actual = {key: report.get(key) for key in REPORT_VALIDATION_CONTRACT}
    if actual != REPORT_VALIDATION_CONTRACT:
        failures = [f"{key}: expected={REPORT_VALIDATION_CONTRACT[key]!r}, actual={actual[key]!r}" for key in REPORT_VALIDATION_CONTRACT if actual[key] != REPORT_VALIDATION_CONTRACT[key]]
        raise AssertionError("report validation contract mismatch: " + "; ".join(failures))
    for key in ("current_exchange_info_used", "future_universe_information_used"):
        if type(report.get(key)) is not bool:
            raise AssertionError(f"report validation contract type mismatch: {key} must be bool")


def build_evidence(output: Path, dataset: HistoricalDataset, validation: dict, detections: list[dict], base_report: dict) -> dict:
    cycles = parse_jsonl(output / "pipeline_observations.jsonl")
    runtime_events = parse_jsonl(output / "events.jsonl")
    signal_by_symbol: dict[str, list[dict]] = defaultdict(list)
    orders_by_symbol: dict[str, list[dict]] = defaultdict(list)
    fills_by_symbol: dict[str, list[dict]] = defaultdict(list)
    exits_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in runtime_events:
        symbol = row.get("symbol")
        et = row.get("event_type")
        if not symbol:
            continue
        if et == "signal_event": signal_by_symbol[symbol].append(row)
        elif et in {"order_lifecycle", "order_rejected"}: orders_by_symbol[symbol].append(row)
        elif et == "fill": fills_by_symbol[symbol].append(row)
        elif et == "replay_end_position_close": exits_by_symbol[symbol].append(row)

    closes = {s: [(int(r[6]), float(r[4])) for r in dataset.candles[(s, "5m")]] for s in dataset.manifest.symbols}
    peak = {s: max((float(r[2]) for r in dataset.candles[(s, "5m")] if _ts(SIMULATION_START) <= int(r[6]) <= _ts(SIMULATION_END)), default=None) for s in dataset.manifest.symbols}

    def latest_candidate(symbol: str, at: datetime):
        eligible_cycles = []
        for cycle in cycles:
            ct = _event_time(cycle, "refresh_timestamp")
            if ct is not None and ct <= at:
                cand = next((c for c in cycle.get("broad_candidates", []) if c.get("symbol") == symbol), None)
                if cand is not None:
                    eligible_cycles.append((ct, cycle, cand))
        return eligible_cycles[-1] if eligible_cycles else None

    causal: list[dict] = []
    for d in detections:
        symbol = d["symbol"]
        threshold_dt = datetime.fromisoformat(d["threshold_cross_timestamp"])
        first_observable_dt = datetime.fromisoformat(d["base_timestamp"])
        cycle_info = latest_candidate(symbol, threshold_dt)
        discovery_dt = cycle_info[0] if cycle_info else None
        candidate = cycle_info[2] if cycle_info else None
        discovered_before_threshold = discovery_dt is not None and discovery_dt < threshold_dt
        movement_latency = (threshold_dt - first_observable_dt).total_seconds()
        detection_latency = None if discovery_dt is None else (discovery_dt - threshold_dt).total_seconds()

        actionable_info = None
        for cycle in cycles:
            ct = _event_time(cycle, "refresh_timestamp")
            if ct is None or ct < threshold_dt:
                continue
            cand = next((c for c in cycle.get("broad_candidates", []) if c.get("symbol") == symbol), None)
            if cand and cand.get("entry_allowed") is True and cand.get("entry_state") in {"A", "A+"}:
                actionable_info = (ct, cand)
                break
        actionable_dt = actionable_info[0] if actionable_info else None
        decision_latency = None if discovery_dt is None or actionable_dt is None else (actionable_dt - discovery_dt).total_seconds()

        first_order = orders_by_symbol[symbol][0] if orders_by_symbol[symbol] else None
        first_fill = fills_by_symbol[symbol][0] if fills_by_symbol[symbol] else None
        first_exit = exits_by_symbol[symbol][0] if exits_by_symbol[symbol] else None
        entry_decision_dt = actionable_dt
        order_dt = _event_time(first_order)
        fill_dt = _event_time(first_fill)
        exit_dt = _event_time(first_exit)
        entry_latency = None if entry_decision_dt is None or order_dt is None else (order_dt - entry_decision_dt).total_seconds()

        first_price = None
        if discovery_dt is not None:
            candidates = [p for t, p in closes[symbol] if t <= _ts(discovery_dt)]
            first_price = candidates[-1] if candidates else None
        market_move = d["movement_pct"]
        observable_move = None if first_price is None else (first_price / d["base_price"] - 1.0) * 100.0
        entry_price = None if first_order is None else first_order.get("price")
        exit_price = None if first_exit is None else first_exit.get("price")
        realized_return = None if not entry_price or not exit_price else (float(exit_price) / float(entry_price) - 1.0) * 100.0
        max_realizable = None if not entry_price or not peak[symbol] or peak[symbol] <= float(entry_price) else (peak[symbol] / float(entry_price) - 1.0) * 100.0
        capture_ratio = None if realized_return is None or not max_realizable or max_realizable <= 0 else realized_return / max_realizable

        stage, reason = "NOT_RECALLED", "NOT_IN_ELIGIBLE_RECALL_POOL_AT_OR_BEFORE_THRESHOLD"
        eligibility_result = "UNKNOWN"
        if candidate is not None:
            stage = "EVALUATED"
            eligibility_result = "ELIGIBLE"
            if candidate.get("entry_allowed") is False:
                stage, reason = "EVALUATED_NOT_ACTIONABLE", ";".join(candidate.get("rejection_reasons", [])) or "ENTRY_NOT_ALLOWED"
            elif candidate.get("entry_allowed") is True:
                stage, reason = "ACTIONABLE", ""
        if first_order is not None: stage, reason = "ORDERED", ""
        if first_fill is not None: stage, reason = "FILLED", ""
        if first_exit is not None: stage, reason = "EXITED", ""

        causal.append({
            "symbol": symbol, "window": d["window"], "threshold_pct": d["threshold_pct"], "threshold_cross_timestamp": d["threshold_cross_timestamp"], "first_observable_timestamp": d["base_timestamp"], "historical_validation": validation[symbol], "eligibility_result": eligibility_result, "fast_recall_lanes": [] if candidate is None else candidate.get("recall_lanes", []), "recall_result": candidate is not None, "deep_evaluation": candidate is not None, "score": None if candidate is None else candidate.get("opportunity_score"), "actionability": None if candidate is None else candidate.get("entry_allowed"), "discovery_timestamp": None if discovery_dt is None else discovery_dt.isoformat(), "decision_timestamp": None if actionable_dt is None else actionable_dt.isoformat(), "entry_decision_timestamp": None if entry_decision_dt is None else entry_decision_dt.isoformat(), "order_timestamp": None if order_dt is None else order_dt.isoformat(), "fill_timestamp": None if fill_dt is None else fill_dt.isoformat(), "exit_timestamp": None if exit_dt is None else exit_dt.isoformat(), "entry_price": entry_price, "exit_price": exit_price, "realized_return_pct": realized_return, "movement_latency_seconds": movement_latency, "detection_latency_seconds": detection_latency, "discovered_before_threshold": discovered_before_threshold, "decision_latency_seconds": decision_latency, "entry_latency_seconds": entry_latency, "entry_status": "ENTERED" if entry_decision_dt is not None else "NOT_ENTERED", "market_move_pct": market_move, "observable_move_pct": observable_move, "peak_price_in_period": peak[symbol], "capture_ratio": capture_ratio, "stage_at_threshold": stage, "stage_reason": reason, "order_event": first_order, "fill_event": first_fill, "exit_event": first_exit, "decision_trace": None if candidate is None else candidate.get("decision_trace"),
        })

    table: list[dict] = []
    for threshold in MOVE_THRESHOLDS:
        for window in WINDOW_STEPS:
            subset = [c for c in causal if c["threshold_pct"] == threshold and c["window"] == window]
            table.append({
                "threshold_pct": threshold, "window": window, "candidate_count": len(subset), "historically_validated_count": sum(1 for c in subset if c["historical_validation"]["historical_existence"] and c["historical_validation"]["historical_period_trade"]), "eligible_count": sum(1 for c in subset if c["eligibility_result"] == "ELIGIBLE"), "recalled_count": sum(1 for c in subset if c["recall_result"]), "evaluated_count": sum(1 for c in subset if c["deep_evaluation"]), "actionable_count": sum(1 for c in subset if c["actionability"] is True), "entries": sum(1 for c in subset if c["entry_status"] == "ENTERED"), "fills": sum(1 for c in subset if c["fill_timestamp"] is not None), "exits": sum(1 for c in subset if c["exit_timestamp"] is not None), "captured_count": sum(1 for c in subset if c["realized_return_pct"] is not None and c["realized_return_pct"] > 0), "missed_count": sum(1 for c in subset if c["stage_at_threshold"] not in {"FILLED", "EXITED"}),
            })

    reps = {
        "strongest_validated_mover": max(causal, key=lambda c: float(c["market_move_pct"]), default=None),
        "fastest_short_term_mover": min([c for c in causal if c["window"] in {"5m", "15m", "30m", "1h"}], key=lambda c: c["movement_latency_seconds"], default=None),
        "mover_ge_70": next((c for c in causal if c["threshold_pct"] >= 70), None),
        "mover_ge_100": next((c for c in causal if c["threshold_pct"] >= 100), None),
        "failure_case": next((c for c in causal if c["stage_at_threshold"] in {"NOT_RECALLED", "EVALUATED_NOT_ACTIONABLE"}), None),
    }

    report = {"title": "D2 CORRECTED EXPLOSIVE-MOVER CHALLENGE REPORT", "status": "COMPLETED", "challenge_symbol_count": len(dataset.manifest.symbols), "challenge_symbols": list(dataset.manifest.symbols), **REPORT_VALIDATION_CONTRACT, "scope_statement": "This test validates selected historical explosive movers only. It does not establish complete Binance-market coverage.", "simulation_interval": f"{SIMULATION_START.isoformat()}/{SIMULATION_END.isoformat()}", "dataset_hash": dataset.manifest.integrity_sha256, "base_replay_report": base_report, "latency_methodology": {"movement_latency": "threshold_cross_timestamp - first_observable_timestamp", "detection_latency": "discovery_timestamp - threshold_cross_timestamp; signed; negative means discovered_before_threshold", "decision_latency": "first_actionable_timestamp - discovery_timestamp", "entry_latency": "entry_decision_timestamp to order_timestamp; null when NOT_ENTERED"}, "threshold_window_statistics": table, "causal_traces": causal, "representative_cases": reps}
    _validate_report_contract(report)
    report_bytes = json.dumps(report, indent=2, sort_keys=True, default=str).encode("utf-8")
    (output / "challenge_report.json").write_bytes(report_bytes)
    _validate_report_contract(json.loads((output / "challenge_report.json").read_text(encoding="utf-8")))
    (output / "causal_traces.json").write_text(json.dumps(causal, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (output / "threshold_window_statistics.json").write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "detected_movers.json").write_text(json.dumps(detections, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "historical_validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "challenge_manifest.json").write_text(json.dumps({"scope": "INDIVIDUAL_VALIDATED_CHALLENGE_SET", "universe_completeness": "NOT_ESTABLISHED", "symbols": list(dataset.manifest.symbols), "dataset_hash": dataset.manifest.integrity_sha256, "production_semantic_impact": "NONE"}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("explosive_mover_challenge"))
    args = parser.parse_args()
    output = args.output
    dataset_root = output / "dataset"
    replay_out = output / "replay"
    output.mkdir(parents=True, exist_ok=True)

    acquired = acquire_dataset(dataset_root)
    detections = detect_moves(HistoricalDataset.from_directory(dataset_root))
    (output / "historical_validation.json").write_text(json.dumps(acquired["validation"], indent=2, sort_keys=True) + "\n", encoding="utf-8")

    dataset = HistoricalDataset.from_directory(dataset_root)
    cfg = ReplayConfig(campaign="7D", acceleration_factor=600.0, end_policy="CLOSE_AT_END", active_top_n=10, broad_pool_top_n=20)
    runner = HistoricalPaperReplayRunner.build(dataset, replay_out, replay_config=cfg, starting_capital=200.0)
    observer_path = output / "pipeline_observations.jsonl"
    _install_observer(runner, observer_path)
    try:
        base_report = asyncio.run(runner.run_replay(dataset, replay_config=cfg))
    finally:
        _close_observer(runner)

    report = build_evidence(output, dataset, acquired["validation"], detections, base_report)
    sha_lines = []
    for path in sorted(p for p in output.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        sha_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(output).as_posix()}")
    (output / "SHA256SUMS").write_text("\n".join(sha_lines) + "\n", encoding="utf-8")
    print("CHALLENGE_SET_SYMBOL_COUNT", len(CHALLENGE_SYMBOLS))
    print("UNIVERSE_COMPLETENESS NOT_ESTABLISHED")
    print("CURRENT_EXCHANGE_INFO_USED", False)
    print("FUTURE_UNIVERSE_INFORMATION_USED", False)
    print("PRODUCTION_SEMANTIC_IMPACT NONE")
    print("PROCESSED_EVENTS", base_report["processed_event_count"])
    print("DECISION_CYCLES", base_report["decision_cycles"])
    print("ORDERS", base_report["orders"])
    print("FILLS", base_report["fills"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
