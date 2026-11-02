"""Structural price confluence zones from closed period levels and higher-timeframe SMAs."""
from datetime import datetime, timezone
from hashlib import sha1
from .confluence import _atr


def _close(rows):
    return [float(row[4]) for row in rows]


def _sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _price(snapshot):
    book = snapshot.get("book") or {}
    try:
        bid, ask = float(book["bidPrice"]), float(book["askPrice"])
        if bid > 0 and ask >= bid:
            return (bid + ask) / 2
    except (KeyError, TypeError, ValueError):
        pass
    rows = snapshot.get("1m") or []
    return float(rows[-1][4]) if rows else 0.0


def _quarter_bars(monthly):
    grouped = {}
    for row in monthly:
        dt = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
        key = (dt.year, (dt.month - 1) // 3)
        grouped.setdefault(key, []).append(row)
    result = []
    for (year, quarter), rows in sorted(grouped.items()):
        if len(rows) == 3:
            result.append((year, quarter + 1, rows[0], rows[-1]))
    return result


def _cluster(points, tolerance):
    points = sorted(points, key=lambda item: item[0])
    groups = []
    current = []
    for point in points:
        if current and point[0] - current[0][0] > tolerance:
            groups.append(current)
            current = []
        current.append(point)
    if current:
        groups.append(current)
    return groups


def detect_price_zones(symbol, snapshot, tick_size):
    """Return closed-bar level clusters and daily/weekly SMA convergence zones."""
    price = _price(snapshot)
    hourly = snapshot.get("1h") or []
    daily = snapshot.get("1d") or []
    weekly = snapshot.get("1w") or []
    monthly = snapshot.get("1M") or []
    if price <= 0 or len(hourly) < 20:
        return []
    atr = _atr(hourly)
    tolerance = max(atr * 0.25, price * 0.001)
    points = []

    for tf, rows, count in (("D", daily, 20), ("W", weekly, 12), ("M", monthly, 12)):
        for row in rows[-count:]:
            points.append((float(row[1]), f"{tf} open"))
            points.append((float(row[4]), f"{tf} close"))
    for year, quarter, first, last in _quarter_bars(monthly)[-8:]:
        points.append((float(first[1]), f"Q{quarter} open"))
        points.append((float(last[4]), f"Q{quarter} close"))

    for tf, rows in (("D", daily), ("W", weekly)):
        closes = _close(rows)
        for period in (21, 50, 200):
            value = _sma(closes, period)
            if value is not None:
                points.append((value, f"{tf} SMA{period}"))

    zones = []
    for group in _cluster(points, tolerance):
        labels = sorted({label for _, label in group})
        period_kinds = {label[0] for label in labels if "SMA" not in label}
        ma_count = sum("SMA" in label for label in labels)
        is_period_cluster = len(group) >= 3 and len(period_kinds) >= 2
        is_ma_cluster = ma_count >= 2
        if not (is_period_cluster or is_ma_cluster):
            continue
        low = min(value for value, _ in group)
        high = max(value for value, _ in group)
        center = sum(value for value, _ in group) / len(group)
        sources = sorted({label for _, label in group})
        key = f"{symbol}|{round(center / max(tolerance, float(tick_size)))}|{'/'.join(sources)}"
        zone_id = sha1(key.encode()).hexdigest()[:20]
        zones.append({
            "zone_id": zone_id, "symbol": symbol, "low": low, "high": high,
            "center": center, "sources": sources, "points": len(group),
            "tolerance": tolerance, "atr1h": atr,
        })
    return zones


def _distance(price, zone):
    if price < zone["low"]:
        return zone["low"] - price
    if price > zone["high"]:
        return price - zone["high"]
    return 0.0


def _buffer(price, zone):
    return max(zone["atr1h"] * 0.25, price * 0.001)


def should_watch_zones(snapshot, zones):
    price = _price(snapshot)
    return price > 0 and any(_distance(price, zone) <= 4 * _buffer(price, zone) for zone in zones)


def nearby_zone_alert(symbol, snapshot, zones, tick_size):
    price = _price(snapshot)
    if price <= 0:
        return None
    close = [(zone, _distance(price, zone)) for zone in zones
             if _distance(price, zone) <= _buffer(price, zone)]
    if not close:
        return None
    zone, distance = min(close, key=lambda item: item[1])
    return {
        "alert_id": f"{symbol}:{zone['zone_id']}", "symbol": symbol,
        "low": zone["low"], "high": zone["high"], "center": zone["center"],
        "price": price, "distance": distance, "sources": zone["sources"],
        "zone_id": zone["zone_id"], "tick_size": tick_size,
    }


def format_price_zone_alert(alert, tz):
    source_text = ", ".join(alert["sources"])
    low, high = alert["low"], alert["high"]
    band = f"{low:,.8g}" if low == high else f"{low:,.8g} – {high:,.8g}"
    return "\n".join([
        f"🧭 FİYAT KONFLUENSİ | {alert['symbol']}",
        f"Yakınlaşılan bölge: {band}",
        f"Anlık fiyat: {alert['price']:,.8g} · merkez: {alert['center']:,.8g}",
        f"Seviye birleşimi: {source_text}",
        "",
        "Bu bir işlem sinyali değildir; giriş, stop ve hedef içermez.",
        "Bölge, tamamlanmış dönem seviyeleri ve/veya günlük-haftalık hareketli ortalamalardan hesaplandı.",
    ])
