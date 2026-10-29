# Crypto Scalp Sentinel

Read-only crypto futures market scanner. Every 15 minutes it evaluates LONG and SHORT setups across a configured Binance USDT perpetual watchlist, using 15m trend, 5m market structure, taker-volume delta and open interest, relative volume, 1m entry confirmation, and ATR-based risk/reward. Signals are **hypothetical**, with no exchange orders.

**Safety defaults:** `DRY_RUN=true`; no Binance order API keys and **no OpenAI API calls**. Telegram delivery remains off until explicitly enabled.

## Railway configuration
Create a Railway service from this repository. A `railway.toml` sets the cron to `*/15 * * * *` UTC and runs `python -m scalp_sentinel.main`.

Set `DRY_RUN=true` for initial data tests. To activate notifications later, set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and PostgreSQL `DATABASE_URL` securely in Railway, then change `DRY_RUN=false`. Never commit secrets. A database is needed for durable cooldown when enabling real notifications.

The default 17-symbol watchlist is BTC, ETH, SOL, BNB, XRP, AVAX, LINK, NEAR, AAVE, ZEC, ENA, HYPE, SUI, DOGE, LTC, WLD and BIO. Trading criteria and thresholds are an **unvalidated hypothesis**, not financial advice or a backtested strategy.

## Tests
`python -m unittest discover -s tests -v`

## Important
The price displayed in a signal is the last CLOSED 1m candle, **not** a tradable quote. Stop and take-profit levels are model estimates. Candle, CVD and OI datasets can be stale or missing; the scanner fails closed in that case. Binance accessibility depends on deployment location.
## Automatic signal outcome journal

Every Telegram-delivered signal is saved in the existing PostgreSQL database
in `scalp_signal_outcomes`. The system does not send exchange orders.

At each 15-minute scan, the scanner fetches closed 1-minute futures candles
after Telegram delivery, starting with the next full minute. It catches up
missed intervals using a database cursor. The reference entry must be reached
within 60 minutes. Only after that simulated fill can a TP or stop be recorded.

- `tp1_success=true`: entry was traded, then TP1 was reached before stop.
- `tp2_success=true`: entry was traded, then TP2 was reached before stop.
- `STOPPED`: stop before TP1.
- `TP1_THEN_STOP`: TP1 was reached, then the original stop before TP2.
- `AMBIGUOUS_ENTRY`, `AMBIGUOUS_EXIT`, `TP1_AMBIGUOUS_EXIT`:
  OHLC candles cannot determine which level came first; no unearned wins.
- `NO_ENTRY`: no reference-price touch within 60 minutes.
- `TIMEOUT` / `TP1_TIMEOUT`: still unresolved 24h after simulated fill.

The view `scalp_signal_performance` exposes the outcome and the explicit
`tp1_success` and `tp2_success` booleans for historical analysis.

```sql
SELECT symbol, side, COUNT(*) AS signals,
       COUNT(*) FILTER (WHERE filled_at_ms IS NOT NULL) AS entries,
       COUNT(*) FILTER (WHERE tp1_success) AS tp1_hits,
       COUNT(*) FILTER (WHERE tp2_success) AS tp2_hits,
       COUNT(*) FILTER (WHERE status = 'STOPPED') AS stopped_before_tp1,
       COUNT(*) FILTER (WHERE status LIKE 'AMBIGUOUS%') AS ambiguous
FROM scalp_signal_performance
GROUP BY symbol, side
ORDER BY symbol, side;
```

These are hypothetical signal-path results, not real exchange fills or returns.
Fees, slippage and funding are excluded. Old Telegram alerts are not
automatically backfilled.

## V2 confirmation engine (2026-10-29)

Default: `V2_ENABLED=true`. Set `V2_ENABLED=false` to revert engine
selection without deleting the original implementation. Version is recorded per
delivered signal; V1 historical rows remain unchanged.

V2 requires 15m/1h/4h EMA trend agreement; a recently CLOSED 5m breakout;
subsequent 1m retest and rejection (never a breakout-only chase); strengthening
taker-volume delta and appropriate open interest; ATR-compatible stops; and
sufficient room to TP1 before confirmed hourly swing congestion. Short alerts
also require RVOL >= 1.0. Volume above MIN_RVOL boosts the six-condition score,
but is not mandatory for otherwise confirmed longs. Cooldowns: at least 180m
long / 240m short.

V2 trade journaling models break-even stop after TP1 (TP1_THEN_BREAKEVEN),
while V1 open entries retain their historical semantics. These remain paper
signals, not live fills or validated P&L. The closed 1m reference touch model
is unchanged and execution quality must be evaluated separately.
