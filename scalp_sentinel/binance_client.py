"""Read-only public Binance USD-M futures REST client."""
import threading
from math import isfinite
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BinanceError(RuntimeError):
    pass


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
            if r.get("contractType") == "PERPETUAL"
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

    def snapshot(self, symbol, now_ms):
        durations = {"1m":60000,"5m":300000,"15m":900000,"1h":3600000,"4h":14400000}
        # Cache by closed-period boundary; never reuse a failed request.
        with ThreadPoolExecutor(max_workers=5) as pool:
            req = {
                tf: pool.submit(self.cached, (symbol,tf), now_ms//duration,
                    "/fapi/v1/klines",symbol=symbol,interval=tf,limit=140)
                for tf,duration in durations.items()
            }
            for key, period, duration in (("taker","5m",300000),("oi","15m",900000),("oi5","5m",300000)):
                path = "/futures/data/takerlongshortRatio" if key=="taker" else "/futures/data/openInterestHist"
                req[key] = pool.submit(self.cached,(symbol,key),now_ms//duration,path,
                    symbol=symbol,period=period,limit=40)
            req["ticker"] = pool.submit(self.cached,(symbol,"ticker"),now_ms//300000,
                "/fapi/v1/ticker/24hr",symbol=symbol)
            req["book"] = pool.submit(self.get,"/fapi/v1/ticker/bookTicker",symbol=symbol)
            out = {key:fut.result() for key,fut in req.items()}
        try:
            for tf,duration in durations.items():
                out[tf] = [r for r in out[tf] if int(r[6])+250 <= now_ms]
                rows=out[tf]
                if len(rows)<65 or now_ms-int(rows[-1][6])>duration+15000:
                    raise BinanceError(f"{symbol} stale/insufficient {tf}")
                for row in rows:
                    o,h,l,c,v=map(float,row[1:6])
                    if (not all(isfinite(x) for x in (o,h,l,c,v)) or l<=0
                            or not l<=min(o,c)<=max(o,c)<=h or v<0):
                        raise BinanceError(f"{symbol} invalid {tf} candle")
                if any(int(b[0])-int(a[0])!=duration for a,b in zip(rows,rows[1:])):
                    raise BinanceError(f"{symbol} noncontiguous {tf}")
            # Taker timestamps identify interval starts; conservatively use
            # completed intervals only. OI timestamps are point observations.
            for key,duration,lag in (("taker",300000,300000),("oi",900000,0),("oi5",300000,0)):
                rows=sorted((r for r in out[key] if int(r["timestamp"])+lag<=now_ms),
                            key=lambda r:int(r["timestamp"]))
                if len(rows)<6 or now_ms-(int(rows[-1]["timestamp"])+lag)>duration+90000:
                    raise BinanceError(f"{symbol} stale/missing {key}")
                if any(int(b["timestamp"])-int(a["timestamp"])!=duration for a,b in zip(rows,rows[1:])):
                    raise BinanceError(f"{symbol} noncontiguous {key}")
                out[key]=rows
                fields=("buyVol","sellVol") if key=="taker" else ("sumOpenInterest",)
                if any(not isfinite(float(r[f])) or float(r[f]) < (0 if key=="taker" else 1e-30)
                       for r in rows for f in fields):
                    raise BinanceError(f"{symbol} invalid {key}")
            out["asof_ms"]=now_ms
            return out
        except Exception:
            # Allow the next minute to retry a delayed publication in the
            # same bucket instead of retaining a stale cached response.
            with self._cache_lock:
                for key in list(self._cache):
                    if key[0]==symbol:
                        del self._cache[key]
            raise
