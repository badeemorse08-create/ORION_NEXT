"""Deterministic scalping opportunity classification, entry readiness, and replay evaluation."""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import mean, pstdev
from typing import Iterable, Mapping, Protocol, Sequence

from models.capital_management import CapitalManager
from models.opportunity import OpportunityCandidate, OpportunityCandidateSet
from models.scalping_opportunity import (
    ABComparison,
    Candle,
    DecisionTrace,
    EntryState,
    OpportunityClass,
    PerformanceMetrics,
    RejectionReason,
    ReplayEvaluation,
    ReplayEvent,
    RiskReward,
    ScalpingCandidateSet,
    SupertrendABResult,
    TimeframeEvidence,
    enrich_candidate,
)


class TimeframeCandleSource(Protocol):
    def candles(self, symbol: str, timeframe: str, limit: int) -> Sequence[Candle]: ...


@dataclass(frozen=True, slots=True)
class ScalpingConfig:
    min_candles: int = 32
    entry_rr_min: float = 1.5
    a_plus_rr: float = 2.0
    a_plus_score: float = 80.0
    a_score: float = 70.0
    b_score: float = 65.0
    a_plus_readiness: float = 0.75
    a_readiness: float = 0.60
    breakout_volume_threshold: float = 1.25
    breakout_range_threshold: float = 1.20
    hysteresis_score_delta: float = 1.0
    broad_pool_top_n: int = 50
    active_top_n: int = 5
    directional_long_min: float = 0.15
    supertrend_weight: float = 0.05

    def __post_init__(self) -> None:
        if self.min_candles < 24:
            raise ValueError("min_candles must support all feature windows")
        if self.entry_rr_min <= 0 or self.a_plus_rr < self.entry_rr_min:
            raise ValueError("invalid risk/reward thresholds")
        if not self.a_plus_score > self.a_score > self.b_score:
            raise ValueError("entry score thresholds must be ordered")
        if not 0 <= self.supertrend_weight <= 0.20:
            raise ValueError("supertrend_weight must be between 0 and 0.20")
        if self.active_top_n <= 0:
            raise ValueError("active_top_n must be positive")
        if self.broad_pool_top_n <= self.active_top_n:
            raise ValueError("broad_pool_top_n must be greater than active_top_n")
        if not 0 < self.directional_long_min <= 1.0:
            raise ValueError("directional_long_min must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class ScalpingFeatures:
    evidence: tuple[TimeframeEvidence, ...]
    directional_evidence: float
    opportunity_class: OpportunityClass
    opportunity_score: float
    entry_timing: float
    risk_reward: RiskReward | None
    supertrend_enabled: bool


class ScalpingEvidenceEngine:
    """Pure multi-timeframe feature engine."""

    def __init__(self, config: ScalpingConfig | None = None) -> None:
        self.config = config or ScalpingConfig()

    @staticmethod
    def _ema(values: Sequence[float], period: int) -> float:
        if len(values) < period:
            raise ValueError("insufficient EMA history")
        alpha = 2.0 / (period + 1.0)
        value = mean(values[:period])
        for current in values[period:]:
            value = alpha * current + (1.0 - alpha) * value
        return value

    @staticmethod
    def _atr(candles: Sequence[Candle], period: int = 14) -> float:
        if len(candles) < period + 1:
            raise ValueError("insufficient ATR history")
        trs: list[float] = []
        previous = candles[0].close
        for candle in candles[1:]:
            trs.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
            previous = candle.close
        return mean(trs[-period:])

    @staticmethod
    def _supertrend_direction(candles: Sequence[Candle], period: int = 10, multiplier: float = 3.0) -> float:
        atr = ScalpingEvidenceEngine._atr(candles, period)
        mid = (candles[-1].high + candles[-1].low) / 2.0
        upper = mid + multiplier * atr
        lower = mid - multiplier * atr
        if candles[-1].close > upper:
            return 1.0
        if candles[-1].close < lower:
            return -1.0
        prev_mid = (candles[-2].high + candles[-2].low) / 2.0
        return 1.0 if candles[-2].close > prev_mid else -1.0 if candles[-2].close < prev_mid else 0.0

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(value, high))

    def _one_timeframe(self, timeframe: str, candles: Sequence[Candle]) -> TimeframeEvidence:
        if len(candles) < self.config.min_candles:
            raise ValueError(f"insufficient candles for {timeframe}")
        closes = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        ranges = [c.high - c.low for c in candles]
        atr = self._atr(candles)
        ema9 = self._ema(closes[-21:], 9)
        ema21 = self._ema(closes[-21:], 21)
        trend_direction = self._clamp(((ema9 / ema21) - 1.0) / 0.03 if ema21 else 0.0, -1.0, 1.0)
        expected = 1 if trend_direction >= 0 else -1
        aligned = [1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0 for i in range(max(1, len(closes) - 8), len(closes))]
        persistence = sum(1 for v in aligned if v == expected) / len(aligned)
        trend_score = self._clamp(abs(trend_direction) * persistence, 0.0, 1.0)
        roc3 = closes[-1] / closes[-4] - 1.0
        roc8 = closes[-1] / closes[-9] - 1.0
        momentum_direction = self._clamp(roc3 / 0.03, -1.0, 1.0)
        momentum_score = abs(momentum_direction)
        acceleration = self._clamp((roc3 - roc8 * 3.0 / 8.0) / 0.02, -1.0, 1.0)
        volume_base = mean(volumes[-11:-1])
        range_base = mean(ranges[-11:-1])
        volume_expansion = self._clamp(volumes[-1] / volume_base, 0.0, 2.0) / 2.0 if volume_base > 0 else 0.0
        range_expansion = self._clamp(ranges[-1] / range_base, 0.0, 2.0) / 2.0 if range_base > 0 else 0.0
        recent_high = max(c.high for c in candles[-8:])
        recent_low = min(c.low for c in candles[-8:])
        structure_score = self._clamp((closes[-1] - recent_low) / (recent_high - recent_low), 0.0, 1.0) if recent_high > recent_low else 0.5
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - 21), len(closes)) if closes[i] > 0 and closes[i - 1] > 0]
        volatility = pstdev(returns) if len(returns) > 1 else 0.0
        regime_score = self._clamp(1.0 - abs(volatility - 0.01) / 0.03, 0.0, 1.0)
        return TimeframeEvidence(
            timeframe,
            regime_score,
            trend_score,
            trend_direction,
            momentum_score,
            momentum_direction,
            abs(acceleration),
            volume_expansion,
            range_expansion,
            structure_score,
            self._supertrend_direction(candles),
            atr,
        )

    def _classify(self, evidence: Mapping[str, TimeframeEvidence]) -> OpportunityClass:
        e1d, e4h, e1h, e15 = evidence["1d"], evidence["4h"], evidence["1h"], evidence["15m"]
        breakout = e15.volume_expansion >= 0.50 and e15.range_expansion >= 0.50 and e1h.acceleration_score >= 0.40
        pullback = e1h.trend_score >= 0.45 and e15.momentum_score >= 0.30 and e15.structure_score >= 0.45 and e15.acceleration_score >= 0.25
        trend = e4h.trend_score >= 0.45 and e1h.trend_score >= 0.45 and e1h.momentum_score >= 0.35
        if breakout:
            return OpportunityClass.BREAKOUT_ACCELERATION
        if pullback:
            return OpportunityClass.PULLBACK_CONTINUATION
        if trend or (e1d.regime_score >= 0.40 and e1h.trend_score >= 0.35):
            return OpportunityClass.TREND_CONTINUATION
        return OpportunityClass.UNCLASSIFIED

    @staticmethod
    def _risk_reward(evidence: Mapping[str, TimeframeEvidence], candles_15m: Sequence[Candle], directional: float) -> RiskReward | None:
        if directional == 0:
            return None
        entry = candles_15m[-1].close
        risk = max(evidence["15m"].atr * 1.2, entry * 0.002)
        stop = entry - risk if directional > 0 else entry + risk
        target = entry + risk * 2.0 if directional > 0 else entry - risk * 2.0
        reward = abs(target - entry)
        return RiskReward(entry, stop, target, risk, reward, reward / risk if risk else 0.0, risk > 0 and reward > 0)

    def compute(self, candle_map: Mapping[str, Sequence[Candle]], *, use_supertrend: bool = False) -> ScalpingFeatures:
        required = ("1d", "4h", "1h", "15m")
        if any(tf not in candle_map for tf in required):
            raise ValueError("all four scalping timeframes are required")
        evidence = {tf: self._one_timeframe(tf, candle_map[tf]) for tf in required}
        directional = self._clamp(
            0.25 * evidence["4h"].trend_direction
            + 0.30 * evidence["1h"].trend_direction
            + 0.30 * evidence["1h"].momentum_direction
            + 0.15 * evidence["15m"].momentum_direction,
            -1.0,
            1.0,
        )
        cls = self._classify(evidence)
        score = (
            0.10 * evidence["1d"].regime_score
            + 0.20 * evidence["4h"].trend_score
            + 0.18 * evidence["1h"].trend_score
            + 0.15 * evidence["1h"].momentum_score
            + 0.12 * evidence["1h"].acceleration_score
            + 0.10 * evidence["15m"].volume_expansion
            + 0.08 * evidence["15m"].range_expansion
            + 0.07 * evidence["1h"].structure_score
        )
        if use_supertrend:
            score += self.config.supertrend_weight * max(0.0, directional * evidence["15m"].supertrend_evidence)
        score = round(self._clamp(score, 0.0, 1.0) * 100.0, 8)
        entry_timing = self._clamp(
            0.35 * evidence["15m"].momentum_score
            + 0.25 * evidence["15m"].acceleration_score
            + 0.20 * evidence["15m"].volume_expansion
            + 0.20 * evidence["15m"].range_expansion,
            0.0,
            1.0,
        )
        return ScalpingFeatures(
            tuple(evidence[tf] for tf in required),
            round(directional, 8),
            cls,
            score,
            entry_timing,
            self._risk_reward(evidence, candle_map["15m"], directional),
            use_supertrend,
        )


