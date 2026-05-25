"""Unified NIFTY option bot with three strategies, one Telegram loop, and live auto-buy.

This file merges the three separate bots into one process so Telegram polling,
Dhan market-data calls, and live order tracking are shared instead of duplicated.

Included strategies
- SCORE: candlestick + VWAP + breakout + volume + trend + PCR score.
- BRAHMASTRA: Supertrend 20,2 + MACD 12,26,9 + VWAP.
- RSIBB: RSI impulse/pullback + Bollinger Band breakout + higher-timeframe RSI.

Main commands
- LIVE                         Enable live monitoring for all strategies.
- STOP                         Stop running scan/backtest and pause live monitoring.
- SCAN YYYY-MM-DD YYYY-MM-DD   Run scans/backtests for all enabled strategies.
- SCAN SCORE YYYY-MM-DD YYYY-MM-DD
- SCAN BRAHMASTRA YYYY-MM-DD YYYY-MM-DD
- SCAN RSIBB YYYY-MM-DD YYYY-MM-DD
- /chain                       Current option chain around ATM.
- /status                      Current NIFTY context for all strategies.
- /position                    Active live positions and pending BUY order.
- /expiry                      Selected expiry.
- /draft                       Latest editable draft.
- DRYRUN                       Alerts only, no real Dhan orders.
- MANUALBUY                    Real Dhan enabled, but wait for BUY command.
- AUTOORDER or AUTOBUY         Real Dhan orders + automatic BUY on signals.
- EDIT ENTRY 125               Edit latest draft. Also SL, T1, T2, QTY, STRIKE, SECURITY.
- RECALC                       Recalculate draft targets from entry/SL.
- BUY                          Place BUY from latest draft.
- MODBUY 125                   Modify a pending limit BUY price.
- CANCELBUY                    Cancel pending BUY.
- BUYSTATUS                    Check pending BUY.

Important
- Live mode can place real Dhan orders when LIVE_TRADING_ENABLED=true and
  AUTO_BUY=true, or after AUTOORDER/AUTOBUY is sent.
- Historical backtests use historical candles where implemented. SCORE scan
  keeps the original behavior of using historical index candles with the current
  option-chain snapshot for the option suggestion.
- This is a signal/order assistant, not financial advice.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import logging
import math
import os
import re
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

DHAN_BASE = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"
IST = ZoneInfo("Asia/Kolkata")

LOG = logging.getLogger("unified_nifty_bot")
SENSITIVE_KEY_RE = re.compile(
    r"(token|access|authorization|password|secret|chat[_-]?id|client[_-]?id|clientid|dhanclientid)",
    re.IGNORECASE,
)

SCAN_RE = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
SCAN_NAMED_RE = re.compile(
    r"^SCAN\s+(SCORE|BRAHMASTRA|RSIBB|RSI|RSI_BB)\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$",
    re.IGNORECASE,
)
STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)

ORDER_FILLED_STATUSES = {"TRADED"}
ORDER_DEAD_STATUSES = {"REJECTED", "CANCELLED", "EXPIRED"}
ORDER_WORKING_STATUSES = {"TRANSIT", "PENDING", "PART_TRADED"}

BULLISH_PATTERNS = {
    "CDLHAMMER",
    "CDLINVERTEDHAMMER",
    "CDLENGULFING",
    "CDLPIERCING",
    "CDLMORNINGSTAR",
    "CDLMORNINGDOJISTAR",
    "CDL3WHITESOLDIERS",
    "CDLTAKURI",
    "CDLDRAGONFLYDOJI",
}

BEARISH_PATTERNS = {
    "CDLSHOOTINGSTAR",
    "CDLHANGINGMAN",
    "CDLENGULFING",
    "CDLDARKCLOUDCOVER",
    "CDLEVENINGSTAR",
    "CDLEVENINGDOJISTAR",
    "CDL3BLACKCROWS",
    "CDLGRAVESTONEDOJI",
    "CDLADVANCEBLOCK",
}


# -----------------------------------------------------------------------------
# Environment helpers
# -----------------------------------------------------------------------------


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def env_time(name: str, default: dt.time) -> dt.time:
    raw = env_str(name, "")
    if not raw:
        return default
    try:
        hour, minute = raw.split(":", 1)
        return dt.time(int(hour), int(minute))
    except Exception:
        return default


def env_csv_strings(name: str, default: str = "") -> List[str]:
    raw = env_str(name, default)
    if not raw:
        return []
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def env_csv_ints(name: str, default: str = "") -> List[int]:
    values: List[int] = []
    for part in env_csv_strings(name, default):
        try:
            values.append(int(part.replace("+", "")))
        except Exception:
            LOG.warning("Ignoring invalid integer in %s: %s", name, part)
    return values


@dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str

    http_timeout: int = 20
    poll_seconds: float = 5.0
    live_enabled_at_start: bool = True
    live_trading_enabled: bool = True
    auto_buy_enabled: bool = True
    enabled_strategies: List[str] = field(default_factory=lambda: ["SCORE", "BRAHMASTRA", "RSIBB"])

    index_security_id: int = 13
    index_segment: str = "IDX_I"
    index_instrument: str = "INDEX"
    index_name: str = "NIFTY 50"
    fno_segment: str = "NSE_FNO"
    option_instrument: str = "OPTIDX"
    preferred_expiry: str = ""

    candle_interval: int = 5
    htf_interval: int = 15
    strikes_window: int = 5
    strike_step: int = 50
    option_search_depth: int = 4
    min_premium: float = 60.0
    preferred_premium_min: float = 80.0
    preferred_premium_max: float = 180.0
    max_premium: float = 250.0
    allow_premium_fallback: bool = False
    allowed_sides: List[str] = field(default_factory=list)
    allowed_rolling_offsets: List[int] = field(default_factory=list)
    blocked_rolling_offsets: List[int] = field(default_factory=list)

    lot_size: int = 65
    lots: int = 1
    brokerage_per_order: float = 0.0
    max_open_positions: int = 3

    order_product_type: str = "MARGIN"
    entry_order_type: str = "LIMIT"
    exit_order_type: str = "LIMIT"
    sl_order_type: str = "STOP_LOSS"
    order_validity: str = "DAY"
    entry_limit_buffer: float = 1.0
    exit_limit_buffer: float = 1.0
    stop_loss_limit_buffer: float = 0.50
    order_status_poll_attempts: int = 10
    order_status_poll_seconds: float = 1.0

    option_sl_pct: float = 0.22
    option_sl_buffer: float = 0.50
    target1_r: float = 1.0
    target2_r: float = 2.0
    trail_after_t1: bool = True
    min_option_risk_points: float = 0.0
    max_option_risk_points: float = 0.0
    max_option_sl_pct: float = 0.0
    backtest_entry_slippage: float = 0.0
    backtest_exit_slippage: float = 0.0

    no_new_trade_after: dt.time = dt.time(15, 0)
    square_off_time: dt.time = dt.time(15, 20)
    max_trades_per_day: int = 2

    # Score strategy.
    min_signal_score: int = 8

    # Brahmastra strategy.
    supertrend_period: int = 20
    supertrend_multiplier: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    signal_lookback: int = 3
    swing_lookback: int = 6
    volume_lookback: int = 20
    volume_multiplier: float = 1.10
    min_body_ratio: float = 0.45
    require_volume: bool = True
    require_market_structure: bool = False
    avoid_first_minutes: int = 15
    nifty_sl_buffer: float = 10.0
    fallback_option_sl_pct: float = 0.20

    # RSI+BB strategy.
    rsi_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    setup_lookback: int = 36
    breakout_lookback: int = 4
    min_pullback_bars: int = 2
    long_impulse_rsi: float = 70.0
    long_pullback_rsi_floor: float = 40.0
    short_impulse_rsi: float = 40.0
    short_pullback_rsi_ceiling: float = 70.0
    htf_long_min_rsi: float = 50.0
    htf_short_max_rsi: float = 50.0
    require_vwap: bool = True

    expired_options_expiry_flag: str = "WEEK"
    expired_options_expiry_code: int = 0
    use_dhan_expired_options_api: bool = True

    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "unified_nifty_bot.log"
    log_to_console: bool = True
    log_http_payloads: bool = False
    log_live_scan_every_seconds: float = 0.0

    @property
    def quantity(self) -> int:
        return max(1, self.lot_size * self.lots)

    @staticmethod
    def from_env(require_credentials: bool = True) -> "Config":
        cfg = Config(
            dhan_client_id=env_str("DHAN_CLIENT_ID"),
            dhan_access_token=env_str("DHAN_ACCESS_TOKEN"),
            telegram_bot_token=env_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=env_str("TELEGRAM_CHAT_ID"),
            http_timeout=env_int("HTTP_TIMEOUT", 20),
            poll_seconds=env_float("POLL_SECONDS", env_float("TG_POLL_INTERVAL", env_float("DHAN_POLL_SECONDS", 5.0))),
            live_enabled_at_start=env_bool("LIVE_ENABLED", True),
            live_trading_enabled=env_bool("LIVE_TRADING_ENABLED", env_bool("AUTO_ORDER", True)),
            auto_buy_enabled=env_bool("AUTO_BUY", env_bool("AUTO_ORDER", True)),
            enabled_strategies=env_csv_strings("ENABLED_STRATEGIES", "SCORE,BRAHMASTRA,RSIBB"),
            index_security_id=env_int("UNDERLYING_SECURITY_ID", env_int("DHAN_UNDERLYING_SECURITY_ID", 13)),
            index_segment=env_str("UNDERLYING_SEGMENT", env_str("DHAN_UNDERLYING_SEG", "IDX_I")).upper(),
            index_instrument=env_str("UNDERLYING_INSTRUMENT", "INDEX").upper(),
            index_name=env_str("INDEX_NAME", "NIFTY 50"),
            fno_segment=env_str("FNO_SEGMENT", "NSE_FNO").upper(),
            option_instrument=env_str("OPTION_INSTRUMENT", "OPTIDX").upper(),
            preferred_expiry=env_str("PREFERRED_EXPIRY", env_str("DHAN_EXPIRY", "")),
            candle_interval=env_int("CANDLE_INTERVAL", 5),
            htf_interval=env_int("HTF_INTERVAL", 15),
            strikes_window=env_int("STRIKES_WINDOW", 5),
            strike_step=env_int("STRIKE_STEP", 50),
            option_search_depth=env_int("OPTION_SEARCH_DEPTH", 4),
            min_premium=env_float("MIN_PREMIUM", 60.0),
            preferred_premium_min=env_float("PREFERRED_PREMIUM_MIN", 80.0),
            preferred_premium_max=env_float("PREFERRED_PREMIUM_MAX", 180.0),
            max_premium=env_float("MAX_PREMIUM", 250.0),
            allow_premium_fallback=env_bool("ALLOW_PREMIUM_FALLBACK", False),
            allowed_sides=env_csv_strings("ALLOWED_SIDES", ""),
            allowed_rolling_offsets=env_csv_ints("ALLOWED_ROLLING_OFFSETS", ""),
            blocked_rolling_offsets=env_csv_ints("BLOCKED_ROLLING_OFFSETS", ""),
            lot_size=env_int("LOT_SIZE", 65),
            lots=env_int("LOTS", 1),
            brokerage_per_order=env_float("BROKERAGE_PER_ORDER", 0.0),
            max_open_positions=env_int("MAX_OPEN_POSITIONS", 3),
            order_product_type=env_str("ORDER_PRODUCT_TYPE", "MARGIN").upper(),
            entry_order_type=env_str("ENTRY_ORDER_TYPE", "LIMIT").upper(),
            exit_order_type=env_str("EXIT_ORDER_TYPE", "LIMIT").upper(),
            sl_order_type=env_str("SL_ORDER_TYPE", "STOP_LOSS").upper(),
            order_validity=env_str("ORDER_VALIDITY", "DAY").upper(),
            entry_limit_buffer=env_float("ENTRY_LIMIT_BUFFER", 1.0),
            exit_limit_buffer=env_float("EXIT_LIMIT_BUFFER", 1.0),
            stop_loss_limit_buffer=env_float("STOP_LOSS_LIMIT_BUFFER", 0.50),
            order_status_poll_attempts=env_int("ORDER_STATUS_POLL_ATTEMPTS", 10),
            order_status_poll_seconds=env_float("ORDER_STATUS_POLL_SECONDS", 1.0),
            option_sl_pct=env_float("OPTION_SL_PCT", 0.22),
            option_sl_buffer=env_float("OPTION_SL_BUFFER", 0.50),
            target1_r=env_float("TARGET1_R", 1.0),
            target2_r=env_float("TARGET2_R", 2.0),
            trail_after_t1=env_bool("TRAIL_AFTER_T1", True),
            min_option_risk_points=env_float("MIN_OPTION_RISK_POINTS", 0.0),
            max_option_risk_points=env_float("MAX_OPTION_RISK_POINTS", 0.0),
            max_option_sl_pct=env_float("MAX_OPTION_SL_PCT", 0.0),
            backtest_entry_slippage=env_float("BACKTEST_ENTRY_SLIPPAGE", 0.0),
            backtest_exit_slippage=env_float("BACKTEST_EXIT_SLIPPAGE", 0.0),
            no_new_trade_after=env_time("NO_NEW_TRADE_AFTER", env_time("NO_NEW_ENTRY_AFTER", dt.time(15, 0))),
            square_off_time=env_time("SQUARE_OFF_TIME", dt.time(15, 20)),
            max_trades_per_day=env_int("MAX_TRADES_PER_DAY", 2),
            min_signal_score=env_int("MIN_SIGNAL_SCORE", 8),
            supertrend_period=env_int("SUPERTREND_PERIOD", 20),
            supertrend_multiplier=env_float("SUPERTREND_MULTIPLIER", 2.0),
            macd_fast=env_int("MACD_FAST", 12),
            macd_slow=env_int("MACD_SLOW", 26),
            macd_signal=env_int("MACD_SIGNAL", 9),
            signal_lookback=env_int("SIGNAL_LOOKBACK", 3),
            swing_lookback=env_int("SWING_LOOKBACK", 6),
            volume_lookback=env_int("VOLUME_LOOKBACK", 20),
            volume_multiplier=env_float("VOLUME_MULTIPLIER", 1.10),
            min_body_ratio=env_float("MIN_BODY_RATIO", 0.45),
            require_volume=env_bool("REQUIRE_VOLUME", True),
            require_market_structure=env_bool("REQUIRE_MARKET_STRUCTURE", False),
            avoid_first_minutes=env_int("AVOID_FIRST_MINUTES", 15),
            nifty_sl_buffer=env_float("NIFTY_SL_BUFFER", 10.0),
            fallback_option_sl_pct=env_float("FALLBACK_OPTION_SL_PCT", 0.20),
            rsi_period=env_int("RSI_PERIOD", 14),
            bb_period=env_int("BB_PERIOD", 20),
            bb_std=env_float("BB_STD", 2.0),
            setup_lookback=env_int("SETUP_LOOKBACK", 36),
            breakout_lookback=env_int("BREAKOUT_LOOKBACK", 4),
            min_pullback_bars=env_int("MIN_PULLBACK_BARS", 2),
            long_impulse_rsi=env_float("LONG_IMPULSE_RSI", 70.0),
            long_pullback_rsi_floor=env_float("LONG_PULLBACK_RSI_FLOOR", 40.0),
            short_impulse_rsi=env_float("SHORT_IMPULSE_RSI", 40.0),
            short_pullback_rsi_ceiling=env_float("SHORT_PULLBACK_RSI_CEILING", 70.0),
            htf_long_min_rsi=env_float("HTF_LONG_MIN_RSI", 50.0),
            htf_short_max_rsi=env_float("HTF_SHORT_MAX_RSI", 50.0),
            require_vwap=env_bool("REQUIRE_VWAP", True),
            expired_options_expiry_flag=env_str("EXPIRED_OPTIONS_EXPIRY_FLAG", "WEEK").upper(),
            expired_options_expiry_code=env_int("EXPIRED_OPTIONS_EXPIRY_CODE", 0),
            use_dhan_expired_options_api=env_bool("USE_DHAN_EXPIRED_OPTIONS_API", True),
            log_level=env_str("LOG_LEVEL", "INFO").upper(),
            log_dir=env_str("LOG_DIR", "logs"),
            log_file=env_str("LOG_FILE", "unified_nifty_bot.log"),
            log_to_console=env_bool("LOG_TO_CONSOLE", True),
            log_http_payloads=env_bool("LOG_HTTP_PAYLOADS", False),
            log_live_scan_every_seconds=env_float("LOG_LIVE_SCAN_EVERY_SECONDS", 0.0),
        )

        if require_credentials:
            missing = []
            if not cfg.dhan_client_id:
                missing.append("DHAN_CLIENT_ID")
            if not cfg.dhan_access_token:
                missing.append("DHAN_ACCESS_TOKEN")
            if not cfg.telegram_bot_token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not cfg.telegram_chat_id:
                missing.append("TELEGRAM_CHAT_ID")
            if missing:
                raise SystemExit("Missing env vars: " + ", ".join(missing))
        return cfg


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    LOG.setLevel(level)
    LOG.propagate = False
    LOG.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s [%(threadName)s] %(name)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log_dir = Path(cfg.log_dir)
    if not log_dir.is_absolute():
        log_dir = Path(__file__).resolve().parent / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / cfg.log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    LOG.addHandler(file_handler)

    if cfg.log_to_console:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(level)
        LOG.addHandler(console)

    LOG.info(
        "Logging initialized | level=%s | file=%s | strategies=%s",
        cfg.log_level,
        log_dir / cfg.log_file,
        cfg.enabled_strategies,
    )


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------


@dataclass
class OptionTradePlan:
    strategy: str
    side: str
    strike: float
    expiry: str
    security_id: int
    entry: float
    stop_loss: float
    target1: float
    target2: float
    risk: float
    quantity: int
    selection_reason: str
    underlying_entry: Optional[float] = None
    underlying_stop_loss: Optional[float] = None
    underlying_target1: Optional[float] = None
    underlying_target2: Optional[float] = None

    @property
    def rr1(self) -> float:
        return round((self.target1 - self.entry) / self.risk, 2) if self.risk > 0 else 0.0

    @property
    def rr2(self) -> float:
        return round((self.target2 - self.entry) / self.risk, 2) if self.risk > 0 else 0.0


@dataclass
class UnifiedSignal:
    strategy: str
    candle_time: dt.datetime
    direction: str
    side: str
    spot: float
    trigger_key: str
    reasons: List[str]
    option_plan: OptionTradePlan
    confidence: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LivePosition:
    plan: OptionTradePlan
    signal_time: dt.datetime
    opened_at: dt.datetime
    trigger_key: str
    strategy: str
    current_sl: float
    remaining_qty: int
    entry_order_id: Optional[str] = None
    entry_order_status: str = "NOT_PLACED"
    entry_filled_qty: int = 0
    entry_avg_price: Optional[float] = None
    stop_order_id: Optional[str] = None
    stop_order_status: str = "NOT_PLACED"
    last_exit_order_id: Optional[str] = None
    t1_hit: bool = False
    last_option_ts: Optional[pd.Timestamp] = None


@dataclass
class BacktestTrade:
    strategy: str
    trade_date: str
    signal_time: str
    entry_time: str
    exit_time: str
    side: str
    strike: float
    expiry: str
    entry: float
    initial_sl: float
    target1: float
    target2: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_points: float
    exit_reason: str
    t1_hit: bool
    selection_reason: str


@dataclass
class BacktestResult:
    strategy: str
    start_date: str
    end_date: str
    trades: List[BacktestTrade]
    errors: List[str]

    @property
    def total_pnl(self) -> float:
        return float(sum(t.pnl for t in self.trades))

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl <= 0)

    @property
    def win_rate(self) -> float:
        return (self.wins / len(self.trades) * 100.0) if self.trades else 0.0

    @property
    def max_drawdown(self) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for trade in self.trades:
            equity += trade.pnl
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return abs(max_dd)


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    return now_ist().date()


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value[:10], "%Y-%m-%d").date()


def parse_date_flexible(value: str) -> Optional[dt.date]:
    value = str(value).strip()
    for fmt_in in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value[:10], fmt_in).date()
        except Exception:
            continue
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.notna(parsed):
            return parsed.date()
    except Exception:
        pass
    return None


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out):
            return default
        return out
    except Exception:
        return default


def fmt(value: Any, decimals: int = 2) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return "-"


def tg_escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def price_tick(value: float) -> float:
    return round(max(float(value), 0.05), 2)


def market_session_open(at_time: Optional[dt.datetime] = None) -> bool:
    now = at_time or now_ist()
    if now.weekday() >= 5:
        return False
    return dt.time(9, 15) <= now.time() <= dt.time(15, 30)


def day_start_end(day: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    return dt.datetime.combine(day, dt.time(9, 15)), dt.datetime.combine(day, dt.time(15, 30))


def closed_candles_only(df: pd.DataFrame, interval_minutes: int, at_time: Optional[dt.datetime] = None) -> pd.DataFrame:
    if df.empty:
        return df
    at_time = (at_time or now_ist()).replace(tzinfo=None)
    candle_end = df["timestamp"] + pd.to_timedelta(interval_minutes, unit="m")
    out = df[candle_end <= at_time - dt.timedelta(seconds=5)].copy()
    return out.reset_index(drop=True)


def redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "<redacted>" if SENSITIVE_KEY_RE.search(str(k)) else redact_for_log(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_for_log(v) for v in value)
    return value


def to_json(value: Any, max_len: int = 3000) -> str:
    try:
        text = json.dumps(redact_for_log(value), ensure_ascii=True, default=str)
    except Exception:
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "...<truncated>"
    return text


def make_correlation_id(prefix: str) -> str:
    raw = f"UNIFY{now_ist().strftime('%y%m%d%H%M%S')}{prefix}{int(time.time() * 1000) % 100000}"
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:30]


def order_status(raw: Dict[str, Any]) -> str:
    return str(raw.get("orderStatus") or raw.get("status") or "UNKNOWN").upper()


def order_id(raw: Dict[str, Any]) -> Optional[str]:
    value = raw.get("orderId") or raw.get("order_id")
    return str(value) if value is not None else None


def order_filled_qty(raw: Dict[str, Any]) -> int:
    return int(num(raw.get("filledQty") or raw.get("filledQuantity") or raw.get("tradedQuantity"), 0))


def order_remaining_qty(raw: Dict[str, Any]) -> int:
    return int(num(raw.get("remainingQuantity"), 0))


def order_average_price(raw: Dict[str, Any]) -> float:
    return num(raw.get("averageTradedPrice") or raw.get("avgPrice") or raw.get("tradedPrice") or raw.get("price"), 0.0)


def side_allowed(side: str, cfg: Config) -> bool:
    allowed = {value.upper() for value in cfg.allowed_sides if value}
    return not allowed or side.upper() in allowed


def rolling_offset_allowed(offset: int, cfg: Config) -> bool:
    if cfg.allowed_rolling_offsets and offset not in set(cfg.allowed_rolling_offsets):
        return False
    if offset in set(cfg.blocked_rolling_offsets):
        return False
    return True


def t1_book_quantity(total_quantity: int, cfg: Config) -> int:
    quantity = max(1, int(total_quantity))
    lot_size = max(1, int(cfg.lot_size))
    if quantity <= lot_size:
        return quantity
    if quantity % lot_size == 0:
        lots = quantity // lot_size
        return lot_size * max(1, lots // 2)
    return max(1, quantity // 2)


def apply_entry_slippage(price: float, cfg: Config) -> float:
    return price_tick(price + max(0.0, cfg.backtest_entry_slippage))


def apply_exit_slippage(price: float, cfg: Config) -> float:
    return price_tick(price - max(0.0, cfg.backtest_exit_slippage))


def trade_plan_reject_reason(plan: OptionTradePlan, cfg: Config) -> Optional[str]:
    if not side_allowed(plan.side, cfg):
        return f"{plan.side} disabled by ALLOWED_SIDES"
    if plan.quantity <= 0:
        return "quantity must be positive"
    if cfg.lot_size > 1 and plan.quantity % cfg.lot_size != 0:
        return f"quantity {plan.quantity} is not a multiple of lot size {cfg.lot_size}"
    if plan.security_id <= 0 and not plan.expiry.startswith("ROLLING"):
        return "option security id is missing"

    risk = max(0.0, plan.entry - plan.stop_loss)
    risk_pct = risk / plan.entry if plan.entry > 0 else 0.0
    if cfg.min_option_risk_points > 0 and risk < cfg.min_option_risk_points:
        return f"risk {risk:.2f} below MIN_OPTION_RISK_POINTS {cfg.min_option_risk_points:.2f}"
    if cfg.max_option_risk_points > 0 and risk > cfg.max_option_risk_points:
        return f"risk {risk:.2f} above MAX_OPTION_RISK_POINTS {cfg.max_option_risk_points:.2f}"
    if cfg.max_option_sl_pct > 0 and risk_pct > cfg.max_option_sl_pct:
        return f"SL risk {risk_pct * 100:.1f}% above MAX_OPTION_SL_PCT {cfg.max_option_sl_pct * 100:.1f}%"
    return None


# -----------------------------------------------------------------------------
# Option-chain helpers
# -----------------------------------------------------------------------------


def get_oc(chain_json: Dict[str, Any]) -> Dict[str, Any]:
    data = chain_json.get("data", chain_json)
    return data.get("oc") or {}


def get_chain_spot(chain_json: Dict[str, Any]) -> float:
    data = chain_json.get("data", chain_json)
    return num(data.get("last_price"))


def get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
    return (
        oc.get(f"{strike:.6f}")
        or oc.get(f"{strike:.2f}")
        or oc.get(f"{strike:.0f}")
        or oc.get(str(int(round(strike))))
        or {}
    )


def nearest_strike(strikes: List[float], spot: float, fallback_step: int = 50) -> float:
    if strikes:
        return min(strikes, key=lambda strike: abs(strike - spot))
    return float(round(spot / fallback_step) * fallback_step)


def infer_step(strikes: List[float], fallback: int = 50) -> int:
    if len(strikes) < 2:
        return fallback
    diffs = sorted(abs(b - a) for a, b in zip(strikes[:-1], strikes[1:]) if abs(b - a) > 0)
    if not diffs:
        return fallback
    return int(round(statistics.median(diffs))) or fallback


def support_resistance_oi(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    support = None
    resistance = None
    best_pe = -1.0
    best_ce = -1.0
    for key, row in oc.items():
        try:
            strike = float(key)
        except Exception:
            continue
        pe_oi = num((row.get("pe") or {}).get("oi"))
        ce_oi = num((row.get("ce") or {}).get("oi"))
        if pe_oi > best_pe:
            best_pe = pe_oi
            support = strike
        if ce_oi > best_ce:
            best_ce = ce_oi
            resistance = strike
    return support, resistance


def pcr_near_atm(oc: Dict[str, Any], center: float, window: int) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        return None
    band = sorted(strikes, key=lambda s: abs(s - center))[: max(2, window * 2)]
    call_oi = 0.0
    put_oi = 0.0
    for strike in band:
        row = get_row(oc, strike)
        call_oi += num((row.get("ce") or {}).get("oi"))
        put_oi += num((row.get("pe") or {}).get("oi"))
    return (put_oi / call_oi) if call_oi > 0 else None


def pcr_full(oc: Dict[str, Any]) -> Optional[float]:
    ce_oi = 0.0
    pe_oi = 0.0
    for row in oc.values():
        ce_oi += num((row.get("ce") or {}).get("oi"))
        pe_oi += num((row.get("pe") or {}).get("oi"))
    return (pe_oi / ce_oi) if ce_oi > 0 else None


def max_pain(oc: Dict[str, Any]) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        return None
    oi_map = {
        strike: (
            num((get_row(oc, strike).get("ce") or {}).get("oi")),
            num((get_row(oc, strike).get("pe") or {}).get("oi")),
        )
        for strike in strikes
    }
    best = None
    best_pain = None
    for settle in strikes:
        pain = sum(max(0.0, settle - s) * ce + max(0.0, s - settle) * pe for s, (ce, pe) in oi_map.items())
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best = settle
    return best


def top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
    items: List[Tuple[float, float]] = []
    for key, value in oc.items():
        try:
            items.append((float(key), num((value.get(side) or {}).get("oi"))))
        except Exception:
            continue
    return sorted(items, key=lambda x: x[1], reverse=True)[:n]


def option_offsets(side: str, depth: int) -> List[int]:
    offsets = [0]
    for i in range(1, depth + 1):
        if side.upper() == "CE":
            offsets.extend([-i, i])
        else:
            offsets.extend([i, -i])
    return offsets


def premium_score(premium: float, offset_or_strike_distance: float, cfg: Config) -> Tuple[int, float, float]:
    preferred_mid = (cfg.preferred_premium_min + cfg.preferred_premium_max) / 2.0
    if cfg.preferred_premium_min <= premium <= cfg.preferred_premium_max:
        band = 0
    elif cfg.min_premium <= premium <= cfg.max_premium:
        band = 1
    else:
        band = 2
    return band, abs(premium - preferred_mid), abs(offset_or_strike_distance)


# -----------------------------------------------------------------------------
# Dhan and Telegram clients
# -----------------------------------------------------------------------------


class DhanApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "access-token": cfg.dhan_access_token,
                "client-id": cfg.dhan_client_id,
            }
        )

    @staticmethod
    def _response_body(response: requests.Response) -> str:
        try:
            return json.dumps(response.json(), ensure_ascii=True)
        except Exception:
            return (response.text or "").strip()

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = f"{DHAN_BASE}{endpoint}"
        started = time.perf_counter()
        if self.cfg.log_http_payloads:
            LOG.debug("Dhan request | %s %s | payload=%s | params=%s", method.upper(), endpoint, to_json(payload), to_json(params))
        try:
            response = self.session.request(
                method.upper(),
                url,
                data=json.dumps(payload) if payload is not None else None,
                params=params,
                timeout=self.cfg.http_timeout,
            )
        except requests.RequestException:
            LOG.exception("Dhan request failed before response | %s %s", method.upper(), endpoint)
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        LOG.info("Dhan response | %s %s | status=%s | elapsed_ms=%.0f", method.upper(), endpoint, response.status_code, elapsed_ms)
        if response.status_code >= 400:
            body = self._response_body(response)
            hint = ""
            if response.status_code == 401:
                hint = " Check DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN."
            if response.status_code == 403:
                hint = " Dhan order APIs may require static IP whitelisting."
            raise requests.HTTPError(f"{response.status_code} Dhan error on {endpoint}: {body}{hint}", response=response)
        return response

    def intraday_candles(
        self,
        security_id: int,
        exchange_segment: str,
        instrument: str,
        interval: int,
        start: dt.datetime,
        end: dt.datetime,
        oi: bool = True,
    ) -> pd.DataFrame:
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval),
            "oi": bool(oi),
            "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        raw = self._request("POST", "/charts/intraday", payload=payload).json()
        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]
        if isinstance(raw, dict):
            return self._dict_to_df(raw)
        if isinstance(raw, list):
            return self._rows_to_df(raw)
        raise ValueError(f"Unexpected intraday response: {type(raw)}")

    def index_intraday(self, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        return self.intraday_candles(
            self.cfg.index_security_id,
            self.cfg.index_segment,
            self.cfg.index_instrument,
            self.cfg.candle_interval,
            start,
            end,
            oi=True,
        )

    def option_intraday(self, security_id: int, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        return self.intraday_candles(
            security_id,
            self.cfg.fno_segment,
            self.cfg.option_instrument,
            self.cfg.candle_interval,
            start,
            end,
            oi=True,
        )

    def rolling_option_intraday(
        self,
        side: str,
        strike_offset: int,
        start: dt.datetime,
        end: dt.datetime,
    ) -> pd.DataFrame:
        if abs(strike_offset) > 10:
            raise ValueError("Dhan rolling option API supports only ATM +/- 10 offsets.")
        strike = "ATM" if strike_offset == 0 else f"ATM{strike_offset:+d}"
        option_type = "CALL" if side.upper() == "CE" else "PUT"
        payload = {
            "exchangeSegment": self.cfg.fno_segment,
            "interval": str(self.cfg.candle_interval),
            "securityId": self.cfg.index_security_id,
            "instrument": self.cfg.option_instrument,
            "expiryFlag": self.cfg.expired_options_expiry_flag,
            "expiryCode": self.cfg.expired_options_expiry_code,
            "strike": strike,
            "drvOptionType": option_type,
            "requiredData": ["open", "high", "low", "close", "volume", "oi", "strike", "spot"],
            "fromDate": start.strftime("%Y-%m-%d"),
            "toDate": (end.date() + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        raw = self._request("POST", "/charts/rollingoption", payload=payload).json()
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        side_key = "ce" if side.upper() == "CE" else "pe"
        series = data.get(side_key)
        if not isinstance(series, dict):
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "open_interest"])
        df = self._dict_to_df(series)
        for source, dest in (("oi", "open_interest"), ("strike", "strike"), ("spot", "spot")):
            if source in series and len(series[source]) == len(df):
                df[dest] = pd.to_numeric(series[source], errors="coerce")
        return df[(df["timestamp"] >= pd.Timestamp(start)) & (df["timestamp"] <= pd.Timestamp(end))].reset_index(drop=True)

    def expiry_list(self) -> List[str]:
        payload = {"UnderlyingScrip": self.cfg.index_security_id, "UnderlyingSeg": self.cfg.index_segment}
        data = self._request("POST", "/optionchain/expirylist", payload=payload).json()
        return [str(x) for x in data.get("data", [])]

    def pick_expiry(self, trade_date: dt.date) -> str:
        if self.cfg.preferred_expiry:
            return self.cfg.preferred_expiry
        expiries = []
        for value in self.expiry_list():
            parsed = parse_date_flexible(value)
            if parsed is not None and parsed >= trade_date:
                expiries.append((parsed, value))
        if not expiries:
            raise RuntimeError("No current Dhan expiry is available.")
        return sorted(expiries)[0][1]

    def option_chain(self, expiry: str) -> Dict[str, Any]:
        payload = {"UnderlyingScrip": self.cfg.index_security_id, "UnderlyingSeg": self.cfg.index_segment, "Expiry": expiry}
        return self._request("POST", "/optionchain", payload=payload).json()

    def place_order(
        self,
        transaction_type: str,
        security_id: int,
        quantity: int,
        order_type: str,
        price: float = 0.0,
        trigger_price: float = 0.0,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "dhanClientId": self.cfg.dhan_client_id,
            "correlationId": correlation_id or make_correlation_id(transaction_type),
            "transactionType": transaction_type.upper(),
            "exchangeSegment": self.cfg.fno_segment,
            "productType": self.cfg.order_product_type,
            "orderType": order_type.upper(),
            "validity": self.cfg.order_validity,
            "securityId": str(security_id),
            "quantity": int(quantity),
            "disclosedQuantity": 0,
            "price": float(price or 0.0),
            "triggerPrice": float(trigger_price or 0.0),
            "afterMarketOrder": False,
            "amoTime": "",
            "boProfitValue": 0.0,
            "boStopLossValue": 0.0,
        }
        LOG.warning("LIVE ORDER | %s", to_json(payload))
        data = self._request("POST", "/orders", payload=payload).json()
        if isinstance(data, dict):
            data["_request"] = payload
        return data

    def modify_order(
        self,
        order_id_value: str,
        quantity: int,
        order_type: str,
        price: float = 0.0,
        trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        payload = {
            "dhanClientId": self.cfg.dhan_client_id,
            "orderId": str(order_id_value),
            "orderType": order_type.upper(),
            "legName": "",
            "quantity": int(quantity),
            "price": float(price or 0.0),
            "disclosedQuantity": 0,
            "triggerPrice": float(trigger_price or 0.0),
            "validity": self.cfg.order_validity,
        }
        data = self._request("PUT", f"/orders/{order_id_value}", payload=payload).json()
        if isinstance(data, dict):
            data["_request"] = payload
        return data

    def cancel_order(self, order_id_value: str) -> Dict[str, Any]:
        response = self._request("DELETE", f"/orders/{order_id_value}")
        if not (response.text or "").strip():
            return {"orderId": order_id_value, "orderStatus": "CANCELLED"}
        return response.json()

    def get_order(self, order_id_value: str) -> Dict[str, Any]:
        return self._request("GET", f"/orders/{order_id_value}").json()

    @staticmethod
    def _rows_to_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
        return DhanApiClient._normalize_df(pd.DataFrame(rows))

    @staticmethod
    def _dict_to_df(data: Dict[str, Any]) -> pd.DataFrame:
        keys = {str(k).lower(): k for k in data.keys()}
        required = ["open", "high", "low", "close", "volume", "timestamp"]
        missing = [key for key in required if key not in keys]
        if missing:
            raise ValueError(f"Intraday response missing keys {missing}; got {list(data.keys())}")
        df = pd.DataFrame(
            {
                "timestamp": data[keys["timestamp"]],
                "open": data[keys["open"]],
                "high": data[keys["high"]],
                "low": data[keys["low"]],
                "close": data[keys["close"]],
                "volume": data[keys["volume"]],
            }
        )
        if "open_interest" in keys:
            df["open_interest"] = data[keys["open_interest"]]
        elif "oi" in keys:
            df["open_interest"] = data[keys["oi"]]
        return DhanApiClient._normalize_df(df)

    @staticmethod
    def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if "timestamp" not in df.columns:
            raise ValueError("Candle data has no timestamp column.")

        numeric_ts = pd.to_numeric(df["timestamp"], errors="coerce")
        if numeric_ts.notna().any():
            sample = float(numeric_ts.dropna().median())
            unit = "ms" if sample > 1_000_000_000_000 else "s"
            df["timestamp"] = (
                pd.to_datetime(numeric_ts, unit=unit, utc=True)
                .dt.tz_convert(IST)
                .dt.tz_localize(None)
            )
        else:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            if getattr(df["timestamp"].dt, "tz", None) is not None:
                df["timestamp"] = df["timestamp"].dt.tz_convert(IST).dt.tz_localize(None)

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "open_interest" not in df.columns:
            df["open_interest"] = np.nan
        else:
            df["open_interest"] = pd.to_numeric(df["open_interest"], errors="coerce")

        out = (
            df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
            .sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )
        return out


class TelegramBot:
    MAX_HTML_LEN = 3900
    MAX_TEXT_LEN = 3900

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
        self.session = requests.Session()
        self._offset = 0

    def _redact(self, text: Any) -> str:
        cleaned = str(text)
        if self.cfg.telegram_bot_token:
            cleaned = cleaned.replace(self.cfg.telegram_bot_token, "<telegram-token-redacted>")
        return cleaned

    @staticmethod
    def _plain_text(text: str) -> str:
        plain = text
        for tag in ("b", "i", "u", "s", "code", "pre"):
            plain = plain.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
        return html.unescape(plain)

    @staticmethod
    def _split_text(text: str, limit: int) -> List[str]:
        if len(text) <= limit:
            return [text]
        chunks: List[str] = []
        current: List[str] = []
        current_len = 0
        for line in text.splitlines():
            line_len = len(line) + 1
            if current and current_len + line_len > limit:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            if line_len > limit:
                for start in range(0, len(line), limit):
                    chunks.append(line[start : start + limit])
                continue
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _post_message(self, text: str, parse_mode: Optional[str]) -> None:
        if not self.enabled:
            LOG.info("Telegram disabled | message=%s", self._plain_text(text).replace("\n", " | ")[:500])
            return
        data = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        response = self.session.post(
            f"{TELEGRAM_BASE}/bot{self.cfg.telegram_bot_token}/sendMessage",
            data=data,
            timeout=self.cfg.http_timeout,
        )
        if response.status_code >= 400:
            body = self._redact(response.text)
            raise RuntimeError(f"Telegram send failed with HTTP {response.status_code}: {body}")

    def send(self, text: str) -> None:
        text = self._redact(text)
        if len(text) > self.MAX_HTML_LEN:
            plain = self._plain_text(text)
            for chunk in self._split_text(plain, self.MAX_TEXT_LEN):
                self._post_message(chunk, parse_mode=None)
            return
        try:
            self._post_message(text, parse_mode="HTML")
        except RuntimeError as exc:
            if "HTTP 400" not in str(exc):
                raise
            plain = self._plain_text(text)
            for chunk in self._split_text(plain, self.MAX_TEXT_LEN):
                self._post_message(chunk, parse_mode=None)

    def get_messages(self) -> List[str]:
        if not self.enabled:
            return []
        try:
            response = self.session.get(
                f"{TELEGRAM_BASE}/bot{self.cfg.telegram_bot_token}/getUpdates",
                params={"offset": self._offset, "timeout": 0},
                timeout=self.cfg.http_timeout + 5,
            )
            response.raise_for_status()
        except Exception:
            LOG.exception("Telegram getUpdates failed")
            return []

        messages: List[str] = []
        for update in response.json().get("result", []):
            self._offset = int(update["update_id"]) + 1
            msg = update.get("message") or update.get("channel_post") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if chat_id == str(self.cfg.telegram_chat_id) and text:
                messages.append(text)
        return messages


class MarketDataCache:
    """One per-process cache so all strategies reuse the same Dhan responses."""

    def __init__(self, cfg: Config, api: DhanApiClient):
        self.cfg = cfg
        self.api = api
        self.expiry: Optional[str] = None
        self._index_day: Optional[dt.date] = None
        self._index_fetched_at = 0.0
        self._index_df: Optional[pd.DataFrame] = None
        self._chain_expiry: Optional[str] = None
        self._chain_fetched_at = 0.0
        self._chain_json: Optional[Dict[str, Any]] = None

    def ensure_expiry(self) -> str:
        today = today_ist()
        if self.expiry is None:
            self.expiry = self.api.pick_expiry(today)
        return self.expiry

    def current_index(self, force: bool = False) -> pd.DataFrame:
        today = today_ist()
        now = time.monotonic()
        ttl = max(1.0, self.cfg.poll_seconds * 0.8)
        if (
            not force
            and self._index_df is not None
            and self._index_day == today
            and now - self._index_fetched_at < ttl
        ):
            return self._index_df.copy()
        start, end = day_start_end(today)
        self._index_df = self.api.index_intraday(start, end)
        self._index_day = today
        self._index_fetched_at = now
        return self._index_df.copy()

    def current_closed_index(self, force: bool = False) -> pd.DataFrame:
        return closed_candles_only(self.current_index(force=force), self.cfg.candle_interval)

    def option_chain(self, force: bool = False) -> Tuple[str, Dict[str, Any]]:
        expiry = self.ensure_expiry()
        now = time.monotonic()
        ttl = max(5.0, self.cfg.poll_seconds * 2.0)
        if (
            not force
            and self._chain_json is not None
            and self._chain_expiry == expiry
            and now - self._chain_fetched_at < ttl
        ):
            return expiry, self._chain_json
        self._chain_json = self.api.option_chain(expiry)
        self._chain_expiry = expiry
        self._chain_fetched_at = now
        return expiry, self._chain_json

    def clear(self) -> None:
        self._index_df = None
        self._chain_json = None
        self._index_fetched_at = 0.0
        self._chain_fetched_at = 0.0


# -----------------------------------------------------------------------------
# Indicators
# -----------------------------------------------------------------------------


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    close = out["close"].astype(float)
    typical = (high + low + close) / 3.0
    out["trade_date"] = out["timestamp"].dt.date
    pv = typical * out["volume"].astype(float)
    vol_cum = out["volume"].astype(float).groupby(out["trade_date"]).cumsum()
    out["vwap"] = pv.groupby(out["trade_date"]).cumsum() / vol_cum.replace(0, np.nan)
    return out


def add_brahmastra_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    out = add_vwap(df)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    close = out["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = wilder_rma(tr, cfg.supertrend_period)
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + cfg.supertrend_multiplier * atr
    basic_lower = hl2 - cfg.supertrend_multiplier * atr

    final_upper = pd.Series(np.nan, index=out.index, dtype=float)
    final_lower = pd.Series(np.nan, index=out.index, dtype=float)
    st = pd.Series(np.nan, index=out.index, dtype=float)
    st_dir = pd.Series(0, index=out.index, dtype=int)

    for i in range(len(out)):
        if pd.isna(atr.iloc[i]):
            continue
        if i == 0 or pd.isna(final_upper.iloc[i - 1]):
            final_upper.iloc[i] = basic_upper.iloc[i]
            final_lower.iloc[i] = basic_lower.iloc[i]
            st_dir.iloc[i] = 1 if close.iloc[i] >= hl2.iloc[i] else -1
            st.iloc[i] = final_lower.iloc[i] if st_dir.iloc[i] == 1 else final_upper.iloc[i]
            continue

        final_upper.iloc[i] = (
            basic_upper.iloc[i]
            if basic_upper.iloc[i] < final_upper.iloc[i - 1] or close.iloc[i - 1] > final_upper.iloc[i - 1]
            else final_upper.iloc[i - 1]
        )
        final_lower.iloc[i] = (
            basic_lower.iloc[i]
            if basic_lower.iloc[i] > final_lower.iloc[i - 1] or close.iloc[i - 1] < final_lower.iloc[i - 1]
            else final_lower.iloc[i - 1]
        )

        if st.iloc[i - 1] == final_upper.iloc[i - 1]:
            if close.iloc[i] <= final_upper.iloc[i]:
                st.iloc[i] = final_upper.iloc[i]
                st_dir.iloc[i] = -1
            else:
                st.iloc[i] = final_lower.iloc[i]
                st_dir.iloc[i] = 1
        else:
            if close.iloc[i] >= final_lower.iloc[i]:
                st.iloc[i] = final_lower.iloc[i]
                st_dir.iloc[i] = 1
            else:
                st.iloc[i] = final_upper.iloc[i]
                st_dir.iloc[i] = -1

    ema_fast = close.ewm(span=cfg.macd_fast, adjust=False, min_periods=cfg.macd_fast).mean()
    ema_slow = close.ewm(span=cfg.macd_slow, adjust=False, min_periods=cfg.macd_slow).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=cfg.macd_signal, adjust=False, min_periods=cfg.macd_signal).mean()
    macd_hist = macd - macd_signal

    candle_range = (high - low).replace(0, np.nan)
    out["body_ratio"] = ((close - out["open"].astype(float)).abs() / candle_range).fillna(0.0)
    out["avg_volume"] = out["volume"].astype(float).rolling(cfg.volume_lookback, min_periods=3).mean()
    out["atr"] = atr
    out["supertrend"] = st
    out["supertrend_dir"] = st_dir
    out["macd"] = macd
    out["macd_signal"] = macd_signal
    out["macd_hist"] = macd_hist
    out["st_flip_up"] = (out["supertrend_dir"].shift(1) == -1) & (out["supertrend_dir"] == 1)
    out["st_flip_down"] = (out["supertrend_dir"].shift(1) == 1) & (out["supertrend_dir"] == -1)
    out["macd_cross_up"] = (out["macd"].shift(1) <= out["macd_signal"].shift(1)) & (out["macd"] > out["macd_signal"])
    out["macd_cross_down"] = (out["macd"].shift(1) >= out["macd_signal"].shift(1)) & (out["macd"] < out["macd_signal"])
    return out.reset_index(drop=True)


def resample_ohlcv_one_day(day_df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    day_df = day_df.sort_values("timestamp").set_index("timestamp")
    out = day_df.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    out = out.dropna(subset=["open", "high", "low", "close"]).reset_index()
    out["available_at"] = out["timestamp"] + pd.to_timedelta(minutes, unit="m")
    return out


def add_rsibb_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    out = add_vwap(df)
    close = out["close"].astype(float)
    out["rsi"] = rsi_wilder(close, cfg.rsi_period)
    out["bb_mid"] = close.rolling(cfg.bb_period, min_periods=cfg.bb_period).mean()
    bb_std = close.rolling(cfg.bb_period, min_periods=cfg.bb_period).std(ddof=0)
    out["bb_upper"] = out["bb_mid"] + cfg.bb_std * bb_std
    out["bb_lower"] = out["bb_mid"] - cfg.bb_std * bb_std
    out["bb_mid_slope"] = out["bb_mid"] - out["bb_mid"].shift(3)

    htf_parts: List[pd.DataFrame] = []
    for _, day_df in out.groupby("trade_date", sort=True):
        htf = resample_ohlcv_one_day(day_df[["timestamp", "open", "high", "low", "close", "volume"]], cfg.htf_interval)
        if htf.empty:
            continue
        htf["htf_rsi"] = rsi_wilder(htf["close"], cfg.rsi_period)
        htf["htf_rsi_slope"] = htf["htf_rsi"] - htf["htf_rsi"].shift(2)
        htf_parts.append(htf[["available_at", "htf_rsi", "htf_rsi_slope"]])

    if htf_parts:
        htf_all = pd.concat(htf_parts).sort_values("available_at")
        out = pd.merge_asof(
            out.sort_values("timestamp"),
            htf_all,
            left_on="timestamp",
            right_on="available_at",
            direction="backward",
        ).drop(columns=["available_at"])
    else:
        out["htf_rsi"] = np.nan
        out["htf_rsi_slope"] = np.nan
    return out.reset_index(drop=True)


# -----------------------------------------------------------------------------
# Option selection and trade plans
# -----------------------------------------------------------------------------


@dataclass
class OptionContract:
    side: str
    strike: float
    expiry: str
    security_id: int
    entry_premium: float
    selection_reason: str
    rolling_offset: Optional[int] = None


def select_live_option_from_chain(chain_json: Dict[str, Any], expiry: str, side: str, cfg: Config) -> OptionContract:
    oc = get_oc(chain_json)
    if not oc:
        raise RuntimeError("Dhan option chain is empty.")
    spot = get_chain_spot(chain_json)
    strikes = sorted(float(k) for k in oc.keys())
    atm = nearest_strike(strikes, spot, cfg.strike_step)
    choices: List[Tuple[Tuple[int, float, float], OptionContract]] = []

    for offset in option_offsets(side, cfg.option_search_depth):
        if not rolling_offset_allowed(offset, cfg):
            continue
        strike = nearest_strike(strikes, atm + offset * infer_step(strikes, cfg.strike_step), cfg.strike_step)
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        premium = num(option.get("top_ask_price")) or num(option.get("last_price"))
        security_id = option.get("security_id") or option.get("securityId")
        if premium <= 0 or security_id is None:
            continue
        if not cfg.allow_premium_fallback and not (cfg.min_premium <= premium <= cfg.max_premium):
            continue
        reason = "preferred live premium" if cfg.preferred_premium_min <= premium <= cfg.preferred_premium_max else "nearest live premium"
        contract = OptionContract(
            side=side,
            strike=float(strike),
            expiry=expiry,
            security_id=int(float(security_id)),
            entry_premium=float(premium),
            selection_reason=reason,
            rolling_offset=offset,
        )
        choices.append((premium_score(premium, offset, cfg), contract))

    if choices:
        return sorted(choices, key=lambda item: item[0])[0][1]

    if cfg.allow_premium_fallback:
        row = get_row(oc, atm)
        option = row.get(side.lower(), {}) if row else {}
        premium = num(option.get("top_ask_price")) or num(option.get("last_price"), 1.0)
        security_id = option.get("security_id") or option.get("securityId")
        if security_id is not None:
            return OptionContract(
                side=side,
                strike=float(atm),
                expiry=expiry,
                security_id=int(float(security_id)),
                entry_premium=float(max(premium, 1.0)),
                selection_reason="fallback ATM option",
                rolling_offset=0,
            )

    raise RuntimeError(f"No {side} option found in premium range {cfg.min_premium:g}-{cfg.max_premium:g}.")


def option_stop_from_history(option_df: pd.DataFrame, entry_pos: int, entry: float, cfg: Config) -> float:
    fallback_sl = entry * (1.0 - cfg.option_sl_pct)
    prior = option_df.iloc[max(0, entry_pos - cfg.swing_lookback) : entry_pos]
    if len(prior) >= 3:
        swing_sl = float(prior["low"].min()) - cfg.option_sl_buffer
        sl = max(swing_sl, fallback_sl)
    else:
        sl = fallback_sl
    if sl >= entry:
        sl = fallback_sl
    return price_tick(sl)


def make_trade_plan(
    strategy: str,
    contract: OptionContract,
    cfg: Config,
    option_df: Optional[pd.DataFrame] = None,
    entry_pos: Optional[int] = None,
    underlying_entry: Optional[float] = None,
    underlying_stop_loss: Optional[float] = None,
    underlying_target1: Optional[float] = None,
    underlying_target2: Optional[float] = None,
) -> OptionTradePlan:
    entry = price_tick(contract.entry_premium)
    if option_df is not None and entry_pos is not None and not option_df.empty:
        sl = option_stop_from_history(option_df, entry_pos, entry, cfg)
    else:
        sl = price_tick(entry * (1.0 - cfg.option_sl_pct))
    risk = price_tick(max(entry - sl, 0.05))
    return OptionTradePlan(
        strategy=strategy,
        side=contract.side,
        strike=contract.strike,
        expiry=contract.expiry,
        security_id=contract.security_id,
        entry=entry,
        stop_loss=sl,
        target1=price_tick(entry + cfg.target1_r * risk),
        target2=price_tick(entry + cfg.target2_r * risk),
        risk=risk,
        quantity=cfg.quantity,
        selection_reason=contract.selection_reason,
        underlying_entry=underlying_entry,
        underlying_stop_loss=underlying_stop_loss,
        underlying_target1=underlying_target1,
        underlying_target2=underlying_target2,
    )


def find_exact_candle(df: pd.DataFrame, timestamp: dt.datetime) -> Optional[int]:
    if df.empty:
        return None
    ts = pd.Timestamp(timestamp)
    matches = df.index[df["timestamp"] == ts].tolist()
    return int(matches[0]) if matches else None


def find_candle_at_or_after(df: pd.DataFrame, timestamp: dt.datetime) -> Optional[int]:
    if df.empty:
        return None
    ts = pd.Timestamp(timestamp)
    matches = df.index[df["timestamp"] >= ts].tolist()
    return int(matches[0]) if matches else None


# -----------------------------------------------------------------------------
# Telegram formatting
# -----------------------------------------------------------------------------


def format_signal_message(signal: UnifiedSignal, live_trading: bool, auto_buy: bool) -> str:
    plan = signal.option_plan
    mode = "AUTO BUY" if live_trading and auto_buy else ("EDIT THEN BUY" if live_trading else "DRY RUN")
    reasons = "\n".join(f"- {tg_escape(reason)}" for reason in signal.reasons[:12])
    lines = [
        f"<b>{tg_escape(signal.strategy)} NIFTY OPTION SIGNAL</b>",
        f"Mode       : {mode}",
        f"Direction  : {tg_escape(signal.direction)}",
        f"Confidence : {tg_escape(signal.confidence or '-')}",
        f"Signal time: {tg_escape(signal.candle_time)}",
        f"Spot       : {fmt(signal.spot)}",
        "",
        "<b>Option Plan</b>",
        f"Buy        : {plan.side} {fmt(plan.strike, 0)}",
        f"Expiry     : {tg_escape(plan.expiry)}",
        f"Security ID: {plan.security_id}",
        f"Entry ref  : Rs {fmt(plan.entry)}",
        f"SL         : Rs {fmt(plan.stop_loss)}",
        f"T1 / T2    : Rs {fmt(plan.target1)} / Rs {fmt(plan.target2)}",
        f"Risk       : Rs {fmt(plan.risk)}",
        f"RR         : 1:{plan.rr1} / 1:{plan.rr2}",
        f"Qty        : {plan.quantity}",
        f"Selector   : {tg_escape(plan.selection_reason)}",
    ]
    if plan.underlying_stop_loss is not None:
        lines += [
            "",
            "<b>Underlying Levels</b>",
            f"NIFTY SL   : {fmt(plan.underlying_stop_loss)}",
            f"NIFTY T1   : {fmt(plan.underlying_target1)}",
            f"NIFTY T2   : {fmt(plan.underlying_target2)}",
        ]
    if signal.metadata:
        compact = []
        for key, value in signal.metadata.items():
            if isinstance(value, float):
                compact.append(f"{key}: {fmt(value)}")
            else:
                compact.append(f"{key}: {value}")
        lines += ["", "<b>Context</b>", "\n".join(tg_escape(x) for x in compact[:8])]
    lines += ["", "<b>Reasons</b>", reasons or "-", f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>"]
    return "\n".join(lines)


def format_order_message(title: str, plan: OptionTradePlan, order: Dict[str, Any], note: Optional[str] = None) -> str:
    req = order.get("_request", {}) if isinstance(order, dict) else {}
    lines = [
        f"<b>{tg_escape(title)}</b>",
        f"Strategy : {tg_escape(plan.strategy)}",
        f"Option   : {plan.side} {fmt(plan.strike, 0)}",
        f"Order ID : {tg_escape(order.get('orderId') or order.get('order_id') or '-')}",
        f"Status   : {tg_escape(order_status(order))}",
        f"Txn      : {tg_escape(order.get('transactionType') or req.get('transactionType') or '-')}",
        f"Qty      : {tg_escape(order.get('quantity') or req.get('quantity') or '-')}",
        f"Filled   : {tg_escape(order.get('filledQty') if order.get('filledQty') is not None else '-')}",
        f"Avg      : Rs {fmt(order.get('averageTradedPrice') or order.get('avgPrice') or 0)}",
        f"Price    : Rs {fmt(order.get('price') or req.get('price') or 0)}",
        f"Trigger  : Rs {fmt(order.get('triggerPrice') or req.get('triggerPrice') or 0)}",
    ]
    if order.get("omsErrorDescription"):
        lines.append(f"Error    : {tg_escape(order.get('omsErrorDescription'))}")
    if note:
        lines += ["", tg_escape(note)]
    lines.append(f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>")
    return "\n".join(lines)


def format_position_message(position: LivePosition, action: str, reason: str, price: Optional[float], event_time: Any) -> str:
    plan = position.plan
    lines = [
        "<b>LIVE POSITION UPDATE</b>",
        f"Action   : {tg_escape(action)}",
        f"Reason   : {tg_escape(reason)}",
        f"Time     : {tg_escape(event_time)}",
        f"Strategy : {tg_escape(position.strategy)}",
        f"Option   : {plan.side} {fmt(plan.strike, 0)}",
        f"Entry    : Rs {fmt(plan.entry)}",
        f"SL       : Rs {fmt(position.current_sl)}",
        f"T1 / T2  : Rs {fmt(plan.target1)} / Rs {fmt(plan.target2)}",
        f"Remain   : {position.remaining_qty}",
        f"T1 hit   : {'yes' if position.t1_hit else 'no'}",
    ]
    if price is not None:
        lines.append(f"Ref price: Rs {fmt(price)}")
    if position.entry_order_id:
        lines.append(f"Entry ID : {tg_escape(position.entry_order_id)}")
    if position.stop_order_id:
        lines.append(f"SL ID    : {tg_escape(position.stop_order_id)}")
    return "\n".join(lines)


def format_backtest_summary(result: BacktestResult) -> str:
    trades = result.trades
    avg_win = statistics.mean([t.pnl for t in trades if t.pnl > 0]) if any(t.pnl > 0 for t in trades) else 0.0
    avg_loss = statistics.mean([t.pnl for t in trades if t.pnl <= 0]) if any(t.pnl <= 0 for t in trades) else 0.0
    best = max((t.pnl for t in trades), default=0.0)
    worst = min((t.pnl for t in trades), default=0.0)
    lines = [
        f"{result.strategy} Backtest Result",
        f"Period      : {result.start_date} to {result.end_date}",
        f"Trades      : {len(trades)}",
        f"Wins/Losses : {result.wins}/{result.losses}",
        f"Win rate    : {result.win_rate:.1f}%",
        f"Total PnL   : Rs {fmt(result.total_pnl)}",
        f"Max DD      : Rs {fmt(result.max_drawdown)}",
        f"Avg win/loss: Rs {fmt(avg_win)} / Rs {fmt(avg_loss)}",
        f"Best/Worst  : Rs {fmt(best)} / Rs {fmt(worst)}",
    ]
    if trades:
        lines += [
            "",
            "Recent trades:",
            f"{'Entry':<16} {'Opt':<10} {'Qty':>5} {'Entry':>7} {'SL':>7} {'T1':>7} {'T2':>7} {'Exit':>7} {'PnL':>10} {'Reason'}",
            "-" * 106,
        ]
        for trade in trades[-10:]:
            entry_time = str(trade.entry_time)[5:16]
            opt = f"{int(trade.strike)}{trade.side}"
            lines.append(
                f"{entry_time:<16} {opt:<10} {trade.quantity:>5} {trade.entry:>7.2f} {trade.initial_sl:>7.2f} "
                f"{trade.target1:>7.2f} {trade.target2:>7.2f} {trade.exit_price:>7.2f} "
                f"{trade.pnl:>10.2f} {trade.exit_reason}"
            )
    if result.errors:
        lines += ["", "Warnings:"]
        lines.extend(f"- {err}" for err in result.errors[:8])
        if len(result.errors) > 8:
            lines.append(f"- plus {len(result.errors) - 8} more")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Strategy base
# -----------------------------------------------------------------------------


class BaseStrategy:
    name = "BASE"

    def __init__(self, cfg: Config, api: DhanApiClient, cache: MarketDataCache):
        self.cfg = cfg
        self.api = api
        self.cache = cache
        self.live_enabled = cfg.live_enabled_at_start
        self.last_candle_time: Optional[str] = None
        self.last_trigger: Optional[str] = None

    def live_check(self, index_df: pd.DataFrame) -> Optional[UnifiedSignal]:
        raise NotImplementedError

    def opposite_exit(self, index_df: pd.DataFrame, idx: int, side: str) -> bool:
        return False

    def run_backtest(self, start_date: str, end_date: str, stop_event: threading.Event) -> BacktestResult:
        return BacktestResult(self.name, start_date, end_date, [], ["Backtest not implemented for this strategy."])


# -----------------------------------------------------------------------------
# SCORE strategy
# -----------------------------------------------------------------------------


def pattern_function_names() -> List[str]:
    try:
        import talib  # type: ignore

        return sorted([name for name in dir(talib) if name.startswith("CDL") and callable(getattr(talib, name))])
    except Exception:
        return []


def detect_patterns(df: pd.DataFrame) -> List[str]:
    if len(df) < 5:
        return []
    try:
        import talib  # type: ignore

        open_ = df["open"].astype(float).to_numpy()
        high = df["high"].astype(float).to_numpy()
        low = df["low"].astype(float).to_numpy()
        close = df["close"].astype(float).to_numpy()
        matches: List[str] = []
        for name in pattern_function_names():
            fn = getattr(talib, name)
            try:
                out = fn(open_, high, low, close)
                if len(out) and int(out[-1]) != 0:
                    matches.append(name)
            except Exception:
                continue
        return matches
    except Exception:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        body = abs(float(last.close) - float(last.open))
        rng = max(float(last.high) - float(last.low), 1e-9)
        upper = float(last.high) - max(float(last.open), float(last.close))
        lower = min(float(last.open), float(last.close)) - float(last.low)
        matches: List[str] = []
        if lower >= 2 * body and upper <= body * 0.3:
            matches.append("CDLHAMMER")
        if upper >= 2 * body and lower <= body * 0.3:
            matches.append("CDLSHOOTINGSTAR")
        if float(last.close) > float(last.open) and float(prev.close) < float(prev.open) and float(last.close) >= float(prev.open) and float(last.open) <= float(prev.close):
            matches.append("CDLENGULFING")
        if float(last.close) < float(last.open) and float(prev.close) > float(prev.open) and float(last.open) >= float(prev.close) and float(last.close) <= float(prev.open):
            matches.append("CDLENGULFING")
        if body / rng <= 0.1:
            matches.append("CDLDOJI")
        return matches


def infer_pattern_direction(patterns: List[str]) -> str:
    bullish = sum(1 for pattern in patterns if pattern in BULLISH_PATTERNS)
    bearish = sum(1 for pattern in patterns if pattern in BEARISH_PATTERNS)
    if bullish > bearish:
        return "BULLISH"
    if bearish > bullish:
        return "BEARISH"
    return "NEUTRAL"


class ScoreStrategy(BaseStrategy):
    name = "SCORE"

    def _score_setup(self, candles: pd.DataFrame, chain: Dict[str, Any], patterns: List[str]) -> Tuple[str, int, int, List[str], Dict[str, Any]]:
        max_score = 11
        direction = infer_pattern_direction(patterns)
        if direction == "NEUTRAL":
            return direction, 0, max_score, [], {}

        enriched = add_vwap(candles)
        last = enriched.iloc[-1]
        prev = enriched.iloc[-2] if len(enriched) >= 2 else enriched.iloc[-1]
        recent = enriched.tail(6)
        recent_avg_volume = float(recent["volume"].mean()) if not recent.empty else float(last.volume)
        day_open = float(enriched.iloc[0].open)
        trend = "uptrend" if float(last.close) > float(last.vwap) and float(last.close) > day_open else (
            "downtrend" if float(last.close) < float(last.vwap) and float(last.close) < day_open else "sideways"
        )
        bullish = direction == "BULLISH"
        score = 0
        reasons: List[str] = []

        side_patterns = [p for p in patterns if (p in BULLISH_PATTERNS if bullish else p in BEARISH_PATTERNS)]
        if side_patterns:
            score += 3
            reasons.append(f"Pattern confirmation: {', '.join(side_patterns[:3])}")
        if bullish and float(last.close) > float(last.vwap):
            score += 2
            reasons.append("Price above VWAP")
        elif not bullish and float(last.close) < float(last.vwap):
            score += 2
            reasons.append("Price below VWAP")
        if bullish and float(last.close) > float(prev.high):
            score += 2
            reasons.append("Close above previous high")
        elif not bullish and float(last.close) < float(prev.low):
            score += 2
            reasons.append("Close below previous low")
        if recent_avg_volume > 0 and float(last.volume) >= recent_avg_volume * 1.15:
            score += 1
            reasons.append("Volume above recent average")
        if bullish and trend == "uptrend":
            score += 1
            reasons.append("Trend aligned to upside")
        elif not bullish and trend == "downtrend":
            score += 1
            reasons.append("Trend aligned to downside")
        candle_range = max(float(last.high) - float(last.low), 1e-9)
        body_ratio = abs(float(last.close) - float(last.open)) / candle_range
        if body_ratio >= 0.55:
            score += 1
            reasons.append("Strong candle body")
        oc = get_oc(chain)
        pcr = pcr_full(oc)
        if bullish and pcr is not None and pcr > 1.05:
            score += 1
            reasons.append(f"PCR bullish ({pcr:.2f})")
        elif not bullish and pcr is not None and pcr < 0.95:
            score += 1
            reasons.append(f"PCR bearish ({pcr:.2f})")

        metadata = {
            "score": f"{min(score, max_score)}/{max_score}",
            "vwap": float(last.vwap),
            "pcr": pcr,
            "trend": trend,
            "patterns": ", ".join(patterns[:5]),
        }
        return direction, min(score, max_score), max_score, reasons, metadata

    def _choose_strike(self, spot: float, strikes: List[float], side: str, score: int) -> float:
        if not strikes:
            return float(round(spot / self.cfg.strike_step) * self.cfg.strike_step)
        step = infer_step(strikes, self.cfg.strike_step)
        atm = nearest_strike(strikes, spot, self.cfg.strike_step)
        if score >= 10:
            if side == "CE":
                candidates = [s for s in strikes if s <= atm - step]
                return max(candidates) if candidates else atm
            candidates = [s for s in strikes if s >= atm + step]
            return min(candidates) if candidates else atm
        return atm

    def _make_score_plan(self, expiry: str, chain: Dict[str, Any], spot: float, side: str, score: int) -> OptionTradePlan:
        oc = get_oc(chain)
        strikes = sorted(float(k) for k in oc.keys())
        strike = self._choose_strike(spot, strikes, side, score)
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        security_id = option.get("security_id") or option.get("securityId")
        option_ltp = num(option.get("top_ask_price")) or num(option.get("last_price"))
        if option_ltp <= 0:
            option_ltp = max(1.0, round(abs(spot - strike) / 4.0, 2))
        sl_pct = 0.15 if score >= 10 else 0.20
        entry = price_tick(option_ltp)
        stop_loss = price_tick(max(entry * (1 - sl_pct), 1.0))
        risk = price_tick(max(entry - stop_loss, 0.05))
        return OptionTradePlan(
            strategy=self.name,
            side=side,
            strike=float(strike),
            expiry=expiry,
            security_id=int(float(security_id)) if security_id is not None else 0,
            entry=entry,
            stop_loss=stop_loss,
            target1=price_tick(entry + risk),
            target2=price_tick(entry + 2 * risk),
            risk=risk,
            quantity=self.cfg.quantity,
            selection_reason="score strategy ATM/slightly ITM option",
        )

    def build_signal(self, candles: pd.DataFrame, idx: int, expiry: str, chain: Dict[str, Any]) -> Optional[UnifiedSignal]:
        window = candles.iloc[: idx + 1].reset_index(drop=True)
        if len(window) < 5:
            return None
        patterns = detect_patterns(window)
        if not patterns:
            return None
        direction, score, max_score, reasons, metadata = self._score_setup(window, chain, patterns)
        if direction == "NEUTRAL" or score < self.cfg.min_signal_score:
            return None
        side = "CE" if direction == "BULLISH" else "PE"
        spot = float(window.iloc[-1].close)
        plan = self._make_score_plan(expiry, chain, spot, side, score)
        confidence = "Strong" if score / max_score >= 0.85 else ("Good" if score / max_score >= 0.70 else "Weak")
        candle_time = window.iloc[-1].timestamp
        candle_dt = candle_time.to_pydatetime() if hasattr(candle_time, "to_pydatetime") else candle_time
        return UnifiedSignal(
            strategy=self.name,
            candle_time=candle_dt,
            direction=direction,
            side=side,
            spot=spot,
            trigger_key=f"SCORE|{candle_dt}|{direction}|{','.join(patterns[:3])}",
            reasons=reasons,
            option_plan=plan,
            confidence=confidence,
            metadata=metadata,
        )

    def live_check(self, index_df: pd.DataFrame) -> Optional[UnifiedSignal]:
        if not self.live_enabled or index_df.empty or len(index_df) < 5:
            return None
        latest = index_df.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_candle_time:
            return None
        self.last_candle_time = candle_time
        expiry, chain = self.cache.option_chain()
        signal = self.build_signal(index_df.reset_index(drop=True), len(index_df) - 1, expiry, chain)
        if signal is None or signal.trigger_key == self.last_trigger:
            return None
        self.last_trigger = signal.trigger_key
        return signal

    def run_backtest(self, start_date: str, end_date: str, stop_event: threading.Event) -> BacktestResult:
        errors: List[str] = []
        trades: List[BacktestTrade] = []
        start_day = parse_date(start_date)
        end_day = parse_date(end_date)
        candles = self.api.index_intraday(dt.datetime.combine(start_day, dt.time(9, 15)), dt.datetime.combine(end_day, dt.time(15, 30)))
        if candles.empty:
            return BacktestResult(self.name, start_date, end_date, [], ["No NIFTY candles returned."])
        expiry, chain = self.cache.option_chain(force=True)
        last_trigger = None
        sent_signals = 0
        for idx in range(4, len(candles)):
            if stop_event.is_set():
                errors.append("Scan stopped by user.")
                break
            signal = self.build_signal(candles.reset_index(drop=True), idx, expiry, chain)
            if signal is None or signal.trigger_key == last_trigger:
                continue
            last_trigger = signal.trigger_key
            sent_signals += 1
            # SCORE kept the original scan behavior: alerts only, not a PnL trade simulator.
            trades.append(
                BacktestTrade(
                    strategy=self.name,
                    trade_date=str(pd.Timestamp(signal.candle_time).date()),
                    signal_time=str(signal.candle_time),
                    entry_time=str(signal.candle_time),
                    exit_time=str(signal.candle_time),
                    side=signal.side,
                    strike=signal.option_plan.strike,
                    expiry=signal.option_plan.expiry,
                    entry=signal.option_plan.entry,
                    initial_sl=signal.option_plan.stop_loss,
                    target1=signal.option_plan.target1,
                    target2=signal.option_plan.target2,
                    exit_price=signal.option_plan.entry,
                    quantity=signal.option_plan.quantity,
                    pnl=0.0,
                    pnl_points=0.0,
                    exit_reason="signal only",
                    t1_hit=False,
                    selection_reason=signal.option_plan.selection_reason,
                )
            )
        errors.append(f"SCORE scan produced {sent_signals} alert(s); PnL is not simulated for this strategy.")
        return BacktestResult(self.name, start_date, end_date, trades, errors)


# -----------------------------------------------------------------------------
# Brahmastra strategy
# -----------------------------------------------------------------------------


def recent_event(df: pd.DataFrame, idx: int, column: str, lookback: int) -> Tuple[bool, Optional[dt.datetime]]:
    start = max(0, idx - lookback + 1)
    for j in range(idx, start - 1, -1):
        if bool(df.iloc[j].get(column, False)):
            ts = df.iloc[j].timestamp
            return True, ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return False, None


def recent_swing_low(df: pd.DataFrame, idx: int, lookback: int) -> float:
    start = max(0, idx - lookback + 1)
    return float(df.iloc[start : idx + 1]["low"].min())


def recent_swing_high(df: pd.DataFrame, idx: int, lookback: int) -> float:
    start = max(0, idx - lookback + 1)
    return float(df.iloc[start : idx + 1]["high"].max())


def market_structure_ok(df: pd.DataFrame, idx: int, side: str) -> bool:
    if idx < 5:
        return False
    last = df.iloc[idx]
    prev = df.iloc[idx - 4 : idx]
    if side == "CE":
        return bool(float(last.close) > float(prev["high"].max()) or (
            float(last.low) > float(prev["low"].min()) and float(last.close) > float(df.iloc[idx - 1].close)
        ))
    return bool(float(last.close) < float(prev["low"].min()) or (
        float(last.high) < float(prev["high"].max()) and float(last.close) < float(df.iloc[idx - 1].close)
    ))


def underlying_levels(df: pd.DataFrame, idx: int, side: str, cfg: Config) -> Tuple[float, float, float]:
    row = df.iloc[idx]
    entry = float(row.close)
    if side == "CE":
        structure = min(recent_swing_low(df, idx, cfg.swing_lookback), float(row.vwap), float(row.supertrend))
        sl = round(structure - cfg.nifty_sl_buffer, 2)
        risk = max(entry - sl, 0.01)
        return sl, round(entry + risk, 2), round(entry + 2.0 * risk, 2)
    structure = max(recent_swing_high(df, idx, cfg.swing_lookback), float(row.vwap), float(row.supertrend))
    sl = round(structure + cfg.nifty_sl_buffer, 2)
    risk = max(sl - entry, 0.01)
    return sl, round(entry - risk, 2), round(entry - 2.0 * risk, 2)


class BrahmastraStrategy(BaseStrategy):
    name = "BRAHMASTRA"

    def evaluate(self, df: pd.DataFrame, idx: int) -> Optional[Tuple[str, str, str, List[str], Dict[str, Any]]]:
        if idx < max(self.cfg.supertrend_period + 2, self.cfg.macd_slow + self.cfg.macd_signal):
            return None
        row = df.iloc[idx]
        if pd.isna(row.supertrend) or pd.isna(row.macd) or pd.isna(row.macd_signal) or pd.isna(row.vwap):
            return None
        market_open = dt.datetime.combine(row.timestamp.date(), dt.time(9, 15))
        if row.timestamp < market_open + dt.timedelta(minutes=self.cfg.avoid_first_minutes):
            return None

        body_ratio = abs(float(row.close) - float(row.open)) / max(float(row.high) - float(row.low), 1e-9)
        strong_body = body_ratio >= self.cfg.min_body_ratio
        avg_volume = float(row.avg_volume) if not pd.isna(row.avg_volume) else float(row.volume)
        volume_ok = (float(row.volume) >= avg_volume * self.cfg.volume_multiplier) if self.cfg.require_volume else True
        st_up, st_up_time = recent_event(df, idx, "st_flip_up", self.cfg.signal_lookback)
        st_down, st_down_time = recent_event(df, idx, "st_flip_down", self.cfg.signal_lookback)
        macd_up, macd_up_time = recent_event(df, idx, "macd_cross_up", self.cfg.signal_lookback)
        macd_down, macd_down_time = recent_event(df, idx, "macd_cross_down", self.cfg.signal_lookback)

        base_reasons = [
            f"Supertrend 20,2: {fmt(row.supertrend)}",
            f"MACD: {float(row.macd):.2f}, Signal: {float(row.macd_signal):.2f}, Hist: {float(row.macd_hist):.2f}",
            f"VWAP: {fmt(row.vwap)}",
            f"Body ratio: {body_ratio * 100:.0f}%",
        ]
        bull = [
            int(row.supertrend_dir) == 1,
            st_up,
            macd_up,
            float(row.macd) > float(row.macd_signal),
            float(row.macd_hist) > 0,
            float(row.close) > float(row.vwap),
            float(row.close) > float(row.open),
            strong_body,
            volume_ok,
        ]
        bear = [
            int(row.supertrend_dir) == -1,
            st_down,
            macd_down,
            float(row.macd) < float(row.macd_signal),
            float(row.macd_hist) < 0,
            float(row.close) < float(row.vwap),
            float(row.close) < float(row.open),
            strong_body,
            volume_ok,
        ]
        if self.cfg.require_market_structure:
            bull.append(market_structure_ok(df, idx, "CE"))
            bear.append(market_structure_ok(df, idx, "PE"))
        if all(bull):
            reasons = [
                "Supertrend flipped red to green",
                "MACD bullish crossover confirmed",
                "Price closed above VWAP",
                "Confirmation candle closed bullish",
            ] + base_reasons
            if self.cfg.require_volume:
                reasons.append(f"Volume {fmt(row.volume, 0)} >= required {fmt(avg_volume * self.cfg.volume_multiplier, 0)}")
            trigger = f"BRAHMASTRA|CE|ST:{st_up_time}|MACD:{macd_up_time}"
            meta = {"vwap": float(row.vwap), "supertrend": float(row.supertrend), "macd_hist": float(row.macd_hist)}
            return "BULLISH", "CE", trigger, reasons, meta
        if all(bear):
            reasons = [
                "Supertrend flipped green to red",
                "MACD bearish crossover confirmed",
                "Price closed below VWAP",
                "Confirmation candle closed bearish",
            ] + base_reasons
            if self.cfg.require_volume:
                reasons.append(f"Volume {fmt(row.volume, 0)} >= required {fmt(avg_volume * self.cfg.volume_multiplier, 0)}")
            trigger = f"BRAHMASTRA|PE|ST:{st_down_time}|MACD:{macd_down_time}"
            meta = {"vwap": float(row.vwap), "supertrend": float(row.supertrend), "macd_hist": float(row.macd_hist)}
            return "BEARISH", "PE", trigger, reasons, meta
        return None

    def build_plan(self, side: str, spot: float, signal_time: dt.datetime, df: pd.DataFrame, idx: int) -> OptionTradePlan:
        expiry, chain = self.cache.option_chain()
        contract = select_live_option_from_chain(chain, expiry, side, self.cfg)
        option_df: Optional[pd.DataFrame] = None
        entry_pos: Optional[int] = None
        try:
            start, end = day_start_end(today_ist())
            option_df = closed_candles_only(self.api.option_intraday(contract.security_id, start, end), self.cfg.candle_interval)
            entry_pos = len(option_df) - 1 if not option_df.empty else None
        except Exception:
            LOG.exception("BRAHMASTRA live option history failed; fallback SL will be used")
        underlying_sl, underlying_t1, underlying_t2 = underlying_levels(df, idx, side, self.cfg)
        return make_trade_plan(
            self.name,
            contract,
            self.cfg,
            option_df,
            entry_pos,
            underlying_entry=spot,
            underlying_stop_loss=underlying_sl,
            underlying_target1=underlying_t1,
            underlying_target2=underlying_t2,
        )

    def build_signal(self, index_df: pd.DataFrame, idx: int) -> Optional[UnifiedSignal]:
        evaluated = self.evaluate(index_df, idx)
        if evaluated is None:
            return None
        direction, side, trigger_key, reasons, meta = evaluated
        row = index_df.iloc[idx]
        candle_time = row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp
        plan = self.build_plan(side, float(row.close), candle_time, index_df, idx)
        return UnifiedSignal(
            strategy=self.name,
            candle_time=candle_time,
            direction=direction,
            side=side,
            spot=float(row.close),
            trigger_key=trigger_key,
            reasons=reasons,
            option_plan=plan,
            confidence="Full setup",
            metadata=meta,
        )

    def live_check(self, index_df: pd.DataFrame) -> Optional[UnifiedSignal]:
        if not self.live_enabled or index_df.empty or len(index_df) < 40:
            return None
        df = add_brahmastra_indicators(index_df, self.cfg)
        latest = df.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_candle_time:
            return None
        self.last_candle_time = candle_time
        signal = self.build_signal(df, len(df) - 1)
        if signal is None or signal.trigger_key == self.last_trigger:
            return None
        self.last_trigger = signal.trigger_key
        return signal

    def opposite_exit(self, index_df: pd.DataFrame, idx: int, side: str) -> bool:
        if idx <= 0 or index_df.empty:
            return False
        df = index_df if "supertrend_dir" in index_df.columns else add_brahmastra_indicators(index_df, self.cfg)
        row = df.iloc[idx]
        if side == "CE":
            return bool(row.st_flip_down) or bool(row.macd_cross_down) or (int(row.supertrend_dir) == -1 and float(row.close) < float(row.vwap))
        return bool(row.st_flip_up) or bool(row.macd_cross_up) or (int(row.supertrend_dir) == 1 and float(row.close) > float(row.vwap))


# -----------------------------------------------------------------------------
# RSI + Bollinger strategy
# -----------------------------------------------------------------------------


class RsiBbStrategy(BaseStrategy):
    name = "RSIBB"

    @staticmethod
    def warmup_ok(row: pd.Series) -> bool:
        required = ["rsi", "bb_mid", "bb_upper", "bb_lower", "vwap", "htf_rsi", "htf_rsi_slope"]
        return all(not pd.isna(row.get(col)) for col in required)

    def build_long_signal(self, df: pd.DataFrame, idx: int) -> Optional[Tuple[str, str, str, List[str], Dict[str, Any]]]:
        row = df.iloc[idx]
        start = max(0, idx - self.cfg.setup_lookback)
        impulse_indices = [j for j in range(start, idx) if float(df.iloc[j].rsi) >= self.cfg.long_impulse_rsi]
        if not impulse_indices:
            return None
        for impulse_idx in reversed(impulse_indices):
            after = df.iloc[impulse_idx + 1 : idx]
            if len(after) < self.cfg.min_pullback_bars:
                continue
            if float(after["rsi"].min()) < self.cfg.long_pullback_rsi_floor:
                continue
            touched_band = bool(((after["low"] <= after["bb_mid"]) | (after["low"] <= after["bb_lower"])).any())
            if not touched_band:
                continue
            breakout_ref = float(after.tail(self.cfg.breakout_lookback)["high"].max())
            if not float(row.close) > breakout_ref:
                continue
            if not float(row.close) > float(row.open):
                continue
            if not float(row.close) > float(row.bb_mid):
                continue
            if self.cfg.require_vwap and not float(row.close) > float(row.vwap):
                continue
            if not (float(row.htf_rsi) > self.cfg.htf_long_min_rsi and float(row.htf_rsi_slope) > 0):
                continue
            impulse_time = df.iloc[impulse_idx].timestamp
            pullback_idx = int(after[((after["low"] <= after["bb_mid"]) | (after["low"] <= after["bb_lower"]))].index[-1])
            pullback_time = df.iloc[pullback_idx].timestamp
            reasons = [
                f"5m RSI impulse above {self.cfg.long_impulse_rsi:g}",
                "Pullback touched Bollinger mid/lower band",
                f"Pullback RSI stayed above {self.cfg.long_pullback_rsi_floor:g}",
                f"Close broke pullback high {fmt(breakout_ref)}",
                f"{self.cfg.htf_interval}m RSI is bullish at {fmt(row.htf_rsi)}",
            ]
            if self.cfg.require_vwap:
                reasons.append("Close is above VWAP")
            trigger = f"RSIBB|CE|{row.timestamp}|impulse:{impulse_time}"
            meta = {
                "rsi_5m": float(row.rsi),
                "rsi_htf": float(row.htf_rsi),
                "vwap": float(row.vwap),
                "bb_mid": float(row.bb_mid),
                "impulse_time": impulse_time,
                "pullback_time": pullback_time,
            }
            return "BULLISH", "CE", trigger, reasons, meta
        return None

    def build_short_signal(self, df: pd.DataFrame, idx: int) -> Optional[Tuple[str, str, str, List[str], Dict[str, Any]]]:
        row = df.iloc[idx]
        start = max(0, idx - self.cfg.setup_lookback)
        impulse_indices = [j for j in range(start, idx) if float(df.iloc[j].rsi) <= self.cfg.short_impulse_rsi]
        if not impulse_indices:
            return None
        for impulse_idx in reversed(impulse_indices):
            after = df.iloc[impulse_idx + 1 : idx]
            if len(after) < self.cfg.min_pullback_bars:
                continue
            if float(after["rsi"].max()) > self.cfg.short_pullback_rsi_ceiling:
                continue
            touched_band = bool(((after["high"] >= after["bb_mid"]) | (after["high"] >= after["bb_upper"])).any())
            if not touched_band:
                continue
            breakdown_ref = float(after.tail(self.cfg.breakout_lookback)["low"].min())
            if not float(row.close) < breakdown_ref:
                continue
            if not float(row.close) < float(row.open):
                continue
            if not float(row.close) < float(row.bb_mid):
                continue
            if self.cfg.require_vwap and not float(row.close) < float(row.vwap):
                continue
            if not (float(row.htf_rsi) < self.cfg.htf_short_max_rsi and float(row.htf_rsi_slope) < 0):
                continue
            impulse_time = df.iloc[impulse_idx].timestamp
            pullback_idx = int(after[((after["high"] >= after["bb_mid"]) | (after["high"] >= after["bb_upper"]))].index[-1])
            pullback_time = df.iloc[pullback_idx].timestamp
            reasons = [
                f"5m RSI weakness below {self.cfg.short_impulse_rsi:g}",
                "Rebound touched Bollinger mid/upper band",
                f"Rebound RSI stayed below {self.cfg.short_pullback_rsi_ceiling:g}",
                f"Close broke rebound low {fmt(breakdown_ref)}",
                f"{self.cfg.htf_interval}m RSI is bearish at {fmt(row.htf_rsi)}",
            ]
            if self.cfg.require_vwap:
                reasons.append("Close is below VWAP")
            trigger = f"RSIBB|PE|{row.timestamp}|impulse:{impulse_time}"
            meta = {
                "rsi_5m": float(row.rsi),
                "rsi_htf": float(row.htf_rsi),
                "vwap": float(row.vwap),
                "bb_mid": float(row.bb_mid),
                "impulse_time": impulse_time,
                "pullback_time": pullback_time,
            }
            return "BEARISH", "PE", trigger, reasons, meta
        return None

    def evaluate(self, df: pd.DataFrame, idx: int) -> Optional[Tuple[str, str, str, List[str], Dict[str, Any]]]:
        if idx < max(self.cfg.bb_period + self.cfg.min_pullback_bars + 2, self.cfg.rsi_period + 5):
            return None
        row = df.iloc[idx]
        if not self.warmup_ok(row):
            return None
        market_open = dt.datetime.combine(row.timestamp.date(), dt.time(9, 15))
        if row.timestamp < market_open + dt.timedelta(minutes=self.cfg.avoid_first_minutes):
            return None
        if row.timestamp.time() >= self.cfg.no_new_trade_after:
            return None
        return self.build_long_signal(df, idx) or self.build_short_signal(df, idx)

    def build_signal(self, index_df: pd.DataFrame, idx: int) -> Optional[UnifiedSignal]:
        evaluated = self.evaluate(index_df, idx)
        if evaluated is None:
            return None
        direction, side, trigger_key, reasons, meta = evaluated
        row = index_df.iloc[idx]
        expiry, chain = self.cache.option_chain()
        contract = select_live_option_from_chain(chain, expiry, side, self.cfg)
        plan = make_trade_plan(self.name, contract, self.cfg)
        candle_time = row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp
        return UnifiedSignal(
            strategy=self.name,
            candle_time=candle_time,
            direction=direction,
            side=side,
            spot=float(row.close),
            trigger_key=trigger_key,
            reasons=reasons,
            option_plan=plan,
            confidence="Full setup",
            metadata=meta,
        )

    def live_check(self, index_df: pd.DataFrame) -> Optional[UnifiedSignal]:
        if not self.live_enabled or index_df.empty or len(index_df) < max(60, self.cfg.bb_period + self.cfg.setup_lookback):
            return None
        df = add_rsibb_indicators(index_df, self.cfg)
        latest = df.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_candle_time:
            return None
        self.last_candle_time = candle_time
        signal = self.build_signal(df, len(df) - 1)
        if signal is None or signal.trigger_key == self.last_trigger:
            return None
        self.last_trigger = signal.trigger_key
        return signal

    def opposite_exit(self, index_df: pd.DataFrame, idx: int, side: str) -> bool:
        if index_df.empty:
            return False
        df = index_df if "rsi" in index_df.columns and "bb_mid" in index_df.columns else add_rsibb_indicators(index_df, self.cfg)
        row = df.iloc[idx]
        if side == "CE":
            return bool(float(row.rsi) < self.cfg.long_pullback_rsi_floor or (float(row.close) < float(row.bb_mid) and float(row.htf_rsi_slope) < 0))
        return bool(float(row.rsi) > self.cfg.short_pullback_rsi_ceiling or (float(row.close) > float(row.bb_mid) and float(row.htf_rsi_slope) > 0))


# -----------------------------------------------------------------------------
# Rolling-option backtest for Brahmastra and RSI+BB
# -----------------------------------------------------------------------------


def fetch_index_range(api: DhanApiClient, start_day: dt.date, end_day: dt.date) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    current = start_day
    while current <= end_day:
        chunk_end = min(current + dt.timedelta(days=89), end_day)
        start_dt = dt.datetime.combine(current, dt.time(9, 15))
        end_dt = dt.datetime.combine(chunk_end, dt.time(15, 30))
        frames.append(api.index_intraday(start_dt, end_dt))
        current = chunk_end + dt.timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


class RollingOptionBacktester:
    def __init__(self, strategy: BaseStrategy):
        self.strategy = strategy
        self.cfg = strategy.cfg
        self.api = strategy.api
        self.rolling_cache: Dict[Tuple[str, int, str], pd.DataFrame] = {}

    def indicator_df(self, raw: pd.DataFrame) -> pd.DataFrame:
        if self.strategy.name == "BRAHMASTRA":
            return add_brahmastra_indicators(raw, self.cfg)
        if self.strategy.name == "RSIBB":
            return add_rsibb_indicators(raw, self.cfg)
        return raw

    def rolling_option_day(self, side: str, offset: int, day: dt.date) -> pd.DataFrame:
        key = (side, offset, day.isoformat())
        if key not in self.rolling_cache:
            start, end = day_start_end(day)
            self.rolling_cache[key] = self.api.rolling_option_intraday(side, offset, start, end)
        return self.rolling_cache[key]

    def select_historical_option(
        self,
        side: str,
        spot_at_entry: float,
        entry_time: dt.datetime,
    ) -> Tuple[OptionContract, pd.DataFrame, int]:
        atm = float(round(spot_at_entry / self.cfg.strike_step) * self.cfg.strike_step)
        choices: List[Tuple[Tuple[int, float, float], OptionContract, pd.DataFrame, int]] = []
        failures: List[str] = []
        for offset in option_offsets(side, self.cfg.option_search_depth):
            if not rolling_offset_allowed(offset, self.cfg):
                continue
            try:
                option_df = self.rolling_option_day(side, offset, entry_time.date())
                entry_pos = find_exact_candle(option_df, entry_time)
                if entry_pos is None:
                    failures.append(f"ATM{offset:+d}: no candle at {entry_time.time()}")
                    continue
                premium = float(option_df.iloc[entry_pos].open)
                if premium <= 0:
                    failures.append(f"ATM{offset:+d}: invalid premium {premium}")
                    continue
                if not self.cfg.allow_premium_fallback and not (self.cfg.min_premium <= premium <= self.cfg.max_premium):
                    failures.append(f"ATM{offset:+d}: premium {premium:.2f} outside range")
                    continue
                strike = atm + offset * self.cfg.strike_step
                if "strike" in option_df.columns and not pd.isna(option_df.iloc[entry_pos].get("strike")):
                    strike = float(option_df.iloc[entry_pos]["strike"])
                reason = (
                    f"Dhan rolling option {self.cfg.expired_options_expiry_flag} "
                    f"expiryCode={self.cfg.expired_options_expiry_code} ATM{offset:+d}"
                )
                contract = OptionContract(
                    side=side,
                    strike=float(strike),
                    expiry=f"ROLLING-{self.cfg.expired_options_expiry_flag}-{self.cfg.expired_options_expiry_code}",
                    security_id=0,
                    entry_premium=apply_entry_slippage(float(premium), self.cfg),
                    selection_reason=reason,
                    rolling_offset=offset,
                )
                choices.append((premium_score(premium, offset, self.cfg), contract, option_df, entry_pos))
            except Exception as exc:
                failures.append(f"ATM{offset:+d}: {exc}")
        if not choices:
            raise RuntimeError("No historical option candidate available. " + "; ".join(failures[:6]))
        _, contract, option_df, entry_pos = sorted(choices, key=lambda item: item[0])[0]
        return contract, option_df, entry_pos

    def simulate_trade(
        self,
        index_df: pd.DataFrame,
        entry_idx: int,
        signal_data: Tuple[str, str, str, List[str], Dict[str, Any]],
        option_df: pd.DataFrame,
        entry_pos: int,
        plan: OptionTradePlan,
    ) -> BacktestTrade:
        direction, side, _trigger, _reasons, _meta = signal_data
        qty = plan.quantity
        t1_qty = t1_book_quantity(qty, self.cfg)
        remaining_qty = qty
        realized = 0.0
        sl = plan.stop_loss
        initial_sl = sl
        t1_hit = False
        exit_price = plan.entry
        exit_reason = "end"
        exit_time = option_df.iloc[entry_pos].timestamp
        exit_order_count = 0
        last_close = plan.entry
        index_by_ts = {pd.Timestamp(row.timestamp): i for i, row in index_df.iterrows()}

        for pos in range(entry_pos, len(option_df)):
            row = option_df.iloc[pos]
            row_ts = pd.Timestamp(row.timestamp)
            high = float(row.high)
            low = float(row.low)
            close = float(row.close)
            last_close = close
            exit_time = row.timestamp

            if row.timestamp.time() >= self.cfg.square_off_time:
                exit_price = apply_exit_slippage(close, self.cfg)
                exit_reason = "square off"
                exit_order_count += 1
                break

            # Conservative same-candle assumption: if SL and target both touch,
            # count the stop first because OHLC cannot reveal intrabar order.
            if low <= sl:
                exit_price = apply_exit_slippage(sl, self.cfg)
                exit_reason = "stop loss" if not t1_hit else "trailing/breakeven stop"
                exit_order_count += 1
                break

            if not t1_hit and high >= plan.target1:
                booked = min(t1_qty, remaining_qty)
                realized += booked * (apply_exit_slippage(plan.target1, self.cfg) - plan.entry)
                remaining_qty -= booked
                t1_hit = True
                sl = max(sl, plan.entry)
                exit_order_count += 1
                if remaining_qty <= 0:
                    exit_price = apply_exit_slippage(plan.target1, self.cfg)
                    exit_reason = "target 1 full"
                    break

            if t1_hit and high >= plan.target2:
                exit_price = apply_exit_slippage(plan.target2, self.cfg)
                exit_reason = "target 2"
                exit_order_count += 1
                break

            if self.cfg.trail_after_t1 and t1_hit and pos > entry_pos:
                prev_low = float(option_df.iloc[pos - 1].low)
                sl = max(sl, price_tick(prev_low - self.cfg.option_sl_buffer), plan.entry)

            idx = index_by_ts.get(row_ts)
            if idx is not None and idx > entry_idx and self.strategy.opposite_exit(index_df, idx, side):
                exit_price = apply_exit_slippage(close, self.cfg)
                exit_reason = f"opposite {self.strategy.name} exit"
                exit_order_count += 1
                break

        if exit_reason == "end":
            exit_price = apply_exit_slippage(last_close, self.cfg)
            exit_order_count += 1

        realized += remaining_qty * (exit_price - plan.entry)
        order_count = 1 + exit_order_count
        net_pnl = realized - order_count * self.cfg.brokerage_per_order
        signal_time = index_df.iloc[entry_idx - 1].timestamp if entry_idx > 0 else option_df.iloc[entry_pos].timestamp

        return BacktestTrade(
            strategy=self.strategy.name,
            trade_date=str(pd.Timestamp(option_df.iloc[entry_pos].timestamp).date()),
            signal_time=str(signal_time),
            entry_time=str(option_df.iloc[entry_pos].timestamp),
            exit_time=str(exit_time),
            side=plan.side,
            strike=plan.strike,
            expiry=plan.expiry,
            entry=plan.entry,
            initial_sl=initial_sl,
            target1=plan.target1,
            target2=plan.target2,
            exit_price=price_tick(exit_price),
            quantity=qty,
            pnl=round(net_pnl, 2),
            pnl_points=round(net_pnl / max(qty, 1), 2),
            exit_reason=exit_reason,
            t1_hit=t1_hit,
            selection_reason=plan.selection_reason,
        )

    def run(self, start_date: str, end_date: str, stop_event: threading.Event) -> BacktestResult:
        start_day = parse_date(start_date)
        end_day = parse_date(end_date)
        raw = fetch_index_range(self.api, start_day, end_day)
        if raw.empty:
            return BacktestResult(self.strategy.name, start_date, end_date, [], ["No NIFTY candles returned by Dhan."])
        index_df = self.indicator_df(raw)
        trades: List[BacktestTrade] = []
        errors: List[str] = []
        active_until: Optional[pd.Timestamp] = None
        trades_per_day: Dict[str, int] = {}
        last_trigger: Optional[str] = None

        for idx in range(len(index_df) - 1):
            if stop_event.is_set():
                errors.append("Scan stopped by user.")
                break
            row_ts = pd.Timestamp(index_df.iloc[idx].timestamp)
            if active_until is not None and row_ts <= active_until:
                continue
            signal_data = self.strategy.evaluate(index_df, idx)  # type: ignore[attr-defined]
            if signal_data is None:
                continue
            _direction, side, trigger, _reasons, _meta = signal_data
            if trigger == last_trigger:
                continue
            if not side_allowed(side, self.cfg):
                last_trigger = trigger
                continue

            entry_idx = idx + 1
            entry_row = index_df.iloc[entry_idx]
            entry_time = entry_row.timestamp.to_pydatetime() if hasattr(entry_row.timestamp, "to_pydatetime") else entry_row.timestamp
            day_key = str(entry_time.date())
            if entry_time.time() >= self.cfg.no_new_trade_after:
                last_trigger = trigger
                continue
            if trades_per_day.get(day_key, 0) >= self.cfg.max_trades_per_day:
                last_trigger = trigger
                continue

            try:
                contract, option_df, entry_pos = self.select_historical_option(side, float(entry_row.open), entry_time)
                plan = make_trade_plan(self.strategy.name, contract, self.cfg, option_df, entry_pos)
                reject_reason = trade_plan_reject_reason(plan, self.cfg)
                if reject_reason:
                    errors.append(f"{entry_time} {side}: skipped by filter: {reject_reason}")
                    last_trigger = trigger
                    continue
                trade = self.simulate_trade(index_df, entry_idx, signal_data, option_df, entry_pos, plan)
                trades.append(trade)
                trades_per_day[day_key] = trades_per_day.get(day_key, 0) + 1
                active_until = pd.Timestamp(trade.exit_time)
            except Exception as exc:
                LOG.exception("%s backtest signal failed | time=%s | side=%s", self.strategy.name, index_df.iloc[idx].timestamp, side)
                errors.append(f"{index_df.iloc[idx].timestamp} {side}: {exc}")
            last_trigger = trigger

        return BacktestResult(self.strategy.name, start_date, end_date, trades, errors)


def _brahmastra_run_backtest(self: BrahmastraStrategy, start_date: str, end_date: str, stop_event: threading.Event) -> BacktestResult:
    return RollingOptionBacktester(self).run(start_date, end_date, stop_event)


def _rsibb_run_backtest(self: RsiBbStrategy, start_date: str, end_date: str, stop_event: threading.Event) -> BacktestResult:
    return RollingOptionBacktester(self).run(start_date, end_date, stop_event)


BrahmastraStrategy.run_backtest = _brahmastra_run_backtest  # type: ignore[method-assign]
RsiBbStrategy.run_backtest = _rsibb_run_backtest  # type: ignore[method-assign]


# -----------------------------------------------------------------------------
# Shared live order manager
# -----------------------------------------------------------------------------


class OrderManager:
    def __init__(self, cfg: Config, api: DhanApiClient, telegram: TelegramBot, cache: MarketDataCache):
        self.cfg = cfg
        self.api = api
        self.telegram = telegram
        self.cache = cache
        self.positions: List[LivePosition] = []
        self.pending_signal: Optional[UnifiedSignal] = None
        self.pending_plan: Optional[OptionTradePlan] = None
        self.pending_entry_order_id: Optional[str] = None
        self.pending_entry_signal: Optional[UnifiedSignal] = None
        self.pending_entry_plan: Optional[OptionTradePlan] = None
        self.pending_entry_last_status: str = ""
        self.pending_entry_last_filled_qty: int = 0

    def handle_signal(self, signal: UnifiedSignal) -> None:
        self.pending_signal = signal
        self.pending_plan = signal.option_plan
        self.telegram.send(format_signal_message(signal, self.cfg.live_trading_enabled, self.cfg.auto_buy_enabled))

        reject_reason = trade_plan_reject_reason(signal.option_plan, self.cfg)
        if reject_reason:
            self.telegram.send(
                "<b>Trade skipped by risk/filter rules</b>\n"
                f"Strategy: {tg_escape(signal.strategy)}\n"
                f"Reason: {tg_escape(reject_reason)}\n\n"
                "The plan is saved as an editable draft. Use /draft, EDIT, RECALC, or BUY after fixing it."
            )
            return

        if not self.cfg.live_trading_enabled:
            self.telegram.send(
                "<b>Editable Draft Saved</b>\n"
                "Real Dhan orders are disabled. Send AUTOORDER to allow real auto-buy, "
                "or MANUALBUY to place only after BUY."
            )
            return
        if not self.cfg.auto_buy_enabled:
            self.telegram.send(
                "<b>Editable Draft Saved</b>\n"
                "Auto-buy is OFF. Review or edit the plan, then send BUY."
            )
            return
        self.execute_buy_order(signal, signal.option_plan, source="auto-buy")

    def wait_order(self, order_id_value: str, attempts: Optional[int] = None) -> Dict[str, Any]:
        attempts = attempts or self.cfg.order_status_poll_attempts
        latest: Dict[str, Any] = {"orderId": order_id_value, "orderStatus": "UNKNOWN"}
        for _ in range(max(1, attempts)):
            latest = self.api.get_order(order_id_value)
            status = order_status(latest)
            if status in ORDER_FILLED_STATUSES or status in ORDER_DEAD_STATUSES:
                return latest
            if status == "PART_TRADED" and order_remaining_qty(latest) == 0:
                return latest
            time.sleep(max(0.1, self.cfg.order_status_poll_seconds))
        return latest

    def execute_buy_order(self, signal: UnifiedSignal, plan: OptionTradePlan, source: str) -> None:
        if self.pending_entry_order_id:
            self.telegram.send("A pending entry BUY order already exists. New BUY skipped until it is filled/cancelled.")
            return
        if len(self.positions) >= self.cfg.max_open_positions:
            self.telegram.send(f"Max open positions reached ({self.cfg.max_open_positions}). New BUY skipped.")
            return
        if any(position.trigger_key == signal.trigger_key for position in self.positions):
            return
        if not self.cfg.live_trading_enabled:
            self.telegram.send("Real Dhan orders are disabled. Send AUTOORDER first if you want to place orders.")
            return
        reject_reason = trade_plan_reject_reason(plan, self.cfg)
        if reject_reason:
            self.telegram.send(f"Cannot place BUY: {tg_escape(reject_reason)}.")
            return
        if plan.security_id <= 0:
            self.telegram.send("Cannot place BUY: option security id is missing or invalid.")
            return

        entry_price = 0.0
        if self.cfg.entry_order_type == "LIMIT":
            entry_price = price_tick(plan.entry + self.cfg.entry_limit_buffer)
        try:
            entry_order = self.api.place_order(
                "BUY",
                plan.security_id,
                plan.quantity,
                self.cfg.entry_order_type,
                price=entry_price,
                correlation_id=make_correlation_id("ENTRY"),
            )
        except Exception as exc:
            failed = {
                "orderId": "-",
                "orderStatus": "REJECTED",
                "omsErrorDescription": str(exc),
                "_request": {
                    "transactionType": "BUY",
                    "quantity": plan.quantity,
                    "price": entry_price,
                    "orderType": self.cfg.entry_order_type,
                },
            }
            self.telegram.send(format_order_message("ENTRY ORDER FAILED", plan, failed))
            return

        entry_id = order_id(entry_order)
        self.telegram.send(format_order_message(f"ENTRY ORDER PLACED - {source}", plan, entry_order))
        if not entry_id:
            return

        latest = self.wait_order(entry_id)
        self.telegram.send(format_order_message("ENTRY ORDER UPDATE", plan, latest))
        filled_qty = order_filled_qty(latest)
        avg = order_average_price(latest)
        status = order_status(latest)

        if filled_qty <= 0:
            if status not in ORDER_DEAD_STATUSES:
                self.pending_entry_order_id = entry_id
                self.pending_entry_signal = signal
                self.pending_entry_plan = plan
                self.pending_entry_last_status = status
                self.pending_entry_last_filled_qty = filled_qty
                self.telegram.send(
                    "<b>Entry order is still pending</b>\n"
                    f"Order ID: <code>{tg_escape(entry_id)}</code>\n"
                    "Use MODBUY 125 to modify limit price, CANCELBUY to cancel, or BUYSTATUS to check."
                )
            return

        if avg > 0:
            plan.entry = price_tick(avg)
            plan.stop_loss = price_tick(avg * (1.0 - self.cfg.option_sl_pct))
            plan.risk = price_tick(max(plan.entry - plan.stop_loss, 0.05))
            plan.target1 = price_tick(plan.entry + self.cfg.target1_r * plan.risk)
            plan.target2 = price_tick(plan.entry + self.cfg.target2_r * plan.risk)

        stop_order = self.place_stop_order(plan, filled_qty, plan.stop_loss)
        position = LivePosition(
            plan=plan,
            signal_time=signal.candle_time,
            opened_at=now_ist().replace(tzinfo=None),
            trigger_key=signal.trigger_key,
            strategy=signal.strategy,
            current_sl=plan.stop_loss,
            remaining_qty=filled_qty,
            entry_order_id=entry_id,
            entry_order_status=status,
            entry_filled_qty=filled_qty,
            entry_avg_price=avg if avg > 0 else None,
            stop_order_id=order_id(stop_order) if stop_order else None,
            stop_order_status=order_status(stop_order) if stop_order else "NOT_PLACED",
            last_option_ts=pd.Timestamp(signal.candle_time),
        )
        self.positions.append(position)
        self.pending_signal = None
        self.pending_plan = None

    def place_stop_order(self, plan: OptionTradePlan, quantity: int, trigger_price: float) -> Optional[Dict[str, Any]]:
        if quantity <= 0:
            return None
        stop_trigger = price_tick(trigger_price)
        stop_limit = 0.0
        if self.cfg.sl_order_type == "STOP_LOSS":
            stop_limit = price_tick(stop_trigger - self.cfg.stop_loss_limit_buffer)
        try:
            stop_order = self.api.place_order(
                "SELL",
                plan.security_id,
                quantity,
                self.cfg.sl_order_type,
                price=stop_limit,
                trigger_price=stop_trigger,
                correlation_id=make_correlation_id("SL"),
            )
            self.telegram.send(format_order_message("STOP ORDER PLACED", plan, stop_order))
            return stop_order
        except Exception as exc:
            failed = {
                "orderId": "-",
                "orderStatus": "REJECTED",
                "omsErrorDescription": str(exc),
                "_request": {
                    "transactionType": "SELL",
                    "quantity": quantity,
                    "price": stop_limit,
                    "triggerPrice": stop_trigger,
                    "orderType": self.cfg.sl_order_type,
                },
            }
            self.telegram.send(
                format_order_message(
                    "STOP ORDER FAILED",
                    plan,
                    failed,
                    "The entry is tracked, but the broker-side protective SL order was not accepted.",
                )
            )
            return None

    def modify_stop(self, position: LivePosition, quantity: int, trigger_price: float, reason: str) -> None:
        trigger_price = price_tick(trigger_price)
        stop_limit = 0.0
        if self.cfg.sl_order_type == "STOP_LOSS":
            stop_limit = price_tick(trigger_price - self.cfg.stop_loss_limit_buffer)
        if quantity <= 0:
            if position.stop_order_id and position.stop_order_status not in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
                cancelled = self.api.cancel_order(position.stop_order_id)
                position.stop_order_status = order_status(cancelled)
                self.telegram.send(format_order_message(f"STOP ORDER CANCELLED - {reason}", position.plan, cancelled))
            return
        if position.stop_order_id and position.stop_order_status not in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
            try:
                order = self.api.modify_order(
                    position.stop_order_id,
                    quantity,
                    self.cfg.sl_order_type,
                    price=stop_limit,
                    trigger_price=trigger_price,
                )
                position.stop_order_status = order_status(order)
                position.current_sl = trigger_price
                self.telegram.send(format_order_message(f"STOP ORDER MODIFIED - {reason}", position.plan, order))
            except Exception as exc:
                failed = {
                    "orderId": position.stop_order_id,
                    "orderStatus": "REJECTED",
                    "omsErrorDescription": str(exc),
                    "_request": {"quantity": quantity, "price": stop_limit, "triggerPrice": trigger_price},
                }
                self.telegram.send(format_order_message(f"STOP ORDER MODIFY FAILED - {reason}", position.plan, failed))
            return
        order = self.place_stop_order(position.plan, quantity, trigger_price)
        if order:
            position.stop_order_id = order_id(order)
            position.stop_order_status = order_status(order)
            position.current_sl = trigger_price

    def sync_stop_order(self, position: LivePosition) -> bool:
        if not position.stop_order_id:
            return True
        latest = self.api.get_order(position.stop_order_id)
        status = order_status(latest)
        remaining_after_stop = order_remaining_qty(latest)
        if status != position.stop_order_status:
            position.stop_order_status = status
            self.telegram.send(format_order_message("STOP ORDER STATUS UPDATE", position.plan, latest))
        if status in ORDER_FILLED_STATUSES or (status == "PART_TRADED" and remaining_after_stop == 0):
            position.remaining_qty = 0
            self.telegram.send(format_position_message(position, "EXIT FULL", "broker stop-loss order executed", order_average_price(latest) or position.current_sl, now_ist().replace(tzinfo=None)))
            return False
        if status == "PART_TRADED" and remaining_after_stop > 0:
            position.remaining_qty = remaining_after_stop
        if status in ORDER_DEAD_STATUSES and position.remaining_qty > 0:
            self.telegram.send(
                format_order_message(
                    "STOP ORDER NOT ACTIVE",
                    position.plan,
                    latest,
                    "The position still has remaining quantity, but the protective SL order is not active.",
                )
            )
        return True

    def sync_entry_order(self, position: LivePosition) -> bool:
        if not position.entry_order_id:
            return False
        latest = self.api.get_order(position.entry_order_id)
        status = order_status(latest)
        filled_qty = order_filled_qty(latest)
        avg = order_average_price(latest)
        if status != position.entry_order_status or filled_qty != position.entry_filled_qty:
            previous_filled = position.entry_filled_qty
            position.entry_order_status = status
            position.entry_filled_qty = filled_qty
            if avg > 0:
                position.entry_avg_price = avg
            self.telegram.send(format_order_message("ENTRY ORDER STATUS UPDATE", position.plan, latest))
            if filled_qty > previous_filled and position.stop_order_id is not None:
                position.remaining_qty += filled_qty - previous_filled
                self.modify_stop(position, position.remaining_qty, position.current_sl, "additional entry fill")
        if filled_qty > 0 and position.stop_order_id is None:
            position.remaining_qty = filled_qty
            self.modify_stop(position, position.remaining_qty, position.current_sl, "entry filled")
        if status in ORDER_DEAD_STATUSES and filled_qty <= 0:
            self.telegram.send(format_order_message("ENTRY ORDER CLOSED WITHOUT FILL", position.plan, latest))
            return False
        return True

    def sell_quantity(self, position: LivePosition, quantity: int, reason: str, ref_price: Optional[float]) -> Optional[Dict[str, Any]]:
        if quantity <= 0:
            return None
        exit_price = 0.0
        if self.cfg.exit_order_type == "LIMIT":
            base_price = ref_price or position.entry_avg_price or position.plan.entry
            exit_price = price_tick(base_price - self.cfg.exit_limit_buffer)
        order = self.api.place_order(
            "SELL",
            position.plan.security_id,
            quantity,
            self.cfg.exit_order_type,
            price=exit_price,
            correlation_id=make_correlation_id("EXIT"),
        )
        position.last_exit_order_id = order_id(order)
        self.telegram.send(format_order_message(f"EXIT ORDER PLACED - {reason}", position.plan, order))
        if position.last_exit_order_id:
            latest = self.wait_order(position.last_exit_order_id, attempts=5)
            if order_status(latest) != order_status(order):
                self.telegram.send(format_order_message(f"EXIT ORDER UPDATE - {reason}", position.plan, latest))
            return latest
        return order

    def cancel_stop_before_exit(self, position: LivePosition, reason: str) -> None:
        if not position.stop_order_id:
            return
        if position.stop_order_status in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
            return
        latest = self.api.get_order(position.stop_order_id)
        latest_status = order_status(latest)
        position.stop_order_status = latest_status
        if latest_status in ORDER_FILLED_STATUSES or (latest_status == "PART_TRADED" and order_remaining_qty(latest) == 0):
            position.remaining_qty = 0
            self.telegram.send(format_order_message("STOP ORDER ALREADY EXECUTED", position.plan, latest))
            return
        if latest_status == "PART_TRADED":
            position.remaining_qty = order_remaining_qty(latest)
        if latest_status in ORDER_DEAD_STATUSES:
            return
        cancelled = self.api.cancel_order(position.stop_order_id)
        position.stop_order_status = order_status(cancelled)
        self.telegram.send(format_order_message(f"STOP ORDER CANCELLED - {reason}", position.plan, cancelled))

    def manage_positions(self, index_df: pd.DataFrame, strategies: Dict[str, BaseStrategy]) -> None:
        if not self.positions:
            return
        still_open: List[LivePosition] = []
        for position in list(self.positions):
            try:
                if self.manage_position(index_df, position, strategies):
                    still_open.append(position)
            except Exception as exc:
                LOG.exception("Live position tracking error | strategy=%s | trigger=%s", position.strategy, position.trigger_key)
                self.telegram.send(f"<b>Live position tracking error</b>\n{tg_escape(exc)}")
                still_open.append(position)
        self.positions = still_open

    def manage_position(self, index_df: pd.DataFrame, position: LivePosition, strategies: Dict[str, BaseStrategy]) -> bool:
        if not self.sync_entry_order(position):
            return False
        if position.entry_filled_qty <= 0:
            return True
        if not self.sync_stop_order(position):
            return False

        start, end = day_start_end(today_ist())
        option_df = closed_candles_only(self.api.option_intraday(position.plan.security_id, start, end), self.cfg.candle_interval)
        if option_df.empty:
            return True
        last_ts = position.last_option_ts or pd.Timestamp(position.signal_time)
        new_rows = option_df[option_df["timestamp"] > last_ts].reset_index(drop=True)
        if new_rows.empty:
            return True

        index_by_ts = {pd.Timestamp(row.timestamp): i for i, row in index_df.iterrows()}
        t1_qty = t1_book_quantity(position.plan.quantity, self.cfg)

        for _, row in new_rows.iterrows():
            row_ts = pd.Timestamp(row.timestamp)
            high = float(row.high)
            low = float(row.low)
            close = float(row.close)
            position.last_option_ts = row_ts

            if not self.sync_stop_order(position):
                return False

            if row.timestamp.time() >= self.cfg.square_off_time:
                self.cancel_stop_before_exit(position, "square off")
                if position.remaining_qty > 0:
                    self.sell_quantity(position, position.remaining_qty, "square off", close)
                    self.telegram.send(format_position_message(position, "EXIT FULL", "square-off time", close, row.timestamp))
                    position.remaining_qty = 0
                return False

            if low <= position.current_sl:
                self.telegram.send(format_position_message(position, "SL TOUCHED", "waiting for broker stop-loss order execution", position.current_sl, row.timestamp))
                return self.sync_stop_order(position)

            if not position.t1_hit and high >= position.plan.target1:
                booked_qty = min(t1_qty, position.remaining_qty)
                new_remaining = position.remaining_qty - booked_qty
                new_sl = max(position.current_sl, position.plan.entry)
                self.modify_stop(position, new_remaining, new_sl, "target 1 hit")
                if booked_qty > 0:
                    self.sell_quantity(position, booked_qty, "target 1", position.plan.target1)
                    position.remaining_qty = new_remaining
                position.t1_hit = True
                self.telegram.send(format_position_message(position, f"BOOK {booked_qty} QTY", "target 1 hit; SL moved to entry", position.plan.target1, row.timestamp))
                if position.remaining_qty <= 0:
                    return False

            if position.t1_hit and high >= position.plan.target2:
                self.cancel_stop_before_exit(position, "target 2")
                if position.remaining_qty > 0:
                    self.sell_quantity(position, position.remaining_qty, "target 2", position.plan.target2)
                    self.telegram.send(format_position_message(position, "EXIT REMAINING", "target 2 hit", position.plan.target2, row.timestamp))
                    position.remaining_qty = 0
                return False

            if self.cfg.trail_after_t1 and position.t1_hit:
                prev = option_df[option_df["timestamp"] < row.timestamp]
                if not prev.empty:
                    new_sl = max(position.current_sl, price_tick(float(prev.iloc[-1].low) - self.cfg.option_sl_buffer), position.plan.entry)
                    if new_sl > position.current_sl:
                        self.modify_stop(position, position.remaining_qty, new_sl, "previous option candle low trail")
                        self.telegram.send(format_position_message(position, "TRAIL SL", "previous option candle low trail", new_sl, row.timestamp))

            strategy = strategies.get(position.strategy)
            idx = index_by_ts.get(row_ts)
            if strategy is not None and idx is not None and pd.Timestamp(index_df.iloc[idx].timestamp) > pd.Timestamp(position.signal_time):
                if strategy.opposite_exit(index_df, idx, position.plan.side):
                    self.cancel_stop_before_exit(position, f"opposite {position.strategy} signal")
                    if position.remaining_qty > 0:
                        action = "EXIT FULL" if not position.t1_hit else "EXIT REMAINING"
                        self.sell_quantity(position, position.remaining_qty, f"opposite {position.strategy} signal", close)
                        self.telegram.send(format_position_message(position, action, f"opposite {position.strategy} signal", close, row.timestamp))
                        position.remaining_qty = 0
                    return False
        return True

    def draft_message(self) -> str:
        if self.pending_plan is None:
            return "No editable trade draft is available."
        plan = self.pending_plan
        signal_time = self.pending_signal.candle_time if self.pending_signal else "-"
        return "\n".join(
            [
                "<b>Editable Trade Draft</b>",
                f"Strategy  : {tg_escape(plan.strategy)}",
                f"Signal    : {tg_escape(signal_time)}",
                f"Option    : {plan.side} {fmt(plan.strike, 0)}",
                f"Expiry    : {tg_escape(plan.expiry)}",
                f"Security  : {plan.security_id}",
                f"Entry ref : Rs {fmt(plan.entry)}",
                f"SL        : Rs {fmt(plan.stop_loss)}",
                f"T1 / T2   : Rs {fmt(plan.target1)} / Rs {fmt(plan.target2)}",
                f"Risk      : Rs {fmt(plan.risk)}",
                f"Qty       : {plan.quantity}",
                f"Selector  : {tg_escape(plan.selection_reason)}",
                "",
                "Edit examples:",
                "<code>EDIT ENTRY 125</code>",
                "<code>EDIT SL 100</code>",
                "<code>EDIT T1 150</code>",
                "<code>EDIT T2 175</code>",
                "<code>EDIT QTY 65</code>",
                "<code>EDIT STRIKE 22500</code>",
                "<code>EDIT SECURITY 123456</code>",
                "<code>RECALC</code>",
                "<code>BUY</code>",
            ]
        )

    def recalc_plan(self, plan: OptionTradePlan) -> None:
        plan.entry = price_tick(plan.entry)
        plan.stop_loss = price_tick(plan.stop_loss)
        plan.risk = price_tick(max(plan.entry - plan.stop_loss, 0.05))
        plan.target1 = price_tick(plan.entry + self.cfg.target1_r * plan.risk)
        plan.target2 = price_tick(plan.entry + self.cfg.target2_r * plan.risk)

    def resolve_security_for_draft(self, plan: OptionTradePlan) -> None:
        if not plan.expiry or plan.expiry.startswith("ROLLING"):
            plan.expiry = self.cache.ensure_expiry()
        _expiry, chain = self.cache.option_chain(force=True)
        oc = get_oc(chain)
        row = get_row(oc, plan.strike)
        option = row.get(plan.side.lower(), {}) if row else {}
        security_id = option.get("security_id") or option.get("securityId")
        if security_id is None:
            raise RuntimeError(f"Could not resolve {plan.side} {fmt(plan.strike, 0)} security id from Dhan option chain.")
        plan.security_id = int(float(security_id))
        premium = num(option.get("top_ask_price")) or num(option.get("last_price"))
        if premium > 0:
            plan.entry = price_tick(premium)
            plan.stop_loss = price_tick(plan.entry * (1.0 - self.cfg.option_sl_pct))
            self.recalc_plan(plan)
        plan.selection_reason = "manual strike resolved from live Dhan option chain"

    def edit_draft(self, field: str, value: str) -> str:
        if self.pending_plan is None:
            return "No draft to edit. Wait for a signal or create one from a scan."
        plan = self.pending_plan
        key = field.strip().lower().replace("_", "")
        raw_value = value.strip()
        if key == "side":
            side = raw_value.upper()
            if side not in {"CE", "PE"}:
                return "SIDE must be CE or PE."
            plan.side = side
            self.resolve_security_for_draft(plan)
        elif key == "strike":
            plan.strike = float(raw_value)
            self.resolve_security_for_draft(plan)
        elif key in {"security", "securityid"}:
            plan.security_id = int(float(raw_value))
            plan.selection_reason = "manual security id"
        elif key == "expiry":
            plan.expiry = raw_value
            self.resolve_security_for_draft(plan)
        elif key in {"entry", "price", "limit"}:
            plan.entry = price_tick(float(raw_value))
            plan.risk = price_tick(max(plan.entry - plan.stop_loss, 0.05))
        elif key in {"sl", "stop", "stoploss"}:
            plan.stop_loss = price_tick(float(raw_value))
            plan.risk = price_tick(max(plan.entry - plan.stop_loss, 0.05))
        elif key in {"t1", "target1"}:
            plan.target1 = price_tick(float(raw_value))
        elif key in {"t2", "target2"}:
            plan.target2 = price_tick(float(raw_value))
        elif key in {"qty", "quantity"}:
            plan.quantity = int(float(raw_value))
            if plan.quantity <= 0:
                return "Quantity must be positive."
            if self.cfg.lot_size > 1 and plan.quantity % self.cfg.lot_size != 0:
                return f"Quantity must be a multiple of lot size {self.cfg.lot_size}."
        else:
            return f"Unknown editable field: {tg_escape(field)}"
        return self.draft_message()

    def buy_draft(self) -> None:
        if self.pending_plan is None:
            self.telegram.send("No draft is available to buy.")
            return
        if self.pending_signal is None:
            self.telegram.send("Draft has no signal context; cannot place order.")
            return
        self.execute_buy_order(self.pending_signal, self.pending_plan, source="manual BUY")

    def modify_pending_buy(self, command: str) -> None:
        if not self.pending_entry_order_id or self.pending_entry_plan is None:
            self.telegram.send("No pending entry BUY order is stored.")
            return
        parts = command.split()
        if len(parts) == 2:
            price = price_tick(float(parts[1]))
            quantity = self.pending_entry_plan.quantity
        else:
            values = {parts[i].lower(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
            price = price_tick(float(values.get("price", values.get("entry", self.pending_entry_plan.entry))))
            quantity = int(float(values.get("qty", values.get("quantity", self.pending_entry_plan.quantity))))
        order = self.api.modify_order(
            self.pending_entry_order_id,
            quantity,
            self.cfg.entry_order_type,
            price=price if self.cfg.entry_order_type == "LIMIT" else 0.0,
            trigger_price=0.0,
        )
        self.pending_entry_plan.entry = price
        self.pending_entry_plan.quantity = quantity
        self.telegram.send(format_order_message("PENDING BUY MODIFIED", self.pending_entry_plan, order))

    def cancel_pending_buy(self) -> None:
        if not self.pending_entry_order_id:
            self.telegram.send("No pending entry BUY order is stored.")
            return
        order = self.api.cancel_order(self.pending_entry_order_id)
        plan = self.pending_entry_plan or self.pending_plan
        if plan:
            self.telegram.send(format_order_message("PENDING BUY CANCELLED", plan, order))
        else:
            self.telegram.send(f"Pending BUY cancelled: <code>{tg_escape(self.pending_entry_order_id)}</code>")
        self.pending_entry_order_id = None
        self.pending_entry_signal = None
        self.pending_entry_plan = None
        self.pending_entry_last_status = ""
        self.pending_entry_last_filled_qty = 0

    def activate_filled_pending_buy(self, latest: Dict[str, Any]) -> None:
        if not self.pending_entry_order_id or self.pending_entry_plan is None or self.pending_entry_signal is None:
            return
        plan = self.pending_entry_plan
        signal = self.pending_entry_signal
        filled_qty = order_filled_qty(latest)
        if filled_qty <= 0:
            return
        if order_status(latest) == "PART_TRADED":
            try:
                cancelled = self.api.cancel_order(self.pending_entry_order_id)
                self.telegram.send(format_order_message("PENDING BUY REMAINDER CANCELLED", plan, cancelled))
            except Exception as exc:
                self.telegram.send(f"<b>Pending buy cancel warning</b>\n{tg_escape(exc)}")
        avg = order_average_price(latest)
        if avg > 0:
            plan.entry = price_tick(avg)
            plan.stop_loss = price_tick(avg * (1.0 - self.cfg.option_sl_pct))
            self.recalc_plan(plan)
        stop_order = self.place_stop_order(plan, filled_qty, plan.stop_loss)
        position = LivePosition(
            plan=plan,
            signal_time=signal.candle_time,
            opened_at=now_ist().replace(tzinfo=None),
            trigger_key=signal.trigger_key,
            strategy=signal.strategy,
            current_sl=plan.stop_loss,
            remaining_qty=filled_qty,
            entry_order_id=self.pending_entry_order_id,
            entry_order_status=order_status(latest),
            entry_filled_qty=filled_qty,
            entry_avg_price=avg if avg > 0 else None,
            stop_order_id=order_id(stop_order) if stop_order else None,
            stop_order_status=order_status(stop_order) if stop_order else "NOT_PLACED",
            last_option_ts=pd.Timestamp(signal.candle_time),
        )
        self.positions.append(position)
        self.telegram.send(format_position_message(position, "ENTRY FILLED", "Dhan buy filled; protective SL placed.", plan.entry, now_ist().replace(tzinfo=None)))
        self.pending_entry_order_id = None
        self.pending_entry_signal = None
        self.pending_entry_plan = None
        self.pending_entry_last_status = ""
        self.pending_entry_last_filled_qty = 0
        self.pending_signal = None
        self.pending_plan = None

    def sync_pending_buy(self) -> None:
        if not self.pending_entry_order_id or self.pending_entry_plan is None:
            return
        latest = self.api.get_order(self.pending_entry_order_id)
        status = order_status(latest)
        filled_qty = order_filled_qty(latest)
        changed = status != self.pending_entry_last_status or filled_qty != self.pending_entry_last_filled_qty
        if changed:
            self.telegram.send(format_order_message("PENDING BUY UPDATE", self.pending_entry_plan, latest))
            self.pending_entry_last_status = status
            self.pending_entry_last_filled_qty = filled_qty
        if filled_qty > 0:
            self.activate_filled_pending_buy(latest)
        elif status in ORDER_DEAD_STATUSES:
            self.telegram.send(f"Pending BUY order closed with status: <b>{tg_escape(status)}</b>")
            self.pending_entry_order_id = None
            self.pending_entry_signal = None
            self.pending_entry_plan = None
            self.pending_entry_last_status = ""
            self.pending_entry_last_filled_qty = 0

    def pending_buy_status(self) -> str:
        if not self.pending_entry_order_id or self.pending_entry_plan is None:
            return "No pending entry BUY order is stored."
        latest = self.api.get_order(self.pending_entry_order_id)
        filled_qty = order_filled_qty(latest)
        status = order_status(latest)
        self.pending_entry_last_status = status
        self.pending_entry_last_filled_qty = filled_qty
        if filled_qty > 0:
            self.activate_filled_pending_buy(latest)
        return "\n".join(
            [
                "<b>Pending BUY Status</b>",
                f"Order ID : {tg_escape(self.pending_entry_order_id)}",
                f"Strategy : {tg_escape(self.pending_entry_plan.strategy)}",
                f"Status   : {tg_escape(status)}",
                f"Filled   : {filled_qty}",
                f"Avg      : Rs {fmt(order_average_price(latest))}",
            ]
        )

    def positions_message(self) -> str:
        lines: List[str] = []
        if not self.positions:
            lines.append("No active live positions are being tracked.")
        else:
            lines += ["<b>Active Live Positions</b>", f"Count: {len(self.positions)}"]
            for idx, position in enumerate(self.positions, start=1):
                plan = position.plan
                lines += [
                    "",
                    f"<b>#{idx} {tg_escape(position.strategy)} {plan.side} {fmt(plan.strike, 0)}</b>",
                    f"Expiry      : {tg_escape(plan.expiry)}",
                    f"Security ID : {plan.security_id}",
                    f"Entry       : Rs {fmt(plan.entry)}",
                    f"Entry Order : {tg_escape(position.entry_order_id or '-')}",
                    f"Entry Status: {tg_escape(position.entry_order_status)}",
                    f"SL Order    : {tg_escape(position.stop_order_id or '-')}",
                    f"SL Status   : {tg_escape(position.stop_order_status)}",
                    f"Current SL  : Rs {fmt(position.current_sl)}",
                    f"Target 1    : Rs {fmt(plan.target1)}",
                    f"Target 2    : Rs {fmt(plan.target2)}",
                    f"T1 Hit      : {'yes' if position.t1_hit else 'no'}",
                    f"Remaining   : {position.remaining_qty} qty",
                    f"Opened at   : {tg_escape(position.opened_at)}",
                ]
        if self.pending_entry_order_id:
            lines += [
                "",
                "<b>Pending Entry BUY</b>",
                f"Order ID : {tg_escape(self.pending_entry_order_id)}",
                f"Strategy : {tg_escape(self.pending_entry_plan.strategy if self.pending_entry_plan else '-')}",
                f"Status   : {tg_escape(self.pending_entry_last_status or '-')}",
                f"Filled   : {self.pending_entry_last_filled_qty}",
            ]
        return "\n".join(lines)


# -----------------------------------------------------------------------------
# Unified bot application
# -----------------------------------------------------------------------------


HELP_TEXT = """<b>Unified NIFTY Bot Commands</b>

