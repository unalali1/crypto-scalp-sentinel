# Paused at user's request — 2026-11-11

Resume the attached professional scalp task, not a new analysis-only task. User asked to pause here and continue in 10 minutes. No merge, PR or production deployment has happened. This is an unfinished WIP checkpoint; do not deploy it before review/tests.

## Context

- Repo unalali1/crypto-scalp-sentinel; base main 3b2e8ed4e37acd89621ed7db5d3802cea67a1dfe.
- Branch feat/standard-forward-risk.
- Railway project ca52bd3f-3327-4200-8065-8dab754060ce; production environment 9848a8f2-dbab-43d5-bf09-c810001b8ce5; scanner 94b3e8e2-5de5-45d4-b429-4b48c86d5d38.
- Production successful deployment 6a8415e0-0915-49fc-94c6-283cd9537ee0, main 3b2e8ed, standard. GitHub main auto-deploy, predeploy python -m scalp_sentinel.verify; start python -m scalp_sentinel.main; 1 replica, zero overlap. Existing PostgreSQL preserved. Railway OAuth returns variable names only, no values; local CLI absent. No DB credentials accessed.
- Authorized: separate branch, meaningful tests, PR, safe deploy after tests; no real exchange orders, paid services, upgrades, DB reset or deletions of old services. Preserve 4h/1d confluence, suppress short confluence. No large backtest. User has not authorized changing that scope.
- Baseline Railway logs 2026-11-10T09:45Z to 2026-11-11T09:45Z: 288 scans, 146 eligible, 48 delivered, zero scan failures. Outcome events in that window: STOPPED 22, TIMEOUT 14, NO_ENTRY 7, TP1_TIMEOUT 2, TP2_HIT 2, TP1_THEN_BREAKEVEN 1. These are event-window counts, not necessarily the same sent cohort. Supplied 153 eligible / 21 stopped baseline is a different unverified window.
- Baseline tests: 96 passed after pip install -r requirements.txt. No AGENTS.md in cloned repo. User instructions in conversation apply. No subagents authorized.

## Findings

Standard sends candidates individually in as_completed order, has no cross-universe ranking, correlation only V3, standard bypasses report/prune. Current execution checks bid/ask but uses close limit reference and symmetric generic costs. Standard open outcomes tracked only every 5m. Existing durable claims/cooldown and worker advisory lock useful; preserve conservative Telegram uncertain behavior.

## Implemented WIP (not yet validated)

- standard_quality.py: TREND/RANGE/SHOCK from closed 15m EMA slope/efficiency and 5m prior ATR; explanatory 10-component 0–100 rank; BTC/peer timestamp matched return correlation, support/resistance room; total/correlated risk and equity-relative 10x/15x margin sizing.
- Config flags STANDARD_ENHANCED (from_env true; dataclass false), REGIME_FILTER_MODE shadow, scan budget45s, example risk .35%, total1%, correlated .5%, daily loss2%, paired shadows true. Daily digest default false, hour08 local; still needs enable in Railway if delivering daily summary.
- standard.py: causal EMA touch, controlled pullback/no-chase, pre-breakout volume baseline and up to3 retest candles; optional enforced regime gate, default observations only; retains primary versions ema-pullback/breakout-retest and adds journal strategy_revision standard-v2.
- execution.py: passive tick-rounded bid/ask-constrained limit, quantities validated, fee/exit half-spread/slip separated; reference vs limit vs indicative market recorded. No entry spread double count. Market paper is paired delayed scenario.
- main.py: bounded pooled scan then global ranking and one deliver call; fresh per-send quote, portfolio risk, logging; minute-level maintenance + 4h/1d confluence retained; config rollback STANDARD_ENHANCED=false. All decisions saved in scan metrics as strategy-specific records; old decisions table also populated.
- journal.py: additive scalp_standard_scans and scalp_daily_reports, daily realized loss guard; primary peers include journal risk; paired hold30, hold60, market-delay shadows; bounded tracking loop budgets and standard scan/shadow retention.
- tracker.py/performance.py: 30s/manual delay then complete next minute, limit penetration tick, delayed market fill at next full minute open+halfspread+slip; gap-aware stop; v2 asymmetric actual notional costs; legacy cost models preserved. Funding remains separate. Needs careful independent review for ambiguity/missing data/costs.
- forward_report.py: bounded last24h aggregate and durable daily snapshot with optional at-most-once Telegram digest; strategy and long/short detail, exclusions, cost/drift, paired shadows.
- notifier.py: v2 Turkish signal message with quality not probability, executable limit ref, market scenario, risk/margin percentages, expiry and net RR.
- binance_client.py: remove unbounded retry-after sleep, shared rate-limit cooldown and snapshot thread deadline; review retry/network behavior.

