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

    # Match Barbarians Trading's rolling 300-bar Fibonacci range, using
    # confirmed 1h and 4h candles so the levels are stable in the scanner.
    fib_ratios = (0, 0.114, 0.214, 0.34, 0.382, 0.5, 0.618, 0.66, 0.786, 0.886, 1)
    for tf in ("1h", "4h"):
        rows = snapshot.get(tf) or []
        if len(rows) < 300:
            continue
        window = rows[-300:]
        high = max(float(row[2]) for row in window)
        low = min(float(row[3]) for row in window)
        for ratio in fib_ratios:
            level = low + (high - low) * ratio
            points.append((level, f"{tf.upper()} Fib {ratio:g}"))

    # Previous-day extension levels from the companion Pine Fib script.
    # Its source uses the prior completed daily candle's high/low range.
    if daily:
        previous = daily[-1]
        range_high, range_low = float(previous[2]), float(previous[3])
        day_range = range_high - range_low
        extension_levels = (
            (1.618, "Previous-day extension 1.618 FOMO"),
            (1.214, "Previous-day extension 1.214 Ex2"),
            (1.114, "Previous-day extension 1.114 Ex1"),
            (0.5, "Previous-day Fib equilibrium 0.500"),
            (-0.114, "Previous-day extension -0.114 Ex1"),
            (-0.214, "Previous-day extension -0.214 Ex2"),
            (-0.618, "Previous-day extension -0.618 FOMO"),
        )
        for ratio, label in extension_levels:
            points.append((range_low + day_range * ratio, label))

    for tf, rows in (("D", daily), ("W", weekly)):
        closes = _close(rows)
        for period in (21, 50, 200):
            value = _sma(closes, period)
            if value is not None:
                points.append((value, f"{tf} SMA{period}"))

    zones = []
    for group in _cluster(points, tolerance):
        labels = sorted({label for _, label in group})
        period_kinds = {label.split()[0] for label in labels if label.startswith(("D ", "W ", "M ", "Q"))}
        ma_count = sum("SMA" in label for label in labels)
        is_period_cluster = len(group) >= 3 and len(period_kinds) >= 2
        is_ma_cluster = ma_count >= 2
        is_fib_zone = any(" Fib " in label or "extension " in label.lower()
                          for label in labels)
        if not (is_period_cluster or is_ma_cluster or is_fib_zone):
            continue
        low = min(value for value, _ in group)
        high = max(value for value, _ in group)
        center = sum(value for value, _ in group) / len(group)
        sources = sorted({label for _, label in group})
        stable_step = max(price * 0.001, float(tick_size) * 10)
        key = f"{symbol}|{round(center / stable_step)}"
        zone_id = sha1(key.encode()).hexdigest()[:20]
        fib_sources = [label for label in sources
                       if " fib " in f" {label.lower()} " or "extension " in label.lower()]
        strong = is_period_cluster or is_ma_cluster or (bool(fib_sources) and len(sources) > len(fib_sources))
        zones.append({
            "zone_id": zone_id, "symbol": symbol, "low": low, "high": high,
            "center": center, "sources": sources, "points": len(group),
            "tolerance": tolerance, "atr1h": atr,
            "strength": "strong" if strong else "digest",
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


def _approach(snapshot, price, zone):
    rows = snapshot.get("1m") or []
    closes = [float(row[4]) for row in rows[-12:]]
    if not closes:
        return None
    recent = closes[-5:] if len(closes) >= 5 else closes
    movement = price - recent[0]

    if price > zone["high"]:
        if movement >= 0:
            return None
        return "Potential support", "From above · approaching the zone"
    if price < zone["low"]:
        if movement <= 0:
            return None
        return "Potential resistance", "From below · approaching the zone"

    for previous in reversed(closes):
        if previous > zone["high"]:
            return "Potential support", "From above · testing the zone"
        if previous < zone["low"]:
            return "Potential resistance", "From below · testing the zone"
    if movement < 0:
        return "Potential support", "From above · testing the zone"
    if movement > 0:
        return "Potential resistance", "From below · testing the zone"
    return "Confluence zone", "Inside the zone · approach direction unclear"


def nearby_zone_alert(symbol, snapshot, zones, tick_size):
    price = _price(snapshot)
    if price <= 0:
        return None
    close = [(zone, _distance(price, zone)) for zone in zones
             if _distance(price, zone) <= _buffer(price, zone)]
    if not close:
        return None
    close.sort(key=lambda item: item[0]["center"])
    groups = []
    for zone, distance in close:
        if groups and zone["low"] - max(item[0]["high"] for item in groups[-1]) <= max(
                0.5 * zone["atr1h"], price * 0.002):
            groups[-1].append((zone, distance))
        else:
            groups.append([(zone, distance)])
    members = min(groups, key=lambda group: min(item[1] for item in group))
    distance = min(item[1] for item in members)
    low = min(item[0]["low"] for item in members)
    high = max(item[0]["high"] for item in members)
    center = (low + high) / 2
    sources = sorted({source for item, _ in members for source in item["sources"]})
    merged = {"low": low, "high": high}
    approach = _approach(snapshot, price, merged)
    if approach is None:
        return None
    role, direction = approach
    stable_step = max(price * 0.001, float(tick_size) * 10)
    zone_id = sha1(f"{symbol}|{round(center / stable_step)}".encode()).hexdigest()[:20]
    is_strong = any(item[0].get("strength") == "strong" for item in members)
    return {
        "alert_id": f"{symbol}:{zone_id}", "zone_key": min((item[0] for item in members), key=lambda item: abs(item["center"] - center))["zone_id"], "symbol": symbol,
        "low": low, "high": high, "center": center, "atr1h": max(item[0]["atr1h"] for item in members),
        "price": price, "distance": distance, "sources": sources,
        "zone_id": zone_id, "tick_size": tick_size,
        "strength": "strong" if is_strong else "digest",
        "role": role, "approach": direction,
    }


def current_price(snapshot):
    return _price(snapshot)


def _source_label(name):
    source_names = {
        "D open": "Daily open", "D close": "Daily close",
        "W open": "Weekly open", "W close": "Weekly close",
        "M open": "Monthly open", "M close": "Monthly close",
    }
    if name in source_names:
        return source_names[name]
    if name.startswith("Q"):
        return name.replace("Q", "Quarterly ", 1)
    if name.startswith("D "):
        return name.replace("D ", "Daily ", 1)
    if name.startswith("W "):
        return name.replace("W ", "Weekly ", 1)
    return name


def format_price_zone_alert(alert, tz):
    source_text = ", ".join(_source_label(name) for name in alert["sources"])
    low, high = alert["low"], alert["high"]
    band = f"{low:,.8g}" if low == high else f"{low:,.8g} – {high:,.8g}"
    distance = "Inside the zone" if alert["distance"] == 0 else f"{alert['distance']:,.8g}"
    return "\n".join([
        f"🧭 PRICE CONFLUENCE | {alert['symbol']}",
        f"Zone type: {alert['role']}",
        f"Approach: {alert['approach']}",
        f"Zone: {band}",
        f"Current price: {alert['price']:,.8g} · midpoint: {alert['center']:,.8g}",
        f"Distance to zone: {distance}",
        f"Confluence: {source_text}",
        "",
        "Context only. This is not a trade signal and includes no entry, stop, or targets.",
        "Based on completed Daily/Weekly/Monthly/Quarterly open-close levels and/or Daily/Weekly SMAs.",
    ])


def format_price_zone_digest(alerts, tz, title="Fib levels"):
    lines = [f"🧭 PRICE CONFLUENCE DIGEST | {title}", ""]
    for alert in sorted(alerts, key=lambda item: (item["symbol"], item["distance"])):
        low, high = alert["low"], alert["high"]
        band = f"{low:,.8g}" if low == high else f"{low:,.8g} – {high:,.8g}"
        sources = ", ".join(_source_label(name) for name in alert["sources"])
        lines.append(
            f"{alert['symbol']} · {alert['role']} · {band} · price {alert['price']:,.8g} "
            f"· {sources}"
        )
    lines.extend(["", "Context only. These are proximity observations, not trade signals."])
    return "\n".join(lines)
