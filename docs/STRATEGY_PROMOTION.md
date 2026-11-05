# Frozen promotion protocol v1 — 2026-11-05

Applies separately to scalp V2.1, V3, 15m control, 15m confirmation,
volume, trend and swing
cohorts. Swing candidate shadows are diagnostic observations, not an
independent portfolio or an extra winner. The first eligible version is not
promoted automatically. `scalp_sentinel.promotion.promotion_report` is a
read-only diagnostic implementing the numerical checks below.

Run an observed-outcome audit with
`python -m scalp_sentinel.promotion --start 2026-11-01T00:00:00Z --end 2027-02-01T00:00:00Z`.
It reads existing PostgreSQL outcomes only; the matched null remains unset,
so it cannot declare promotion until an independent null study is supplied.

## Primary outcome and sample

- Unit: every **decided alert** (filled and resolved, NO_ENTRY or INVALIDATED).
  NO_ENTRY/INVALIDATED contribute 0 R. Active, DATA_GAP, ambiguous outcome
  without a conservative bound, and unresolved funding are excluded from the
  return denominator and counted separately. R is initial stop distance, not
  leveraged account return.
- At least 150 decided alerts spanning at least 56 calendar days. At least two
  *predeclared* regimes each have at least 20 decided alerts. A cohort with
  missing regime labels fails this check. Sample count is a gate, not a power
  calculation or a promise of profitability.
- Day-block bootstrap: resample UTC signal days, keeping every symbol and alert
  of a sampled day together. One-sided lower bound must be above 0 for the
  conservative net R per decided alert. Use 5,000 or more seeded draws.
  Resampling days does not capture longer serial dependence; run longer blocks
  and a subsequent untouched period as sensitivity checks.
- Account for all tried cohorts, including unsuccessful ones. Default one-sided
  familywise alpha is 0.05/7 (Bonferroni); adding a cohort increases the
  denominator. An alternative is to freeze one winner and validate in a fresh,
  untouched time window without choosing again from that window.
- Double the execution fee/slippage allowance and require positive mean net R.
  Funding is unchanged in this check and must be complete. Require observed
  DATA_GAP < 2% of terminal alerts, and no incomplete settled outcomes.
- No one symbol or UTC day may contribute >=25% of **positive R contributions**.
  Use the positive sum denominator because a net total near zero makes a
  percentage of total net R unstable; inspect loss concentration separately.
- A separately generated, preregistered matched random-direction **and**
  inverted-direction control is mandatory. The candidate must exceed the
  95th percentile of the matched null *after* applying the same selection,
  execution, fill and funding assumptions. Until both controls are computed
  on a truly untouched period, `null_percentile` remains unset and the gate
  fails. Hundreds of random draws from four events are not hundreds of
  independent trades. Preserve time-of-day, symbol, regime and trade-cluster
  structure when building the null.

## Important separation and exclusions

The scalp reference exit is 50% at 1.5R and 50% at 2.5R with the remaining
stop moving to entry after TP1. Swing uses 1.5R/3R. They need different
null distributions and must never share a pooled win rate. The familiar
zero-gross-R fair random-walk calculation is a useful mathematical sanity
check, not evidence that real fills, capped holding time or market drift have
zero expectancy.

Do not promote a rule chosen to remove the inspected 2025–26 holdout losses.
Historical current-listed universes omit delisted contracts. The official
Binance public archive exposes futures klines by symbol/month, but its exact
historical coverage, listing dates, checksum and funding availability must be
verified before any 2020–24 point-in-time claim. A missing symbol or 1m segment
is not a zero-return trade. Track BTC daily trend veto counts by direction.

## Decision record

Publish the sample window, frozen code commit, attempted-cohort count, every
gate result, null construction/seed, all missing data, year and long/short
tables, and sensitivity to 30/60/120-second manual delay and stricter limit
fills. Paper results remain informational; no exchange orders are authorized
by this protocol.
