import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

from models.paper_capital import (
    FeeModel,
    LedgerEventType,
    LedgerSide,
    PaperLedger,
    SlippageModel,
    VIRTUAL_STARTING_EQUITY,
    VirtualWallet,
)


class TestPaperCapitalContracts(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)

    def test_virtual_wallet_defaults_to_200_and_reserves_cash(self):
        wallet = VirtualWallet()
        self.assertEqual(wallet.starting_equity, VIRTUAL_STARTING_EQUITY)
        self.assertEqual(wallet.cash, 200.0)
        self.assertEqual(wallet.available_cash, 200.0)
        reserved = PaperLedger().reserve_cash(self.t0, 50.0).replay().wallet
        self.assertEqual(reserved.reserved_cash, 50.0)
        self.assertEqual(reserved.available_cash, 150.0)
        released = PaperLedger().reserve_cash(self.t0, 50.0).release_cash(self.t0 + timedelta(minutes=1), 50.0).replay().wallet
        self.assertEqual(released.reserved_cash, 0.0)

    def test_models_and_ledger_are_immutable(self):
        wallet = VirtualWallet()
        with self.assertRaises(FrozenInstanceError):
            wallet.cash = 1.0  # type: ignore[misc]
        ledger = PaperLedger().record_order(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        with self.assertRaises(FrozenInstanceError):
            ledger.events = ()  # type: ignore[misc]
        self.assertEqual(len(ledger.events), 1)

    def test_timezone_aware_timestamps_are_normalized_to_utc(self):
        plus3 = datetime(2026, 8, 21, 13, 0, tzinfo=timezone(timedelta(hours=3)))
        ledger = PaperLedger().record_order(plus3, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        self.assertEqual(ledger.events[0].timestamp, self.t0)

    def test_fee_model_is_deterministic(self):
        model = FeeModel(rate=0.001, minimum=0.5)
        self.assertEqual(model.fee(100.0), 0.5)
        self.assertAlmostEqual(model.fee(1000.0), 1.0)

    def test_slippage_model_is_deterministic_and_explicit(self):
        model = SlippageModel(rate=0.01)
        self.assertAlmostEqual(model.execution_price(LedgerSide.BUY, 100.0), 101.0)
        self.assertAlmostEqual(model.execution_price(LedgerSide.SELL, 100.0), 99.0)
        self.assertAlmostEqual(model.amount(LedgerSide.BUY, 100.0, 2.0), 2.0)

    def test_buy_fill_creates_position_and_accounts_fee_and_slippage(self):
        ledger = PaperLedger(fee_model=FeeModel(rate=0.01), slippage_model=SlippageModel(rate=0.01))
        ledger = ledger.record_order(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.record_fill(self.t0 + timedelta(seconds=1), "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.mark(self.t0 + timedelta(minutes=1), "BTCUSDT", 105.0)
        state = ledger.replay()
        self.assertEqual(state.position("BTCUSDT").quantity, 1.0)
        self.assertAlmostEqual(state.position("BTCUSDT").average_price, 101.0)
        self.assertAlmostEqual(state.wallet.cash, 97.99)
        self.assertAlmostEqual(state.open_position_value, 105.0)
        self.assertAlmostEqual(state.unrealized_pnl, 4.0)
        self.assertAlmostEqual(state.cumulative_fees, 1.01)
        self.assertAlmostEqual(state.cumulative_slippage, 1.0)
        event_types = {event.event_type for event in ledger.events}
        self.assertIn(LedgerEventType.FEE, event_types)
        self.assertIn(LedgerEventType.SLIPPAGE, event_types)

    def test_sell_fill_realizes_pnl_and_closes_position(self):
        ledger = PaperLedger(fee_model=FeeModel(rate=0.01), slippage_model=SlippageModel(rate=0.0))
        ledger = ledger.record_fill(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.record_fill(self.t0 + timedelta(hours=1), "BTCUSDT", LedgerSide.SELL, 1.0, 110.0)
        state = ledger.replay()
        self.assertEqual(state.position("BTCUSDT").quantity, 0.0)
        self.assertAlmostEqual(state.realized_pnl, 10.0)
        self.assertAlmostEqual(state.cumulative_fees, 2.1)
        self.assertAlmostEqual(state.wallet.cash, 207.9)
        self.assertAlmostEqual(state.equity, 207.9)
        self.assertTrue(state.accounting_identity_holds())
        self.assertIn(LedgerEventType.PNL, {event.event_type for event in ledger.events})

    def test_required_trade_event_types_are_written(self):
        ledger = PaperLedger().record_order(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.record_fill(self.t0 + timedelta(seconds=1), "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.record_exit(self.t0 + timedelta(minutes=1), "BTCUSDT", 1.0, 110.0)
        ledger = ledger.mark(self.t0 + timedelta(minutes=2), "BTCUSDT", 110.0)
        ledger = ledger.snapshot(self.t0 + timedelta(minutes=3))
        event_types = {event.event_type for event in ledger.events}
        self.assertTrue({LedgerEventType.ORDER, LedgerEventType.FILL, LedgerEventType.POSITION, LedgerEventType.EXIT, LedgerEventType.SNAPSHOT}.issubset(event_types))

    def test_accounting_identity_and_reproducibility_hold(self):
        ledger = PaperLedger(fee_model=FeeModel(rate=0.001), slippage_model=SlippageModel(rate=0.005))
        ledger = ledger.record_fill(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.mark(self.t0 + timedelta(minutes=5), "BTCUSDT", 102.0)
        state = ledger.replay()
        self.assertTrue(state.accounting_identity_holds())
        self.assertAlmostEqual(state.equity, VIRTUAL_STARTING_EQUITY - state.accounting_adjustments, places=9)
        self.assertAlmostEqual(state.cumulative_slippage, 0.5)
        replay_again = ledger.replay()
        self.assertEqual(state, replay_again)

    def test_drawdown_tracks_peak_and_maximum(self):
        ledger = PaperLedger()
        ledger = ledger.record_fill(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.mark(self.t0 + timedelta(minutes=1), "BTCUSDT", 120.0)
        peak = ledger.replay()
        self.assertAlmostEqual(peak.peak_equity, 220.0)
        ledger = ledger.mark(self.t0 + timedelta(minutes=2), "BTCUSDT", 90.0)
        state = ledger.replay()
        self.assertAlmostEqual(state.current_drawdown, 30.0)
        self.assertAlmostEqual(state.maximum_drawdown, 30.0)

    def test_equity_curve_is_timestamped_and_replayable(self):
        ledger = PaperLedger().record_fill(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, 100.0)
        ledger = ledger.mark(self.t0 + timedelta(minutes=1), "BTCUSDT", 105.0)
        ledger = ledger.snapshot(self.t0 + timedelta(minutes=1))
        ledger = ledger.mark(self.t0 + timedelta(minutes=2), "BTCUSDT", 95.0)
        ledger = ledger.snapshot(self.t0 + timedelta(minutes=2))
        curve = ledger.equity_curve()
        self.assertEqual(len(curve), 2)
        self.assertEqual(curve[0].timestamp, self.t0 + timedelta(minutes=1))
        self.assertEqual(curve[1].timestamp, self.t0 + timedelta(minutes=2))
        self.assertGreater(curve[0].equity, curve[1].equity)

    def test_invalid_negative_and_non_finite_values_are_rejected(self):
        with self.assertRaises(ValueError):
            FeeModel(rate=-0.1)
        with self.assertRaises(ValueError):
            SlippageModel(rate=float("nan"))
        with self.assertRaises(ValueError):
            PaperLedger().record_fill(self.t0, "BTCUSDT", LedgerSide.BUY, -1.0, 100.0)
        with self.assertRaises(ValueError):
            PaperLedger().record_fill(self.t0, "BTCUSDT", LedgerSide.BUY, 1.0, float("inf"))
        with self.assertRaises(ValueError):
            PaperLedger().record_fill(self.t0, "BTCUSDT", LedgerSide.SELL, 1.0, 100.0)


if __name__ == "__main__":
    unittest.main()
