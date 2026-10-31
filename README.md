# Crypto Scalp Sentinel

Read-only Binance USDT perpetual scanner. **No exchange orders and no OpenAI calls.**
Default watchlist: BTC, ETH, SOL, BNB, XRP, AVAX, LINK, NEAR, AAVE, ZEC, ENA,
HYPE, SUI, DOGE, LTC, WLD and BIO. Thresholds are unvalidated hypotheses.

## 2026-10-30 staged improvement

1. **V2.1 primary:** preserves V2 trend/breakout/retest logic, fixes outcome
   ambiguity, uses fresh executable-side quotes as a delivery gate, and adds
   cost/risk, setup deduplication and open-exposure limits.
2. **Timing:** discover on each 5m boundary; monitor candidates on each closed
   1m bar. Cache higher timeframes until their next boundary. BTC context and
   open exposure context are refreshed too. No full-watchlist reload each minute.
   Delayed flow/OI publication is a no-trade pending state, retried the next
   minute without relaxing freshness or discarding valid higher-timeframe candles.
   Taker delta now comes directly from closed Binance 5m klines (base volume
   minus taker-buy volume yields taker-sell volume). This avoids delayed taker
   statistics and removes that extra HTTP request. Flow source and OI ages are
   recorded with new signals; OI freshness gates remain enforced.
3. **V3 shadow:** compare breakout expansion, retest contraction, renewed
   trigger volume, normalized taker delta, aligned 5m OI, trend/range/shock
   regime, BTC context and side-adjusted return correlation. No extra Telegram
   alerts from shadow. V3 is NOT automatically promoted.
4. **15m control cohort:** a separate V2.1 paper cohort sampled on quarter-hour
   minutes, with the same execution gates/costs, helps isolate timing changes.
   Worker downtime can miss these observations; it is not a historical V2 clone.
5. **Measurement:** versioned paper net-R bounds, partial exits, fee/slippage
   assumptions, actual historical funding, MFE/MAE, drawdown and loss streaks.
   Archive decision frames for chronological replay and persist reject reasons.

