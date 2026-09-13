from datetime import datetime, timedelta, timezone
import ast
from pathlib import Path
import unittest

from binansScanner.models.signal_snapshot import (
    MaterialChangePolicy,
    MaterialChangeReason,
    SignalIdentity,
    SignalSnapshot,
    SignalValidity,
    build_next_snapshot,
    evaluate_snapshot,
)


UTC = timezone.utc
T0 = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


class SignalSnapshotContractTests(unittest.TestCase):
    def identity(self) -> SignalIdentity:
        return SignalIdentity(symbol="BTCUSDT", strategy="momentum", intent="entry")

    def snapshot(self, *, version=1, price=100.0, generated_at=T0, valid_until=None, confidence=0.8, quality=0.8, direction="BUY", decision="ENTRY_NOW", context="ctx-a") -> SignalSnapshot:
        return SignalSnapshot(
            identity=self.identity(),
            version=version,
            direction=direction,
            decision=decision,
            confidence=confidence,
            quality=quality,
            entry_plan={"entry_price": price, "mode": "limit"},
            generated_at=generated_at,
            valid_until=valid_until or generated_at + timedelta(hours=1),
            market_context_fingerprint=context,
        )

    def test_identity_is_unique_and_deterministic(self):
        a = self.identity()
        b = SignalIdentity(symbol="BTCUSDT", strategy="momentum", intent="entry")
        c = SignalIdentity(symbol="BTCUSDT", strategy="mean-reversion", intent="entry")
        self.assertEqual(a.identity_key, b.identity_key)
        self.assertNotEqual(a.identity_key, c.identity_key)
        self.assertEqual(len(a.identity_key), 64)

    def test_first_snapshot_is_version_one(self):
        relation = build_next_snapshot(
            previous=None, identity=self.identity(), direction="BUY", decision="ENTRY_NOW",
            confidence=0.8, quality=0.8, entry_plan={"entry_price": 100.0},
            generated_at=T0, valid_until=T0 + timedelta(hours=1),
            policy=MaterialChangePolicy(entry_price_change_pct=0.10),
        )
        self.assertEqual(relation.current.version, 1)
        self.assertIsNone(relation.previous)
        self.assertFalse(relation.material_change)

    def test_version_is_monotonic_and_previous_relation_is_explicit(self):
        v1 = self.snapshot()
        relation = build_next_snapshot(
            previous=v1, identity=v1.identity, direction="BUY", decision="ENTRY_NOW",
            confidence=0.81, quality=0.81, entry_plan={"entry_price": 101.0},
            generated_at=T0 + timedelta(minutes=1), valid_until=T0 + timedelta(hours=2),
            policy=MaterialChangePolicy(entry_price_change_pct=0.10), market_context_fingerprint="ctx-a",
        )
        self.assertEqual(relation.current.version, 2)
        self.assertEqual(relation.previous_version, 1)
        self.assertEqual(relation.current_version, 2)
        self.assertFalse(relation.material_change)

    def test_mandatory_buy_100_to_buy_118_is_stale(self):
        v1 = self.snapshot(price=100.0)
        policy = MaterialChangePolicy(entry_price_change_pct=0.10)
        relation = build_next_snapshot(
            previous=v1, identity=v1.identity, direction="BUY", decision="ENTRY_NOW",
            confidence=0.8, quality=0.8, entry_plan={"entry_price": 118.0},
            generated_at=T0 + timedelta(minutes=5), valid_until=T0 + timedelta(hours=2),
            policy=policy, market_context_fingerprint="ctx-a",
        )
        self.assertTrue(relation.material_change)
        self.assertIn(MaterialChangeReason.ENTRY_PRICE_CHANGED, relation.reasons)
        self.assertEqual(
            evaluate_snapshot(relation.current, previous=v1, at=T0 + timedelta(minutes=5), policy=policy),
            SignalValidity.STALE,
        )

    def test_direction_and_decision_changes_are_material(self):
        previous = self.snapshot()
        relation = build_next_snapshot(
            previous=previous, identity=previous.identity, direction="SELL", decision="SKIP",
            confidence=0.8, quality=0.8, entry_plan={"entry_price": 100.0},
            generated_at=T0 + timedelta(minutes=1), valid_until=T0 + timedelta(hours=2),
            policy=MaterialChangePolicy(entry_price_change_pct=0.10), market_context_fingerprint="ctx-a",
        )
        self.assertIn(MaterialChangeReason.DIRECTION_CHANGED, relation.reasons)
        self.assertIn(MaterialChangeReason.DECISION_CHANGED, relation.reasons)

    def test_market_context_and_threshold_crossing_are_material(self):
        previous = self.snapshot(confidence=0.40, quality=0.40, context="ctx-a")
        relation = build_next_snapshot(
            previous=previous, identity=previous.identity, direction="BUY", decision="ENTRY_NOW",
            confidence=0.60, quality=0.60, entry_plan={"entry_price": 100.0},
            generated_at=T0 + timedelta(minutes=1), valid_until=T0 + timedelta(hours=2),
            policy=MaterialChangePolicy(entry_price_change_pct=0.10, confidence_threshold=0.50, quality_threshold=0.50),
            market_context_fingerprint="ctx-b",
        )
        self.assertIn(MaterialChangeReason.MARKET_CONTEXT_CHANGED, relation.reasons)
        self.assertIn(MaterialChangeReason.CONFIDENCE_THRESHOLD_CROSSED, relation.reasons)
        self.assertIn(MaterialChangeReason.QUALITY_THRESHOLD_CROSSED, relation.reasons)

    def test_expiry_is_explicit_and_not_stale_by_itself(self):
        current = self.snapshot(valid_until=T0 + timedelta(minutes=10))
        self.assertEqual(evaluate_snapshot(current, at=T0 + timedelta(minutes=9)), SignalValidity.ACTIVE)
        self.assertEqual(evaluate_snapshot(current, at=T0 + timedelta(minutes=10)), SignalValidity.EXPIRED)

    def test_previous_expiry_at_new_generation_is_material(self):
        previous = self.snapshot(valid_until=T0 + timedelta(minutes=5))
        relation = build_next_snapshot(
            previous=previous, identity=previous.identity, direction="BUY", decision="ENTRY_NOW",
            confidence=0.8, quality=0.8, entry_plan={"entry_price": 100.0},
            generated_at=T0 + timedelta(minutes=6), valid_until=T0 + timedelta(hours=2),
            policy=MaterialChangePolicy(entry_price_change_pct=0.10), market_context_fingerprint="ctx-a",
        )
        self.assertIn(MaterialChangeReason.VALIDITY_EXPIRED, relation.reasons)
        self.assertTrue(relation.material_change)

    def test_no_fabricated_entry_price_fallback(self):
        previous = self.snapshot()
        relation = build_next_snapshot(
            previous=previous, identity=previous.identity, direction="BUY", decision="ENTRY_NOW",
            confidence=0.8, quality=0.8, entry_plan={"mode": "market"},
            generated_at=T0 + timedelta(minutes=1), valid_until=T0 + timedelta(hours=2),
            policy=MaterialChangePolicy(entry_price_change_pct=0.10), market_context_fingerprint="ctx-a",
        )
        self.assertNotIn(MaterialChangeReason.ENTRY_PRICE_CHANGED, relation.reasons)

    def test_snapshot_is_immutable_and_timezones_are_normalized(self):
        value = self.snapshot()
        self.assertEqual(value.generated_at.tzinfo, UTC)
        with self.assertRaises((AttributeError, TypeError)):
            value.direction = "SELL"

    def test_deterministic_canonical_payload(self):
        a = self.snapshot(price=100.0)
        b = self.snapshot(price=100.0)
        self.assertEqual(a.canonical_payload(), b.canonical_payload())

    def test_identity_mismatch_is_rejected(self):
        previous = self.snapshot()
        other = SignalIdentity(symbol="ETHUSDT", strategy="momentum", intent="entry")
        with self.assertRaisesRegex(ValueError, "different signal identity"):
            build_next_snapshot(
                previous=previous, identity=other, direction="BUY", decision="ENTRY_NOW",
                confidence=0.8, entry_plan={"entry_price": 100.0},
                generated_at=T0 + timedelta(minutes=1), valid_until=T0 + timedelta(hours=2),
                policy=MaterialChangePolicy(entry_price_change_pct=0.10),
            )

    def test_signal_snapshot_has_no_live_execution_dependency(self):
        module_path = Path(__file__).resolve().parents[1] / "models" / "signal_snapshot.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        forbidden = ("execution", "order", "position", "binance")
        self.assertFalse(any(any(token in name.lower() for token in forbidden) for name in imported), imported)


if __name__ == "__main__":
    unittest.main()
