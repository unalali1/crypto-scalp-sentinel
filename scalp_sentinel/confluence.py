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


def _series_divergences(rows):
    closes=[float(r[4]) for r in rows]
    highs=[float(r[2]) for r in rows]
    lows=[float(r[3]) for r in rows]
    volumes=[float(r[5]) for r in rows]
    oscillators={
        "RSI":_rsi(closes),
        "MACD":_macd_hist(closes),
        "OBV":_obv(closes,volumes),
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
        if age<0 or age>MAX_CONFIRMATION_AGE:
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


def detect_confluences(symbol,snapshot,tick_size):
    """Find non-repainting divergence + nearby confirmed-pivot confluence zones."""
    alerts=[]
    for timeframe in ("5m","15m"):
        rows=snapshot.get(timeframe) or []
        if len(rows)<60:
            continue
        atr=_atr(rows)
        if atr<=0:
            continue
        price=float(rows[-1][4])
        half=max(atr*ZONE_ATR,price*0.0005)
        for side,pivot_i,indicators,age in _series_divergences(rows):
            pivot=float(rows[pivot_i][4])
            if abs(price-pivot)>atr*MAX_DISTANCE_ATR:
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
