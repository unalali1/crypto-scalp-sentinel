# Standard forward release — 11 November 2026

## What changed and why

The former standard worker delivered each eligible candidate as its HTTP task finished. Standard mode bypassed the regular reporting and retention path. Its book check did not distinguish passive limits from aggressive entry economics. The current standard path ranks the whole bounded collection and reports cost-aware hypothetical outcomes. It supplies independent trading ideas; it does not infer that the user entered every recommendation.

The 24-hour log window 2026-11-10 09:45 UTC through 2026-11-11 09:45 UTC had 288 scans, 146 eligible setups and 48 sent signals. Outcome events in that window were 22 STOPPED, 14 TIMEOUT, 7 NO_ENTRY, 2 TP1_TIMEOUT, 2 TP2_HIT, 1 TP1_THEN_BREAKEVEN. Event-window outcomes are not an audited matched cohort of the same 48 sends. They are operational evidence only, not parameter-fitting data.

## Entry rules

- EMA pullback: closed 15m EMA21/50 directional context, closed 5m EMA9/21/50 alignment, a preceding EMA21 touch using each candle's own contemporaneous EMA, closed reaction/reclaim, controlled distance and range.
- Breakout/retest: a 5m level derived from 12 candles before the breakout; breakout volume compared with the preceding 20 bars (excluding the breakout); up to three following candles to retest/hold the frozen level; a closed reaction within 1 ATR.
- Structural swing stop with 0.15 ATR buffer and at least 1 ATR distance; existing configurable stop/ATR/liquidity/spread gates retained.
- A six-times-prior-ATR extreme movement pauses new setups for the affected symbol until it leaves the last-three-candle window. This is a predeclared safety limit, not a fitted profitability result.
- TREND/RANGE/SHOCK use EMA slope, price efficiency and prior ATR. REGIME_FILTER_MODE=shadow records the stricter TREND-only hypothesis; it does not enforce it. Regime contributes to ranking. Only a separate forward validation can justify enforcing it.
- 1m bars resolve paper execution and exits. Additional 1m technical entry confirmation is not a mandatory filter.

## Ranked ideas and hypothetical sizing

A 45-second collection budget precedes one ranking batch. A new bookTicker batch refreshes prices before ranking; a further single-symbol quote validates each send. Every distinct candidate passing the strategy and executable-quote checks is eligible for notification, in quality order. Network failures/deadlines are counted; a partially collected universe is explicitly incomplete.

The 0–100 score sums ten disclosed components: trend 15, structure room 15, volume 10, entry distance 10, liquidity 10, volatility 10, estimated net reward/risk 10, costs 10, BTC alignment 5, diversification 5. This is an uncalibrated ordinal quality score, not a success probability. Snapshot ranking cannot guarantee the same order after subsequent market movement.

At the user's request on 11 November, standard primary notifications have no daily-loss gate, aggregate/correlated portfolio gate, open-position cap, same-symbol open-position veto, signal-count quota or account-style cooldown. An existing paper recommendation is not evidence of a real position. Closed losses and incomplete paper results remain recorded for performance analysis and never stop later ideas. Retired DAILY_LOSS_LIMIT_PCT, MAX_TOTAL_RISK_PCT and MAX_CORRELATED_RISK_PCT settings are no longer read. Legacy V2.1/V3/shadow throttles remain scoped to those paths.

Five-minute returns use matched timestamps for the explanatory diversification score only; missing peer correlation can lower that score but does not veto a standard idea. Each setup identity (symbol, direction, strategy, closed trigger candle) is delivered at most once. Telegram uncertainty still pauses delivery for reconciliation; failed, expired or invalid quotes are not sent. Journal metadata marks new ideas with advisory_only=true and notification_policy=independent-ideas-v1, allowing later comparison with earlier notification rules.

Illustrative notional/equity = risk_fraction / (stop_fraction × (1 + estimated_cost_R)), using a hypothetical 0.35% risk per idea. Initial margin/equity is that notional multiple divided by leverage, shown for 10x and 15x. These are sizing examples, not enforced account limits. No account balance, monetary size, exchange order or automatic leverage change is invented.

## Execution and costs

Primary entry is a tick-rounded passive limit constrained by bid/ask and the original close reference. Both book quantities must be positive and finite; quantities and top-book notional are saved. Without account size or full depth, capacity cannot be certified.

