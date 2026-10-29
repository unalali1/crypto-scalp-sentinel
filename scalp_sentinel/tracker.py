"""Record and resolve hypothetical signals using post-notification 1m futures candles.

A fill is only presumed when a later complete 1m candle trades through the
referenced entry. Intrabar ordering ambiguity is recorded, never called a win.
No orders or private exchange endpoints are used.
"""
import json
import logging
import time

log = logging.getLogger("sentinel.tracker")
MINUTE = 60_000
ENTRY_WINDOW_MS = 60 * MINUTE
MAX_HOLD_MS = 24 * 60 * MINUTE
ACTIVE = ("AWAITING_ENTRY", "OPEN", "TP1_HIT")

DDL = """CREATE TABLE IF NOT EXISTS scalp_signal_outcomes (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    signal_ts BIGINT NOT NULL,
    sent_at_ms BIGINT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    stop_price DOUBLE PRECISION NOT NULL,
    tp1_price DOUBLE PRECISION NOT NULL,
    tp2_price DOUBLE PRECISION NOT NULL,
    score INTEGER NOT NULL,
    checks_json JSONB NOT NULL,
    rvol DOUBLE PRECISION NOT NULL,
    cvd DOUBLE PRECISION NOT NULL,
    oi_change_pct DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'AWAITING_ENTRY',
    filled_at_ms BIGINT,
    tp1_at_ms BIGINT,
    tp2_at_ms BIGINT,
    stop_at_ms BIGINT,
    last_checked_ms BIGINT NOT NULL,
    finished_at_ms BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_version TEXT NOT NULL DEFAULT 'v1',
    UNIQUE(symbol, side, signal_ts)
)"""


def init_db(conn):
    conn.execute(DDL)
    conn.execute("ALTER TABLE scalp_signal_outcomes ADD COLUMN IF NOT EXISTS strategy_version TEXT NOT NULL DEFAULT 'v1'")
    conn.execute("""CREATE OR REPLACE VIEW scalp_signal_performance AS
        SELECT id, symbol, side, signal_ts, sent_at_ms, score, strategy_version,
               entry_price, stop_price, tp1_price, tp2_price,
               status, filled_at_ms, tp1_at_ms, tp2_at_ms, stop_at_ms,
               (tp1_at_ms IS NOT NULL) AS tp1_success,
               (tp2_at_ms IS NOT NULL) AS tp2_success,
               finished_at_ms, updated_at
        FROM scalp_signal_outcomes""")
    conn.commit()


