"""Telegram paper signal delivery. Never log credentials."""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import requests


def fmt_price(value, tick):
    digits = max(0, -Decimal(tick).as_tuple().exponent)
    return f"{value:,.{digits}f}"


def format_signal(signal, tz):
    entry, stop = signal.entry, signal.stop
    risk_pct = abs(entry - stop) / entry * 100
    title = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
    dt = datetime.fromtimestamp(signal.snapshot_ts / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz))
    rows = [
        f"{title} | {signal.symbol}",
        f"Altı koşul: {signal.score}/6 | {dt:%Y-%m-%d %H:%M} ({tz})",
        "",
        f"Giriş referansı: {fmt_price(entry, signal.tick_size)} USDT (son kapanmış 1dk)",
        f"Stop: {fmt_price(stop, signal.tick_size)} USDT | Fiyat riski: %{risk_pct:.2f}",
        f"TP1 (1.5R): {fmt_price(signal.tp1, signal.tick_size)} USDT",
        f"TP2 (2.5R): {fmt_price(signal.tp2, signal.tick_size)} USDT",
        "",
        "Teyitler:",
    ]
    rows.extend(f"{'✓' if ok else '✗'} {name}" for name, ok in signal.checks.items())
    rows.extend([
        "",
        f"5dk ATR: {fmt_price(signal.atr5, signal.tick_size)} | Göreli hacim: {signal.rvol:.2f}x",
        f"CVD son 15dk: {signal.cvd3:,.0f} | OI 1s: %{signal.oi_change_pct:+.2f}",
        f"Spread: {signal.spread_bps:.2f} baz puan",
        "",
        "UYARI: Otomatik emir değildir. Giriş son kapanmış mum referansıdır; "
        "gerçek fiyat değişebilir. TP seviyeleri matematiksel R hedefleridir.",
    ])
    return "\n".join(rows)


def send_telegram(token, chat_id, message):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        data = response.json()
        if response.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: HTTP {response.status_code}")
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram connection failed") from None
