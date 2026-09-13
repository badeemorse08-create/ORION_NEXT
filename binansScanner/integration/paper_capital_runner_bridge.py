"""Paper Runner integration boundary for the canonical Capital Manager."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from models.capital_management import AllocationAudit, AllocationCandidate, AllocationConfig, CapitalManager
from models.order_position_lifecycle import OrderState
from models.paper_capital import PaperLedger


@dataclass(frozen=True, slots=True)
class PaperLedgerAccountingView:
    """Read-only projection of canonical PaperLedger state for CapitalManager."""

    ledger: PaperLedger
    policy_reserved: float = 0.0

    @property
    def equity(self) -> float:
        return self.ledger.replay().equity

    @property
    def realized_pnl(self) -> float:
        return self.ledger.replay().realized_pnl

    @property
    def unrealized_pnl(self) -> float:
        return self.ledger.replay().unrealized_pnl

    @property
    def reserved_capital(self) -> float:
        return self.ledger.replay().wallet.reserved_cash + self.policy_reserved

    @property
    def committed_capital(self) -> float:
        state = self.ledger.replay()
        return sum(position.quantity * position.average_price for position in state.positions)


class PaperRunnerCapitalBridge:
    """Single sizing/reservation authority used by the paper runner.

    Capital allocation recovery is journal-driven. The JSONL journal is the
    only durable source for policy reservation/binding state; PaperLedger
    remains authoritative for accounting state.
    """

    def __init__(self, config: AllocationConfig, ledger: PaperLedger, journal_path: Path | None = None) -> None:
        self.config = config
        self.ledger = ledger
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self._pending_notional: dict[str, float] = {}
        self._order_to_allocation: dict[str, str] = {}
        self._allocation_to_order: dict[str, str] = {}
        self._allocation_state: dict[str, str] = {}
        self.manager = CapitalManager(config, accounting=self._view())
        self._load_journal()
        self._refresh_accounting()

    def _view(self) -> PaperLedgerAccountingView:
        return PaperLedgerAccountingView(self.ledger, sum(self._pending_notional.values()))

    def _refresh_accounting(self) -> None:
        self.manager.sync_from_accounting(self._view())

    def _sync_ledger(self, ledger: PaperLedger) -> None:
        self.ledger = ledger
        self._refresh_accounting()

    def _append_journal(self, event: dict[str, object]) -> None:
        if self.journal_path is None:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()

    def _load_journal(self) -> None:
        if self.journal_path is None or not self.journal_path.exists():
            return
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                self._apply_journal_event(json.loads(line))

    def _apply_journal_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", ""))
        allocation_id = str(event.get("allocation_id", ""))
        if not allocation_id:
            return
        if kind == "RESERVE":
            if self._allocation_state.get(allocation_id) in {"RELEASED", "FILLED", "EXITED"}:
                return
            candidate = AllocationCandidate(
                symbol=str(event["symbol"]),
                rank=int(event.get("rank", 1)),
                opportunity_score=float(event.get("opportunity_score", 0.0)),
                intent=str(event.get("intent", "ENTRY")),
            )
            audit = self.manager.calculate(candidate, float(event["required_symbol_minimum"]))
            if not audit.accepted or audit.allocation_id != allocation_id or abs(audit.final_order_notional - float(event["final_order_notional"])) > 1e-9:
                raise ValueError("invalid durable capital reservation journal state")
            self._pending_notional[allocation_id] = audit.final_order_notional
            self._allocation_state[allocation_id] = "RESERVED"
        elif kind == "BIND":
            order_id = str(event["order_id"])
            if allocation_id not in self._pending_notional and self._allocation_state.get(allocation_id) not in {"RESERVED", "FILLED"}:
                return
            self._order_to_allocation[order_id] = allocation_id
            self._allocation_to_order[allocation_id] = order_id
            if self._allocation_state.get(allocation_id) != "FILLED":
                self._allocation_state[allocation_id] = "BOUND"
        elif kind == "FILL":
            order_id = str(event["order_id"])
            if self._allocation_state.get(allocation_id) == "FILLED":
                return
            self._pending_notional.pop(allocation_id, None)
            self.manager.on_fill(allocation_id)
            self._allocation_state[allocation_id] = "FILLED"
            if order_id:
                self._order_to_allocation[order_id] = allocation_id
                self._allocation_to_order[allocation_id] = order_id
        elif kind == "RELEASE":
            if self._allocation_state.get(allocation_id) in {"RELEASED", "EXITED", "FILLED"}:
                return
            order_id = self._allocation_to_order.pop(allocation_id, None)
            if order_id is not None:
                self._order_to_allocation.pop(order_id, None)
            self._pending_notional.pop(allocation_id, None)
            self.manager.on_cancel(allocation_id)
            self._allocation_state[allocation_id] = "RELEASED"
        elif kind == "EXIT":
            if self._allocation_state.get(allocation_id) == "EXITED":
                return
            order_id = self._allocation_to_order.pop(allocation_id, None)
            if order_id is not None:
                self._order_to_allocation.pop(order_id, None)
            self._pending_notional.pop(allocation_id, None)
            self.manager.on_exit(allocation_id)
            self._allocation_state[allocation_id] = "EXITED"

    def sync_policy_positions(self) -> None:
        self._refresh_accounting()
        state = self.ledger.replay()
        self.manager.set_active_positions(position.symbol for position in state.positions if position.quantity > 0.0)

    @property
    def pending_reserved(self) -> float:
        return sum(self._pending_notional.values())

    def allocation_for(
        self,
        *,
        symbol: str,
        rank: int,
        opportunity_score: float,
        required_symbol_minimum: float,
        intent: str = "ENTRY",
    ) -> AllocationAudit:
        self.sync_policy_positions()
        audit = self.manager.calculate(
            AllocationCandidate(symbol=symbol, rank=rank, opportunity_score=opportunity_score, intent=intent),
            required_symbol_minimum,
        )
        if audit.accepted:
            self._pending_notional[audit.allocation_id] = audit.final_order_notional
            self._allocation_state[audit.allocation_id] = "RESERVED"
            self._append_journal(
                {
                    "type": "RESERVE",
                    "allocation_id": audit.allocation_id,
                    "symbol": audit.symbol,
                    "rank": rank,
                    "opportunity_score": opportunity_score,
                    "intent": audit.intent,
                    "desired_allocation": audit.desired_allocation,
                    "required_symbol_minimum": audit.required_symbol_minimum,
                    "final_order_notional": audit.final_order_notional,
                    "capital_mode": audit.capital_mode.value,
                }
            )
            self._refresh_accounting()
        return audit

    def bind_order(self, allocation_id: str, order_id: str) -> None:
        if self._allocation_state.get(allocation_id) not in {"RESERVED", "BOUND"}:
            raise ValueError("allocation is not reserved")
        existing = self._allocation_to_order.get(allocation_id)
        if existing is not None and existing != order_id:
            raise ValueError("allocation is already bound to an order")
        self._order_to_allocation[order_id] = allocation_id
        self._allocation_to_order[allocation_id] = order_id
        self._allocation_state[allocation_id] = "BOUND"
        self._append_journal({"type": "BIND", "allocation_id": allocation_id, "order_id": order_id})

    def release(self, allocation_id: str, *, reason: str) -> bool:
        state = self._allocation_state.get(allocation_id)
        if state in {None, "RELEASED", "EXITED", "FILLED"}:
            return False
        order_id = self._allocation_to_order.pop(allocation_id, None)
        if order_id is not None:
            self._order_to_allocation.pop(order_id, None)
        self._pending_notional.pop(allocation_id, None)
        self._allocation_state[allocation_id] = "RELEASED"
        self.manager.on_cancel(allocation_id)
        self._append_journal({"type": "RELEASE", "allocation_id": allocation_id, "reason": reason})
        self._refresh_accounting()
        return True

    def release_for_order(self, order_id: str, *, reason: str) -> Optional[str]:
        allocation_id = self._order_to_allocation.get(order_id)
        if allocation_id is None:
            return None
        released = self.release(allocation_id, reason=reason)
        return allocation_id if released else None

    def on_fill(self, order_id: str) -> Optional[str]:
        allocation_id = self._order_to_allocation.get(order_id)
        if allocation_id is None:
            return None
        if self._allocation_state.get(allocation_id) == "FILLED":
            return allocation_id
        self._pending_notional.pop(allocation_id, None)
        self._allocation_state[allocation_id] = "FILLED"
        self._refresh_accounting()
        self.manager.on_fill(allocation_id)
        self._append_journal({"type": "FILL", "allocation_id": allocation_id, "order_id": order_id})
        self.sync_policy_positions()
        return allocation_id

    def reconcile_terminal_orders(self, order_lifecycle: object) -> None:
        """Release reservations for terminal non-filled orders after D5 revalidation."""
        get_order = getattr(order_lifecycle, "get", None)
        if not callable(get_order):
            return
        for order_id in tuple(self._order_to_allocation):
            try:
                state = get_order(order_id).state
            except (KeyError, AttributeError):
                continue
            if state is OrderState.PENDING or state is OrderState.FILLED:
                continue
            self.release_for_order(order_id, reason=f"TERMINAL:{state.value}")

    def on_exit_symbol(self, symbol: str) -> None:
        for allocation_id, order_id in tuple(self._allocation_to_order.items()):
            if allocation_id.split("::", 1)[0] == symbol:
                if self._allocation_state.get(allocation_id) == "EXITED":
                    continue
                self._pending_notional.pop(allocation_id, None)
                self._allocation_to_order.pop(allocation_id, None)
                self._order_to_allocation.pop(order_id, None)
                self._refresh_accounting()
                self.manager.on_exit(allocation_id)
                self._allocation_state[allocation_id] = "EXITED"
                self._append_journal({"type": "EXIT", "allocation_id": allocation_id, "symbol": symbol})
        self.sync_policy_positions()

    def audit_state(self) -> dict[str, float | int]:
        self._refresh_accounting()
        return {
            "reserved_capital": self.manager.reserved_capital,
            "committed_capital": self.manager.committed_capital,
            "available_capital": self.manager.available_capital,
            "trading_capital": self.manager.trading_capital,
        }

    def recover(self, ledger: PaperLedger | None = None) -> "PaperRunnerCapitalBridge":
        """Recover policy allocation state exclusively from the durable journal."""
        recovered = PaperRunnerCapitalBridge(self.config, ledger or self.ledger, self.journal_path)
        recovered.sync_policy_positions()
        return recovered


__all__ = ["PaperLedgerAccountingView", "PaperRunnerCapitalBridge"]
