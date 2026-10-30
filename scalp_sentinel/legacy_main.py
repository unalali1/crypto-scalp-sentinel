"""Railway Cron entrypoint: one read-only scan, then exit."""
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .binance_client import BinanceClient, BinanceError
from .config import Config
from .engine import evaluate
from .notifier import format_signal, send_telegram
from .storage import Cooldowns
from .tracker import track_signals

log = logging.getLogger("sentinel")


def scan_one(client, symbol, details, config, now_ms):
    price_filter = next((f for f in details["filters"] if f["filterType"] == "PRICE_FILTER"), None)
    if not price_filter or float(price_filter["tickSize"]) <= 0:
        raise RuntimeError(f"{symbol} invalid price tick")
    snapshot = client.snapshot(symbol, now_ms)
    return evaluate(symbol, snapshot, price_filter["tickSize"], config)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    log.info("engine_version=%s", "v2" if cfg.v2_enabled else "v1")
    if cfg.score_threshold not in (5, 6):
        raise ValueError("SCORE_THRESHOLD must be 5 or 6")
    if cfg.max_stop_atr < cfg.min_stop_atr:
        raise ValueError("MAX_STOP_ATR must be >= MIN_STOP_ATR")
    if not cfg.dry_run and not (cfg.telegram_chat_id and cfg.telegram_token and cfg.database_url):
        raise RuntimeError("Telegram token, chat id and PostgreSQL DATABASE_URL required in live mode")

    client = BinanceClient(cfg.base_url)
    if not cfg.dry_run:
        try:
            track_signals(cfg.database_url, client)
        except Exception as exc:
            log.error("tracking_run_failed kind=%s", type(exc).__name__)
    exchange = client.symbol_info()
    symbols = [sym for sym in cfg.symbols if sym in exchange]
    if not symbols:
        raise RuntimeError("No supported USDT perpetuals in watchlist")
    for sym in cfg.symbols:
        if sym not in exchange:
            log.warning("unsupported_symbol symbol=%s", sym)

    now_ms = int(time.time() * 1000)
    log.info("scan_started symbols=%s dry_run=%s", len(symbols), cfg.dry_run)
    valid, errors, assessments = [], [], 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(scan_one, client, sym, exchange[sym], cfg, now_ms): sym for sym in symbols}
        for future in as_completed(jobs):
            sym = jobs[future]
            try:
                signals = future.result()
                assessments += 1
                for sig in signals:
                    log.info("assessment %s", json.dumps({
                        "symbol": sym, "side": sig.side, "score": sig.score,
                        "eligible": sig.eligible, "checks": sig.checks,
                        "entry": sig.entry, "stop": sig.stop,
                        "rvol": round(sig.rvol, 2), "cvd3": round(sig.cvd3, 2),
                        "oi_pct": round(sig.oi_change_pct, 2),
                    }, ensure_ascii=False))
                    if sig.eligible:
                        valid.append(sig)
                if not signals:
                    log.info("assessment symbol=%s status=liquidity_or_spread_filtered", sym)
            except Exception as exc:
                errors.append(sym)
                log.error("scan_failed symbol=%s kind=%s reason=%s",
                          sym, type(exc).__name__,
                          str(exc)[:160] if isinstance(exc, BinanceError) else "unavailable")

    if errors and len(errors) >= max(2, len(symbols) // 2):
        raise RuntimeError(f"Data retrieval failure for {len(errors)}/{len(symbols)} symbols")

    valid.sort(key=lambda sig: (sig.score, sig.rvol), reverse=True)
    sent = 0
    storage = Cooldowns(cfg.database_url, cfg.dry_run)
    try:
        for sig in valid:
            if sent >= cfg.max_signals_per_run:
                break
            if not storage.allowed(sig.symbol, sig.side, max(cfg.cooldown_minutes, 240 if sig.side == "SHORT" else 180) if cfg.v2_enabled else cfg.cooldown_minutes):
                log.info("cooldown_skip symbol=%s side=%s", sig.symbol, sig.side)
                continue
            msg = format_signal(sig, cfg.timezone)
            if cfg.dry_run:
                log.info("DRY_RUN paper signal:\n%s", msg)
            else:
                send_telegram(cfg.telegram_token, cfg.telegram_chat_id, msg)
                storage.mark_sent(sig.symbol, sig.side, signal=sig)
                log.info("telegram_sent symbol=%s side=%s score=%s", sig.symbol, sig.side, sig.score)
            sent += 1
    finally:
        storage.close()
    log.info("scan_finished scanned=%s failed=%s eligible=%s delivered_or_previewed=%s",
             assessments, len(errors), len(valid), sent)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.getLogger("sentinel").error("run_failed kind=%s reason=%s", type(exc).__name__, str(exc)[:160])
        sys.exit(1)

