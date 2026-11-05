# Architecture notes — Phase 0 recon

Snapshot: `main` at `9a3961787297ffb95c0690e11cbfe19916cae2bd` (2026-11-05). This is a code map, not a performance claim. No strategy behavior is changed here.

## Runtime and data flow

1. `scalp_sentinel.main.main` loads `Config.from_env()`, validates startup requirements, opens a PostgreSQL `Journal`, acquires the worker lock, and runs `Runner.tick` once or continuously. `railway.toml` defines the deployment command. `verify.py` is the preflight entry point.
2. `BinanceClient` calls public USDⓈ-M REST endpoints. `symbol_info` supplies filters; `snapshot` retrieves closed 1m/5m/15m/1h/4h bars, market/flow and open-interest inputs, book quote, and freshness metadata. A full symbol scan runs on a 5m bucket; watched/pending symbols are rescanned between full passes. `DataPending` keeps incomplete observations out of decisions.
3. `engine.evaluate` / `candidate` / `_v2_candidate` produce V2.1 candidates. `research.candidate_v3` and `v2_shadow_variants` produce research candidates. `Runner.tick` assesses the completed symbol futures as they arrive. `Runner.deliver` applies cohort-local rolling count, active exposure, cooldown, optional V3 correlation, then fetches a fresh `/fapi/v1/ticker/bookTicker` and calls `execution.prepare` for spread, drift and estimated cost checks.
4. Before primary Telegram delivery, `Journal.claim` records the setup. `notifier.format_signal` and `send_telegram` format/send the primary message; `Cooldowns.mark_sent` and `tracker.record_sent` persist the sent paper trade. Uncertain primary delivery is marked `uncertain`; a pending attempt pauses subsequent primary sends. Shadow cohorts go to `Journal.add_shadow` without trade alerts. `scalp_decisions` and `scalp_candidate_arrivals` capture reasons and arrival order. Delivery occurs as candidate work completes, so the winner of a constrained slot can depend on completion order.
5. `Journal.archive` stores compressed replay frames. `Journal.settle_primary` and `track_shadows` fetch subsequent closed 1m bars, call `tracker.advance_trade`, then fetch historical funding through `journal.funding`. `performance.outcome_bounds` calculates gross and estimated fee/slippage R bounds; `performance.settle_funding` applies historical rates and resolves `costs_complete`. `report.py` and `promotion.py` read outcomes for descriptive reports only; they never set `PRIMARY_STRATEGY`.
6. `confluence.py` and `price_zones.py` derive context digests; `Runner.deliver_confluences` and `deliver_price_zones` deliver them to the configured context destination and journal their state. They are separate from trade alerts.

## Entry, exit, uncertainty and economics

- `execution.prepare` is the live scalp decision-time quote/cost gate. `journal.paper_trade` freezes the paper setup, validity window and cost assumptions into `journal_json`. `tracker.advance_trade` advances only with ordered, contiguous closed 1m candles: an entry-level touch is a presumed paper fill, not proof of an exchange fill. It marks entry/exit bars with unknowable intrabar ordering as `AMBIGUOUS_*`, and missing contiguous observations as `DATA_GAP` in the callers. Stops, TP1, TP2, breakeven after TP1 and timeouts are here.
- `performance.outcome_bounds` uses the stored fee and slippage bps on both sides and returns an R interval. It does not silently turn unresolved funding into zero. `journal.funding` obtains public `/fapi/v1/fundingRate`; `performance.settle_funding` applies funding with an interval for same-minute timing ambiguity. `performance.summarize` counts `NO_ENTRY` and `INVALIDATED` at zero R per decided alert, while excluding unresolved gaps from decided results.
- The book-touch fill assumption, synthetic historical quotes and inability to order events within a 1m bar limit inference from paper results. Do not compare a backtest's simulated fill with a real fill without labeling that difference.

## Shared functions and differences between paths

| Path | Decision | Execution gate | Paper outcome and economics | Distinct inputs |
| --- | --- | --- | --- | --- |
| Live scalp | `engine.evaluate`, `research.candidate_v3` | `execution.prepare` | `journal.paper_trade`, `tracker.advance_trade`, `performance.outcome_bounds`, `journal.funding` / `performance.settle_funding` | Current REST snapshots and a fresh quote just before delivery; actual Telegram timing. |
| Observed-frame replay | Same `engine.evaluate` or `candidate_v3` | Same `execution.prepare` | Same `paper_trade`, `advance_trade`, optional `journal.funding` | Archived frames, archived book, `replay.validate_frame`; does not reproduce notification latency or live quote fetch. |
| Live swing shadow | `swing_engine.evaluate_swing` | `swing_execution.prepare_swing` | Same `paper_trade`, `advance_trade`, `journal.funding` | `swing_market.live_snapshot`; independent `SwingRunner`, symbol set and budget/cooldown. |
| `swing_backtest` | Same `evaluate_swing` | Same `prepare_swing` | Same `paper_trade`, `advance_trade`, `performance.settle_funding` | Historical REST bars, synthetic spread/book and configurable decision delay; historical funding; limited by data coverage. |

