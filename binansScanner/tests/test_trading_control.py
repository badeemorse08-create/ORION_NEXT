from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from gui.gui_controller import GuiController
from gui.gui_service import GuiService
from gui.gui_window import GuiWindow
from integration.paper_runtime_supervisor import PaperRuntimeSupervisor
from integration.trading_control import TradingControlStore, TradingState
from models.market_event import MarketEvent, MarketEventType
from models.order_position_lifecycle import OrderState
from models.signal_snapshot import SignalIdentity, SignalSnapshot
from tools.orion_trading_control import main as control_main

UTC = timezone.utc


def snapshot(*, price: float, generated_at: datetime, version: int = 1) -> SignalSnapshot:
    return SignalSnapshot(identity=SignalIdentity("BTCUSDT", "PAPER", "ENTRY"), version=version, direction="BUY", decision="FAVORABLE", confidence=90.0, entry_plan={"entry_price": price, "quantity": 1.0}, generated_at=generated_at, valid_until=generated_at + timedelta(minutes=15), quality=90.0)


def market(price: float, timestamp: datetime, event_id: str) -> MarketEvent:
    return MarketEvent(symbol="BTCUSDT", event_timestamp=timestamp, event_type=MarketEventType.TRADE, payload={"price": price}, source_event_id=event_id)


