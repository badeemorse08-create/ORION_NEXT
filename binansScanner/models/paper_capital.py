"""Deterministic paper-capital accounting and replay contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Optional

VIRTUAL_STARTING_EQUITY = 200.0


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: float, field_name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


class LedgerEventType(str, Enum):
    ORDER = "ORDER"
    FILL = "FILL"
    POSITION = "POSITION"
    EXIT = "EXIT"
    FEE = "FEE"
    SLIPPAGE = "SLIPPAGE"
    PNL = "PNL"
    SNAPSHOT = "SNAPSHOT"
    RESERVE = "RESERVE"
    RELEASE = "RELEASE"


class LedgerSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class FeeModel:
    rate: float = 0.0
    minimum: float = 0.0

    def __post_init__(self) -> None:
        if _finite(self.rate, "rate") < 0.0 or _finite(self.minimum, "minimum") < 0.0:
            raise ValueError("fee model values must be non-negative")

    def fee(self, notional: float) -> float:
        notional = abs(_finite(notional, "notional"))
        return max(notional * self.rate, self.minimum if notional else 0.0)


@dataclass(frozen=True, slots=True)
class SlippageModel:
    rate: float = 0.0

    def __post_init__(self) -> None:
        if _finite(self.rate, "rate") < 0.0:
            raise ValueError("slippage rate must be non-negative")

    def execution_price(self, side: LedgerSide, reference_price: float) -> float:
        price = _finite(reference_price, "reference_price")
        if price <= 0.0:
            raise ValueError("reference_price must be positive")
        if side is LedgerSide.BUY:
            return price * (1.0 + self.rate)
        if side is LedgerSide.SELL:
            return price * (1.0 - self.rate)
        return price

    def amount(self, side: LedgerSide, reference_price: float, quantity: float) -> float:
        quantity = abs(_finite(quantity, "quantity"))
        reference = _finite(reference_price, "reference_price")
        return abs(self.execution_price(side, reference) - reference) * quantity


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: float = 0.0
    average_price: float = 0.0
    realized_pnl: float = 0.0

    def mark_to_market(self, price: float) -> float:
        return self.quantity * _finite(price, "price")

    def unrealized_pnl(self, price: float) -> float:
        if self.quantity == 0.0:
            return 0.0
        return self.mark_to_market(price) - (self.quantity * self.average_price)


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    timestamp: datetime
    equity: float
    cash: float
    reserved_cash: float
    available_cash: float
    open_position_value: float
    unrealized_pnl: float
    realized_pnl: float
    peak_equity: float
    drawdown: float
    maximum_drawdown: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    sequence: int
    timestamp: datetime
    event_type: LedgerEventType
    symbol: Optional[str] = None
    side: Optional[LedgerSide] = None
    quantity: float = 0.0
    price: float = 0.0
    notional: float = 0.0
    fee: float = 0.0
    slippage: float = 0.0
    realized_pnl: float = 0.0
    cash_delta: float = 0.0
    position_quantity_delta: float = 0.0
    reserved_cash_delta: float = 0.0

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        for name in ("quantity", "price", "notional", "fee", "slippage", "realized_pnl", "cash_delta", "position_quantity_delta", "reserved_cash_delta"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.quantity < 0.0:
            raise ValueError("quantity must be non-negative")


@dataclass(frozen=True, slots=True)
class VirtualWallet:
    starting_equity: float = VIRTUAL_STARTING_EQUITY
    cash: float = VIRTUAL_STARTING_EQUITY
    reserved_cash: float = 0.0

    def __post_init__(self) -> None:
        for name in ("starting_equity", "cash", "reserved_cash"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.starting_equity <= 0.0:
            raise ValueError("starting_equity must be positive")
        if self.cash < 0.0 or self.reserved_cash < 0.0:
            raise ValueError("cash and reserved_cash must be non-negative")
        if self.reserved_cash > self.cash:
            raise ValueError("reserved_cash cannot exceed cash")

    @property
    def available_cash(self) -> float:
        return self.cash - self.reserved_cash


@dataclass(frozen=True, slots=True)
class PaperAccountState:
    wallet: VirtualWallet
    positions: tuple[Position, ...] = ()
    last_prices: tuple[tuple[str, float], ...] = ()
    realized_pnl: float = 0.0
    cumulative_fees: float = 0.0
    cumulative_slippage: float = 0.0
    peak_equity: float = VIRTUAL_STARTING_EQUITY
    maximum_drawdown: float = 0.0

    def position(self, symbol: str) -> Position:
        for position in self.positions:
            if position.symbol == symbol:
                return position
        return Position(symbol=symbol)

    def price(self, symbol: str) -> Optional[float]:
        for key, value in self.last_prices:
            if key == symbol:
                return value
        return None

    @property
    def open_position_value(self) -> float:
        return sum(position.mark_to_market(self.price(position.symbol)) for position in self.positions if self.price(position.symbol) is not None)

    @property
    def unrealized_pnl(self) -> float:
        return sum(position.unrealized_pnl(self.price(position.symbol)) for position in self.positions if self.price(position.symbol) is not None)

    @property
    def equity(self) -> float:
        return self.wallet.cash + self.open_position_value

    @property
    def current_drawdown(self) -> float:
        return max(self.peak_equity - self.equity, 0.0)

    @property
    def accounting_adjustments(self) -> float:
        # Fees reduce equity directly. Slippage is already represented in
        # execution_price and therefore already carried by P&L/position cost.
        return self.cumulative_fees - self.realized_pnl - self.unrealized_pnl

    def accounting_identity_holds(self) -> bool:
        return math.isclose(self.starting_equity, self.wallet.cash + self.open_position_value + self.accounting_adjustments, rel_tol=0.0, abs_tol=1e-9)

    @property
    def starting_equity(self) -> float:
        return self.wallet.starting_equity


@dataclass(frozen=True, slots=True)
class PaperLedger:
    starting_equity: float = VIRTUAL_STARTING_EQUITY
    fee_model: FeeModel = field(default_factory=FeeModel)
    slippage_model: SlippageModel = field(default_factory=SlippageModel)
    events: tuple[LedgerEvent, ...] = ()

    def __post_init__(self) -> None:
        if _finite(self.starting_equity, "starting_equity") <= 0.0:
            raise ValueError("starting_equity must be positive")
        previous = 0
        for event in self.events:
            if event.sequence != previous + 1:
                raise ValueError("ledger sequences must be contiguous")
            previous = event.sequence

    @property
    def starting_wallet(self) -> VirtualWallet:
        return VirtualWallet(starting_equity=self.starting_equity, cash=self.starting_equity)

    def append(self, event: LedgerEvent) -> "PaperLedger":
        if event.sequence != len(self.events) + 1:
            raise ValueError("next ledger sequence must be contiguous")
        return PaperLedger(self.starting_equity, self.fee_model, self.slippage_model, self.events + (event,))

    def _event(self, event_type: LedgerEventType, timestamp: datetime, **kwargs: object) -> LedgerEvent:
        return LedgerEvent(sequence=len(self.events) + 1, timestamp=timestamp, event_type=event_type, **kwargs)

    def record_order(self, timestamp: datetime, symbol: str, side: LedgerSide, quantity: float, reference_price: float) -> "PaperLedger":
        quantity = _finite(quantity, "quantity")
        price = _finite(reference_price, "reference_price")
        if quantity <= 0.0 or price <= 0.0:
            raise ValueError("order quantity and reference_price must be positive")
        return self.append(self._event(LedgerEventType.ORDER, timestamp, symbol=symbol, side=side, quantity=quantity, price=price, notional=quantity * price))

    def reserve_cash(self, timestamp: datetime, amount: float) -> "PaperLedger":
        amount = _finite(amount, "amount")
        if amount <= 0.0 or amount > self.replay().wallet.available_cash:
            raise ValueError("reservation exceeds available cash")
        return self.append(self._event(LedgerEventType.RESERVE, timestamp, reserved_cash_delta=amount))

    def release_cash(self, timestamp: datetime, amount: float) -> "PaperLedger":
        amount = _finite(amount, "amount")
        if amount <= 0.0 or amount > self.replay().wallet.reserved_cash:
            raise ValueError("release exceeds reserved cash")
        return self.append(self._event(LedgerEventType.RELEASE, timestamp, reserved_cash_delta=-amount))

    def record_fill(self, timestamp: datetime, symbol: str, side: LedgerSide, quantity: float, reference_price: float) -> "PaperLedger":
        quantity = _finite(quantity, "quantity")
        reference = _finite(reference_price, "reference_price")
        if quantity <= 0.0 or reference <= 0.0:
            raise ValueError("fill quantity and reference_price must be positive")
        state = self.replay()
        current = state.position(symbol)
        execution = self.slippage_model.execution_price(side, reference)
        fee = self.fee_model.fee(execution * quantity)
        slip = self.slippage_model.amount(side, reference, quantity)
        if side is LedgerSide.BUY and execution * quantity + fee > state.wallet.available_cash:
            raise ValueError("insufficient available cash for fill")
        if side is LedgerSide.SELL and quantity > current.quantity:
            raise ValueError("sell quantity cannot exceed open position")
        realized = (execution - current.average_price) * quantity if side is LedgerSide.SELL else 0.0
        cash_delta = -(execution * quantity + fee) if side is LedgerSide.BUY else (execution * quantity - fee)
        ledger = self.append(self._event(LedgerEventType.FILL, timestamp, symbol=symbol, side=side, quantity=quantity, price=execution, notional=execution * quantity, fee=fee, slippage=slip, realized_pnl=realized, cash_delta=cash_delta, position_quantity_delta=(quantity if side is LedgerSide.BUY else -quantity)))
        ledger = ledger.append(ledger._event(LedgerEventType.FEE, timestamp, symbol=symbol, side=side, quantity=quantity, price=execution, notional=execution * quantity, fee=fee))
        ledger = ledger.append(ledger._event(LedgerEventType.SLIPPAGE, timestamp, symbol=symbol, side=side, quantity=quantity, price=execution, notional=execution * quantity, slippage=slip))
        if realized:
            ledger = ledger.append(ledger._event(LedgerEventType.PNL, timestamp, symbol=symbol, side=side, quantity=quantity, price=execution, notional=execution * quantity, realized_pnl=realized))
        return ledger

    def record_exit(self, timestamp: datetime, symbol: str, quantity: float, reference_price: float) -> "PaperLedger":
        state = self.replay()
        position = state.position(symbol)
        if position.quantity <= 0.0 or quantity <= 0.0 or quantity > position.quantity:
            raise ValueError("exit quantity must be within the open position")
        return self.append(self._event(LedgerEventType.EXIT, timestamp, symbol=symbol, side=LedgerSide.SELL, quantity=quantity, price=reference_price, notional=quantity * reference_price, position_quantity_delta=-quantity))

    def mark(self, timestamp: datetime, symbol: str, market_price: float) -> "PaperLedger":
        market_price = _finite(market_price, "market_price")
        if market_price <= 0.0:
            raise ValueError("market_price must be positive")
        return self.append(self._event(LedgerEventType.POSITION, timestamp, symbol=symbol, price=market_price))

    def snapshot(self, timestamp: datetime) -> "PaperLedger":
        state = self.replay()
        return self.append(self._event(LedgerEventType.SNAPSHOT, timestamp, notional=state.equity))

    def replay(self) -> PaperAccountState:
        wallet = self.starting_wallet
        positions: dict[str, Position] = {}
        prices: dict[str, float] = {}
        realized = 0.0
        fees = 0.0
        slippage = 0.0
        peak = self.starting_equity
        max_dd = 0.0
        for event in self.events:
            if event.event_type is LedgerEventType.FILL:
                symbol = event.symbol or ""
                current = positions.get(symbol, Position(symbol))
                if event.side is LedgerSide.BUY:
                    new_qty = current.quantity + event.quantity
                    avg = ((current.quantity * current.average_price) + (event.quantity * event.price)) / new_qty
                    positions[symbol] = Position(symbol, new_qty, avg, current.realized_pnl)
                elif event.side is LedgerSide.SELL:
                    remaining = current.quantity - event.quantity
                    realized += event.realized_pnl
                    positions[symbol] = Position(symbol, max(remaining, 0.0), current.average_price if remaining > 0.0 else 0.0, current.realized_pnl + event.realized_pnl)
                wallet = VirtualWallet(wallet.starting_equity, wallet.cash + event.cash_delta, wallet.reserved_cash)
                prices[symbol] = event.price
                fees += event.fee
                slippage += event.slippage
            elif event.event_type in (LedgerEventType.RESERVE, LedgerEventType.RELEASE):
                wallet = VirtualWallet(wallet.starting_equity, wallet.cash, wallet.reserved_cash + event.reserved_cash_delta)
            elif event.event_type is LedgerEventType.POSITION:
                if event.symbol:
                    prices[event.symbol] = event.price
            equity = wallet.cash + sum(position.mark_to_market(prices[symbol]) for symbol, position in positions.items() if symbol in prices)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return PaperAccountState(wallet, tuple(sorted(positions.values(), key=lambda p: p.symbol)), tuple(sorted(prices.items())), realized, fees, slippage, peak, max_dd)

    def latest_equity_snapshot(self) -> EquitySnapshot:
        state = self.replay()
        return EquitySnapshot(self.events[-1].timestamp if self.events else datetime.now(timezone.utc), state.equity, state.wallet.cash, state.wallet.reserved_cash, state.wallet.available_cash, state.open_position_value, state.unrealized_pnl, state.realized_pnl, state.peak_equity, state.current_drawdown, state.maximum_drawdown)

    def equity_curve(self) -> tuple[EquitySnapshot, ...]:
        curve: list[EquitySnapshot] = []
        for index, event in enumerate(self.events, start=1):
            if event.event_type is LedgerEventType.SNAPSHOT:
                state = PaperLedger(self.starting_equity, self.fee_model, self.slippage_model, self.events[:index]).replay()
                curve.append(EquitySnapshot(event.timestamp, state.equity, state.wallet.cash, state.wallet.reserved_cash, state.wallet.available_cash, state.open_position_value, state.unrealized_pnl, state.realized_pnl, state.peak_equity, state.current_drawdown, state.maximum_drawdown))
        return tuple(curve)


__all__ = [
    "VIRTUAL_STARTING_EQUITY",
    "LedgerEventType",
    "LedgerSide",
    "FeeModel",
    "SlippageModel",
    "Position",
    "EquitySnapshot",
    "LedgerEvent",
    "VirtualWallet",
    "PaperAccountState",
    "PaperLedger",
]