## Exact next step and current test failure

Latest test run: 96 run, 1 ERROR, 95 pass. Failure in test_standard.StandardTests.test_worker_runs_once_per_closed_bar_without_flow_data: its fake Client.scalp_snapshot takes 3 positional args, enhanced now passes deadline as fourth. This test should explicitly exercise legacy rollback with standard_enhanced=False (existing fixtures lack book quantities) AND new enhanced runner integration tests must cover real new signature and quantities. Do not merely weaken tests.

## Remaining work before deploy

1. Thoroughly review implementation, fix defects and add deterministic regression tests: global late-best ranking/tie order, risk/correlation/fresh peer data, passive and delayed market costs/no double count, invalid/stale/NaN quotes, unclosed/future candles, stop+TP ambiguity/gaps, entry penetration, daily limit/digest dedup, restart claims/cooldowns, retention, 4h/1d suppression.
2. Potential issues to investigate:
   - Market shadow fill changes entry_price only within JSON shadow (okay); targets stay fixed, R changes. Verify risk and cost reporting consistent; tick rounding of delayed fill not yet done.
   - Daily loss currently ignores unresolved funding adjustment and missing netR; characterize/report conservatively. No account balance known.
   - Shock filter default shadow only lowers score (does not hard throttle); decide sensible predeclared safety handling without overfit and explain.
   - Scores refreshed before send; initial rank from snapshot books vs refreshed quote changes should not silently claim exact final global optimum.
   - Top-book quantity checked >0 but sizing/depth capacity not enforced with unknown capital. No invented size.
   - Limit fill delay can skip first minute by design; gaps in invalidation and market entry timeframe tests needed.
   - Existing tracker loops up to4 pages can exceed newly added outer budget; catch_up2 pages, context17x2 may extend cycles; ensure bounded total below300s.
   - Missing/late snapshots are excluded for correlation instead of reusing stale cache (intentional fail closed).
   - If all snapshots fail no durable scan metric yet; failure observability should be explicit.
   - Ambiguous TP1 gap lower bound and funding marking require review; uncertain outcomes never label wins.
   - standard daily report snapshot is frozen at08 previous local day; incomplete outcomes remain labeled in snapshot, rolling log updates.
   - Add user-facing read-only report CLI or integrate new report with existing report.py if useful; docs/STRATEGY_PROMOTION.md protocol retained, new cohorts must be accounted for separately.
   - pending_delivery early return currently doesn't record per-candidate blocked reasons.
3. Real PostgreSQL integration recommended. Container has no postgres binaries/sudo; can download/extract PostgreSQL package locally or use temporary integration service in GitHub Actions (no Railway paid/new service). Existing verify performs live additive migration before deploy. Never reset existing DB.
4. Add concise audit/rules/rollback/validation docs, no profit claims. Full existing tests + new tests, meaningful live read-only Binance smoke (no Telegram), then PR, CI, merge with expected SHA, verify Railway deploy and several 5m cycles; ideally observe genuine signal + outcome/daily records, never send fake signals.
5. Need choose enable DAILY_SUMMARY_ENABLED=true on Railway if appropriate (daily requested configurable). No secret values printed. All current changes unshipped.
6. Final report in Turkish covering concrete issues/files/rules/risk/tests/commit&PR/deployment/first operational evidence/remaining assumptions. Do not claim measured profitability.

Local path at pause: /workspace/scratch/8da56bcaa3c1/crypto-scalp-sentinel.
