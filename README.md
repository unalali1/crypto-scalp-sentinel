# Crypto Scalp Sentinel

## Production signal-only mode (`PRIMARY_STRATEGY=standard`)

The standard scanner checks closed Binance USD-M perpetual 5-minute candles on
each five-minute boundary, using 15-minute EMA 21/50 trend context. EMA 9/21/50
pullback and volume breakout/retest are independent LONG/SHORT setups. Open
interest, CVD, 1-minute triggers, swing cohorts and replay storage are not
required for these alerts. Existing `v2.1` and `v3` paths remain available for
rollback. Every assessed setup and rejection reason is logged; no setup means
no trade alert.
The existing 5m/15m directional confluence digest runs independently once
per 15-minute bucket from the same validated candles, after trade decisions.
Confluence is a context notification, not an entry/stop/target trade signal.

Keep `DRY_RUN=false` to send Telegram **paper signals** and track hypothetical
fills. The project uses public market-data endpoints only and never submits
exchange orders. `DRY_RUN=true` skips Telegram and PostgreSQL persistence.
The standard mode defaults to a 45-minute maximum paper hold and a 0.40R
round-trip cost cap (both configurable), with 1.5R/2.5R targets and break-even
tracking after TP1. Shared quote freshness, spread, liquidity, price drift and
deduplication gates still apply. `COOLDOWN_MINUTES` applies without the legacy
three/four-hour floor. The old replay archive is not written in this mode,
which prevents its unbounded disk growth. Existing archive rows require
separate, deliberate database cleanup.

Read-only Binance USDⓈ-M perpetual scanner for crypto and TradFi markets.
**No exchange orders and no OpenAI calls.** Default watchlist includes 17 crypto
pairs plus XAUUSDT (gold), XAGUSDT (silver) and BZUSDT (Brent crude). Thresholds
are unvalidated hypotheses.

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

## 2026-11-04 latency and selectivity update

The production V2.1 rules and the 90-second stale-trigger gate are unchanged.
An eligible candidate is now delivered as soon as its symbol's snapshot is
evaluated, instead of waiting for every other symbol, replay archive writes,
and context alerts. This favors a valid timely alert when other market-data
requests are slow. When several symbols qualify at once, the first completed
snapshot can consume a rolling alert slot before a later, stronger candidate;
the old global score sort could not be preserved without waiting. The delivery
quote is still checked at real wall-clock time.

Context-only confluence and price-zone detection runs after signal delivery on
one full scan per 15-minute bucket. Confluences and strong price zones are sent
as bounded digests; standalone Fibonacci levels keep their own digest. Context
sampling is less frequent, so a zone touched only between samples may not be
reported. It never creates a trade outcome.

V2.1 volume-required and trend-regime variants now run as separate paper-only
cohorts alongside the existing 15m-close confirmation and V3 cohorts. No
variant is promoted by its filter passing. The report adds no-entry counts
and mean net R per decided alert (zero for NO_ENTRY/INVALIDATED), alongside
the existing mean per resolved trade. Active and data-gap outcomes are
excluded from the decided denominator. New eligible-candle-age and tick
duration logs make expiry and throughput measurable in production.

The frozen paper promotion rules and read-only cohort audit are in
[docs/STRATEGY_PROMOTION.md](docs/STRATEGY_PROMOTION.md). No candidate is
promoted without a matched null comparison and untouched validation.

## Independent paper swing hypothesis

`python -m scalp_sentinel.swing_main` is a separate, paper-only process. It
has no order or Telegram path and does not change V2.1 trade notifications.
Run it as a separate Railway cron service at `5 */4 * * *` (UTC), with
`DATABASE_URL` pointed at the existing PostgreSQL service and pre-deploy
command `python -m scalp_sentinel.swing_verify`. It requires no trading key or
Telegram credentials. The worker refuses decisions outside 5–20 minutes after
a completed 4h bar; an interrupted cron does not replay a stale setup.

`SWING_SYMBOLS` defaults to the configured watchlist, but the swing service
only considers ordinary USD-M crypto perpetuals. TradFi perpetuals and pairs
with under 400 complete daily candles are excluded at this stage. The swing
client uses closed 1d/4h/1h candles, with the last complete UTC day's quote
volume for reproducible liquidity checks. Delayed OI never gates this price
and volume baseline. BTC daily trend is a mandatory market-context gate for
both directions, even when BTC is absent from `SWING_SYMBOLS`.

The frozen v1 hypothesis is a daily EMA50/200 aligned trend, a 4h pullback
near EMA20 and reclaim, a same-boundary 1h close confirmation, 4h relative
volume, unobstructed 1.5R path, and liquidity/spread/risk gates. All gates
are required; no score threshold is tuned to force a weekly alert quota.
At most one candidate is paper-recorded in a rolling 24h period; at most two
positions are pending/open, with a side-adjusted 4h return-correlation veto.
A seven-day same-symbol/side cooldown suppresses repeated setups. These caps
control alert volume and hypothetical portfolio exposure, not expected return.

The paper reference entry expires after two hours; 50% is modeled at 1.5R,
50% at 3R, and the remaining stop moves to entry after TP1. Maximum hold is
five days. Actual closed 1m bars, ambiguous intrabar order, gap exits, fee,
slippage and historical funding use the existing paper tracker. Suggested
0.5% per position and 1% aggregate account-risk figures are *references* in
the journal: no account balance, leverage, liquidation price or actual order
is available, so the scanner does not present these as enforced account risk.
Assessments and rejection reasons are stored separately from scalp decisions.
The scalp worker accepts an optional `CONTEXT_CHAT_ID` for its confluence and
zone digests. Trade Telegram alerts continue to use `TELEGRAM_CHAT_ID`; with
no separate chat ID, the existing destination is retained.

