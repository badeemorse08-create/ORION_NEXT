# ORION Paper Capital Management / Portfolio Allocation Contract

## Scope

This package owns capital sizing and portfolio allocation policy only. Accounting remains authoritative elsewhere. It does not discover opportunities, calculate signals, decide entries/exits, or execute orders.

## Capital modes

`FIXED_ALLOCATION` keeps the configured base allocation stable after realized profit/loss. Equity still changes normally.

`COMPOUNDING` recomputes allocation from trading capital after realized P&L. The configured allocation rate is never hard-coded.

Starting capital is configurable; `$50` is the reference experiment value.

## Capital state

The allocation boundary separates:

- total equity
- realized P&L
- unrealized P&L
- reserved capital
- committed capital
- trading capital
- available capital

By default, only realized P&L is reusable for new allocations. Unrealized P&L remains valuation evidence and does not increase trading capital.

The manager may consume an external read-only `AccountingView` supplied by the authoritative ledger. It does not reconstruct the ledger or replay accounting internally.

## Sizing

`desired_allocation` is calculated before symbol minimum normalization.

`final_order_notional = max(desired_allocation, required_symbol_minimum)`.

The symbol minimum is a protection rule, not a sizing policy, and is supplied per symbol. The manager never assumes a universal minimum such as `$5`.

Every allocation records:

- allocation ID
- symbol and intent
- desired allocation
- required symbol minimum
- final order notional
- capital mode
- available capital before/after
- reserved capital before
- whether minimum promotion was applied
- accepted/rejected reason

## Portfolio allocation

Ranked candidates are processed deterministically by rank, then score, then symbol/intent. The default policy does **not** impose an artificial single-position concurrency cap. `max_concurrent_positions` is an explicit optional portfolio risk limit; when configured, it caps simultaneous pending/active symbols. When omitted, concurrency remains bounded by available/reserved capital and duplicate-symbol protection.

An existing active symbol or duplicate pending/committed symbol+intent is not allocated again. The manager never closes an existing position to make room for a better candidate.

## Reservations

A pending allocation reserves capital exactly once. `CANCEL`, `REJECT`, and `EXPIRE` release the reservation. `FILL` moves the reservation to committed capital; `EXIT` releases committed capital. Order/position lifecycle remains owned by D4.

## Safety boundary

The manager is downstream of opportunity ranking and upstream of order placement. It cannot override downstream D4/D5/D6 rejection. When downstream rejects an entry, its allocation reservation must be released through the lifecycle callback.

## D6 compatibility

The current engineering baseline does not contain the separately accepted D6 `VirtualWallet` / `PaperLedger` package. Therefore this package exposes a minimal read-only `AccountingView` boundary and does not duplicate accounting logic. When the D6 accounting package is integrated into `main`, it supplies equity, realized P&L, unrealized P&L, reserved capital, and committed capital through that boundary.

No changes are made to D6 accounting semantics in this package.

## Rejections

Canonical rejection reasons:

- `INSUFFICIENT_CAPITAL`
- `MAX_CONCURRENT_POSITIONS`
- `DUPLICATE_ALLOCATION`
- `INVALID_NOTIONAL`
- `INELIGIBLE_OPPORTUNITY`

The reasons are deterministic and auditable.