<b>Live control</b>
LIVE - enable live monitoring for all enabled strategies
STOP - stop running scan and pause live monitoring
DRYRUN - alerts only, no real orders
MANUALBUY - real orders enabled, but wait for BUY
AUTOORDER or AUTOBUY - real orders enabled + auto-buy

<b>Backtest / scan</b>
SCAN 2026-05-01 2026-05-14 - run all enabled strategies
SCAN SCORE 2026-05-01 2026-05-14
SCAN BRAHMASTRA 2026-05-01 2026-05-14
SCAN RSIBB 2026-05-01 2026-05-14

<b>Editable draft / orders</b>
/draft - latest signal draft
EDIT ENTRY 125, EDIT SL 100, EDIT T1 150, EDIT T2 175, EDIT QTY 65
EDIT STRIKE 22500, EDIT SECURITY 123456
RECALC - recalculate T1/T2 from entry and SL
BUY - place Dhan BUY from draft
MODBUY 125 - modify pending limit BUY price
CANCELBUY - cancel pending BUY
BUYSTATUS - check pending BUY

<b>Info</b>
/chain - option chain around ATM
/status - NIFTY context and bot mode
/position - active positions and pending BUY
/expiry - selected expiry
/help - this message

<b>Strategy live toggles</b>
/score_live, /brahmastra_live, /rsibb_live
/score_stop, /brahmastra_stop, /rsibb_stop