## Run and deploy

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
DRY_RUN=true python -m scalp_sentinel.main
```

A dry run never writes PostgreSQL or sends Telegram, even if credentials are
present. `RUN_CONTINUOUS=false` runs one scan and exits. A continuous worker
waits until each minute boundary plus 3 seconds, discovers on new 5m buckets,
and stops gracefully on SIGTERM. PostgreSQL advisory locking permits one worker.
Three consecutive failed ticks exit nonzero so Railway can restart it.

For the production worker set `RUN_CONTINUOUS=true`, **clear the Railway cron**,
set `restartPolicyType=ON_FAILURE`, and keep one replica. Do not run a continuous
worker under the old cron schedule. The repository config sets pre-deploy unit
checks and additive database schema verification. Deployment does not alter
credentials. `DATABASE_URL`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be
set for `DRY_RUN=false`. No exchange trading API key is required.

## Defaults and assumptions

| Setting | Default | Meaning |
|---|---|---|
| PRIMARY_STRATEGY | v2.1 | Telegram engine; v3 is an explicit later opt-in |
| SHADOW_ENABLED | true | V3 and 15m control paper cohorts |
| ENTRY_WINDOW_MINUTES | 5 | Reference limit entry expires |
| MAX_HOLD_MINUTES | 360 | Paper position time exit |
| MAX_ENTRY_DRIFT_ATR | 0.20 | Absolute current quote/reference separation |
| PAPER_FEE_BPS | 5 | Assumed fee per side, not an account-specific quote |
| PAPER_SLIPPAGE_BPS | 2 | Assumed slippage per side |
| MAX_COST_R | 0.25 | Reject expensive setups relative to initial risk |
| MAX_OPEN_SIGNALS | 3 | Per-cohort pending/open exposure cap |
| MAX_SIGNALS_PER_RUN | 3 | Now enforced over a rolling 15m window |
| CORRELATION_THRESHOLD | 0.80 | V3 side-adjusted correlation veto |
| BREAKOUT_RVOL | 1.15 | V3 breakout bar versus preceding 20 bars |
| MIN_DELTA_RATIO | 0.05 | V3 directional normalized taker delta |
| MIN_EFFICIENCY | 0.25 | V3 directional efficiency floor |
| ARCHIVE_DAYS | 30 | Compressed frames/assessments/decision retention |

V2.1 retains the existing `SCORE_THRESHOLD`, `MIN_RVOL`, stop ATR bounds and
minimum 180m long / 240m short cooldown. New setup identity is required even
when cooldown expires. An active signal blocks another signal in that symbol,
including an opposing direction. V3's additional filters initially run only in
shadow; the primary engine does not claim their benefit before comparison.

## Scanner diagnostics

Each evaluated symbol now logs its side-specific failed gates and each scan
logs aggregate gate pass/fail counts, prefilter rejection reasons, and data
pending counts. This separates a quiet market from unavailable inputs or a
specific strategy condition. Delayed 5m OI is isolated to the V3 shadow cohort;
it no longer blocks a v2.1 primary snapshot. A stale 15m OI input still fails
closed for v2.1 because its CVD/OI gate depends on that series.

## Entry and outcome semantics

The notification is a **paper limit-reference setup**, not a market order.
The latest bid/ask must be close to the reference and fresh immediately before
sending. The message states a validity deadline, acceptable quote range and
invalidation. Filling is modeled by a future complete 1m candle touching the
reference; the partially observed delivery minute is intentionally omitted.
Immediate moves in that delivery minute are not captured; this is not real execution.
If the stop (or TP1 for new models)
is reached before entry, the setup is INVALIDATED. No fill by the deadline is
NO_ENTRY. Candles straddling the entry deadline are excluded conservatively.

New paper outcomes assume 50% closed at TP1 (1.5R), 50% at TP2 (2.5R), with the
remaining stop moved to entry after TP1. Thus both targets yield 2R gross and
TP1 then break-even yields .75R gross before costs. Gap stops use the worse
observed open; scheduled time exits use the first open at/after the deadline.
Same-bar TP1/new break-even/TP2 order is ambiguous, never an automatic TP2 win.

The model subtracts two-sided fee/slippage allowances on entry notional.
Historical funding is charged/credited on remaining notional. Uncertain funding
ordering is bounded. Missing funding is **not** assumed zero: `costs_complete`
remains false and those trades are excluded from complete net metrics. Cost
estimates are configurable assumptions; account fees, fill quality and actual
leverage/account equity are not measured. R means initial price risk.

Historical V1/V2 terminal rows are not rewritten. Existing V1 open rows retain
their original stop and 60m/24h horizons; existing V2 rows keep those horizons but
receive the invalidation/ambiguity correctness fixes when subsequently checked.
Old target-touch statistics must not be mixed with V2.1/V3 net-R results.

## Reports and replay

```
python -m scalp_sentinel.report
python -m scalp_sentinel.replay --version v3 --start 2026-11-01T00:00:00Z --end 2026-11-08T00:00:00Z --settle-funding
```

The report is also logged hourly. It groups by version, symbol, side and regime,
plus per-version totals. It includes entry counts, ambiguity counts, lower/upper
mean net R, conservative profit factor, equal-risk sequence drawdown and longest
loss streak. This is not a leveraged portfolio equity curve. Shadow and primary
cohorts enforce their own budgets and cooldowns.

Replay reads observed compressed frames in timestamp order, rejects future data,
and never invents unavailable OI. A history gap ends that paper path as DATA_GAP,
not a win. Unresolved positions at the end of the window stay unresolved. Funding
can be settled through the read-only historical API with `--settle-funding`.
Replay cannot reproduce Telegram latency or actual fills and does not promote a
strategy. Use a later untouched window to evaluate a frozen parameter set;
repeat across regimes, and do not optimize and report on the same sample.
There is no validated backtest result yet: the new archive starts at deployment.

Tables: `scalp_signal_outcomes` (primary), `scalp_shadow_outcomes`,
`scalp_setup_claims`, `scalp_replay_frames` (gzip BYTEA), `scalp_assessments`,
`scalp_decisions`. Original `scalp_signal_performance` view columns are preserved.
Raw frames and rejection diagnostics are retained for ARCHIVE_DAYS; outcomes are
retained. The deployment preflight verifies additive migrations without dropping
old data. Configuration thresholds and cost assumptions accompany new outcomes.

## Failure handling and rollback

A setup is durably claimed before Telegram delivery. If delivery is uncertain,
it is never resent automatically. A matching committed outcome reconciles the
claim; otherwise primary delivery pauses and logs
`delivery_uncertain_primary_paused` until the Telegram/DB record is reconciled.
Shadow research may continue. This trades at-most-once delivery for avoiding
unmeasured duplicate alerts; Telegram has no idempotency key.

First rollback choice: keep `PRIMARY_STRATEGY=v2.1` and set
`SHADOW_ENABLED=false`; retain the corrected tracker. To return to periodic
operation, also set `RUN_CONTINUOUS=false` and restore cron `*/15 * * * *`.
Do not deploy an old tracker over open new-model trades: it cannot interpret
their deadlines and partial exits. `V2_ENABLED=false` invokes the historical
single-scan V1 entrypoint; use only with periodic operation after new-model
positions are drained. No automated rollback deletes data or changes secrets.
