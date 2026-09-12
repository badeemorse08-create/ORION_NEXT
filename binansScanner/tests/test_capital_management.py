import unittest

from models.capital_management import (
    AllocationCandidate,
    AllocationConfig,
    AllocationRejection,
    CapitalManager,
    CapitalMode,
)


class AccountingStub:
    def __init__(self, equity=50.0, realized=0.0, unrealized=0.0, reserved=0.0, committed=0.0):
        self.equity = equity
        self.realized_pnl = realized
        self.unrealized_pnl = unrealized
        self.reserved_capital = reserved
        self.committed_capital = committed


class TestCapitalManagement(unittest.TestCase):
    def candidate(self, symbol, rank=1, score=100.0, eligible=True, intent="ENTRY"):
        return AllocationCandidate(symbol, rank, score, eligible, intent)

    def test_default_policy_allows_multiple_concurrent_allocations(self):
        manager = CapitalManager(
            AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0)
        )
        results = manager.allocate_ranked([
            (self.candidate("BTCUSDT", 1, 90), 5.0),
            (self.candidate("ETHUSDT", 2, 80), 5.0),
            (self.candidate("SOLUSDT", 3, 70), 5.0),
        ])
        self.assertTrue(all(result.accepted for result in results))
        self.assertEqual(manager.reserved_capital, 30.0)
        self.assertEqual(manager.available_capital, 20.0)

    def test_default_policy_has_no_artificial_single_position_cap(self):
        config = AllocationConfig(starting_capital=50, fixed_allocation=10.0)
        self.assertIsNone(config.max_concurrent_positions)

    def test_fixed_allocation_50_does_not_compound(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, allocation_rate=0.10, max_concurrent_positions=3))
        first = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        self.assertEqual(first.final_order_notional, 5.0)
        manager.on_fill(first.allocation_id)
        manager.on_exit(first.allocation_id)
        manager.record_realized_pnl(0.50)
        second = manager.calculate(self.candidate("ETHUSDT"), 0.0)
        self.assertEqual(second.desired_allocation, 5.0)
        self.assertAlmostEqual(manager.snapshot().total_equity, 50.50)

    def test_compounding_reuses_realized_profit(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.COMPOUNDING, allocation_rate=0.10, max_concurrent_positions=3))
        first = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        self.assertEqual(first.final_order_notional, 5.0)
        manager.on_fill(first.allocation_id)
        manager.on_exit(first.allocation_id)
        manager.record_realized_pnl(1.0)
        second = manager.calculate(self.candidate("ETHUSDT"), 0.0)
        self.assertAlmostEqual(second.final_order_notional, 5.1)

    def test_compounding_loss_reduces_next_allocation(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.COMPOUNDING, allocation_rate=0.10, max_concurrent_positions=3))
        manager.record_realized_pnl(-2.0)
        result = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        self.assertAlmostEqual(result.final_order_notional, 4.8)

    def test_minimum_notional_promotes_small_allocation(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=3.0, max_concurrent_positions=3))
        result = manager.calculate(self.candidate("BTCUSDT"), 5.0)
        self.assertTrue(result.accepted)
        self.assertEqual(result.final_order_notional, 5.0)
        self.assertTrue(result.minimum_adjustment_applied)

    def test_minimum_equal_does_not_change(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=5.0, max_concurrent_positions=3))
        result = manager.calculate(self.candidate("BTCUSDT"), 5.0)
        self.assertEqual(result.final_order_notional, 5.0)
        self.assertFalse(result.minimum_adjustment_applied)

    def test_minimum_below_desired_preserves_desired(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=8.0, max_concurrent_positions=3))
        result = manager.calculate(self.candidate("BTCUSDT"), 5.0)
        self.assertEqual(result.final_order_notional, 8.0)

    def test_multiple_concurrent_allocations_are_allowed_within_configured_limit(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0, max_concurrent_positions=3))
        results = manager.allocate_ranked([
            (self.candidate("BTCUSDT", 1, 90), 5.0),
            (self.candidate("ETHUSDT", 2, 80), 5.0),
            (self.candidate("SOLUSDT", 3, 70), 5.0),
        ])
        self.assertTrue(all(result.accepted for result in results))
        self.assertEqual(manager.reserved_capital, 30.0)
        self.assertEqual(manager.available_capital, 20.0)

    def test_available_capital_protects_reserved_cash(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=25.0, max_concurrent_positions=3))
        first = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        self.assertTrue(first.accepted)
        second = manager.calculate(self.candidate("ETHUSDT"), 0.0)
        self.assertTrue(second.accepted)
        third = manager.calculate(self.candidate("SOLUSDT"), 0.0)
        self.assertEqual(third.rejection_reason, AllocationRejection.INSUFFICIENT_CAPITAL)

    def test_reserved_capital_never_exceeds_available_capital(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, fixed_allocation=15.0))
        results = manager.allocate_ranked([
            (self.candidate("BTCUSDT", 1), 5.0),
            (self.candidate("ETHUSDT", 2), 5.0),
            (self.candidate("SOLUSDT", 3), 5.0),
            (self.candidate("ADAUSDT", 4), 5.0),
        ])
        accepted = [result for result in results if result.accepted]
        self.assertEqual(len(accepted), 3)
        self.assertLessEqual(manager.reserved_capital, 50.0)
        self.assertGreaterEqual(manager.available_capital, 0.0)

    def test_max_concurrent_positions_rejects_at_configured_limit(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=5.0, max_concurrent_positions=2))
        first = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        second = manager.calculate(self.candidate("ETHUSDT"), 0.0)
        manager.on_fill(first.allocation_id)
        manager.on_fill(second.allocation_id)
        result = manager.calculate(self.candidate("SOLUSDT"), 0.0)
        self.assertEqual(result.rejection_reason, AllocationRejection.MAX_CONCURRENT_POSITIONS)

    def test_duplicate_allocation_is_rejected(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=5.0, max_concurrent_positions=3))
        first = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        second = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        self.assertTrue(first.accepted)
        self.assertEqual(second.rejection_reason, AllocationRejection.DUPLICATE_ALLOCATION)

    def test_existing_position_is_not_closed_to_make_room(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, fixed_allocation=5.0, max_concurrent_positions=1))
        manager.set_active_positions(["BTCUSDT"])
        result = manager.calculate(self.candidate("ETHUSDT"), 0.0)
        self.assertEqual(result.rejection_reason, AllocationRejection.MAX_CONCURRENT_POSITIONS)
        self.assertIn("BTCUSDT", manager._positions)

    def test_ineligible_opportunity_is_rejected(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=5.0))
        result = manager.calculate(self.candidate("BTCUSDT", eligible=False), 0.0)
        self.assertEqual(result.rejection_reason, AllocationRejection.INELIGIBLE_OPPORTUNITY)

    def test_cancel_releases_reservation(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0))
        result = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        self.assertEqual(manager.reserved_capital, 10.0)
        manager.on_cancel(result.allocation_id)
        self.assertEqual(manager.reserved_capital, 0.0)
        self.assertEqual(manager.available_capital, 50.0)

    def test_reject_releases_reservation(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0))
        result = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        manager.on_reject(result.allocation_id)
        self.assertEqual(manager.reserved_capital, 0.0)

    def test_expire_releases_reservation(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0))
        result = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        manager.on_expire(result.allocation_id)
        self.assertEqual(manager.reserved_capital, 0.0)

    def test_fill_transitions_reservation_to_committed_capital(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0))
        result = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        manager.on_fill(result.allocation_id)
        self.assertEqual(manager.reserved_capital, 0.0)
        self.assertEqual(manager.committed_capital, 10.0)
        self.assertEqual(manager.available_capital, 40.0)

    def test_exit_releases_committed_capital(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0))
        result = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        manager.on_fill(result.allocation_id)
        manager.on_exit(result.allocation_id)
        self.assertEqual(manager.committed_capital, 0.0)
        self.assertEqual(manager.available_capital, 50.0)

    def test_unrealized_pnl_does_not_increase_trading_capital(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.COMPOUNDING, allocation_rate=0.10))
        manager.record_unrealized_pnl(20.0)
        self.assertEqual(manager.total_equity, 70.0)
        self.assertEqual(manager.trading_capital, 50.0)
        self.assertEqual(manager.desired_allocation(), 5.0)

    def test_accounting_boundary_consumes_external_state(self):
        accounting = AccountingStub(equity=60.0, realized=10.0, unrealized=-1.0, reserved=5.0, committed=10.0)
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.COMPOUNDING, allocation_rate=0.10), accounting=accounting)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot.total_equity, 60.0)
        self.assertEqual(snapshot.realized_pnl, 10.0)
        self.assertEqual(snapshot.unrealized_pnl, -1.0)
        self.assertEqual(snapshot.reserved_capital, 5.0)
        self.assertEqual(snapshot.committed_capital, 10.0)
        self.assertEqual(snapshot.trading_capital, 60.0)
        self.assertEqual(snapshot.available_capital, 45.0)

    def test_same_inputs_are_deterministic(self):
        config = AllocationConfig(starting_capital=50, mode=CapitalMode.COMPOUNDING, allocation_rate=0.10, max_concurrent_positions=3)
        candidates = [(self.candidate("BTCUSDT", 1, 90), 5.0), (self.candidate("ETHUSDT", 2, 80), 5.0)]
        a = CapitalManager(config).allocate_ranked(candidates)
        b = CapitalManager(config).allocate_ranked(candidates)
        self.assertEqual(a, b)

    def test_ranking_is_deterministic(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=10.0, max_concurrent_positions=3))
        results = manager.allocate_ranked([
            (self.candidate("ETHUSDT", 2, 90), 5.0),
            (self.candidate("BTCUSDT", 1, 10), 5.0),
        ])
        self.assertEqual([r.symbol for r in results], ["BTCUSDT", "ETHUSDT"])

    def test_no_allocation_exceeds_available_capital(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.COMPOUNDING, allocation_rate=0.90, max_concurrent_positions=3))
        result = manager.calculate(self.candidate("BTCUSDT"), 100.0)
        self.assertEqual(result.rejection_reason, AllocationRejection.INSUFFICIENT_CAPITAL)

    def test_invalid_configuration_rejected(self):
        with self.assertRaises(ValueError):
            AllocationConfig(starting_capital=0, fixed_allocation=5.0)
        with self.assertRaises(ValueError):
            AllocationConfig(starting_capital=50, allocation_rate=1.2)
        with self.assertRaises(ValueError):
            AllocationConfig(starting_capital=50, max_concurrent_positions=0, fixed_allocation=5.0)

    def test_e2e_fixed_profit_flow(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, allocation_rate=0.10, max_concurrent_positions=2))
        first = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        manager.on_fill(first.allocation_id)
        manager.on_exit(first.allocation_id)
        manager.record_realized_pnl(0.50)
        second = manager.calculate(self.candidate("ETHUSDT"), 0.0)
        self.assertEqual(manager.total_equity, 50.50)
        self.assertEqual(second.final_order_notional, 5.0)

    def test_e2e_compounding_profit_flow(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.COMPOUNDING, allocation_rate=0.10, max_concurrent_positions=2))
        first = manager.calculate(self.candidate("BTCUSDT"), 0.0)
        manager.on_fill(first.allocation_id)
        manager.on_exit(first.allocation_id)
        manager.record_realized_pnl(1.0)
        second = manager.calculate(self.candidate("ETHUSDT"), 0.0)
        self.assertEqual(manager.trading_capital, 51.0)
        self.assertAlmostEqual(second.final_order_notional, 5.1)

    def test_e2e_minimum_notional(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=3.0))
        result = manager.calculate(self.candidate("ADAUSDT"), 5.0)
        self.assertEqual(result.final_order_notional, 5.0)

    def test_e2e_multiple_positions_respect_capital(self):
        manager = CapitalManager(AllocationConfig(starting_capital=50, mode=CapitalMode.FIXED_ALLOCATION, fixed_allocation=15.0, max_concurrent_positions=3))
        results = manager.allocate_ranked([
            (self.candidate("BTCUSDT", 1, 100), 5.0),
            (self.candidate("ETHUSDT", 2, 90), 5.0),
            (self.candidate("SOLUSDT", 3, 80), 5.0),
            (self.candidate("ADAUSDT", 4, 70), 5.0),
        ])
        self.assertEqual(sum(r.accepted for r in results), 3)
        self.assertLessEqual(manager.reserved_capital, 50.0)


if __name__ == "__main__":
    unittest.main()