Live mode can place REAL DHAN ORDERS when order mode is enabled.
"""


class UnifiedNiftyBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = DhanApiClient(cfg)
        self.telegram = TelegramBot(cfg)
        self.cache = MarketDataCache(cfg, self.api)
        self.order_manager = OrderManager(cfg, self.api, self.telegram, self.cache)
        self.live_enabled = cfg.live_enabled_at_start
        self.scan_thread: Optional[threading.Thread] = None
        self.scan_stop_event = threading.Event()
        self.scan_lock = threading.Lock()
        self._last_live_scan_log_at = 0.0

        enabled = {self.normalize_strategy_name(name) for name in cfg.enabled_strategies}
        self.strategies: Dict[str, BaseStrategy] = {}
        if "SCORE" in enabled:
            self.strategies["SCORE"] = ScoreStrategy(cfg, self.api, self.cache)
        if "BRAHMASTRA" in enabled:
            self.strategies["BRAHMASTRA"] = BrahmastraStrategy(cfg, self.api, self.cache)
        if "RSIBB" in enabled:
            self.strategies["RSIBB"] = RsiBbStrategy(cfg, self.api, self.cache)
        if not self.strategies:
            raise SystemExit("No strategies enabled. Set ENABLED_STRATEGIES=SCORE,BRAHMASTRA,RSIBB")

    @staticmethod
    def normalize_strategy_name(name: str) -> str:
        cleaned = name.strip().upper().replace("-", "_")
        if cleaned in {"RSI", "RSI_BB", "RSI+BB", "RSIBB"}:
            return "RSIBB"
        if cleaned in {"BRAHMA", "BRAHMASTRA"}:
            return "BRAHMASTRA"
        if cleaned in {"SCORE", "SIGNAL"}:
            return "SCORE"
        return cleaned

    def live_scan_log(self, message: str, *args: Any, force: bool = False) -> None:
        every = max(0.0, self.cfg.log_live_scan_every_seconds)
        now = time.monotonic()
        if force or every <= 0.0 or now - self._last_live_scan_log_at >= every:
            LOG.info(message, *args)
            self._last_live_scan_log_at = now
        else:
            LOG.debug(message, *args)

    def set_all_live(self, enabled: bool) -> None:
        self.live_enabled = enabled
        for strategy in self.strategies.values():
            strategy.live_enabled = enabled

    def strategy_live(self, name: str, enabled: bool) -> str:
        strategy_name = self.normalize_strategy_name(name)
        strategy = self.strategies.get(strategy_name)
        if strategy is None:
            return f"Strategy not enabled: {tg_escape(strategy_name)}"
        strategy.live_enabled = enabled
        return f"{strategy_name} live monitoring {'enabled' if enabled else 'paused'}."

    def startup_message(self) -> str:
        mode = "REAL ORDERS" if self.cfg.live_trading_enabled else "DRY RUN ALERTS"
        buy_mode = "AUTO BUY" if self.cfg.auto_buy_enabled else "EDIT THEN BUY"
        return "\n".join(
            [
                f"<b>{tg_escape(self.cfg.index_name)} Unified Bot is online</b>",
                "",
                f"Live monitoring: {'ON' if self.live_enabled else 'OFF'}",
                f"Mode: {mode}",
                f"Buy flow: {buy_mode}",
                f"Strategies: {', '.join(self.strategies.keys())}",
                f"Poll seconds: {self.cfg.poll_seconds:g}",
                "",
                "One Telegram loop and one Dhan client are shared by all strategies.",
                "Send /help for commands.",
            ]
        )

    def chain_message(self) -> str:
        expiry, chain = self.cache.option_chain(force=True)
        oc = get_oc(chain)
        spot = get_chain_spot(chain)
        if not oc:
            return "Option chain is empty."
        strikes = sorted(float(k) for k in oc.keys())
        atm = nearest_strike(strikes, spot, self.cfg.strike_step)
        nearby = sorted(strikes, key=lambda strike: abs(strike - atm))[: self.cfg.strikes_window * 2 + 1]
        support, resistance = support_resistance_oi(oc)
        pcr = pcr_near_atm(oc, spot, self.cfg.strikes_window)
        pain = max_pain(oc)
        lines = [
            f"<b>{tg_escape(self.cfg.index_name)} Option Chain</b>",
            f"Expiry: {tg_escape(expiry)} | Spot: {fmt(spot)} | ATM: {fmt(atm, 0)}",
            f"PCR: {fmt(pcr)} | Max Pain: {fmt(pain, 0)}",
            f"Support: {fmt(support, 0)} | Resistance: {fmt(resistance, 0)}",
            "",
            "<pre>",
            f"{'Strike':<8} {'Tag':<6} {'CE OI':>10} {'CE LTP':>8} {'CE Ask':>8} | {'PE Ask':>8} {'PE LTP':>8} {'PE OI':>10}",
            "-" * 78,
        ]
        for strike in sorted(nearby):
            row = get_row(oc, strike)
            ce = row.get("ce") or {}
            pe = row.get("pe") or {}
            tag = ("ATM" if strike == atm else "") + (" S" if strike == support else "") + (" R" if strike == resistance else "")
            lines.append(
                f"{strike:<8.0f} {tag.strip():<6} {num(ce.get('oi')):>10.0f} {num(ce.get('last_price')):>8.2f} "
                f"{num(ce.get('top_ask_price')):>8.2f} | {num(pe.get('top_ask_price')):>8.2f} "
                f"{num(pe.get('last_price')):>8.2f} {num(pe.get('oi')):>10.0f}"
            )
        lines += ["</pre>", "S=Support R=Resistance", f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>"]
        return "\n".join(lines)

    def status_message(self) -> str:
        lines = [
            f"<b>{tg_escape(self.cfg.index_name)} Unified Status</b>",
            f"Live monitoring: {'ON' if self.live_enabled else 'OFF'}",
            f"Order mode: {'REAL ORDERS' if self.cfg.live_trading_enabled else 'DRY RUN'}",
            f"Buy flow: {'AUTO BUY' if self.cfg.auto_buy_enabled else 'EDIT THEN BUY'}",
            f"Strategies: {', '.join(self.strategies.keys())}",
            f"Open positions: {len(self.order_manager.positions)}",
        ]
        try:
            raw = self.cache.current_closed_index(force=True)
            if not raw.empty:
                row = raw.iloc[-1]
                lines += [
                    "",
                    "<b>Latest NIFTY Candle</b>",
                    f"Time  : {tg_escape(row.timestamp)}",
                    f"Close : {fmt(row.close)}",
                    f"High  : {fmt(row.high)}",
                    f"Low   : {fmt(row.low)}",
                ]
                vwap_df = add_vwap(raw)
                lines.append(f"VWAP  : {fmt(vwap_df.iloc[-1].vwap)}")
                if "BRAHMASTRA" in self.strategies and len(raw) >= 40:
                    bdf = add_brahmastra_indicators(raw, self.cfg)
                    brow = bdf.iloc[-1]
                    lines += [
                        "",
                        "<b>Brahmastra</b>",
                        f"ST dir: {int(brow.supertrend_dir)} | ST: {fmt(brow.supertrend)}",
                        f"MACD hist: {fmt(brow.macd_hist)}",
                    ]
                if "RSIBB" in self.strategies and len(raw) >= max(60, self.cfg.bb_period + self.cfg.setup_lookback):
                    rdf = add_rsibb_indicators(raw, self.cfg)
                    rrow = rdf.iloc[-1]
                    lines += [
                        "",
                        "<b>RSI+BB</b>",
                        f"RSI 5m: {fmt(rrow.rsi)} | RSI {self.cfg.htf_interval}m: {fmt(rrow.htf_rsi)}",
                        f"BB lower/mid/upper: {fmt(rrow.bb_lower)} / {fmt(rrow.bb_mid)} / {fmt(rrow.bb_upper)}",
                    ]
        except Exception as exc:
            LOG.exception("Status candle context failed")
            lines += ["", f"Candle context error: {tg_escape(exc)}"]
        try:
            expiry, chain = self.cache.option_chain()
            oc = get_oc(chain)
            spot = get_chain_spot(chain)
            support, resistance = support_resistance_oi(oc)
            pcr = pcr_near_atm(oc, spot, self.cfg.strikes_window)
            lines += [
                "",
                "<b>Option Chain</b>",
                f"Expiry: {tg_escape(expiry)}",
                f"Spot: {fmt(spot)} | PCR: {fmt(pcr)}",
                f"Support: {fmt(support, 0)} | Resistance: {fmt(resistance, 0)}",
            ]
        except Exception as exc:
            LOG.exception("Status option context failed")
            lines += ["", f"Option context error: {tg_escape(exc)}"]
        lines.append(f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>")
        return "\n".join(lines)

    def strike_snapshot(self, side: str, strike: float) -> str:
        expiry, chain = self.cache.option_chain(force=True)
        oc = get_oc(chain)
        spot = get_chain_spot(chain)
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        if not option:
            strikes = sorted(float(k) for k in oc.keys())
            if not strikes:
                return "Option chain is empty."
            return f"Strike not found: {side} {fmt(strike, 0)}\nRange: {fmt(strikes[0], 0)} - {fmt(strikes[-1], 0)}"
        return "\n".join(
            [
                "<b>Strike Snapshot</b>",
                f"Expiry: {tg_escape(expiry)}",
                f"Spot: {fmt(spot)}",
                f"Option: {side} {fmt(strike, 0)}",
                f"Security ID: {tg_escape(option.get('security_id') or option.get('securityId') or '-')}",
                f"LTP: Rs {fmt(option.get('last_price'))}",
                f"Ask: Rs {fmt(option.get('top_ask_price'))}",
                f"OI: {fmt(option.get('oi'), 0)}",
            ]
        )

    def start_scan(self, strategy_name: Optional[str], start_date: str, end_date: str) -> None:
        with self.scan_lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                self.telegram.send("A scan/backtest is already running. Send STOP first.")
                return
            self.scan_stop_event.clear()
            self.scan_thread = threading.Thread(
                target=self.scan_worker,
                args=(strategy_name, start_date, end_date),
                daemon=True,
                name=f"Scan-{strategy_name or 'ALL'}-{start_date}-{end_date}",
            )
            self.scan_thread.start()

    def scan_worker(self, strategy_name: Optional[str], start_date: str, end_date: str) -> None:
        try:
            if strategy_name:
                names = [self.normalize_strategy_name(strategy_name)]
            else:
                names = list(self.strategies.keys())
            missing = [name for name in names if name not in self.strategies]
            if missing:
                self.telegram.send(f"Strategy not enabled: {', '.join(missing)}")
                return
            self.telegram.send(
                "<b>Scan/backtest started</b>\n"
                f"Strategies: {', '.join(names)}\n"
                f"Period: {tg_escape(start_date)} to {tg_escape(end_date)}"
            )
            for name in names:
                if self.scan_stop_event.is_set():
                    self.telegram.send("Scan stopped by user.")
                    break
                strategy = self.strategies[name]
                result = strategy.run_backtest(start_date, end_date, self.scan_stop_event)
                self.telegram.send(f"<pre>{tg_escape(format_backtest_summary(result))}</pre>")
                self.save_backtest_outputs(result)
        except Exception as exc:
            LOG.exception("Scan worker error")
            self.telegram.send(f"<b>Scan/backtest error</b>\n{tg_escape(exc)}")
        finally:
            self.scan_stop_event.clear()
            with self.scan_lock:
                self.scan_thread = None

    def save_backtest_outputs(self, result: BacktestResult) -> None:
        output_dir = Path(__file__).resolve().parent / "backtest_results"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{result.strategy.lower()}_{result.start_date}_to_{result.end_date}"
        csv_path = output_dir / f"{stamp}.csv"
        json_path = output_dir / f"{stamp}.json"
        pd.DataFrame([dataclasses.asdict(trade) for trade in result.trades]).to_csv(csv_path, index=False)
        payload = {
            "strategy": result.strategy,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "trades": [dataclasses.asdict(trade) for trade in result.trades],
            "errors": result.errors,
            "summary": {
                "total_pnl": result.total_pnl,
                "wins": result.wins,
                "losses": result.losses,
                "win_rate": result.win_rate,
                "max_drawdown": result.max_drawdown,
            },
        }
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        self.telegram.send(f"Saved results:\nCSV: <code>{tg_escape(csv_path)}</code>\nJSON: <code>{tg_escape(json_path)}</code>")

    def dispatch(self, raw: str) -> None:
        command = raw.strip()
        lower = command.split("@")[0].strip().lower()

        named_scan = SCAN_NAMED_RE.match(command)
        if named_scan:
            self.start_scan(named_scan.group(1), named_scan.group(2), named_scan.group(3))
            return

        scan = SCAN_RE.match(command)
        if scan:
            self.start_scan(None, scan.group(1), scan.group(2))
            return

        strike_match = STRIKE_RE.match(command.replace(" ", ""))
        if strike_match:
            self.telegram.send(self.strike_snapshot(strike_match.group(1).upper(), float(strike_match.group(2))))
            return

        edit_match = re.match(r"^(?:EDIT|SET)\s+([A-Za-z_]+)\s+(.+)$", command, re.IGNORECASE)
        if edit_match:
            self.telegram.send(self.order_manager.edit_draft(edit_match.group(1), edit_match.group(2)))
            return

        if lower in {"/help", "help", "/start", "start"}:
            self.telegram.send(HELP_TEXT)
        elif lower in {"/chain", "chain"}:
            self.telegram.send(self.chain_message())
        elif lower in {"/status", "status"}:
            self.telegram.send(self.status_message())
        elif lower in {"/position", "position"}:
            self.telegram.send(self.order_manager.positions_message())
        elif lower in {"/expiry", "expiry"}:
            self.telegram.send(f"Selected expiry: <b>{tg_escape(self.cache.ensure_expiry())}</b>")
        elif lower in {"/draft", "draft", "plan"}:
            self.telegram.send(self.order_manager.draft_message())
        elif lower in {"buy", "/buy", "confirm", "/confirm", "placebuy", "/placebuy"}:
            self.order_manager.buy_draft()
        elif lower in {"recalc", "/recalc"}:
            if self.order_manager.pending_plan is None:
                self.telegram.send("No draft is available to recalculate.")
            else:
                self.order_manager.recalc_plan(self.order_manager.pending_plan)
                self.telegram.send(self.order_manager.draft_message())
        elif lower.startswith("modbuy"):
            self.order_manager.modify_pending_buy(command)
        elif lower in {"cancelbuy", "/cancelbuy"}:
            self.order_manager.cancel_pending_buy()
        elif lower in {"buystatus", "/buystatus"}:
            self.telegram.send(self.order_manager.pending_buy_status())
        elif lower in {"canceldraft", "/canceldraft"}:
            self.order_manager.pending_signal = None
            self.order_manager.pending_plan = None
            self.telegram.send("Editable trade draft cleared.")
        elif lower in {"live", "/live"}:
            self.set_all_live(True)
            self.telegram.send("Live monitoring enabled for all enabled strategies.")
        elif lower in {"stop", "/stop"}:
            self.scan_stop_event.set()
            self.set_all_live(False)
            self.telegram.send("Stop requested. Running scan will stop, and live monitoring is paused. Send LIVE to enable again.")
        elif lower in {"dryrun", "/dryrun"}:
            self.cfg.live_trading_enabled = False
            self.cfg.auto_buy_enabled = False
            self.telegram.send("Dry-run mode enabled. Signals will alert, but no Dhan orders will be placed.")
        elif lower in {"manualbuy", "/manualbuy"}:
            self.cfg.live_trading_enabled = True
            self.cfg.auto_buy_enabled = False
            self.telegram.send("Manual-buy mode enabled. Signals create editable drafts; send BUY to place Dhan order.")
        elif lower in {"autoorder", "/autoorder", "autobuy", "/autobuy"}:
            self.cfg.live_trading_enabled = True
            self.cfg.auto_buy_enabled = True
            self.telegram.send("Auto-buy mode enabled. The next valid live signal can place a Dhan BUY order automatically.")
        elif lower in {"/score_live", "score_live"}:
            self.telegram.send(self.strategy_live("SCORE", True))
        elif lower in {"/score_stop", "score_stop"}:
            self.telegram.send(self.strategy_live("SCORE", False))
        elif lower in {"/brahmastra_live", "brahmastra_live"}:
            self.telegram.send(self.strategy_live("BRAHMASTRA", True))
        elif lower in {"/brahmastra_stop", "brahmastra_stop"}:
            self.telegram.send(self.strategy_live("BRAHMASTRA", False))
        elif lower in {"/rsibb_live", "rsibb_live", "/rsi_live", "rsi_live"}:
            self.telegram.send(self.strategy_live("RSIBB", True))
        elif lower in {"/rsibb_stop", "rsibb_stop", "/rsi_stop", "rsi_stop"}:
            self.telegram.send(self.strategy_live("RSIBB", False))
        else:
            self.telegram.send(f"Unknown command: <code>{tg_escape(command)}</code>\n\n{HELP_TEXT}")

    def process_telegram_messages(self) -> None:
        for message in self.telegram.get_messages():
            LOG.info("Telegram command received | message=%s", message)
            try:
                self.dispatch(message)
            except Exception as exc:
                LOG.exception("Command failed | message=%s", message)
                self.telegram.send(f"<b>Command error</b>\n{tg_escape(exc)}")

    def live_tick(self) -> None:
        if not self.live_enabled:
            return
        if not market_session_open():
            self.live_scan_log("Live scan skipped | market closed | now=%s", now_ist())
            return
        self.order_manager.sync_pending_buy()
        index_df = self.cache.current_closed_index()
        if index_df.empty:
            self.live_scan_log("Live scan skipped | no closed candles", force=True)
            return
        self.order_manager.manage_positions(index_df.reset_index(drop=True), self.strategies)
        for name, strategy in self.strategies.items():
            if not strategy.live_enabled:
                continue
            try:
                signal = strategy.live_check(index_df.reset_index(drop=True))
            except Exception as exc:
                LOG.exception("Strategy live check failed | strategy=%s", name)
                self.telegram.send(f"<b>{tg_escape(name)} live check error</b>\n{tg_escape(exc)}")
                continue
            if signal is not None:
                LOG.warning("Live signal accepted | strategy=%s | trigger=%s", name, signal.trigger_key)
                self.order_manager.handle_signal(signal)

    def run(self) -> None:
        print(f"Unified NIFTY bot started | {self.cfg.index_name}")
        LOG.warning("Unified bot starting | strategies=%s | live=%s", list(self.strategies.keys()), self.live_enabled)
        self.telegram.send(self.startup_message())
        while True:
            try:
                self.process_telegram_messages()
                self.live_tick()
                time.sleep(max(1.0, self.cfg.poll_seconds))
            except KeyboardInterrupt:
                self.telegram.send(f"{self.cfg.index_name} Unified Bot stopped.")
                print("Stopped.")
                return
            except requests.HTTPError as exc:
                LOG.exception("Main loop HTTP error")
                print(f"HTTP error: {exc}")
                time.sleep(5)
            except Exception as exc:
                LOG.exception("Main loop error")
                print(f"Error: {exc}")
                try:
                    self.telegram.send(f"<b>Main loop error</b>\n{tg_escape(exc)}")
                except Exception:
                    LOG.exception("Could not send main loop error to Telegram")
                time.sleep(5)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def cmd_live(_: argparse.Namespace) -> None:
    cfg = Config.from_env(require_credentials=True)
    setup_logging(cfg)
    UnifiedNiftyBot(cfg).run()


def cmd_backtest(args: argparse.Namespace) -> None:
    cfg = Config.from_env(require_credentials=True)
    setup_logging(cfg)
    bot = UnifiedNiftyBot(cfg)
    name = bot.normalize_strategy_name(args.strategy) if args.strategy else None
    if name:
        names = [name]
    else:
        names = list(bot.strategies.keys())
    for strategy_name in names:
        strategy = bot.strategies.get(strategy_name)
        if strategy is None:
            print(f"Strategy not enabled: {strategy_name}")
            continue
        result = strategy.run_backtest(args.start, args.end, threading.Event())
        print(format_backtest_summary(result))
        bot.save_backtest_outputs(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified NIFTY option-buying bot")
    sub = parser.add_subparsers(dest="command")

    live = sub.add_parser("live", help="Run the unified live Telegram bot")
    live.set_defaults(func=cmd_live)

    backtest = sub.add_parser("backtest", help="Run a scan/backtest from CLI")
    backtest.add_argument("--start", required=True, help="YYYY-MM-DD")
    backtest.add_argument("--end", required=True, help="YYYY-MM-DD")
    backtest.add_argument("--strategy", default="", help="SCORE, BRAHMASTRA, RSIBB, or blank for all enabled")
    backtest.set_defaults(func=cmd_backtest)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["live"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
