"""Independent price/volume snapshot: delayed OI never gates a swing setup."""
from concurrent.futures import ThreadPoolExecutor
from math import isfinite

from .binance_client import BinanceError
from .swing_engine import DAY, FOUR_HOURS, HOUR

PERIODS={"1h":(HOUR,120),"4h":(FOUR_HOURS,220),"1d":(DAY,450)}


def validate_bars(rows,period,now_ms,min_rows):
    closed=[r for r in rows if int(r[6])+250<=now_ms]
    if len(closed)<min_rows or now_ms-int(closed[-1][6])>period+15*60_000:
        raise BinanceError("swing candles stale or insufficient")
    if any(int(b[0])-int(a[0])!=period for a,b in zip(closed,closed[1:])):
        raise BinanceError("swing candle gap")
    for row in closed:
        o,h,l,c,v=map(float,row[1:6])
        if not all(isfinite(x) for x in (o,h,l,c,v)) or l<=0 or v<0 or not l<=min(o,c)<=max(o,c)<=h:
            raise BinanceError("invalid swing candle")
    return closed


def live_snapshot(client,symbol,now_ms):
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={tf:pool.submit(client.get,"/fapi/v1/klines",symbol=symbol,interval=tf,limit=limit)
              for tf,(_duration,limit) in PERIODS.items()}
        jobs["book"]=pool.submit(client.get,"/fapi/v1/ticker/bookTicker",symbol=symbol)
        result={key:job.result() for key,job in jobs.items()}
    for tf,(duration,_limit) in PERIODS.items():
        result[tf]=validate_bars(result[tf],duration,now_ms,400 if tf=="1d" else 90 if tf=="4h" else 20)
    # Last completed UTC day is reproducible in historical replay. A rolling
    # 24h ticker is not, so it cannot be used to select the live cohort.
    result["quote_volume"]=float(result["1d"][-1][7])
    if not isfinite(result["quote_volume"]) or result["quote_volume"]<0:
        raise BinanceError("invalid swing quote volume")
    return result