class ScalpingDecisionEngine:
    def __init__(self, config: ScalpingConfig | None = None) -> None:
        self.config = config or ScalpingConfig()
        self.features = ScalpingEvidenceEngine(self.config)

    def decide(
        self,
        candidate: OpportunityCandidate,
        candle_map: Mapping[str, Sequence[Candle]],
        *,
        capital_manager: CapitalManager | None = None,
        pause: bool = False,
        active_symbols: Iterable[str] = (),
        use_supertrend: bool = False,
    ) -> OpportunityCandidate:
        eligible = not candidate.eligibility_reasons
        try:
            features = self.features.compute(candle_map, use_supertrend=use_supertrend)
        except (ValueError, ZeroDivisionError, KeyError):
            trace = DecisionTrace(
                False,
                False,
                (),
                OpportunityClass.UNCLASSIFIED,
                0.0,
                0.0,
                EntryState.D,
                False,
                (RejectionReason.MARKET_DATA_FAILURE,),
                ("market_data_failure",),
            )
            return enrich_candidate(
                candidate,
                opportunity_class=OpportunityClass.UNCLASSIFIED,
                entry_state=EntryState.D,
                entry_readiness=0.0,
                risk_reward=None,
                timeframe_evidence=(),
                decision_trace=trace,
            )
        reasons: list[str] = []
        rejection: list[RejectionReason] = []
        allowed = True
        if not eligible:
            allowed = False
            rejection.append(RejectionReason.STRATEGY)
            reasons.append("ineligible_opportunity")
        if pause:
            allowed = False
            rejection.append(RejectionReason.PAUSE)
            reasons.append("trading_paused")
        if candidate.symbol in set(active_symbols):
            allowed = False
            rejection.append(RejectionReason.DUPLICATE_POSITION)
            reasons.append("duplicate_position")
        if capital_manager is not None and capital_manager.desired_allocation() > capital_manager.available_capital + 1e-9:
            allowed = False
            rejection.append(RejectionReason.CAPITAL)
            reasons.append("insufficient_capital")

        # D1 owns long-entry approval only. Direction is evaluated explicitly before
        # any actionable state can be produced; it is never silently overridden.
        if features.directional_evidence < -self.config.directional_long_min:
            allowed = False
            rejection.append(RejectionReason.DIRECTIONAL_CONFLICT)
            reasons.append("directional_conflict_long_entry_blocked")
        elif abs(features.directional_evidence) < self.config.directional_long_min:
            allowed = False
            rejection.append(RejectionReason.DIRECTIONAL_INSUFFICIENT)
            reasons.append("directional_evidence_insufficient_for_long_entry")

        rr_ok = features.risk_reward is not None and features.risk_reward.valid and features.risk_reward.ratio >= self.config.entry_rr_min
        if not rr_ok:
            allowed = False
            rejection.append(RejectionReason.RISK)
            reasons.append("risk_reward_below_threshold")

        actionable_direction = (
            RejectionReason.DIRECTIONAL_CONFLICT not in rejection
            and RejectionReason.DIRECTIONAL_INSUFFICIENT not in rejection
        )
        if (
            features.opportunity_score >= self.config.a_plus_score
            and features.entry_timing >= self.config.a_plus_readiness
            and features.risk_reward
            and features.risk_reward.ratio >= self.config.a_plus_rr
            and allowed
            and actionable_direction
        ):
            state = EntryState.A_PLUS
        elif (
            features.opportunity_score >= self.config.a_score
            and features.entry_timing >= self.config.a_readiness
            and rr_ok
            and allowed
            and actionable_direction
        ):
            state = EntryState.A
        elif (
            features.opportunity_score >= self.config.b_score
            and RejectionReason.CAPITAL not in rejection
            and RejectionReason.MARKET_DATA_FAILURE not in rejection
            and RejectionReason.DIRECTIONAL_CONFLICT not in rejection
            and RejectionReason.DIRECTIONAL_INSUFFICIENT not in rejection
        ):
            state = EntryState.B
            allowed = False
            reasons.append("opportunity_good_entry_timing_not_ready")
        elif RejectionReason.DIRECTIONAL_CONFLICT in rejection or RejectionReason.DIRECTIONAL_INSUFFICIENT in rejection:
            state = EntryState.C
            allowed = False
        elif rejection and not allowed:
            state = EntryState.D if RejectionReason.RISK in rejection or RejectionReason.CAPITAL in rejection else EntryState.C
        else:
            state = EntryState.C

        trace = DecisionTrace(
            True,
            eligible,
            (
                "regime",
                "trend",
                "momentum",
                "acceleration",
                "volume_expansion",
                "range_expansion",
                "structure",
                "directional_evidence",
                "supertrend_evidence",
            ),
            features.opportunity_class,
            features.opportunity_score,
            features.directional_evidence,
            state,
            allowed,
            tuple(dict.fromkeys(rejection)),
            tuple(dict.fromkeys(reasons + [features.opportunity_class.value.lower()])),
        )
        return enrich_candidate(
            candidate,
            opportunity_class=features.opportunity_class,
            entry_state=state,
            entry_readiness=features.entry_timing,
            risk_reward=features.risk_reward,
            timeframe_evidence=features.evidence,
            decision_trace=trace,
        )


