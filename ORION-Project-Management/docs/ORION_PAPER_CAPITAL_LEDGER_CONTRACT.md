# ORION_NEXT — D6 Paper Capital / Ledger Contract

## Purpose

This contract defines deterministic virtual-capital accounting for Paper Bot only. It does not alter order lifecycle, execution semantics, opportunity discovery, or any live path.

## Starting Capital

The canonical default virtual starting equity is **$200.00**.

The account exposes:

- `starting_equity`
- `cash`
- `reserved_cash`
- `available_cash`
- open positions and their market value
- realized and unrealized P&L
- fees and slippage
- equity and drawdown

## Ledger

The ledger is immutable and append-only. Events are sequenced contiguously and normalized to UTC timestamps.

Required trade/accounting events are:

- `ORDER`
- `FILL`
- `POSITION`
- `EXIT`
- `FEE`
- `SLIPPAGE`
- `PNL`
- `SNAPSHOT`

`RESERVE` and `RELEASE` support deterministic reserved-cash accounting without changing order lifecycle semantics.

## Fees

`FeeModel` is deterministic and configurable by rate plus optional minimum fee. Fees are recorded explicitly and reduce cash/equity.

## Slippage

`SlippageModel` is deterministic and configurable by rate. Execution price is adjusted by side:

- BUY: reference price × (1 + rate)
- SELL: reference price × (1 - rate)

Slippage amount is recorded as a dedicated ledger event. It is also reflected in execution price and therefore in position cost/P&L; it is not subtracted twice from the accounting identity.

## P&L

Realized P&L is calculated from execution price versus the position average cost for closed quantity.

Unrealized P&L is mark-to-market against the latest recorded market price.

All P&L values are derived from ledger events and replay state; no hidden mutable balance is required.

## Accounting Identity

For every replayed account state:

`starting_equity = cash + open_position_market_value + accounting_adjustments`

where:

`accounting_adjustments = cumulative_fees - realized_pnl - unrealized_pnl`

Slippage is already included in execution price/position cost and therefore must not be subtracted twice.

## Equity Curve / Drawdown

`SNAPSHOT` events create timestamped `EquitySnapshot` records containing starting/ending account values, cash, open-position value, P&L, peak equity, current drawdown, and maximum drawdown.

Maximum drawdown is the greatest observed peak-to-current equity reduction during deterministic replay.

## Replay

`PaperLedger.replay()` reconstructs wallet, positions, market marks, realized P&L, fees, slippage, peak equity and maximum drawdown solely from the ledger sequence.

Repeated replay of an unchanged ledger must return the same immutable state.

## Boundaries

This contract does not:

- execute live orders;
- modify the existing Order Lifecycle;
- modify Opportunity Discovery;
- start the requested 24-hour simulation;
- change `main`.

The layer is a financial simulation/accounting boundary for Paper Bot only.
