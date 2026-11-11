import os
from dataclasses import dataclass

DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,AVAXUSDT,LINKUSDT,NEARUSDT,AAVEUSDT,ZECUSDT,ENAUSDT,HYPEUSDT,SUIUSDT,DOGEUSDT,LTCUSDT,WLDUSDT,BIOUSDT,XAUUSDT,XAGUSDT,BZUSDT"


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
    v2_enabled: bool

    continuous: bool = False
    primary_strategy: str = "v2.1"
    shadow_enabled: bool = True
    entry_window_minutes: int = 5
    max_hold_minutes: int = 360
    max_entry_drift_atr: float = 0.20
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    max_cost_r: float = 0.25
    max_open_signals: int = 3
    correlation_threshold: float = 0.80
    archive_days: int = 30
    breakout_rvol: float = 1.15
    min_delta_ratio: float = 0.05
    min_efficiency: float = 0.25
    context_chat_id: str = ""
    standard_enhanced: bool = False
    regime_filter_mode: str = "shadow"
    scan_budget_seconds: int = 45
    risk_per_trade_pct: float = .35
    standard_exit_shadows: bool = True
    daily_summary_enabled: bool = False
    daily_summary_hour: int = 8

    def validate(self):
        from math import isfinite
        from zoneinfo import ZoneInfo
        ZoneInfo(self.timezone)
        if any(not isfinite(v) for v in vars(self).values() if isinstance(v, (int, float))):
            raise ValueError("Nonfinite configuration")
        if self.regime_filter_mode not in ('shadow', 'enforce'):
            raise ValueError("REGIME_FILTER_MODE must be shadow or enforce")
        if not 10 <= self.scan_budget_seconds <= 60 or not 0 <= self.daily_summary_hour <= 23:
            raise ValueError("Invalid scan/report timing")
        if not .25 <= self.risk_per_trade_pct <= .5:
            raise ValueError("RISK_PER_TRADE_PCT must be .25 to .50")
        if self.primary_strategy not in ("v2.1", "v3", "standard"):
            raise ValueError("PRIMARY_STRATEGY must be v2.1, v3 or standard")
        if self.score_threshold not in (5,6) or self.min_stop_atr <= 0 or self.max_stop_atr < self.min_stop_atr:
            raise ValueError("Invalid signal thresholds")
        if not 2 <= self.entry_window_minutes <= 10 or not 30 <= self.max_hold_minutes <= 1440:
            raise ValueError("Invalid paper entry/hold window")
        if not 0 < self.max_entry_drift_atr <= .5 or not 0 < self.max_cost_r <= .5:
            raise ValueError("Invalid execution limits")
        if not 0 <= self.fee_bps <= 100 or not 0 <= self.slippage_bps <= 100:
            raise ValueError("Invalid cost assumptions")
        if not 1 <= self.max_open_signals <= 10 or not 1 <= self.max_signals_per_run <= 10:
            raise ValueError("Invalid signal limits")
        if not .5 <= self.correlation_threshold <= 1 or not 1 <= self.archive_days <= 90:
            raise ValueError("Invalid correlation/retention")
        if not 0 < self.min_efficiency <= 1 or not 0 < self.min_delta_ratio <= 1 or self.breakout_rvol < 1:
            raise ValueError("Invalid research thresholds")
        if not self.dry_run and not (self.telegram_token and self.telegram_chat_id and self.database_url):
            raise ValueError("Telegram and PostgreSQL configuration required")

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
            v2_enabled=bool_env("V2_ENABLED", True),
            continuous=bool_env("RUN_CONTINUOUS", False),
            primary_strategy=os.getenv("PRIMARY_STRATEGY", "v2.1"),
            shadow_enabled=bool_env("SHADOW_ENABLED", True),
            entry_window_minutes=int(os.getenv("ENTRY_WINDOW_MINUTES", 5)),
            max_hold_minutes=int(os.getenv("MAX_HOLD_MINUTES", 45 if os.getenv("PRIMARY_STRATEGY") == "standard" else 360)),
            max_entry_drift_atr=float(os.getenv("MAX_ENTRY_DRIFT_ATR", .20)),
            fee_bps=float(os.getenv("PAPER_FEE_BPS", 5)),
            slippage_bps=float(os.getenv("PAPER_SLIPPAGE_BPS", 2)),
            max_cost_r=float(os.getenv("MAX_COST_R", .40 if os.getenv("PRIMARY_STRATEGY") == "standard" else .25)),
            max_open_signals=int(os.getenv("MAX_OPEN_SIGNALS", 3)),
            correlation_threshold=float(os.getenv("CORRELATION_THRESHOLD", .80)),
            archive_days=int(os.getenv("ARCHIVE_DAYS", 30)),
            breakout_rvol=float(os.getenv("BREAKOUT_RVOL", 1.15)),
            min_delta_ratio=float(os.getenv("MIN_DELTA_RATIO", .05)),
            min_efficiency=float(os.getenv("MIN_EFFICIENCY", .25)),
            context_chat_id=os.getenv("CONTEXT_CHAT_ID", "").strip(),
            standard_enhanced=bool_env("STANDARD_ENHANCED", True),
            regime_filter_mode=os.getenv("REGIME_FILTER_MODE", "shadow"),
            scan_budget_seconds=int(os.getenv("SCAN_BUDGET_SECONDS", 45)),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", .35)),
            standard_exit_shadows=bool_env("STANDARD_EXIT_SHADOWS", True),
            daily_summary_enabled=bool_env("DAILY_SUMMARY_ENABLED", False),
            daily_summary_hour=int(os.getenv("DAILY_SUMMARY_HOUR", 8)),
        )