class ScalpingCandidatePoolManager:
    """Maintains a broad evaluated pool and a smaller hysteresis-stable active set."""

    def __init__(self, config: ScalpingConfig | None = None) -> None:
        self.config = config or ScalpingConfig()
        self._previous_active: dict[str, float] = {}

    def select(self, broad_pool: OpportunityCandidateSet, enriched: Sequence[OpportunityCandidate]) -> ScalpingCandidateSet:
        if broad_pool.top_n <= self.config.active_top_n:
            raise ValueError("broad pool must be larger than active set")
        ordered = sorted(enriched, key=lambda item: (-item.opportunity_score, item.symbol))
        selected = ordered[: self.config.active_top_n]
        by_symbol = {item.symbol: item for item in ordered}
        for symbol, previous_score in self._previous_active.items():
            incumbent = by_symbol.get(symbol)
            if incumbent and incumbent not in selected and incumbent.opportunity_score >= previous_score - self.config.hysteresis_score_delta and selected:
                worst = min(selected, key=lambda item: (item.opportunity_score, item.symbol))
                if incumbent.opportunity_score >= worst.opportunity_score - self.config.hysteresis_score_delta:
                    selected = [item for item in selected if item.symbol != worst.symbol] + [incumbent]
        selected.sort(key=lambda item: (-item.opportunity_score, item.symbol))
        active = tuple(
            OpportunityCandidate(
                item.symbol,
                item.opportunity_score,
                i,
                item.metrics,
                item.eligibility_reasons,
                item.score_components,
                item.directional_evidence,
                item.opportunity_class,
                item.entry_state,
                item.entry_readiness,
                item.risk_reward,
                item.timeframe_evidence,
                item.decision_trace,
            )
            for i, item in enumerate(selected[: self.config.active_top_n], 1)
        )
        self._previous_active = {item.symbol: item.opportunity_score for item in active}
        broad = OpportunityCandidateSet(
            tuple(
                OpportunityCandidate(
                    item.symbol,
                    item.opportunity_score,
                    i,
                    item.metrics,
                    item.eligibility_reasons,
                    item.score_components,
                    item.directional_evidence,
                    item.opportunity_class,
                    item.entry_state,
                    item.entry_readiness,
                    item.risk_reward,
                    item.timeframe_evidence,
                    item.decision_trace,
                )
                for i, item in enumerate(ordered, 1)
            ),
            len(ordered),
            broad_pool.snapshot_timestamp,
        )
        return ScalpingCandidateSet(broad, OpportunityCandidateSet(active, len(active), broad_pool.snapshot_timestamp), True)


