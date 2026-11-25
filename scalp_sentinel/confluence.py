"""Directional confluence alerts adapted from selected Barbarians Trading concepts.

Only confirmed, closed-candle divergences near a recent swing pivot are reported.
These are context alerts, not trade recommendations or entry signals.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .engine import price_round


PIVOT_LEFT = 5
PIVOT_RIGHT = 5
MIN_PIVOT_GAP = 6
MAX_PIVOT_GAP = 100
MAX_CONFIRMATION_AGE = 4
MIN_DIVERGENT_INDICATORS = 2
ZONE_ATR = 0.25
MAX_DISTANCE_ATR = 1.0


def _ema(values, period):
    alpha=2/(period+1)
    out=[]
    value=values[0]
    for x in values:
        value=alpha*x+(1-alpha)*value
        out.append(value)
    return out


def _rsi(values, period=14):
    if len(values)<period+1:
        return [None]*len(values)
    out=[None]*len(values)
    gains=[max(values[i]-values[i-1],0.0) for i in range(1,len(values))]
    losses=[max(values[i-1]-values[i],0.0) for i in range(1,len(values))]
    avg_gain=sum(gains[:period])/period
    avg_loss=sum(losses[:period])/period
    def value(g,l):
        if l==0:
            return 50.0 if g==0 else 100.0
        return 100.0-100.0/(1.0+g/l)
    out[period]=value(avg_gain,avg_loss)
    for i in range(period+1,len(values)):
        avg_gain=(avg_gain*(period-1)+gains[i-1])/period
        avg_loss=(avg_loss*(period-1)+losses[i-1])/period
        out[i]=value(avg_gain,avg_loss)
    return out


def _macd_hist(values):
    if not values:
        return []
    macd=[a-b for a,b in zip(_ema(values,12),_ema(values,26))]
    signal=_ema(macd,9)
    return [a-b for a,b in zip(macd,signal)]


def _obv(closes,volumes):
    out=[0.0]*len(closes)
    for i in range(1,len(closes)):
        out[i]=out[i-1]+(volumes[i] if closes[i]>closes[i-1]
                         else -volumes[i] if closes[i]<closes[i-1] else 0.0)
    return out


def _mfi(highs,lows,closes,volumes,period=14):
    typical=[(h+l+c)/3 for h,l,c in zip(highs,lows,closes)]
    positive=[0.0]*len(closes)
    negative=[0.0]*len(closes)
    for i in range(1,len(closes)):
        flow=typical[i]*volumes[i]
        if typical[i]>typical[i-1]:
            positive[i]=flow
        elif typical[i]<typical[i-1]:
            negative[i]=flow
    out=[None]*len(closes)
    for i in range(period,len(closes)):
        pos=sum(positive[i-period+1:i+1])
        neg=sum(negative[i-period+1:i+1])
        out[i]=100.0 if neg==0 and pos>0 else 50.0 if pos==neg==0 else 100.0-100.0/(1.0+pos/neg)
    return out


def _stoch(highs, lows, closes, period=14, smooth=3):
    raw = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        raw[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
    out = [None] * len(closes)
    for i in range(period + smooth - 2, len(closes)):
        vals = raw[i - smooth + 1:i + 1]
        if all(v is not None for v in vals):
            out[i] = sum(vals) / smooth
    return out


def _cci(highs, lows, closes, period=10):
    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    out = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        win = tp[i - period + 1:i + 1]
        mean = sum(win) / period
        mad = sum(abs(x - mean) for x in win) / period
        out[i] = 0.0 if mad == 0 else (tp[i] - mean) / (0.015 * mad)
    return out


def _cmf(highs, lows, closes, volumes, period=21):
    out = [None] * len(closes)
    mfv = [
        0.0 if h == l else ((c - l) - (h - c)) / (h - l) * v
        for h, l, c, v in zip(highs, lows, closes, volumes)
    ]
    for i in range(period - 1, len(closes)):
        vol_sum = sum(volumes[i - period + 1:i + 1])
        out[i] = None if vol_sum <= 0 else sum(mfv[i - period + 1:i + 1]) / vol_sum
    return out


def _atr(rows,period=14):
    tr=[]
    for i in range(1,len(rows)):
        high,low,prev_close=float(rows[i][2]),float(rows[i][3]),float(rows[i-1][4])
        tr.append(max(high-low,abs(high-prev_close),abs(low-prev_close)))
    return sum(tr[-period:])/min(period,len(tr)) if tr else 0.0


def _confirmed_pivots(values,mode):
    pivots=[]
    end=len(values)-PIVOT_RIGHT
    for i in range(PIVOT_LEFT,end):
        left=values[i-PIVOT_LEFT:i]
        right=values[i+1:i+PIVOT_RIGHT+1]
        if mode=="low" and values[i]<=min(left) and values[i]<min(right):
            pivots.append(i)
        elif mode=="high" and values[i]>=max(left) and values[i]>max(right):
            pivots.append(i)
    return pivots


def _series_divergences(rows, max_age=MAX_CONFIRMATION_AGE):
    closes=[float(r[4]) for r in rows]
    highs=[float(r[2]) for r in rows]
    lows=[float(r[3]) for r in rows]
    volumes=[float(r[5]) for r in rows]
    oscillators={
        "RSI":_rsi(closes),
        "MACD":_macd_hist(closes),
        "Stoch":_stoch(highs,lows,closes),
        "CCI":_cci(highs,lows,closes),
        "OBV":_obv(closes,volumes),
        "CMF":_cmf(highs,lows,closes,volumes),
        "MFI":_mfi(highs,lows,closes,volumes),
    }
    output=[]
    for kind,side in (("low","LONG"),("high","SHORT")):
        pivots=_confirmed_pivots(closes,kind)
        if len(pivots)<2:
            continue
        current=pivots[-1]
        older=next((p for p in reversed(pivots[:-1])
                    if MIN_PIVOT_GAP<=current-p<=MAX_PIVOT_GAP),None)
        if older is None:
            continue
        age=len(rows)-1-(current+PIVOT_RIGHT)
        if age<0 or age>max_age:
            continue
        price_div=(closes[current]<closes[older] if side=="LONG"
                   else closes[current]>closes[older])
        if not price_div:
            continue
        matched=[]
        for name,values in oscillators.items():
            a,b=values[older],values[current]
            if a is None or b is None:
                continue
            agrees=(b>a if side=="LONG" else b<a)
            if agrees:
                matched.append(name)
        if len(matched)>=MIN_DIVERGENT_INDICATORS:
            output.append((side,current,matched,age))
    return output


def detect_confluences(symbol,snapshot,tick_size,timeframes=("4h","1d"),
                       max_age=MAX_CONFIRMATION_AGE,max_distance_atr=MAX_DISTANCE_ATR):
    """Find non-repainting divergence + nearby confirmed-pivot confluence zones."""
    alerts=[]
    for timeframe in timeframes:
        rows=snapshot.get(timeframe) or []
        if len(rows)<60:
            continue
        atr=_atr(rows)
        if atr<=0:
            continue
        price=float(rows[-1][4])
        half=max(atr*ZONE_ATR,price*0.0005)
        for side,pivot_i,indicators,age in _series_divergences(rows,max_age=max_age):
            pivot=float(rows[pivot_i][4])
            if max_distance_atr is not None and abs(price-pivot)>atr*max_distance_atr:
                continue
            tick=float(tick_size)
            low=price_round(max(tick,pivot-half),tick_size,up=False)
            high=price_round(pivot+half,tick_size,up=True)
            pivot_ts=int(rows[pivot_i][0])
            alerts.append({
                "alert_id":f"{symbol}:{timeframe}:{side}:{pivot_ts}",
                "symbol":symbol,"side":side,"timeframe":timeframe,
                "indicators":indicators,"pivot":pivot,"zone_low":low,
                "zone_high":high,"price":price,"atr":atr,
                "pivot_ts":pivot_ts,"age_bars":age,
            })
    return alerts


def _current_spot(snapshot):
    book=snapshot.get("book") or {}
    try:
        bid,ask=float(book["bidPrice"]),float(book["askPrice"])
        if bid>0 and ask>=bid:
            return (bid+ask)/2
    except (KeyError,TypeError,ValueError):
        pass
    rows=snapshot.get("5m") or snapshot.get("1m") or []
    return float(rows[-1][4]) if rows else 0.0


def nearby_confluence_alerts(symbol,snapshot,tick_size,timeframes=("4h","1d")):
    """Return 4h/1d divergence zones only when live price approaches or enters them."""
    price=_current_spot(snapshot)
    if price<=0:
        return []
    five=snapshot.get("5m") or []
    recent_closes=[float(r[4]) for r in five[-6:]] if len(five)>=2 else []
    movement=price-recent_closes[0] if recent_closes else 0.0
    zones=detect_confluences(symbol,snapshot,tick_size,timeframes=timeframes,
                             max_age=24,max_distance_atr=None)
    out=[]
    for zone in zones:
        low,high,atr=zone["zone_low"],zone["zone_high"],zone["atr"]
        buffer=max(atr*0.60,price*0.004)
        if price>high:
            distance=price-high
            if distance>buffer or movement>=0:
                continue
            role,approach="Destek bölgesi","Yukarıdan desteğe yaklaşıyor"
        elif price<low:
            distance=low-price
            if distance>buffer or movement<=0:
                continue
            role,approach="Direnç bölgesi","Aşağıdan dirence yaklaşıyor"
        else:
            distance=0.0
            role=("Destek bölgesi" if zone["side"]=="LONG" else "Direnç bölgesi")
            approach="Bölge içinde"
        pct=distance/price*100 if price>0 else 0.0
        out.append(dict(
            zone,
            zone_id=zone["alert_id"],
            zone_key=zone["alert_id"],
            low=low,
            high=high,
            atr1h=atr,
            price=price,
            distance=distance,
            distance_pct=pct,
            role=role,
            approach=approach,
        ))
    return out


def format_confluence_alert(alert,tz):
    dt=datetime.fromtimestamp(alert["pivot_ts"]/1000,tz=timezone.utc).astimezone(ZoneInfo(tz))
    side="🟢 LONG yönlü" if alert["side"]=="LONG" else "🔴 SHORT yönlü"
    return "\n".join([
        f"🧭 YÖNSEL KONFLUENS | {alert['symbol']}",
        f"{side} izleme alanı · {alert['timeframe']}",
        f"Bölge: {alert['zone_low']:,.8g} – {alert['zone_high']:,.8g}",
        f"Son fiyat: {alert['price']:,.8g}",
        f"Doğrulanan uyumsuzluklar: {', '.join(alert['indicators'])}",
        f"Pivot zamanı: {dt:%Y-%m-%d %H:%M} ({tz}) · doğrulama yaşı: {alert['age_bars']} bar",
        "",
        "Bu bir işlem sinyali değildir; giriş, stop ve hedef içermez.",
        "Yönsel değerlendirme için konfluens alanını izleyin; tek başına işlem gerekçesi saymayın.",
    ])


def format_confluence_digest(alerts,tz):
    """One compact message for a bounded group of context observations."""
    lines=["🧭 YÖNSEL KONFLUENS ÖZETİ", ""]
    for alert in sorted(alerts,key=lambda item:(item["symbol"],item["timeframe"],item["side"])):
        side="LONG" if alert["side"]=="LONG" else "SHORT"
        lines.append(
            f"{alert['symbol']} · {side} · {alert['timeframe']} · "
            f"{alert['zone_low']:,.8g}–{alert['zone_high']:,.8g} · "
            f"{', '.join(alert['indicators'])}"
        )
    lines.extend(["", "Bu bir işlem sinyali değildir; giriş, stop ve hedef içermez."])
    return "\n".join(lines)


def format_confluence_proximity_digest(alerts,tz):
    """Advance warning digest when price approaches a 4h or 1d confluence zone."""
    lines=["🧭 YAKLAŞAN KONFLUENS BÖLGESİ (4H / 1D)", ""]
    for alert in sorted(alerts,key=lambda item:(item["symbol"],item["timeframe"],item["distance"])):
        icon="🟢" if alert["side"]=="LONG" else "🔴"
        dist_txt="Bölge içinde" if alert["distance"]==0 else f"{alert['distance']:,.8g} (%{alert['distance_pct']:.2f})"
        lines.append(
            f"{icon} {alert['symbol']} · {alert['timeframe']} · {alert['role']} ({alert['approach']})\n"
            f"   Bölge: {alert['zone_low']:,.8g}–{alert['zone_high']:,.8g} | Güncel: {alert['price']:,.8g} | Mesafe: {dist_txt}\n"
            f"   Uyumsuzluk: {', '.join(alert['indicators'])}"
        )
    lines.extend(["", "Önden bilgilendirme amaçlıdır; giriş/stop içeren işlem sinyali değildir."])
    return "\n".join(lines)