Historical replay can be run on at least 300 days of USD-M kline history:

```
python -m scalp_sentinel.swing_backtest --start 2025-01-01T00:00:00Z --end 2026-11-01T00:00:00Z --symbols BTCUSDT,ETHUSDT,SOLUSDT
```

Research diagnostics can replay the same frozen rules with adverse execution
assumptions and an RVOL-first *paper comparator*:

```
python -m scalp_sentinel.swing_backtest --start 2025-11-01T00:00:00Z --end 2026-11-01T00:00:00Z --symbols BTCUSDT,ETHUSDT,SOLUSDT --diagnostics --stress --compare-ranking
```

Each JSON log entry has a `kind`: `swing_replay_summary` (including coverage and
per-period aggregate), `swing_trade` (selected fill/cost/MFE/MAE),
`swing_candidate` (every eligible candidate and rejection reason), or
`swing_counterfactual` (an independently tracked rejected candidate). The
counterfactuals may overlap selected positions and **cannot** be added to the
portfolio return. `--stress` adds 10-minute/7.5 bp and 15-minute/7.5 bp
decision scenarios with higher fee/slippage assumptions. `--compare-ranking`
changes candidate order only in historical paper replay; the live paper worker
continues to rank by liquidity/spread. Detailed `replay()` results also retain
symbol/side summaries. Coverage counts show how many 4h decisions had sufficient
closed history; current `exchangeInfo` cannot include delisted historical
contracts, so the replay does not claim a complete point-in-time universe.
The previous holdout has already been inspected and must not be reused to
select a new rule. No diagnostic automatically promotes swing notifications.
For future untouched observations, the paper worker also stores an executable
candidate rejected by portfolio selection in the isolated
`swing-candidate-shadow` cohort. The existing shadow tracker resolves it with
the same closed 1m bars and funding accounting. Candidate shadows never count
toward the one-per-day selected paper cap, active exposure or Telegram alerts;
the original selection and rejection reason remain in `scalp_swing_assessments`.

### Swing research status (2026-11-04)

The 17-symbol 2025-11-01 to 2026-11-01 retrospective run produced the
following equal-risk *paper* results. Figures are mean net R per decided
alert, not account returns. The last third was a holdout with a five-day
embargo; it had only 2–4 signals per scenario.

| Frozen paper scenario | Development | Holdout |
| --- | ---: | ---: |
| Existing liquidity rank, 5m delay, 5 bp spread, 5/2 bp fee/slippage | 19; +0.209 R | 4; -0.535 R |
| 10m delay, 7.5 bp spread, 5/4 bp fee/slippage | 19; +0.021 R | 3; -0.627 R |
| 15m delay, 7.5 bp spread, 7/5 bp fee/slippage | 16; +0.319 R | 2; -0.963 R |
| Exploratory RVOL-first rank, original execution assumptions | 18; -0.024 R | 4; -0.535 R |

There were no historical 1m data gaps. The 15m result uses a different set of
selected trades because 16 candidates failed the price-drift gate; its higher
development mean is not evidence of improved execution. Under the existing
rank, all development selections were shorts. There was no multi-candidate
selection event in the holdout. The currently listed universe misses delisted
historical contracts. Historical spreads, quote execution and liquidation are
not observed. No ranking or exit rule was promoted; the swing service remains
paper-only while new untouched candidate outcomes accrue.

The first two-thirds is a development window and the last third is a holdout;
a five-day embargo excludes positions crossing that boundary. Replayed
decisions assume a five-minute delay, a fixed 5 bp historical spread, and a
later 1m open as an executable quote proxy; historical books/real fills and
liquidation are unavailable. Backtest and live paper outcomes are not
interchangeable. No strategy is promoted automatically; assess net R per
decided alert, missing data and drawdown before considering a separate swing
Telegram channel.

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
The volume and trend cohorts are also shadow-only. The 15m sampling cohort
still depends on the worker observing the exact quarter-hour start; compare
its coverage before drawing timing conclusions.

## Scanner diagnostics

Each evaluated symbol now logs its side-specific failed gates and each scan
logs aggregate gate pass/fail counts, prefilter rejection reasons, and data
pending counts. This separates a quiet market from unavailable inputs or a
specific strategy condition. Delayed 5m OI is isolated to the V3 shadow cohort;
it no longer blocks a v2.1 primary snapshot. A stale 15m OI input still fails
closed for v2.1 because its CVD/OI gate depends on that series.

## Execution funnel diagnostics

`python -m scalp_sentinel.report` now includes `last_30d_execution_funnel`:
per-cohort delivery decision reasons (including a separate last-24-hour count),
sent/reference-filled/decided outcomes,
mean net R per decided alert, and median delivery spread, quote age and
closed-candle age where recorded. An unresolved outcome or missing funding
costs is excluded from decided return; NO_ENTRY and INVALIDATED count as zero.
The assessment and decision denominators reflect their retention window and
are not a historical lifetime total. A new batch-written candidate-arrival
record counts scans with multiple primary candidates and how often the first
candidate ranked below another on score/RVOL. It does not establish that the
later candidate could have filled or earned more. For future primary decisions, logs also split snapshot age, evaluation-to-delivery,
quote fetch and quote age to diagnose stale triggers without relaxing the gate.
Recording happens after
time-sensitive delivery; the primary rule and Telegram routing do not change.
New quote metrics only appear on newly recorded signals.

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
mean net R per trade and per decided alert, conservative profit factor, equal-risk sequence drawdown and longest
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