class ScalpingReplayEvaluator:
    """Deterministic replay metrics with explicit event semantics and time basis."""

    @staticmethod
    def _coerce_evaluation(results: ReplayEvaluation | Sequence[ReplayEvent], evaluation_days: float | None) -> ReplayEvaluation:
        if isinstance(results, ReplayEvaluation):
            if evaluation_days is not None:
                return ReplayEvaluation(results.events, evaluation_days)
            return results
        events = tuple(results)
        if evaluation_days is None:
            raise ValueError("evaluation_days must be explicitly supplied when passing events")
        return ReplayEvaluation(events, evaluation_days)

    @staticmethod
    def metrics(results: ReplayEvaluation | Sequence[ReplayEvent], evaluation_days: float | None = None) -> PerformanceMetrics:
        evaluation = ScalpingReplayEvaluator._coerce_evaluation(results, evaluation_days)
        events = evaluation.events
        if not events:
            return PerformanceMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        captured = [event for event in events if event.opportunity_captured]
        accepted = [event for event in captured if event.entry_accepted]
        returns = [event.return_pct for event in accepted]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss:
            profit_factor = gross_profit / gross_loss
        elif gross_profit:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        equity = peak = drawdown = 0.0
        for value in returns:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)

        profitable = [event for event in events if event.profitable_opportunity]
        missed_profitable = [event for event in profitable if not event.entry_accepted]
        entry_acceptance_rate = len(accepted) / len(captured) if captured else 0.0
        false_negative_rate = len(missed_profitable) / len(profitable) if profitable else 0.0

        return PerformanceMetrics(
            opportunity_capture_rate=len(captured) / len(events),
            entry_acceptance_rate=entry_acceptance_rate,
            trades_per_day=len(accepted) / evaluation.evaluation_days,
            win_rate=len(wins) / len(returns) if returns else 0.0,
            expectancy=mean(returns) if returns else 0.0,
            profit_factor=profit_factor,
            maximum_drawdown=drawdown,
            capital_utilization=mean(event.capital_utilization_pct for event in events),
            average_hold_time=mean(event.hold_time_hours for event in accepted) if accepted else 0.0,
            fees_slippage_impact=mean(event.fees_slippage_pct for event in accepted) if accepted else 0.0,
            false_negative_rate=false_negative_rate,
        )

    @staticmethod
    def _delta(new: float, old: float) -> float:
        if math.isinf(new) and math.isinf(old):
            return 0.0 if new == old else (float("inf") if new > old else float("-inf"))
        return new - old

    @staticmethod
    def compare(
        baseline_results: ReplayEvaluation | Sequence[ReplayEvent],
        improved_results: ReplayEvaluation | Sequence[ReplayEvent],
        *,
        baseline_evaluation_days: float | None = None,
        improved_evaluation_days: float | None = None,
    ) -> ABComparison:
        baseline = ScalpingReplayEvaluator.metrics(baseline_results, baseline_evaluation_days)
        improved = ScalpingReplayEvaluator.metrics(improved_results, improved_evaluation_days)
        return ABComparison(
            baseline,
            improved,
            improved.opportunity_capture_rate - baseline.opportunity_capture_rate,
            improved.entry_acceptance_rate - baseline.entry_acceptance_rate,
            improved.expectancy - baseline.expectancy,
            improved.maximum_drawdown - baseline.maximum_drawdown,
            ScalpingReplayEvaluator._delta(improved.profit_factor, baseline.profit_factor),
            improved.false_negative_rate - baseline.false_negative_rate,
        )

    @staticmethod
    def compare_supertrend(
        baseline_results: ReplayEvaluation | Sequence[ReplayEvent],
        supertrend_results: ReplayEvaluation | Sequence[ReplayEvent],
        *,
        baseline_evaluation_days: float | None = None,
        supertrend_evaluation_days: float | None = None,
    ) -> SupertrendABResult:
        baseline = ScalpingReplayEvaluator.metrics(baseline_results, baseline_evaluation_days)
        with_supertrend = ScalpingReplayEvaluator.metrics(supertrend_results, supertrend_evaluation_days)
        return SupertrendABResult(
            baseline,
            with_supertrend,
            with_supertrend.opportunity_capture_rate - baseline.opportunity_capture_rate,
            with_supertrend.expectancy - baseline.expectancy,
            ScalpingReplayEvaluator._delta(with_supertrend.profit_factor, baseline.profit_factor),
            with_supertrend.maximum_drawdown - baseline.maximum_drawdown,
            with_supertrend.false_negative_rate - baseline.false_negative_rate,
        )