`swing_engine.daily_direction` contains the BTC daily direction gate. `swing_market.validate_bars`, `swing_backtest.closed_asof` and `replay.validate_frame` are relevant to closed-bar/as-of checks. Sharing a decision function does not make the surrounding selection, book, delivery timing or funding availability identical.

## Configuration

`Config.from_env` reads: `SYMBOLS`, `BINANCE_BASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`, `DRY_RUN`, `TIMEZONE`, `MIN_QUOTE_VOLUME_USDT`, `MAX_SPREAD_BPS`, `MIN_RVOL`, `SCORE_THRESHOLD`, `MIN_STOP_ATR`, `MAX_STOP_ATR`, `COOLDOWN_MINUTES`, `MAX_SIGNALS_PER_RUN`, `V2_ENABLED`, `RUN_CONTINUOUS`, `PRIMARY_STRATEGY`, `SHADOW_ENABLED`, `ENTRY_WINDOW_MINUTES`, `MAX_HOLD_MINUTES`, `MAX_ENTRY_DRIFT_ATR`, `PAPER_FEE_BPS`, `PAPER_SLIPPAGE_BPS`, `MAX_COST_R`, `MAX_OPEN_SIGNALS`, `CORRELATION_THRESHOLD`, `ARCHIVE_DAYS`, `BREAKOUT_RVOL`, `MIN_DELTA_RATIO`, `MIN_EFFICIENCY`, `CONTEXT_CHAT_ID`. `swing_main` also reads `SWING_SYMBOLS` (and `DATABASE_URL`, `BINANCE_BASE_URL`). CLI report/replay tools read `DATABASE_URL`; the backtest's dates, symbols, delay, spread, fees and slip are CLI arguments. Do not place real credential values in committed config or command output.

## PostgreSQL objects

| Object | Owner / purpose |
| --- | --- |
| `scalp_signal_cooldowns` | `storage.Cooldowns`: primary symbol/side send cooldown. |
| `scalp_signal_outcomes`, `scalp_signal_performance` view | `tracker.init_db`: primary paper positions and read view. |
| `scalp_setup_claims` | `Journal`: setup claim/delivery status, including uncertain attempts. |
| `scalp_shadow_outcomes` | `Journal`: paper shadow and swing positions with cohort labels. |
| `scalp_replay_frames` | `Journal`: compressed observed frames for replay. |
| `scalp_assessments`, `scalp_swing_assessments` | `Journal`: scalp/swing assessments. |
| `scalp_decisions`, `scalp_candidate_arrivals` | `Journal`: selection and delivery reasons, arrival order. |
| `scalp_confluence_alerts`, `scalp_price_zone_alerts`, `scalp_price_zone_state` | `Journal`: context delivery and observed zone state. |

Schema initialization is in `storage.py`, `tracker.py`, `journal.py`; `verify.py` and `swing_verify.py` perform startup/preflight checks. Schema changes in later phases require additive migrations and updated preflight verification.

## Phase 0 credential and ignore audit

- Scope: the 51 commits reachable from current `main`, plus 50 additional commits reachable from 16 other listed branches (101 distinct commits in all); 161 distinct text blob revisions. Existing uncommitted local working-tree files were scanned separately. TruffleHog v2.2.1 ran on the local three-commit checkout; its 18 known-secret regex rules and additional Telegram/PostgreSQL/GitHub/Railway token patterns were applied to the reachable GitHub blob revisions. Commit messages were also checked for the additional patterns. **No matches in this defined scan**; no candidate values are recorded in this note. The full remote scan was pattern based, not a full TruffleHog entropy/verification scan. Deleted refs, inaccessible PR refs, external deployment logs and deployed environment variables are outside scope; absence of a match is not proof of no exposure.
- The tracked `.env.example` contains example settings; never substitute live values there. The current `.gitignore` covers exact `.env`, `__pycache__/`, `*.pyc`, `*.db`, `.pytest_cache/`, `.venv/`. It does **not** cover `.env.local` / other `.env.*`, `*.sqlite*`, `*.log`, CSV/JSON exports or downloaded archive/cache directories. Address those gaps in a later authorized change; this phase changes documentation only.
- If any real value was ever committed on any ref or exposed in logs, screenshots, PRs or shared configuration, rotate the **Telegram bot token** with BotFather and the **PostgreSQL password/connection URL** at the provider, then update Railway's environment. Rotate any exposed Railway, GitHub, OpenAI or other service token as applicable; revoke any exchange credential if one was ever introduced. Current scanner should need only public Binance endpoints and no trading credential. Given this scan found no concrete leaked value, rotation is a precaution if prior exposure cannot be ruled out, not a claim of a confirmed leak.
