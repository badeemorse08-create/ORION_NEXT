"""D1 fast recall and scalping pipeline orchestration."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Mapping, Sequence

from models.opportunity import OpportunityCandidate, OpportunityCandidateSet
from models.scalping_opportunity import (
    Candle,
    DecisionTrace,
    EntryState,
    OpportunityClass,
    RejectionReason,
    ScalpingCandidateSet,
)
from services.opportunity_discovery import OpportunityDiscovery
from services.scalping_opportunity import ScalpingCandidatePoolManager, ScalpingDecisionEngine


@dataclass(frozen=True, slots=True)
class RecallResult:
    candidates: tuple[OpportunityCandidate, ...]
    provenance: tuple[tuple[str, tuple[str, ...]], ...]
    counts: tuple[tuple[str, int], ...]


class FastRecall:
    """Recall union performed before deep scalping evaluation/ranking.

    Recall is deliberately broader than ranking. It uses only already-available
    market metadata and never grants Entry Readiness, sizing, or execution authority.
    """

    LANES = (
        "composite_opportunity",
        "high_mover",
        "short_term_acceleration",
        "volume_expansion",
        "breakout_range_expansion",
    )

    def __init__(self, *, lane_top_n: int = 5, composite_top_n: int = 10) -> None:
        if lane_top_n <= 0 or composite_top_n <= 0:
            raise ValueError("recall lane sizes must be positive")
        self.lane_top_n = lane_top_n
        self.composite_top_n = composite_top_n

    @staticmethod
    def _finite(value: float | None) -> float:
        return value if value is not None and value == value and abs(value) != float("inf") else 0.0

    def _score(self, candidate: OpportunityCandidate, lane: str) -> float:
        metrics = candidate.metrics
        if lane == "composite_opportunity":
            return self._finite(candidate.opportunity_score)
        if lane == "high_mover":
            return abs(self._finite(metrics.price_change_pct_24h))
        if lane == "short_term_acceleration":
            explicit = metrics.short_term_acceleration
            if explicit is not None:
                return abs(self._finite(explicit))
            direction = abs(self._finite(metrics.momentum_direction))
            quality = self._finite(metrics.momentum_quality)
            return direction * quality
        if lane == "volume_expansion":
            explicit = metrics.volume_expansion
            return self._finite(explicit) if explicit is not None else self._finite(metrics.volume_quality)
        explicit = metrics.breakout_score
        if explicit is not None:
            return self._finite(explicit)
        range_score = self._finite(metrics.range_expansion)
        structure = self._finite(metrics.structure_quality)
        return max(range_score, structure)

    def recall(self, candidates: Sequence[OpportunityCandidate], *, broad_limit: int) -> RecallResult:
        by_symbol = {candidate.symbol: candidate for candidate in candidates}
        lane_members: dict[str, tuple[str, ...]] = {}
        for lane in self.LANES:
            limit = self.composite_top_n if lane == "composite_opportunity" else self.lane_top_n
            ranked = sorted(by_symbol.values(), key=lambda item: (-self._score(item, lane), item.symbol))
            lane_members[lane] = tuple(item.symbol for item in ranked[:limit])

        provenance: dict[str, list[str]] = {}
        for lane in self.LANES:
            for symbol in lane_members[lane]:
                provenance.setdefault(symbol, []).append(lane)

        recalled_symbols = tuple(sorted(provenance, key=lambda symbol: (-by_symbol[symbol].opportunity_score, symbol)))
        recalled_symbols = recalled_symbols[:broad_limit]
        recalled = tuple(by_symbol[symbol] for symbol in recalled_symbols)
        provenance_out = tuple((symbol, tuple(provenance[symbol])) for symbol in recalled_symbols)
        counts = tuple((lane, len(lane_members[lane])) for lane in self.LANES)
        return RecallResult(recalled, provenance_out, counts)


class ScalpingOpportunityPipeline:
    """Eligible universe -> fast recall -> broad pool -> deep evaluation -> active set."""

    _RECALL_UNIVERSE_LIMIT = 10_000
    DEFAULT_CANDLE_FETCH_CONCURRENCY = 4

    def __init__(
        self,
        discovery: OpportunityDiscovery,
        candle_source,
        *,
        decision_engine: ScalpingDecisionEngine | None = None,
        pool_manager: ScalpingCandidatePoolManager | None = None,
        recall: FastRecall | None = None,
        candle_fetch_concurrency: int = DEFAULT_CANDLE_FETCH_CONCURRENCY,
    ) -> None:
        if candle_fetch_concurrency <= 0:
            raise ValueError("candle_fetch_concurrency must be positive")
        self.discovery = discovery
        self.candle_source = candle_source
        self.decision_engine = decision_engine or ScalpingDecisionEngine()
        self.pool_manager = pool_manager or ScalpingCandidatePoolManager(self.decision_engine.config)
        self.recall = recall or FastRecall()
        self.candle_fetch_concurrency = candle_fetch_concurrency

    @staticmethod
    def _raw_to_candles(raw: Sequence[object]) -> tuple[Candle, ...]:
        result: list[Candle] = []
        for row in raw:
            if isinstance(row, Candle):
                result.append(row)
                continue
            if not isinstance(row, Sequence) or len(row) < 6:
                continue
            result.append(
                Candle(
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                )
            )
        return tuple(result)

    @staticmethod
    def _daily_handoff(discovery: OpportunityDiscovery):
        source = getattr(discovery, "_metrics_source", None)
        take = getattr(source, "take_daily_candle_handoff", None)
        return take() if callable(take) else None

    def _candles_for(self, symbol: str, daily_handoff=None) -> Mapping[str, Sequence[Candle]]:
        required_limit = self.decision_engine.config.min_candles
        daily_rows = None
        if daily_handoff is not None and getattr(daily_handoff, "limit", None) == required_limit:
            daily_rows = getattr(daily_handoff, "candles", {}).get(symbol)

        result: dict[str, Sequence[Candle]] = {}
        if daily_rows is not None:
            result["1d"] = self._raw_to_candles(daily_rows)
        else:
            result["1d"] = tuple(self.candle_source.candles(symbol, "1d", required_limit))

        for timeframe in ("4h", "1h", "15m"):
            result[timeframe] = tuple(self.candle_source.candles(symbol, timeframe, required_limit))
        return result

    @staticmethod
    def _rebuild(
        candidate: OpportunityCandidate,
        *,
        opportunity_class: str | None = None,
        entry_state: str | None = None,
        entry_allowed: bool | None = None,
        extra_rejections: tuple[RejectionReason, ...] = (),
        extra_reasons: tuple[str, ...] = (),
    ) -> OpportunityCandidate:
        trace = candidate.decision_trace
        if isinstance(trace, DecisionTrace):
            rejection = tuple(dict.fromkeys(trace.rejection_reasons + extra_rejections))
            reasons = tuple(dict.fromkeys(trace.reasons + extra_reasons))
            trace = DecisionTrace(
                trace.discovered,
                trace.eligible,
                trace.measured_features,
                OpportunityClass(opportunity_class or trace.opportunity_class.value),
                trace.opportunity_score,
                trace.directional_evidence,
                EntryState(entry_state or trace.entry_state.value),
                trace.entry_allowed if entry_allowed is None else entry_allowed,
                rejection,
                reasons,
            )
        return OpportunityCandidate(
            candidate.symbol,
            candidate.opportunity_score,
            candidate.rank,
            candidate.metrics,
            candidate.eligibility_reasons,
            candidate.score_components,
            candidate.directional_evidence,
            opportunity_class or candidate.opportunity_class,
            entry_state or candidate.entry_state,
            candidate.entry_readiness,
            candidate.risk_reward,
            candidate.timeframe_evidence,
            trace,
            candidate.recall_lanes,
        )

    def _classify_from_evidence(self, candidate: OpportunityCandidate) -> OpportunityClass:
        evidence = {}
        for item in candidate.timeframe_evidence:
            timeframe = getattr(item, "timeframe", None)
            if timeframe is None:
                raise ValueError("classification evidence missing timeframe")
            evidence[timeframe] = item
        required = ("1d", "4h", "1h", "15m")
        if tuple(sorted(evidence)) != tuple(sorted(required)):
            raise ValueError("classification evidence is incomplete")
        return self.decision_engine.features._classify(evidence)

    def _classification_integrity(self, candidate: OpportunityCandidate) -> OpportunityCandidate:
        trace = candidate.decision_trace
        if not isinstance(trace, DecisionTrace):
            return candidate

        if trace.entry_state in (EntryState.C, EntryState.D) and trace.entry_allowed:
            candidate = self._rebuild(
                candidate,
                entry_allowed=False,
                extra_rejections=(RejectionReason.ENTRY_STATE_CONFLICT,),
                extra_reasons=("entry_state_conflict_blocked",),
            )
            trace = candidate.decision_trace

        if candidate.opportunity_class != OpportunityClass.UNCLASSIFIED.value:
            return candidate

        try:
            classified = self._classify_from_evidence(candidate)
        except (ValueError, KeyError, TypeError, AttributeError):
            classified = OpportunityClass.UNCLASSIFIED

        if classified != OpportunityClass.UNCLASSIFIED:
            return self._rebuild(
                candidate,
                opportunity_class=classified.value,
                extra_reasons=("classification_revalidated_from_class_specific_evidence",),
            )

        return self._rebuild(
            candidate,
            entry_state=EntryState.C.value,
            entry_allowed=False,
            extra_rejections=(RejectionReason.CLASSIFICATION_INSUFFICIENT,),
            extra_reasons=("classification_insufficient_evidence",),
        )

    def _with_recall_lanes(self, candidate: OpportunityCandidate, provenance: Mapping[str, tuple[str, ...]]) -> OpportunityCandidate:
        lanes = tuple(provenance.get(candidate.symbol, ()))
        components = tuple(candidate.score_components) + tuple((f"recall:{lane}", 1.0) for lane in lanes)
        return OpportunityCandidate(
            candidate.symbol,
            candidate.opportunity_score,
            candidate.rank,
            candidate.metrics,
            candidate.eligibility_reasons,
            components,
            candidate.directional_evidence,
            candidate.opportunity_class,
            candidate.entry_state,
            candidate.entry_readiness,
            candidate.risk_reward,
            candidate.timeframe_evidence,
            candidate.decision_trace,
            lanes,
        )

    def _evaluate_candidate(self, candidate: OpportunityCandidate, provenance: Mapping[str, tuple[str, ...]], daily_handoff=None, decision_kwargs: Mapping[str, object] | None = None) -> OpportunityCandidate:
        try:
            candle_map = self._candles_for(candidate.symbol, daily_handoff)
        except TimeoutError:
            raise
        except Exception:
            candle_map = {}
        decided = self.decision_engine.decide(candidate, candle_map, **dict(decision_kwargs or {}))
        return self._with_recall_lanes(self._classification_integrity(decided), provenance)

    def _evaluate_candidates(self, candidates: Sequence[OpportunityCandidate], provenance: Mapping[str, tuple[str, ...]], daily_handoff=None, decision_kwargs: Mapping[str, object] | None = None) -> tuple[OpportunityCandidate, ...]:
        if not candidates:
            return ()
        indexed: list[OpportunityCandidate | None] = [None] * len(candidates)
        with ThreadPoolExecutor(max_workers=min(self.candle_fetch_concurrency, len(candidates)), thread_name_prefix="orion-scalping-candles") as executor:
            future_to_index = {
                executor.submit(self._evaluate_candidate, candidate, provenance, daily_handoff, decision_kwargs): index
                for index, candidate in enumerate(candidates)
            }
            try:
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    indexed[index] = future.result()
            except TimeoutError:
                for future in future_to_index:
                    future.cancel()
                raise
        return tuple(item for item in indexed if item is not None)

    def discover(self, top_n: int | None = None, **decision_kwargs) -> ScalpingCandidateSet:
        """Run fast recall across the eligible universe before deep scalping ranking."""
        broad_top_n = top_n if top_n is not None else self.pool_manager.config.broad_pool_top_n
        if broad_top_n <= self.pool_manager.config.active_top_n:
            raise ValueError("broad discovery pool must be larger than active_top_n")

        universe = self.discovery.discover(top_n=self._RECALL_UNIVERSE_LIMIT)
        daily_handoff = self._daily_handoff(self.discovery)
        recall_result = self.recall.recall(universe.candidates, broad_limit=broad_top_n)
        if len(recall_result.candidates) <= self.pool_manager.config.active_top_n:
            raise ValueError("fast recall did not produce a sufficiently broad candidate pool")

        provenance = dict(recall_result.provenance)
        enriched = self._evaluate_candidates(recall_result.candidates, provenance, daily_handoff, decision_kwargs)

        broad_input = OpportunityCandidateSet(tuple(enriched), len(enriched), universe.snapshot_timestamp)
        result = self.pool_manager.select(broad_input, enriched)
        broad = tuple(self._with_recall_lanes(item, provenance) for item in result.broad_pool.candidates)
        active = tuple(self._with_recall_lanes(item, provenance) for item in result.active_set.candidates)
        return ScalpingCandidateSet(
            OpportunityCandidateSet(broad, len(broad), result.broad_pool.snapshot_timestamp),
            OpportunityCandidateSet(active, len(active), result.active_set.snapshot_timestamp),
            result.refreshed,
            tuple((symbol, tuple(lanes)) for symbol, lanes in recall_result.provenance),
            recall_result.counts,
        )
