import os
from dataclasses import dataclass

DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,AVAXUSDT,LINKUSDT,NEARUSDT,AAVEUSDT,ZECUSDT,ENAUSDT"


def bool_env(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class Config:
    symbols: tuple[str, ...]
    base_url: str
    telegram_token: str
    telegram_chat_id: str
    database_url: str
    dry_run: bool
    timezone: str
    min_quote_volume_usdt: float
    max_spread_bps: float
    min_rvol: float
    score_threshold: int
    min_stop_atr: float
    max_stop_atr: float
    cooldown_minutes: int
    max_signals_per_run: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            symbols=tuple(dict.fromkeys(x.strip().upper() for x in os.getenv("SYMBOLS", DEFAULT_SYMBOLS).split(",") if x.strip())),
            base_url=os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com").rstrip("/"),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            dry_run=bool_env("DRY_RUN", True),
            timezone=os.getenv("TIMEZONE", "Asia/Shanghai"),
            min_quote_volume_usdt=float(os.getenv("MIN_QUOTE_VOLUME_USDT", 15_000_000)),
            max_spread_bps=float(os.getenv("MAX_SPREAD_BPS", 10)),
            min_rvol=float(os.getenv("MIN_RVOL", 1.15)),
            score_threshold=int(os.getenv("SCORE_THRESHOLD", 5)),
            min_stop_atr=float(os.getenv("MIN_STOP_ATR", 0.6)),
            max_stop_atr=float(os.getenv("MAX_STOP_ATR", 3.0)),
            cooldown_minutes=int(os.getenv("COOLDOWN_MINUTES", 90)),
            max_signals_per_run=int(os.getenv("MAX_SIGNALS_PER_RUN", 3)),
        )
