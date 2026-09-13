# ORION Historical Paper Replay — Implementation

Baseline: `f7c7341e5f3cf13ed05cde35342122badb85185c`
Branch: `verification/historical-paper-replay-design`

## Boundary

Replay replaces the public market-data transport boundary with `HistoricalMarketDataSource` and replaces the live websocket source with `HistoricalMarketEventStream`. Downstream `MarketUniverseDiscovery`, `OpportunityDiscovery`, `FastRecall`, `ScalpingOpportunityPipeline`, `PaperRealtimeLifecycle`, `PaperRuntimeSupervisor`, and Paper ledger contracts remain the canonical implementations.

## Dataset

A dataset is immutable after preload and is described by a manifest containing period, source, symbols, event types, timeframes, timestamp convention, ordering convention, version, and SHA-256 integrity hash. Event, metadata, and candle files are loaded before replay starts.

## Replay clock

`ReplayClock` owns simulation time independently from wall-clock time. `simulation_timestamp` controls visibility and discovery refresh. `wall_clock_timestamp` is observational only. Acceleration changes wall-clock pacing semantics and never changes event timestamps or ordering.

## Progressive release / no look-ahead

The stream sorts events deterministically and advances the simulation clock to each event timestamp before release. Historical metadata and candles are resolved only from snapshots/rows whose timestamps are at or before the current simulation timestamp. Future rows remain inaccessible to downstream discovery.

## Order/fill causality

The replay reuses `PaperRealtimeLifecycle` for orders and fills. A fill can occur only while a paper order already exists and a later visible market event satisfies the existing D4/D5 lifecycle rules. No future candle is inspected to force an earlier fill.

## Historical universe

Universe metadata is resolved as-of simulation time from the latest visible metadata snapshot. A symbol is not visible before the snapshot that establishes it.

## Paper-only safety

The historical source implements only offline dataset reads and raises on unsupported market-data paths instead of falling back to Binance. No API credentials or exchange order interfaces are used. The runtime lifecycle remains the existing Paper runtime.

## End-of-run policy

This implementation uses `CLOSE_AT_END`. Open paper positions are closed using the last market price already visible in the dataset at the simulation end timestamp. The policy is recorded in replay evidence.

## Recovery

The existing `PaperRuntimeSupervisor.recover()` implementation is reused. Verification compares uninterrupted execution with checkpoint recovery plus deterministic continuation. No replay-specific state is persisted into the live production path.

## Campaigns

One replay engine supports `7D`, `30D`, `90D`, and `365D` campaign labels. Dataset period selection remains a dataset/manifest concern rather than separate business engines.

## Evidence

Each replay output includes code SHA, dataset hash/version, configuration, simulation and wall-clock timing, event counts, orders/fills/positions, capital state, runtime health, duplicate/out-of-order detection, end-of-run policy, and lookahead verification. `events.jsonl` records the progressive run history.

## Limitations

Replay requires a complete immutable historical dataset covering every market-data contract consumed by the selected Paper path. No live Binance fallback exists. Dataset completeness and exchange metadata fidelity are therefore prerequisites for historical validity.