- Closed trigger age <=90 seconds; book transaction age <=15 seconds; future quote tolerance 2 seconds.
- Existing ATR price-drift limit and entry expiry remain enforced.
- Paper entry starts at the first complete 1m bar after a 30-second manual-delay allowance (typically 30–90 seconds). A price touch alone is insufficient: one tick of penetration is required. This is a conservative OHLC proxy, not proof of queue execution.
- Gaps through a passive entry and stop are ambiguous adverse exposure, not silently NO_ENTRY. Same-bar stop/target ordering stays bounded; it is never labeled a confirmed win.
- Passive entry embeds no spread/slippage charge. Exit allowance includes half the observed spread plus configured slippage once. Fees conservatively use the configured taker allowance on both legs and actual weighted exit notional. Funding is settled separately from exchange history.
- Delayed-market shadow uses the later minute open plus half-spread/slippage, rounded adversely to a tick, and rejects excessive drift. Its entry friction is embedded in fill price and is not charged twice. Fixed targets and stop are retained; realized R uses the resulting actual modeled entry risk.
- Spread and slippage remain estimates; future depth/spread/latency may be worse.

Reference exits remain half at 1.5R, half at 2.5R, with the remaining stop moved to entry after TP1. Break-even is gross price break-even. Default hold is 45 minutes. Paired 30/60-minute and delayed-market shadows start from the same notification timestamp and are never additional Telegram recommendations.

## Persistence and reporting

Additive tables: scalp_standard_scans and scalp_daily_reports. Existing outcomes, claims, legacy cooldown records and advisory worker lock remain. Each sent idea receives an independent hypothetical outcome, including multiple ideas in the same symbol. Hourly rolling 24-hour log reports and daily immutable local-calendar snapshots include scan/eligible/sent/blocked counts, entries, no-entry share, outcomes, ambiguity, costs, drift, net-R bounds, PF, equal-risk drawdown/loss streak, separate strategy/revision and LONG/SHORT groups. Missing costs and active trades are excluded from return averages. NO_ENTRY/INVALIDATED contribute zero to per-decided-alert return. Ambiguous bounds are retained but not called wins.

DAILY_SUMMARY_ENABLED controls one daily Telegram digest at DAILY_SUMMARY_HOUR (default 08:00 in TIMEZONE). Claim-before-send gives at-most-once attempts across restarts. A timeout is marked uncertain and never automatically resent. It cannot promise exactly-once delivery; manual reconciliation is needed for uncertain primary sends.

Scan diagnostics retain ARCHIVE_DAYS (default 30); only settled standard comparison shadows expire after that window. Primary outcomes and compact daily reports remain. No candle replay archive is added for standard mode. Earlier model results are not retroactively rewritten.

## Operations, validation and rollback

The same Railway scanner/PostgreSQL are used; no additional paid service. Public GET-only Binance endpoints. Rate limits set a shared cooldown without unbounded Retry-After sleeps. Snapshot collection has a deadline; tracking/settlement are bounded batches with persistent cursors. Cycle/scan durations are logged. Closed 4h/1d confluence remains; short-timeframe confluence remains suppressed.

GitHub Actions runs the complete unittest suite, including isolated disposable PostgreSQL integration tests for additive migrations, claims, cooldowns, worker locks, reports, retention and uncertain Telegram delivery across restart. Telegram calls in tests are mocked. Railway predeploy runs deterministic tests and validates the live additive schema; isolated database tests skip unless TEST_DATABASE_URL is present.

The outcome uniqueness key now includes strategy_version, so EMA pullback and breakout/retest ideas for the same symbol, direction and candle each retain their own result. Migration installs the new unique index before dropping the old three-column restriction, in one transaction; no outcome rows are deleted or rewritten.

Configuration rollback: keep PRIMARY_STRATEGY=standard and set STANDARD_ENHANCED=false, then redeploy. This returns original strategy/execution rules; independent-idea delivery, historical records, ongoing trade policies, global batching and reporting remain. A code rollback must retain the updated tracker persistence key. Pre-advisory builds use an incompatible three-column ON CONFLICT target and must not be redeployed unchanged after this migration. Do not delete outcomes to restore the old key.

Profitability remains unproven. Preserve STRATEGY_PROMOTION.md: no automatic strategy promotion, no tuning to this short baseline, no historical backtest project. New cohorts/revisions must be counted in multiple-comparison correction and independently satisfy the existing forward sample/null gates.

## Prospective technical-score experiment
`STANDARD_MIN_SCORE=60` filters enhanced primary notifications after fresh-quote execution checks and final ranking. Below-threshold ideas are atomically claimed as primary/filtered and recorded in `standard-score-v1-low`; they are not sent or retried as high-scoring ideas. Both groups retain the same entry, stop, targets and cost model. Active silent ideas participate in diversification context. Silent experiment history survives ordinary shadow pruning.

Messages show technical suitability and the two lowest components normalized by their maximum points. The formula is unchanged. Daily reports include a rolling 30-day prospective comparison, excluding legacy unfiltered records. Initial review requires both seven elapsed days and 100 resolved high-group outcomes. This is observational paper evidence, not a randomized experiment or a profitability guarantee. No account-level risk gates or automatic orders are introduced.
