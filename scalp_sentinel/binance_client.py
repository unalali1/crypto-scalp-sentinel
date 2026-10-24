"""Read-only public Binance USD-M futures REST client."""
import threading
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

    def snapshot(self, symbol, now_ms):
        with ThreadPoolExecutor(max_workers=5) as pool:
            req = {
                tf: pool.submit(self.get, "/fapi/v1/klines", symbol=symbol, interval=tf, limit=140)
                for tf in ("1m", "5m", "15m", "1h", "4h")
            }
            req["taker"] = pool.submit(self.get, "/futures/data/takerlongshortRatio", symbol=symbol, period="5m", limit=40)
            req["oi"] = pool.submit(self.get, "/futures/data/openInterestHist", symbol=symbol, period="15m", limit=12)
            req["ticker"] = pool.submit(self.get, "/fapi/v1/ticker/24hr", symbol=symbol)
            req["book"] = pool.submit(self.get, "/fapi/v1/ticker/bookTicker", symbol=symbol)
            out = {key: fut.result() for key, fut in req.items()}

        durations = {"1m": 60000, "5m": 300000, "15m": 900000,
                     "1h": 3600000, "4h": 14400000}
        for tf, duration in durations.items():
            out[tf] = [row for row in out[tf] if int(row[6]) + 250 <= now_ms]
            if len(out[tf]) < 65:
                raise BinanceError(f"{symbol} insufficient closed {tf} candles")
            if now_ms - int(out[tf][-1][6]) > duration + 180000:
                raise BinanceError(f"{symbol} stale {tf} candle")
        if not out["taker"] or not out["oi"]:
            raise BinanceError(f"{symbol} missing CVD/OI")
        if now_ms - int(out["taker"][-1]["timestamp"]) > 1800000:
            raise BinanceError(f"{symbol} stale taker volume")
        if now_ms - int(out["oi"][-1]["timestamp"]) > 3600000:
            raise BinanceError(f"{symbol} stale OI")
        return out
