# Crypto Scalp Sentinel

Read-only crypto futures market scanner. Every 15 minutes it evaluates LONG and SHORT setups across a configured Binance USDT perpetual watchlist, using 15m trend, 5m market structure, taker-volume delta and open interest, relative volume, 1m entry confirmation, and ATR-based risk/reward. Signals are **hypothetical**, with no exchange orders.

**Safety defaults:** `DRY_RUN=true`; no Binance order API keys and **no OpenAI API calls**. Telegram delivery remains off until explicitly enabled.

## Railway configuration
Create a Railway service from this repository. A `railway.toml` sets the cron to `*/15 * * * *` UTC and runs `python -m scalp_sentinel.main`.

Set `DRY_RUN=true` for initial data tests. To activate notifications later, set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and PostgreSQL `DATABASE_URL` securely in Railway, then change `DRY_RUN=false`. Never commit secrets. A database is needed for durable cooldown when enabling real notifications.

The first watchlist is BTC, ETH, SOL, BNB, XRP, AVAX, LINK, NEAR, AAVE, ZEC and ENA. Trading criteria and thresholds are an **unvalidated hypothesis**, not financial advice or a backtested strategy.

## Tests
`python -m unittest discover -s tests -v`

## Important
The price displayed in a signal is the last CLOSED 1m candle, **not** a tradable quote. Stop and take-profit levels are model estimates. Candle, CVD and OI datasets can be stale or missing; the scanner fails closed in that case. Binance accessibility depends on deployment location.