"""Read-only public Binance USD-M futures REST client."""
import threading
from math import isfinite
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BinanceError(RuntimeError):
    pass


class DataPending(BinanceError):
    """Published statistics have not caught up; skip trading and retry later."""
    def __init__(self, symbol, dataset, age_ms, count):
        self.dataset=dataset
        super().__init__(f"{symbol} pending {dataset} age_ms={age_ms} records={count}")


class BinanceClient:
    def __init__(self, base_url):
        self.base_url = base_url
        self._local = threading.local()
        self._cache = {}
        self._cache_lock = threading.Lock()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            retry = Retry(total=2, backoff_factor=0.7,
                          status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods={"GET"}, respect_retry_after_header=True)
            session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8))
            session.headers.update({"User-Agent": "CryptoScalpSentinel/0.1"})
            self._local.session = session
        return session

    def get(self, path, **params):
        try:
            response = self._session().get(f"{self.base_url}{path}", params=params, timeout=12)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("code", 0) < 0:
                raise BinanceError(f"Binance API error {result['code']} on {path}")
            return result
        except requests.HTTPError as exc:
            code = getattr(exc.response, "status_code", "unknown")
            raise BinanceError(f"Binance HTTP {code} at {path}") from None
        except (requests.RequestException, ValueError) as exc:
            raise BinanceError(f"Binance read failed on {path}: {type(exc).__name__}") from None

    def symbol_info(self):
        data = self.get("/fapi/v1/exchangeInfo")
        return {
            r["symbol"]: r for r in data["symbols"]
            if r.get("contractType") in ("PERPETUAL", "TRADIFI_PERPETUAL")
            and r.get("quoteAsset") == "USDT"
            and r.get("status") == "TRADING"
        }

    def cached(self, key, bucket, path, **params):
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached and cached[0] == bucket:
            return cached[1]
        value = self.get(path, **params)
        with self._cache_lock:
            self._cache[key] = (bucket, value)
        return value

    def scalp_snapshot(self, symbol, now_ms):
        """Small, closed-candle snapshot for the standard 5m strategies."""
        out = {}
        for tf, duration in (("5m", 300000), ("15m", 900000)):
            rows = self.cached((symbol, "standard", tf), now_ms // duration,
                               "/fapi/v1/klines", symbol=symbol, interval=tf, limit=100)
            bars = [r for r in rows if int(r[6]) + 250 <= now_ms]
            expected_open = now_ms // duration * duration - duration
            if len(bars) < 60 or int(bars[-1][0]) != expected_open:
                # Retry an unpublished latest bar in the same bucket.
                with self._cache_lock:
                    self._cache.pop((symbol, "standard", tf), None)
                raise DataPending(symbol, tf, now_ms - (int(bars[-1][6]) + 1) if bars else None, len(bars))
            if any(int(b[0]) - int(a[0]) != duration for a, b in zip(bars, bars[1:])):
                raise BinanceError(f"{symbol} noncontiguous {tf}")
            for bar in bars:
                o, h, l, c, v = map(float, bar[1:6])
                if not all(isfinite(x) for x in (o, h, l, c, v)) or l <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
                    raise BinanceError(f"{symbol} invalid {tf} candle")
            out[tf] = bars
        out["ticker"] = self.cached((symbol, "standard", "ticker"), now_ms // 300000,
                                    "/fapi/v1/ticker/24hr", symbol=symbol)
        out["book"] = self.get("/fapi/v1/ticker/bookTicker", symbol=symbol)
        out["asof_ms"] = now_ms
        return out

    def context_candles(self, symbol, timeframe, now_ms):
        """Fetch and validate the latest fully closed 4h or UTC daily candle."""
        durations = {"4h": 14400000, "1d": 86400000}
        duration = durations[timeframe]
        key = (symbol, "context", timeframe)
        rows = self.cached(key, now_ms // duration, "/fapi/v1/klines",
                           symbol=symbol, interval=timeframe, limit=140)
        bars = [r for r in rows if int(r[6]) + 250 <= now_ms]
        expected_open = now_ms // duration * duration - duration
        if len(bars) < 60 or int(bars[-1][0]) != expected_open:
            with self._cache_lock:
                self._cache.pop(key, None)
            raise DataPending(symbol, timeframe,
                              now_ms - (int(bars[-1][6]) + 1) if bars else None, len(bars))
        if any(int(b[0]) - int(a[0]) != duration for a, b in zip(bars, bars[1:])):
            raise BinanceError(f"{symbol} noncontiguous {timeframe}")
        for bar in bars:
            o, h, l, c, v = map(float, bar[1:6])
            if not all(isfinite(x) for x in (o, h, l, c, v)) or l <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
                raise BinanceError(f"{symbol} invalid {timeframe} candle")
        return bars

    def snapshot(self, symbol, now_ms):
        durations = {"1m":60000,"5m":300000,"15m":900000,"1h":3600000,"4h":14400000,"1d":86400000,"1w":604800000}
        from datetime import datetime, timezone
        current = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        month_bucket = current.year * 12 + current.month
        buckets = {tf: now_ms // duration for tf, duration in durations.items()}
        buckets["1w"] = (now_ms + 3 * 86400000) // 604800000
        # Cache by closed-period boundary; never reuse a failed request.
        with ThreadPoolExecutor(max_workers=8) as pool:
            req = {
                tf: pool.submit(self.cached, (symbol,tf), buckets[tf],
                    "/fapi/v1/klines",symbol=symbol,interval=tf,
                    limit=300 if tf in ("1h","4h") else 220 if tf in ("1d","1w") else 140)
                for tf,duration in durations.items()
            }
            req["1M"] = pool.submit(self.cached, (symbol,"1M"), month_bucket,
                "/fapi/v1/klines",symbol=symbol,interval="1M",limit=26)
            for key, period, duration in (("oi","15m",900000),("oi5","5m",300000)):
                path = "/futures/data/openInterestHist"
                req[key] = pool.submit(self.cached,(symbol,key),now_ms//duration,path,
                    symbol=symbol,period=period,limit=40)
            req["ticker"] = pool.submit(self.cached,(symbol,"ticker"),now_ms//300000,
                "/fapi/v1/ticker/24hr",symbol=symbol)
            req["book"] = pool.submit(self.get,"/fapi/v1/ticker/bookTicker",symbol=symbol)
            # 5m OI is only required by the V3 research cohort. Fetch it in parallel,
        # but do not let its publication lag discard the v2.1 primary snapshot.
        out = {}
        optional_levels = {"1d","1w","1M"}
        for key,fut in req.items():
            if key == "oi5":
                continue
            try:
                out[key] = fut.result()
            except Exception as exc:
                if key not in optional_levels:
                    raise
                out[key] = []
                if isinstance(exc, KeyError):
                    with self._cache_lock:
                        self._cache[(symbol,key)] = (buckets.get(key,month_bucket), [])
                out.setdefault("level_data_pending", []).append(
                    f"{key}:{type(exc).__name__}")
        try:
            out["oi5"] = req["oi5"].result()
        except Exception as exc:
            out["oi5_pending_reason"] = f"fetch_failed:{type(exc).__name__}"
        try:
            for tf,duration in durations.items():
                out[tf] = [r for r in out[tf] if int(r[6])+250 <= now_ms]
                rows=out[tf]
                if tf in optional_levels and not rows:
                    continue
                if (tf not in optional_levels and len(rows)<65) or now_ms-int(rows[-1][6])>duration+15000:
                    if tf in optional_levels:
                        out[tf]=[]
                        out.setdefault("level_data_pending", []).append(f"{tf}:stale_or_insufficient")
                        continue
                    raise BinanceError(f"{symbol} stale/insufficient {tf}")
                for row in rows:
                    o,h,l,c,v=map(float,row[1:6])
                    if (not all(isfinite(x) for x in (o,h,l,c,v)) or l<=0
                            or not l<=min(o,c)<=max(o,c)<=h or v<0):
                        raise BinanceError(f"{symbol} invalid {tf} candle")
                if any(int(b[0])-int(a[0])!=duration for a,b in zip(rows,rows[1:])):
                    raise BinanceError(f"{symbol} noncontiguous {tf}")
            monthly=[r for r in out.get("1M",[]) if int(r[6])+250<=now_ms]
            if monthly and now_ms-int(monthly[-1][6])>32*86400000:
                monthly=[]
                out.setdefault("level_data_pending", []).append("1M:stale")
            for row in monthly:
                o,h,l,c,v=map(float,row[1:6])
                if (not all(isfinite(x) for x in (o,h,l,c,v)) or l<=0
                        or not l<=min(o,c)<=max(o,c)<=h or v<0):
                    raise BinanceError(f"{symbol} invalid 1M candle")
            for a,b in zip(monthly,monthly[1:]):
                am=datetime.fromtimestamp(int(a[0])/1000,tz=timezone.utc)
                bm=datetime.fromtimestamp(int(b[0])/1000,tz=timezone.utc)
                if (bm.year*12+bm.month)!=(am.year*12+am.month+1):
                    raise BinanceError(f"{symbol} noncontiguous 1M")
            out["1M"]=monthly
            # Binance USD-M klines contain total base volume at [5] and
            # taker-buy base volume at [9]. Derive sell volume from the same
            # CLOSED candle, avoiding the delayed statistical endpoint and
            # an extra HTTP call. Do not substitute a price-direction proxy.
            out['taker']=[]
            for row in out['5m'][-40:]:
                if len(row)<10:
                    raise BinanceError(f'{symbol} missing kline taker volume')
                total,buy=float(row[5]),float(row[9])
                if not isfinite(buy) or not 0<=buy<=total:
                    raise BinanceError(f'{symbol} invalid kline taker volume')
                out['taker'].append(dict(timestamp=int(row[0]),buyVol=buy,sellVol=total-buy))
            out['flow_source']='closed_5m_kline_taker_volume'
            # Derived taker timestamps identify interval starts; OI timestamps
            # are point observations. Never admit future/unclosed intervals.
            def validate_series(key,duration,lag):
                rows=sorted((r for r in out[key] if int(r["timestamp"])+lag<=now_ms),
                            key=lambda r:int(r["timestamp"]))
                age=now_ms-(int(rows[-1]["timestamp"])+lag) if rows else None
                if len(rows)<6 or age>duration+90000:
                    raise DataPending(symbol,key,age,len(rows))
                if any(int(b["timestamp"])-int(a["timestamp"])!=duration for a,b in zip(rows,rows[1:])):
                    raise BinanceError(f"{symbol} noncontiguous {key}")
                fields=("buyVol","sellVol") if key=="taker" else ("sumOpenInterest",)
                if any(not isfinite(float(r[f])) or float(r[f]) < (0 if key=="taker" else 1e-30)
                       for r in rows for f in fields):
                    raise BinanceError(f"{symbol} invalid {key}")
                return rows,age

            for key,duration,lag in (("taker",300000,300000),("oi",900000,0)):
                out[key],_ = validate_series(key,duration,lag)

            if "oi5" in out:
                try:
                    out["oi5"],out["oi5_age_ms"] = validate_series("oi5",300000,0)
                    out["oi5_available"] = True
                except DataPending as exc:
                    out.pop("oi5",None)
                    out["oi5_available"] = False
                    out["oi5_pending_reason"] = str(exc)
                    with self._cache_lock:
                        self._cache.pop((symbol,"oi5"),None)
            else:
                out["oi5_available"] = False
            out["asof_ms"]=now_ms
            out['oi_age_ms']=now_ms-int(out['oi'][-1]['timestamp'])
            return out
        except DataPending as exc:
            # Publication can lag a boundary. Keep valid candles cached, but
            # retry the delayed statistics next minute without relaxing freshness.
            with self._cache_lock:
                for key in ('taker','oi','oi5'):
                    self._cache.pop((symbol,key),None)
            raise
        except Exception:
            # Allow the next minute to retry a delayed publication in the
            # same bucket instead of retaining a stale cached response.
            with self._cache_lock:
                for key in list(self._cache):
                    if key[0]==symbol:
                        del self._cache[key]
            raise