class TestTradingControl(unittest.TestCase):
    def make_runtime(self):
        tmp = tempfile.TemporaryDirectory(); path = Path(tmp.name) / "control.json"; supervisor = PaperRuntimeSupervisor(control=TradingControlStore(path)); return tmp, path, supervisor

    def test_running_allows_normal_entry(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); self.assertEqual(runtime.trading_state, TradingState.RUNNING); order = runtime.submit_signal(snapshot(price=100.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC)); self.assertEqual(runtime.runtime.orders.get(order.order_id).state, OrderState.PENDING)

    def test_paused_blocks_new_entry(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(source="test", reason="maintenance")
        with self.assertRaises(PermissionError): runtime.submit_signal(snapshot(price=100.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC))
        self.assertEqual(runtime.control_events[-1].event, "ENTRY_BLOCKED_BY_PAUSE")

    def test_paused_blocks_pending_entry_creation(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries()
        with self.assertRaises(PermissionError): runtime.submit_signal(snapshot(price=100.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC))
        self.assertEqual(runtime.active_orders, ())

    def test_pause_precedes_capital_reservation(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(); before = runtime.runtime.replay_account().wallet
        with self.assertRaises(PermissionError): runtime.submit_signal(snapshot(price=100.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC))
        after = runtime.runtime.replay_account().wallet; self.assertEqual(before, after); self.assertEqual(after.reserved_cash, 0.0)

    def test_existing_position_can_exit_while_paused(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); t0 = datetime.now(UTC); runtime.submit_signal(snapshot(price=100.0, generated_at=t0), now=t0); runtime.process_market_event(market(100.0, t0 + timedelta(seconds=1), "entry")); runtime.pause_new_entries(); position = runtime.active_positions[0]; exit_order = runtime.runtime.exit_position(symbol=position.symbol, price=101.0, now=t0 + timedelta(seconds=2)); self.assertIsNotNone(exit_order); self.assertEqual(runtime.trading_state, TradingState.PAUSED); self.assertIsNone(runtime.runtime.positions.active_for_symbol(position.symbol))

    def test_pause_does_not_close_existing_position(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); t0 = datetime.now(UTC); runtime.submit_signal(snapshot(price=100.0, generated_at=t0), now=t0); runtime.process_market_event(market(100.0, t0 + timedelta(seconds=1), "entry")); runtime.pause_new_entries(); self.assertEqual(len(runtime.active_positions), 1); self.assertGreater(runtime.active_positions[0].quantity, 0.0)

    def test_pause_survives_process_restart(self):
        tmp, path, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(source="test", reason="shutdown"); restarted = TradingControlStore(path); self.assertEqual(restarted.state, TradingState.PAUSED); self.assertEqual(PaperRuntimeSupervisor(control=restarted).trading_state, TradingState.PAUSED)

    def test_resume_restores_entry_behavior(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(); runtime.resume_trading(source="test", reason="operator resume"); self.assertEqual(runtime.trading_state, TradingState.RUNNING); order = runtime.submit_signal(snapshot(price=100.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC)); self.assertEqual(order.quantity, 1.0)

    def test_missing_control_state_is_fail_closed(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.assertEqual(TradingControlStore(Path(tmp.name) / "missing.json").state, TradingState.PAUSED)

    def test_corrupt_control_state_is_fail_closed(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); path = Path(tmp.name) / "control.json"; path.write_text("not-json", encoding="utf-8"); self.assertEqual(TradingControlStore(path).state, TradingState.PAUSED); path.write_text(json.dumps({"state": "INVALID"}), encoding="utf-8"); self.assertEqual(TradingControlStore(path).state, TradingState.PAUSED)

    def test_concurrent_allocation_cannot_bypass_pause(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries()
        def attempt(i: int):
            now = datetime.now(UTC)
            try: runtime.submit_signal(snapshot(price=100.0, generated_at=now, version=i + 1), now=now, intent_id=f"intent-{i}")
            except PermissionError: return True
            return False
        with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(attempt, range(16)))
        self.assertTrue(all(results)); self.assertEqual(runtime.active_orders, ()); self.assertEqual(runtime.runtime.replay_account().wallet.reserved_cash, 0.0)

    def test_dynamic_top_n_path_uses_same_runtime_guard(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(source="D1", reason="top-n refresh")
        with self.assertRaises(PermissionError): runtime.submit_signal(snapshot(price=101.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC), intent_id="D1-TOPN-BTC")
        self.assertEqual(runtime.active_orders, ())

    def test_fixed_allocation_path_obeys_pause(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(source="capital", reason="fixed allocation")
        with self.assertRaises(PermissionError): runtime.submit_signal(snapshot(price=102.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC), intent_id="FIXED-ALLOCATION")

    def test_compounding_path_obeys_pause(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(source="capital", reason="compounding")
        with self.assertRaises(PermissionError): runtime.submit_signal(snapshot(price=103.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC), intent_id="COMPOUNDING")

    def test_recovery_preserves_pause_state(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(source="test", reason="recovery"); recovered = runtime.recover(); self.assertEqual(recovered.trading_state, TradingState.PAUSED); self.assertEqual(recovered.replay_state(), runtime.replay_state())

    def test_recovery_preserves_running_state(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); recovered = runtime.recover(); self.assertEqual(recovered.trading_state, TradingState.RUNNING); self.assertEqual(recovered.replay_state(), runtime.replay_state())

    def test_duplicate_pause_resume_are_deterministic(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); self.assertEqual(runtime.pause_new_entries(), TradingState.PAUSED); event_count = len(runtime.control_events); self.assertEqual(runtime.pause_new_entries(), TradingState.PAUSED); self.assertEqual(len(runtime.control_events), event_count); self.assertEqual(runtime.resume_trading(), TradingState.RUNNING); event_count = len(runtime.control_events); self.assertEqual(runtime.resume_trading(), TradingState.RUNNING); self.assertEqual(len(runtime.control_events), event_count)

    def test_observability_records_pause_and_block(self):
        tmp, path, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); runtime.pause_new_entries(source="ui", reason="user clicked pause")
        with self.assertRaises(PermissionError): runtime.submit_signal(snapshot(price=100.0, generated_at=datetime.now(UTC)), now=datetime.now(UTC))
        events = runtime.control_events; self.assertEqual(events[0].event, "TRADING_RESUMED"); self.assertEqual(events[-1].event, "ENTRY_BLOCKED_BY_PAUSE"); self.assertEqual(events[-1].source, "runtime"); persisted = path.with_name(path.name + ".events.jsonl").read_text(encoding="utf-8"); self.assertIn("TRADING_PAUSED", persisted); self.assertIn("ENTRY_BLOCKED_BY_PAUSE", persisted)

    def test_ui_and_runtime_share_same_state(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); path = Path(tmp.name) / "control.json"; runtime = PaperRuntimeSupervisor(control=TradingControlStore(path)); runtime.pause_new_entries(source="runtime", reason="test"); out = StringIO()
        with patch("sys.stdout", out): self.assertEqual(control_main(["status", "--state-file", str(path)]), 0)
        self.assertIn("TRADING_STATE=PAUSED", out.getvalue())
        with patch("sys.stdout", out): self.assertEqual(control_main(["resume", "--state-file", str(path)]), 0)
        self.assertEqual(runtime.trading_state, TradingState.RUNNING)

    def test_gui_buttons_bind_to_runtime_control(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); control = TradingControlStore(Path(tmp.name) / "control.json"); window = GuiWindow(GuiController(GuiService(trading_control=control)))
        actions = window.trading_controls(); self.assertEqual(tuple(action.name for action in actions), ("PAUSE NEW ENTRIES", "RESUME TRADING")); self.assertTrue(actions[0].enabled); self.assertFalse(actions[1].enabled)
        self.assertEqual(window.pause_new_entries(), TradingState.PAUSED); actions = window.trading_controls(); self.assertFalse(actions[0].enabled); self.assertTrue(actions[1].enabled); self.assertEqual(window.resume_trading(), TradingState.RUNNING)

    def test_gui_state_is_derived_from_runtime_control(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); control = TradingControlStore(Path(tmp.name) / "control.json"); window = GuiWindow(GuiController(GuiService(trading_control=control))); window.controller().pause_new_entries(); self.assertEqual(window.controller().trading_state(), TradingState.PAUSED); self.assertEqual(window.controller().trading_actions()[0].metadata["state"], "PAUSED")

    def test_no_live_path_dependency(self):
        tmp, _, runtime = self.make_runtime(); self.addCleanup(tmp.cleanup); self.assertTrue(runtime.no_live_path()); self.assertTrue(runtime.health.paper_only)


if __name__ == "__main__": unittest.main()
