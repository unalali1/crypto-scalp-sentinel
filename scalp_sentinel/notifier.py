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
        f"Scalp Sentinel {getattr(signal, 'strategy_version', 'v1').upper()} | Teyit: {signal.score}/{len(signal.checks)} | {dt:%Y-%m-%d %H:%M} ({tz})",
        "",
        f"Giriş referansı: {fmt_price(entry, signal.tick_size)} USDT (son kapanmış 1dk, piyasa emri değildir)",
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
        f"Net taker hacmi 15dk: {signal.cvd3:,.0f} | OI 1 saat: %{signal.oi_change_pct:+.2f}",
        f"Spread: {signal.spread_bps:.2f} baz puan",
        "",
        "UYARI: Otomatik emir değildir. Giriş son kapanmış mum referansıdır; "
        "gerçek fiyat değişebilir. TP seviyeleri matematiksel R hedefleridir.",
    ])
    policy=getattr(signal,"journal",{})
    if policy.get("cost_model"):
        expires=datetime.fromtimestamp(policy["expires_at_ms"]/1000,tz=timezone.utc).astimezone(ZoneInfo(tz))
        rows.extend([
            "",
            f"Geçerlilik: {expires:%H:%M:%S} ({tz}); bu saatten sonra giriş yok.",
            f"Fiyat kontrol aralığı: {fmt_price(policy['price_min'],signal.tick_size)}–{fmt_price(policy['price_max'],signal.tick_size)}; aralık dışında fiyatı kovalamayın.",
            "Model: referans fiyattan limit giriş; girişten önce stop veya TP1 aşılırsa iptal.",
            "Çıkış modeli: TP1'de %50, kalan %50 TP2; TP1 sonrası kalan stop girişe taşınır.",
            f"Azami takip: {policy['max_hold_ms']//60000} dakika. Tahmini işlem maliyeti: {policy['estimated_cost_r']:.2f}R (funding hariç).",
            f"Maliyet varsayımı: her yönde {policy['fee_bps']:g} bp komisyon + {policy['slippage_bps']:g} bp kayma; gerçek borsa hesabınız farklı olabilir.",
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