def record_sent(conn, signal, sent_at_ms):
    """Called only after a successful Telegram send, in cooldown transaction."""
    first_full_minute = ((sent_at_ms + MINUTE - 1) // MINUTE) * MINUTE
    conn.execute(
        """INSERT INTO scalp_signal_outcomes
        (symbol, side, signal_ts, sent_at_ms, entry_price, stop_price,
         tp1_price, tp2_price, score, checks_json, rvol, cvd,
         oi_change_pct, last_checked_ms, strategy_version)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
        ON CONFLICT (symbol, side, signal_ts) DO NOTHING""",
        (signal.symbol, signal.side, signal.snapshot_ts, sent_at_ms,
         signal.entry, signal.stop, signal.tp1, signal.tp2, signal.score,
         json.dumps(signal.checks), signal.rvol, signal.cvd3,
         signal.oi_change_pct, first_full_minute - MINUTE,
         getattr(signal, "strategy_version", "v1")),
    )


COLUMNS = ("id", "symbol", "side", "sent_at_ms", "entry_price", "stop_price",
           "tp1_price", "tp2_price", "status", "filled_at_ms", "tp1_at_ms",
           "tp2_at_ms", "stop_at_ms", "last_checked_ms", "finished_at_ms",
           "strategy_version")


def touched(low, high, level):
    return low <= level <= high


def advance_trade(trade, candles):
    """Pure, deterministic state transition for ordered, closed 1m candles.

    Returns an updated copy. Candle data is the Binance kline array format.
    The entry candle must not contain any stop/target touch: such a candle
    cannot establish the correct sequence from OHLC data alone.
    """
    trade = dict(trade)
    short = trade["side"] == "SHORT"
    entry, stop = trade["entry_price"], trade["stop_price"]
    tp1, tp2 = trade["tp1_price"], trade["tp2_price"]
    expected = trade["last_checked_ms"] + MINUTE
    for candle in candles:
        opened = int(candle[0])
        if opened != expected:
            raise ValueError(f"noncontiguous 1m candles, expected {expected}, got {opened}")
        expected += MINUTE
        high, low = float(candle[2]), float(candle[3])
        if low > high or low <= 0:
            raise ValueError("invalid 1m candle")
        state = trade["status"]
        if state == "AWAITING_ENTRY":
            if opened >= trade["sent_at_ms"] + ENTRY_WINDOW_MS:
                trade["status"] = "NO_ENTRY"
                trade["finished_at_ms"] = opened
            elif touched(low, high, entry):
                # Entry was reached; only subsequent candles can establish a
                # defensible stop or target when the entry is intrabar.
                if (touched(low, high, stop)
                        or (low <= tp1 if short else high >= tp1)):
                    trade["status"] = "AMBIGUOUS_ENTRY"
                    trade["finished_at_ms"] = opened
                else:
                    trade["status"] = "OPEN"
                    trade["filled_at_ms"] = opened
        elif state in ("OPEN", "TP1_HIT"):
            if opened >= trade["filled_at_ms"] + MAX_HOLD_MS:
                trade["status"] = "TP1_TIMEOUT" if state == "TP1_HIT" else "TIMEOUT"
                trade["finished_at_ms"] = opened
            else:
                effective_stop = (entry if state == "TP1_HIT" and
                                  trade.get("strategy_version", "v1") == "v2" else stop)
                stop_hit = high >= effective_stop if short else low <= effective_stop
                tp1_hit = low <= tp1 if short else high >= tp1
                tp2_hit = low <= tp2 if short else high >= tp2
                if state == "OPEN":
                    if stop_hit and tp1_hit:
                        trade["status"] = "AMBIGUOUS_EXIT"
                        trade["finished_at_ms"] = opened
                    elif stop_hit:
                        trade["status"] = "STOPPED"
                        trade["stop_at_ms"] = opened
                        trade["finished_at_ms"] = opened
                    elif tp1_hit:
                        trade["tp1_at_ms"] = opened
                        if tp2_hit:
                            trade["tp2_at_ms"] = opened
                            trade["status"] = "TP2_HIT"
                            trade["finished_at_ms"] = opened
                        else:
                            trade["status"] = "TP1_HIT"
                else:
                    if stop_hit and tp2_hit:
                        trade["status"] = "TP1_AMBIGUOUS_EXIT"
                        trade["finished_at_ms"] = opened
                    elif tp2_hit:
                        trade["tp2_at_ms"] = opened
                        trade["status"] = "TP2_HIT"
                        trade["finished_at_ms"] = opened
                    elif stop_hit:
                        trade["stop_at_ms"] = opened
                        trade["status"] = ("TP1_THEN_BREAKEVEN" if
                                           trade.get("strategy_version", "v1") == "v2"
                                           else "TP1_THEN_STOP")
                        trade["finished_at_ms"] = opened
        trade["last_checked_ms"] = opened
        if trade["status"] not in ACTIVE:
            break
    return trade


def _persist(conn, trade):
    conn.execute(
        """UPDATE scalp_signal_outcomes SET
        status=%s, filled_at_ms=%s, tp1_at_ms=%s, tp2_at_ms=%s,
        stop_at_ms=%s, last_checked_ms=%s, finished_at_ms=%s,
        updated_at=NOW() WHERE id=%s""",
        (trade["status"], trade["filled_at_ms"], trade["tp1_at_ms"],
         trade["tp2_at_ms"], trade["stop_at_ms"], trade["last_checked_ms"],
         trade["finished_at_ms"], trade["id"]),
    )
    conn.commit()


def track_signals(database_url, client, now_ms=None):
    """Resume every outstanding signal from its saved 1m candle cursor.

    Binance allows up to 1000 candles per request; progress is committed
    after every page, and later cron runs continue any remaining backlog.
    """
    import psycopg
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    last_closed_open = ((now_ms - 2000) // MINUTE - 1) * MINUTE
    with psycopg.connect(database_url, connect_timeout=8) as conn:
        init_db(conn)
        rows = conn.execute(
            "SELECT " + ",".join(COLUMNS) +
            " FROM scalp_signal_outcomes WHERE status IN (%s,%s,%s)"
            " ORDER BY sent_at_ms ASC LIMIT 200", ACTIVE,
        ).fetchall()
        resolved = 0
        for row in rows:
            trade = dict(zip(COLUMNS, row))
            try:
                for _ in range(4):  # bounded work per signal; next cron resumes
                    start = trade["last_checked_ms"] + MINUTE
                    if start > last_closed_open or trade["status"] not in ACTIVE:
                        break
                    end = min(last_closed_open, start + 999 * MINUTE)
                    bars = client.get(
                        "/fapi/v1/klines", symbol=trade["symbol"],
                        interval="1m", startTime=start,
                        endTime=end + MINUTE - 1, limit=1000,
                    )
                    bars = [b for b in bars if start <= int(b[0]) <= end]
                    if not bars:
                        log.warning("no_history symbol=%s id=%s", trade["symbol"], trade["id"])
                        break
                    previous = trade["status"]
                    trade = advance_trade(trade, bars)
                    _persist(conn, trade)
                    if trade["status"] not in ACTIVE:
                        resolved += 1
                        log.info("outcome id=%s symbol=%s side=%s result=%s tp1=%s tp2=%s",
                                 trade["id"], trade["symbol"], trade["side"],
                                 trade["status"], bool(trade["tp1_at_ms"]),
                                 bool(trade["tp2_at_ms"]))
                        break
                    if len(bars) < 1000:
                        break
            except Exception as exc:
                conn.rollback()
                log.warning("track_failed id=%s symbol=%s kind=%s",
                            trade["id"], trade["symbol"], type(exc).__name__)
        log.info("tracking_finished active_checked=%d resolved=%d", len(rows), resolved)
