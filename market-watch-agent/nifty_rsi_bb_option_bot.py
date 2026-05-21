"""NIFTY RSI + Bollinger option-buying bot with Dhan live hooks and backtests.

Strategy implemented from the RSI/Bollinger idea:
- CE setup: RSI shows strength above 70, price pulls back to Bollinger mid/lower
  without RSI losing 40, then price breaks the pullback high with higher-timeframe
  RSI support.
- PE setup: RSI shows weakness below 40, price rebounds to Bollinger mid/upper
  without RSI recovering above 70, then price breaks the rebound low with
  higher-timeframe RSI weakness.

Backtest rule:
- A signal is evaluated only on completed NIFTY 5-minute candles.
- Entry happens on the next NIFTY candle open.
- Option entry/exit prices come from Dhan historical rolling option candles at
  the same timestamp. Current LTP is never used for backtest prices.

This file supports both auto-buy and editable Telegram drafts. The current
defaults are automatic live orders because this user asked for auto-buy; set
LIVE_TRADING_ENABLED=false or send DRYRUN in Telegram for alert-only mode.
Validate credentials, lot size, Dhan order permissions/static IP, and your own
backtest results before real-money use.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

DHAN_BASE = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"
IST = ZoneInfo("Asia/Kolkata")

LOG = logging.getLogger("nifty_rsi_bb_bot")
SENSITIVE_KEY_RE = re.compile(
    r"(token|access|authorization|password|secret|chat[_-]?id|client[_-]?id|clientid)",
    re.IGNORECASE,
)
SCAN_RE = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _env_time(name: str, default: dt.time) -> dt.time:
    raw = _env_str(name, "")
    if not raw:
        return default
    try:
        hour, minute = raw.split(":", 1)
        return dt.time(int(hour), int(minute))
    except Exception:
        return default


def _env_csv_strings(name: str, default: str = "") -> List[str]:
    raw = _env_str(name, default)
    if not raw:
        return []
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def _env_csv_ints(name: str, default: str = "") -> List[int]:
    values: List[int] = []
    for part in _env_csv_strings(name, default):
        try:
            values.append(int(part.replace("+", "")))
        except Exception:
            LOG.warning("Ignoring invalid integer in %s: %s", name, part)
    return values


@dataclass
class Config:
    dhan_client_id: str = ""
    dhan_access_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    http_timeout: int = 20
    poll_seconds: float = 5.0

    index_security_id: int = 13
    index_segment: str = "IDX_I"
    index_instrument: str = "INDEX"
    index_name: str = "NIFTY 50"
    fno_segment: str = "NSE_FNO"
    option_instrument: str = "OPTIDX"

    candle_interval: int = 5
    htf_interval: int = 15
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
    avoid_first_minutes: int = 15

    strike_step: int = 50
    option_search_depth: int = 4
    min_premium: float = 60.0
    preferred_premium_min: float = 80.0
    preferred_premium_max: float = 180.0
    max_premium: float = 250.0
    allow_premium_fallback: bool = False

    option_sl_pct: float = 0.22
    option_sl_buffer: float = 0.50
    target1_r: float = 1.0
    target2_r: float = 2.0
    trail_after_t1: bool = True
    allowed_sides: List[str] = field(default_factory=list)
    allowed_rolling_offsets: List[int] = field(default_factory=list)
    blocked_rolling_offsets: List[int] = field(default_factory=list)
    min_option_risk_points: float = 0.0
    max_option_risk_points: float = 0.0
    max_option_sl_pct: float = 0.0
    backtest_entry_slippage: float = 0.0
    backtest_exit_slippage: float = 0.0

    lot_size: int = 65
    lots: int = 2
    brokerage_per_order: float = 0.0

    no_new_trade_after: dt.time = dt.time(15, 0)
    square_off_time: dt.time = dt.time(15, 20)
    max_trades_per_day: int = 2

    live_enabled_at_start: bool = True
    live_trading_enabled: bool = True
    auto_buy_enabled: bool = True
    order_product_type: str = "INTRADAY"
    entry_order_type: str = "LIMIT"
    exit_order_type: str = "LIMIT"
    sl_order_type: str = "STOP_LOSS"
    order_validity: str = "DAY"
    entry_limit_buffer: float = 1.0
    exit_limit_buffer: float = 1.0
    stop_loss_limit_buffer: float = 0.50
    order_status_poll_attempts: int = 8
    order_status_poll_seconds: float = 1.0
    preferred_expiry: str = ""

    expired_options_expiry_flag: str = "WEEK"
    expired_options_expiry_code: int = 0

    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "rsi_bb_option_bot.log"
    log_to_console: bool = True

    @property
    def quantity(self) -> int:
        return max(1, self.lot_size * self.lots)

    @staticmethod
    def from_env(require_dhan: bool = False) -> "Config":
        cfg = Config(
            dhan_client_id=_env_str("DHAN_CLIENT_ID"),
            dhan_access_token=_env_str("DHAN_ACCESS_TOKEN"),
            telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env_str("TELEGRAM_CHAT_ID"),
            http_timeout=_env_int("HTTP_TIMEOUT", 20),
            poll_seconds=_env_float("POLL_SECONDS", _env_float("TG_POLL_INTERVAL", _env_float("DHAN_POLL_SECONDS", 5.0))),
            index_security_id=_env_int("UNDERLYING_SECURITY_ID", _env_int("DHAN_UNDERLYING_SECURITY_ID", 13)),
            index_segment=_env_str("UNDERLYING_SEGMENT", _env_str("DHAN_UNDERLYING_SEG", "IDX_I")).upper(),
            index_instrument=_env_str("UNDERLYING_INSTRUMENT", "INDEX").upper(),
            index_name=_env_str("INDEX_NAME", "NIFTY 50"),
            fno_segment=_env_str("FNO_SEGMENT", "NSE_FNO").upper(),
            option_instrument=_env_str("OPTION_INSTRUMENT", "OPTIDX").upper(),
            candle_interval=_env_int("CANDLE_INTERVAL", 5),
            htf_interval=_env_int("HTF_INTERVAL", 15),
            rsi_period=_env_int("RSI_PERIOD", 14),
            bb_period=_env_int("BB_PERIOD", 20),
            bb_std=_env_float("BB_STD", 2.0),
            setup_lookback=_env_int("SETUP_LOOKBACK", 36),
            breakout_lookback=_env_int("BREAKOUT_LOOKBACK", 4),
            min_pullback_bars=_env_int("MIN_PULLBACK_BARS", 2),
            long_impulse_rsi=_env_float("LONG_IMPULSE_RSI", 70.0),
            long_pullback_rsi_floor=_env_float("LONG_PULLBACK_RSI_FLOOR", 40.0),
            short_impulse_rsi=_env_float("SHORT_IMPULSE_RSI", 40.0),
            short_pullback_rsi_ceiling=_env_float("SHORT_PULLBACK_RSI_CEILING", 70.0),
            htf_long_min_rsi=_env_float("HTF_LONG_MIN_RSI", 50.0),
            htf_short_max_rsi=_env_float("HTF_SHORT_MAX_RSI", 50.0),
            require_vwap=_env_bool("REQUIRE_VWAP", True),
            avoid_first_minutes=_env_int("AVOID_FIRST_MINUTES", 15),
            strike_step=_env_int("STRIKE_STEP", 50),
            option_search_depth=_env_int("OPTION_SEARCH_DEPTH", 4),
            min_premium=_env_float("MIN_PREMIUM", 60.0),
            preferred_premium_min=_env_float("PREFERRED_PREMIUM_MIN", 80.0),
            preferred_premium_max=_env_float("PREFERRED_PREMIUM_MAX", 180.0),
            max_premium=_env_float("MAX_PREMIUM", 250.0),
            allow_premium_fallback=_env_bool("ALLOW_PREMIUM_FALLBACK", False),
            option_sl_pct=_env_float("OPTION_SL_PCT", 0.22),
            option_sl_buffer=_env_float("OPTION_SL_BUFFER", 0.50),
            target1_r=_env_float("TARGET1_R", 1.0),
            target2_r=_env_float("TARGET2_R", 2.0),
            trail_after_t1=_env_bool("TRAIL_AFTER_T1", True),
            allowed_sides=_env_csv_strings("ALLOWED_SIDES", ""),
            allowed_rolling_offsets=_env_csv_ints("ALLOWED_ROLLING_OFFSETS", ""),
            blocked_rolling_offsets=_env_csv_ints("BLOCKED_ROLLING_OFFSETS", ""),
            min_option_risk_points=_env_float("MIN_OPTION_RISK_POINTS", 0.0),
            max_option_risk_points=_env_float("MAX_OPTION_RISK_POINTS", 0.0),
            max_option_sl_pct=_env_float("MAX_OPTION_SL_PCT", 0.0),
            backtest_entry_slippage=_env_float("BACKTEST_ENTRY_SLIPPAGE", 0.0),
            backtest_exit_slippage=_env_float("BACKTEST_EXIT_SLIPPAGE", 0.0),
            lot_size=_env_int("LOT_SIZE", 65),
            lots=_env_int("LOTS", 2),
            brokerage_per_order=_env_float("BROKERAGE_PER_ORDER", 0.0),
            no_new_trade_after=_env_time("NO_NEW_TRADE_AFTER", _env_time("NO_NEW_ENTRY_AFTER", dt.time(15, 0))),
            square_off_time=_env_time("SQUARE_OFF_TIME", dt.time(15, 20)),
            max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", 2),
            live_enabled_at_start=_env_bool("LIVE_ENABLED", True),
            live_trading_enabled=_env_bool("LIVE_TRADING_ENABLED", _env_bool("AUTO_ORDER", True)),
            auto_buy_enabled=_env_bool("AUTO_BUY", _env_bool("AUTO_ORDER", True)),
            order_product_type=_env_str("ORDER_PRODUCT_TYPE", "INTRADAY").upper(),
            entry_order_type=_env_str("ENTRY_ORDER_TYPE", "LIMIT").upper(),
            exit_order_type=_env_str("EXIT_ORDER_TYPE", "LIMIT").upper(),
            sl_order_type=_env_str("SL_ORDER_TYPE", "STOP_LOSS").upper(),
            order_validity=_env_str("ORDER_VALIDITY", "DAY").upper(),
            entry_limit_buffer=_env_float("ENTRY_LIMIT_BUFFER", 1.0),
            exit_limit_buffer=_env_float("EXIT_LIMIT_BUFFER", 1.0),
            stop_loss_limit_buffer=_env_float("STOP_LOSS_LIMIT_BUFFER", 0.50),
            order_status_poll_attempts=_env_int("ORDER_STATUS_POLL_ATTEMPTS", 8),
            order_status_poll_seconds=_env_float("ORDER_STATUS_POLL_SECONDS", 1.0),
            preferred_expiry=_env_str("PREFERRED_EXPIRY", _env_str("DHAN_EXPIRY", "")),
            expired_options_expiry_flag=_env_str("EXPIRED_OPTIONS_EXPIRY_FLAG", "WEEK").upper(),
            expired_options_expiry_code=_env_int("EXPIRED_OPTIONS_EXPIRY_CODE", 0),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            log_dir=_env_str("LOG_DIR", "logs"),
            log_file=_env_str("LOG_FILE", "rsi_bb_option_bot.log"),
            log_to_console=_env_bool("LOG_TO_CONSOLE", True),
        )
        if require_dhan and (not cfg.dhan_client_id or not cfg.dhan_access_token):
            raise SystemExit("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in .env")
        return cfg


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    LOG.setLevel(level)
    LOG.propagate = False
    LOG.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s:%(lineno)d - %(message)s",
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


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------


@dataclass
class RsiBbSignal:
    candle_time: dt.datetime
    side: str
    direction: str
    spot: float
    rsi: float
    htf_rsi: float
    bb_mid: float
    bb_upper: float
    bb_lower: float
    vwap: float
    impulse_time: dt.datetime
    pullback_time: dt.datetime
    trigger_key: str
    reasons: List[str]


@dataclass
class OptionContract:
    side: str
    strike: float
    expiry: str
    security_id: int
    entry_premium: float
    selection_reason: str
    rolling_offset: Optional[int] = None


@dataclass
class OptionTradePlan:
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


@dataclass
class BacktestTrade:
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


@dataclass
class LivePosition:
    plan: OptionTradePlan
    signal_time: dt.datetime
    opened_at: dt.datetime
    trigger_key: str
    current_sl: float
    remaining_qty: int
    entry_order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    t1_hit: bool = False
    last_option_ts: Optional[pd.Timestamp] = None


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


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
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return "-"


def tg_escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def price_tick(value: float) -> float:
    return round(max(float(value), 0.05), 2)


def round_strike(spot: float, step: int) -> float:
    return float(round(spot / step) * step)


def day_start_end(day: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    return dt.datetime.combine(day, dt.time(9, 15)), dt.datetime.combine(day, dt.time(15, 30))


def market_session_open(at_time: Optional[dt.datetime] = None) -> bool:
    now = at_time or now_ist()
    if now.weekday() >= 5:
        return False
    return dt.time(9, 15) <= now.time() <= dt.time(15, 30)


def redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "<redacted>" if SENSITIVE_KEY_RE.search(str(k)) else redact_for_log(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(v) for v in value]
    return value


def to_json(value: Any, max_len: int = 3000) -> str:
    try:
        text = json.dumps(redact_for_log(value), default=str, ensure_ascii=True)
    except Exception:
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "...<truncated>"
    return text


def closed_candles_only(df: pd.DataFrame, interval_minutes: int, at_time: Optional[dt.datetime] = None) -> pd.DataFrame:
    if df.empty:
        return df
    at_time = (at_time or now_ist()).replace(tzinfo=None)
    candle_end = df["timestamp"] + pd.to_timedelta(interval_minutes, unit="m")
    out = df[candle_end <= at_time - dt.timedelta(seconds=5)].copy()
    return out.reset_index(drop=True)


def make_correlation_id(prefix: str) -> str:
    raw = f"RSIBB{now_ist().strftime('%y%m%d%H%M%S')}{prefix}{int(time.time() * 1000) % 100000}"
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:30]


def order_status(raw: Dict[str, Any]) -> str:
    return str(raw.get("orderStatus") or raw.get("status") or "UNKNOWN").upper()


def order_id(raw: Dict[str, Any]) -> Optional[str]:
    value = raw.get("orderId") or raw.get("order_id")
    return str(value) if value is not None else None


def order_filled_qty(raw: Dict[str, Any]) -> int:
    return int(num(raw.get("filledQty") or raw.get("filledQuantity") or raw.get("tradedQuantity"), 0))


def order_avg_price(raw: Dict[str, Any]) -> float:
    return num(raw.get("averageTradedPrice") or raw.get("avgPrice") or raw.get("tradedPrice"), 0)


def find_exact_candle(df: pd.DataFrame, timestamp: dt.datetime) -> Optional[int]:
    if df.empty:
        return None
    ts = pd.Timestamp(timestamp)
    matches = df.index[df["timestamp"] == ts].tolist()
    return int(matches[0]) if matches else None


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def t1_book_quantity(total_quantity: int, cfg: Config) -> int:
    """Return a T1 booking quantity that respects option lot size."""
    quantity = max(1, int(total_quantity))
    lot_size = max(1, int(cfg.lot_size))
    if quantity <= lot_size:
        return quantity
    if quantity % lot_size == 0:
        lots = quantity // lot_size
        return lot_size * max(1, lots // 2)
    return max(1, quantity // 2)


def side_allowed(side: str, cfg: Config) -> bool:
    allowed = {value.upper() for value in cfg.allowed_sides if value}
    return not allowed or side.upper() in allowed


def rolling_offset_allowed(offset: int, cfg: Config) -> bool:
    if cfg.allowed_rolling_offsets and offset not in set(cfg.allowed_rolling_offsets):
        return False
    if offset in set(cfg.blocked_rolling_offsets):
        return False
    return True


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
# Dhan + Telegram clients
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

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = f"{DHAN_BASE}{endpoint}"
        LOG.debug("Dhan request | %s %s | payload=%s", method.upper(), endpoint, to_json(payload))
        try:
            response = self.session.request(
                method.upper(),
                url,
                data=json.dumps(payload) if payload is not None else None,
                params=params,
                timeout=self.cfg.http_timeout,
            )
        except requests.RequestException:
            LOG.exception("Dhan request failed before response | %s %s", method, endpoint)
            raise

        if response.status_code >= 400:
            body = response.text
            try:
                body = json.dumps(response.json(), ensure_ascii=True)
            except Exception:
                pass
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
        oi: bool = False,
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
            oi=False,
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
            "toDate": end.strftime("%Y-%m-%d"),
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
            try:
                expiry_date = dt.datetime.strptime(value[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if expiry_date >= trade_date:
                expiries.append((expiry_date, value))
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
        if not response.text.strip():
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
        missing = [k for k in required if k not in keys]
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

        return (
            df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
            .sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )


class TelegramBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
        self.session = requests.Session()
        self._offset = 0

    def send(self, text: str) -> None:
        if not self.enabled:
            LOG.info("Telegram disabled | message=%s", text.replace("\n", " | ")[:500])
            return
        data = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text[:3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = self.session.post(
            f"{TELEGRAM_BASE}/bot{self.cfg.telegram_bot_token}/sendMessage",
            data=data,
            timeout=self.cfg.http_timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Telegram send failed: {response.status_code} {response.text}")

    def get_messages(self) -> List[str]:
        if not self.enabled:
            return []
        try:
            response = self.session.get(
                f"{TELEGRAM_BASE}/bot{self.cfg.telegram_bot_token}/getUpdates",
                params={"offset": self._offset, "timeout": 0},
                timeout=self.cfg.http_timeout,
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


# -----------------------------------------------------------------------------
# Indicators and signal generation
# -----------------------------------------------------------------------------


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _resample_ohlcv_one_day(day_df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    day_df = day_df.sort_values("timestamp").set_index("timestamp")
    out = day_df.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    out = out.dropna(subset=["open", "high", "low", "close"]).reset_index()
    out["available_at"] = out["timestamp"] + pd.to_timedelta(minutes, unit="m")
    return out


def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    out["trade_date"] = out["timestamp"].dt.date

    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    typical = (high + low + close) / 3.0
    pv = typical * out["volume"].astype(float)
    out["vwap"] = pv.groupby(out["trade_date"]).cumsum() / out["volume"].astype(float).groupby(out["trade_date"]).cumsum()

    out["rsi"] = rsi_wilder(close, cfg.rsi_period)
    out["bb_mid"] = close.rolling(cfg.bb_period, min_periods=cfg.bb_period).mean()
    bb_std = close.rolling(cfg.bb_period, min_periods=cfg.bb_period).std(ddof=0)
    out["bb_upper"] = out["bb_mid"] + cfg.bb_std * bb_std
    out["bb_lower"] = out["bb_mid"] - cfg.bb_std * bb_std
    out["bb_mid_slope"] = out["bb_mid"] - out["bb_mid"].shift(3)

    htf_parts: List[pd.DataFrame] = []
    for _, day_df in out.groupby("trade_date", sort=True):
        htf = _resample_ohlcv_one_day(day_df[["timestamp", "open", "high", "low", "close", "volume"]], cfg.htf_interval)
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


def _warmup_ok(row: pd.Series) -> bool:
    required = ["rsi", "bb_mid", "bb_upper", "bb_lower", "vwap", "htf_rsi", "htf_rsi_slope"]
    return all(not pd.isna(row.get(col)) for col in required)


def build_rsi_bb_signal(df: pd.DataFrame, idx: int, cfg: Config) -> Optional[RsiBbSignal]:
    if idx < max(cfg.bb_period + cfg.min_pullback_bars + 2, cfg.rsi_period + 5):
        return None

    row = df.iloc[idx]
    if not _warmup_ok(row):
        return None

    market_open = dt.datetime.combine(row.timestamp.date(), dt.time(9, 15))
    if row.timestamp < market_open + dt.timedelta(minutes=cfg.avoid_first_minutes):
        return None
    if row.timestamp.time() >= cfg.no_new_trade_after:
        return None

    signal = _build_long_signal(df, idx, cfg)
    if signal:
        return signal
    return _build_short_signal(df, idx, cfg)


def _build_long_signal(df: pd.DataFrame, idx: int, cfg: Config) -> Optional[RsiBbSignal]:
    row = df.iloc[idx]
    start = max(0, idx - cfg.setup_lookback)
    impulse_indices = [j for j in range(start, idx) if float(df.iloc[j].rsi) >= cfg.long_impulse_rsi]
    if not impulse_indices:
        return None

    for impulse_idx in reversed(impulse_indices):
        after = df.iloc[impulse_idx + 1 : idx]
        if len(after) < cfg.min_pullback_bars:
            continue
        if float(after["rsi"].min()) < cfg.long_pullback_rsi_floor:
            continue
        touched_band = bool(((after["low"] <= after["bb_mid"]) | (after["low"] <= after["bb_lower"])).any())
        if not touched_band:
            continue
        breakout_ref = float(after.tail(cfg.breakout_lookback)["high"].max())
        if not float(row.close) > breakout_ref:
            continue
        if not float(row.close) > float(row.open):
            continue
        if not float(row.close) > float(row.bb_mid):
            continue
        if cfg.require_vwap and not float(row.close) > float(row.vwap):
            continue
        if not (float(row.htf_rsi) > cfg.htf_long_min_rsi and float(row.htf_rsi_slope) > 0):
            continue

        impulse_time = df.iloc[impulse_idx].timestamp
        pullback_idx = int(after[((after["low"] <= after["bb_mid"]) | (after["low"] <= after["bb_lower"]))].index[-1])
        pullback_time = df.iloc[pullback_idx].timestamp
        reasons = [
            f"5m RSI impulse above {cfg.long_impulse_rsi:g}",
            "Pullback touched Bollinger mid/lower band",
            f"Pullback RSI stayed above {cfg.long_pullback_rsi_floor:g}",
            f"Close broke pullback high {fmt(breakout_ref)}",
            f"{cfg.htf_interval}m RSI is bullish at {fmt(row.htf_rsi)}",
        ]
        if cfg.require_vwap:
            reasons.append("Close is above VWAP")
        candle_time = row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp
        return RsiBbSignal(
            candle_time=candle_time,
            side="CE",
            direction="BULLISH",
            spot=float(row.close),
            rsi=float(row.rsi),
            htf_rsi=float(row.htf_rsi),
            bb_mid=float(row.bb_mid),
            bb_upper=float(row.bb_upper),
            bb_lower=float(row.bb_lower),
            vwap=float(row.vwap),
            impulse_time=impulse_time.to_pydatetime() if hasattr(impulse_time, "to_pydatetime") else impulse_time,
            pullback_time=pullback_time.to_pydatetime() if hasattr(pullback_time, "to_pydatetime") else pullback_time,
            trigger_key=f"CE|{candle_time}|impulse:{impulse_time}",
            reasons=reasons,
        )
    return None


def _build_short_signal(df: pd.DataFrame, idx: int, cfg: Config) -> Optional[RsiBbSignal]:
    row = df.iloc[idx]
    start = max(0, idx - cfg.setup_lookback)
    impulse_indices = [j for j in range(start, idx) if float(df.iloc[j].rsi) <= cfg.short_impulse_rsi]
    if not impulse_indices:
        return None

    for impulse_idx in reversed(impulse_indices):
        after = df.iloc[impulse_idx + 1 : idx]
        if len(after) < cfg.min_pullback_bars:
            continue
        if float(after["rsi"].max()) > cfg.short_pullback_rsi_ceiling:
            continue
        touched_band = bool(((after["high"] >= after["bb_mid"]) | (after["high"] >= after["bb_upper"])).any())
        if not touched_band:
            continue
        breakdown_ref = float(after.tail(cfg.breakout_lookback)["low"].min())
        if not float(row.close) < breakdown_ref:
            continue
        if not float(row.close) < float(row.open):
            continue
        if not float(row.close) < float(row.bb_mid):
            continue
        if cfg.require_vwap and not float(row.close) < float(row.vwap):
            continue
        if not (float(row.htf_rsi) < cfg.htf_short_max_rsi and float(row.htf_rsi_slope) < 0):
            continue

        impulse_time = df.iloc[impulse_idx].timestamp
        pullback_idx = int(after[((after["high"] >= after["bb_mid"]) | (after["high"] >= after["bb_upper"]))].index[-1])
        pullback_time = df.iloc[pullback_idx].timestamp
        reasons = [
            f"5m RSI weakness below {cfg.short_impulse_rsi:g}",
            "Rebound touched Bollinger mid/upper band",
            f"Rebound RSI stayed below {cfg.short_pullback_rsi_ceiling:g}",
            f"Close broke rebound low {fmt(breakdown_ref)}",
            f"{cfg.htf_interval}m RSI is bearish at {fmt(row.htf_rsi)}",
        ]
        if cfg.require_vwap:
            reasons.append("Close is below VWAP")
        candle_time = row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp
        return RsiBbSignal(
            candle_time=candle_time,
            side="PE",
            direction="BEARISH",
            spot=float(row.close),
            rsi=float(row.rsi),
            htf_rsi=float(row.htf_rsi),
            bb_mid=float(row.bb_mid),
            bb_upper=float(row.bb_upper),
            bb_lower=float(row.bb_lower),
            vwap=float(row.vwap),
            impulse_time=impulse_time.to_pydatetime() if hasattr(impulse_time, "to_pydatetime") else impulse_time,
            pullback_time=pullback_time.to_pydatetime() if hasattr(pullback_time, "to_pydatetime") else pullback_time,
            trigger_key=f"PE|{candle_time}|impulse:{impulse_time}",
            reasons=reasons,
        )
    return None


def opposite_exit(index_df: pd.DataFrame, idx: int, side: str, cfg: Config) -> bool:
    row = index_df.iloc[idx]
    if side == "CE":
        return bool(
            float(row.rsi) < cfg.long_pullback_rsi_floor
            or (float(row.close) < float(row.bb_mid) and float(row.htf_rsi_slope) < 0)
        )
    return bool(
        float(row.rsi) > cfg.short_pullback_rsi_ceiling
        or (float(row.close) > float(row.bb_mid) and float(row.htf_rsi_slope) > 0)
    )


# -----------------------------------------------------------------------------
# Option selection and trade plan
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


def nearest_strike(strikes: List[float], spot: float, fallback_step: int) -> float:
    if strikes:
        return min(strikes, key=lambda strike: abs(strike - spot))
    return round_strike(spot, fallback_step)


def option_offsets(side: str, depth: int) -> List[int]:
    offsets = [0]
    for i in range(1, depth + 1):
        if side == "CE":
            offsets.extend([-i, i])
        else:
            offsets.extend([i, -i])
    return offsets


def premium_score(premium: float, offset: int, cfg: Config) -> Tuple[int, float, int]:
    preferred_mid = (cfg.preferred_premium_min + cfg.preferred_premium_max) / 2.0
    if cfg.preferred_premium_min <= premium <= cfg.preferred_premium_max:
        band = 0
    elif cfg.min_premium <= premium <= cfg.max_premium:
        band = 1
    else:
        band = 2
    return band, abs(premium - preferred_mid), abs(offset)


def select_live_option_from_chain(chain_json: Dict[str, Any], expiry: str, side: str, cfg: Config) -> OptionContract:
    oc = get_oc(chain_json)
    if not oc:
        raise RuntimeError("Dhan option chain is empty.")
    spot = get_chain_spot(chain_json)
    strikes = sorted(float(k) for k in oc.keys())
    atm = nearest_strike(strikes, spot, cfg.strike_step)
    choices: List[Tuple[Tuple[int, float, int], OptionContract]] = []

    for offset in option_offsets(side, cfg.option_search_depth):
        if not rolling_offset_allowed(offset, cfg):
            continue
        strike = nearest_strike(strikes, atm + offset * cfg.strike_step, cfg.strike_step)
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

    if not choices:
        raise RuntimeError(f"No {side} option found in premium range {cfg.min_premium:g}-{cfg.max_premium:g}.")
    return sorted(choices, key=lambda item: item[0])[0][1]


def option_stop_from_history(option_df: pd.DataFrame, entry_pos: int, entry: float, cfg: Config) -> float:
    fallback_sl = entry * (1.0 - cfg.option_sl_pct)
    prior = option_df.iloc[max(0, entry_pos - 6) : entry_pos]
    if len(prior) >= 3:
        swing_sl = float(prior["low"].min()) - cfg.option_sl_buffer
        sl = max(swing_sl, fallback_sl)
    else:
        sl = fallback_sl
    if sl >= entry:
        sl = fallback_sl
    return price_tick(sl)


def make_trade_plan(
    contract: OptionContract,
    option_df: Optional[pd.DataFrame],
    entry_pos: Optional[int],
    cfg: Config,
) -> OptionTradePlan:
    entry = price_tick(contract.entry_premium)
    if option_df is not None and entry_pos is not None:
        sl = option_stop_from_history(option_df, entry_pos, entry, cfg)
    else:
        sl = price_tick(entry * (1.0 - cfg.option_sl_pct))
    risk = price_tick(max(entry - sl, 0.05))
    return OptionTradePlan(
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
    )


# -----------------------------------------------------------------------------
# Backtester
# -----------------------------------------------------------------------------


class RsiBbBacktester:
    def __init__(self, cfg: Config, api: DhanApiClient):
        self.cfg = cfg
        self.api = api
        self.rolling_cache: Dict[Tuple[str, int, str], pd.DataFrame] = {}

    def fetch_index_range(self, start_day: dt.date, end_day: dt.date) -> pd.DataFrame:
        frames: List[pd.DataFrame] = []
        current = start_day
        while current <= end_day:
            chunk_end = min(current + dt.timedelta(days=89), end_day)
            start_dt = dt.datetime.combine(current, dt.time(9, 15))
            end_dt = dt.datetime.combine(chunk_end, dt.time(15, 30))
            frames.append(self.api.index_intraday(start_dt, end_dt))
            current = chunk_end + dt.timedelta(days=1)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    def run(self, start_date: str, end_date: str) -> BacktestResult:
        start_day = parse_date(start_date)
        end_day = parse_date(end_date)
        raw = self.fetch_index_range(start_day, end_day)
        if raw.empty:
            return BacktestResult(start_date, end_date, [], ["No NIFTY candles returned by Dhan."])
        index_df = add_indicators(raw, self.cfg)

        trades: List[BacktestTrade] = []
        errors: List[str] = []
        active_until: Optional[pd.Timestamp] = None
        trades_per_day: Dict[str, int] = {}
        last_trigger: Optional[str] = None

        for idx in range(len(index_df) - 1):
            row_ts = pd.Timestamp(index_df.iloc[idx].timestamp)
            if active_until is not None and row_ts <= active_until:
                continue
            signal = build_rsi_bb_signal(index_df, idx, self.cfg)
            if signal is None or signal.trigger_key == last_trigger:
                continue
            if not side_allowed(signal.side, self.cfg):
                LOG.info("Backtest signal skipped by ALLOWED_SIDES | time=%s | side=%s", signal.candle_time, signal.side)
                last_trigger = signal.trigger_key
                continue

            entry_idx = idx + 1
            entry_row = index_df.iloc[entry_idx]
            entry_time = entry_row.timestamp.to_pydatetime() if hasattr(entry_row.timestamp, "to_pydatetime") else entry_row.timestamp
            day_key = str(entry_time.date())
            if entry_time.time() >= self.cfg.no_new_trade_after:
                continue
            if trades_per_day.get(day_key, 0) >= self.cfg.max_trades_per_day:
                continue

            try:
                contract, option_df, entry_pos = self.select_historical_option(signal.side, float(entry_row.open), entry_time)
                plan = make_trade_plan(contract, option_df, entry_pos, self.cfg)
                reject_reason = trade_plan_reject_reason(plan, self.cfg)
                if reject_reason:
                    LOG.info(
                        "Backtest trade skipped by risk/filter rules | time=%s | side=%s | reason=%s",
                        signal.candle_time,
                        signal.side,
                        reject_reason,
                    )
                    last_trigger = signal.trigger_key
                    continue
                trade = self.simulate_trade(index_df, entry_idx, signal, option_df, entry_pos, plan)
                trades.append(trade)
                trades_per_day[day_key] = trades_per_day.get(day_key, 0) + 1
                active_until = pd.Timestamp(trade.exit_time)
            except Exception as exc:
                LOG.exception("Backtest signal failed | time=%s | side=%s", signal.candle_time, signal.side)
                errors.append(f"{signal.candle_time} {signal.side}: {exc}")
            last_trigger = signal.trigger_key

        return BacktestResult(start_date, end_date, trades, errors)

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
        atm = round_strike(spot_at_entry, self.cfg.strike_step)
        choices: List[Tuple[Tuple[int, float, int], OptionContract, pd.DataFrame, int]] = []
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
                    selection_reason=reason + self.slippage_note(),
                    rolling_offset=offset,
                )
                choices.append((premium_score(premium, offset, self.cfg), contract, option_df, entry_pos))
            except Exception as exc:
                failures.append(f"ATM{offset:+d}: {exc}")

        if not choices:
            raise RuntimeError("No historical option candidate available. " + "; ".join(failures[:6]))
        _, contract, option_df, entry_pos = sorted(choices, key=lambda item: item[0])[0]
        return contract, option_df, entry_pos

    def slippage_note(self) -> str:
        if self.cfg.backtest_entry_slippage <= 0 and self.cfg.backtest_exit_slippage <= 0:
            return ""
        return f" | slippage entry={self.cfg.backtest_entry_slippage:g} exit={self.cfg.backtest_exit_slippage:g}"

    def simulate_trade(
        self,
        index_df: pd.DataFrame,
        entry_idx: int,
        signal: RsiBbSignal,
        option_df: pd.DataFrame,
        entry_pos: int,
        plan: OptionTradePlan,
    ) -> BacktestTrade:
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
            # count the stop first because intrabar order is unknowable from OHLC.
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
            if idx is not None and idx > entry_idx and opposite_exit(index_df, idx, signal.side, self.cfg):
                exit_price = apply_exit_slippage(close, self.cfg)
                exit_reason = "opposite RSI/BB exit"
                exit_order_count += 1
                break

        if exit_reason == "end":
            exit_price = apply_exit_slippage(last_close, self.cfg)
            exit_order_count += 1

        realized += remaining_qty * (exit_price - plan.entry)
        order_count = 1 + exit_order_count
        net_pnl = realized - order_count * self.cfg.brokerage_per_order

        return BacktestTrade(
            trade_date=str(pd.Timestamp(option_df.iloc[entry_pos].timestamp).date()),
            signal_time=str(signal.candle_time),
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


# -----------------------------------------------------------------------------
# Live bot
# -----------------------------------------------------------------------------


class RsiBbLiveBot:
    def __init__(self, cfg: Config, api: DhanApiClient, telegram: TelegramBot):
        self.cfg = cfg
        self.api = api
        self.telegram = telegram
        self.expiry: Optional[str] = None
        self.last_candle_time: Optional[str] = None
        self.last_trigger: Optional[str] = None
        self.position: Optional[LivePosition] = None
        self.pending_signal: Optional[RsiBbSignal] = None
        self.pending_plan: Optional[OptionTradePlan] = None
        self.pending_entry_order_id: Optional[str] = None
        self.pending_entry_plan: Optional[OptionTradePlan] = None
        self.pending_entry_last_status: str = ""
        self.pending_entry_last_filled_qty: int = 0
        self.live_enabled = cfg.live_enabled_at_start

    def ensure_expiry(self) -> str:
        today = now_ist().date()
        if self.expiry is None:
            self.expiry = self.api.pick_expiry(today)
        return self.expiry

    def current_index(self) -> pd.DataFrame:
        now = now_ist().replace(tzinfo=None)
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return self.api.index_intraday(start, end)

    def build_live_plan(self, signal: RsiBbSignal) -> OptionTradePlan:
        expiry = self.ensure_expiry()
        chain = self.api.option_chain(expiry)
        contract = select_live_option_from_chain(chain, expiry, signal.side, self.cfg)
        return make_trade_plan(contract, None, None, self.cfg)

    def wait_order(self, order_id_value: str) -> Dict[str, Any]:
        latest: Dict[str, Any] = {"orderId": order_id_value, "orderStatus": "UNKNOWN"}
        for _ in range(max(1, self.cfg.order_status_poll_attempts)):
            latest = self.api.get_order(order_id_value)
            status = order_status(latest)
            if status in {"TRADED", "REJECTED", "CANCELLED", "EXPIRED"}:
                return latest
            time.sleep(max(0.1, self.cfg.order_status_poll_seconds))
        return latest

    def open_position(self, signal: RsiBbSignal, plan: OptionTradePlan) -> None:
        self.pending_signal = signal
        self.pending_plan = plan
        self.telegram.send(format_signal_message(signal, plan, live_trading=self.cfg.live_trading_enabled, auto_buy=self.cfg.auto_buy_enabled))

        reject_reason = trade_plan_reject_reason(plan, self.cfg)
        if reject_reason:
            self.telegram.send(
                "<b>Trade skipped by risk/filter rules</b>\n"
                f"Reason: {tg_escape(reject_reason)}\n\n"
                "The plan is still saved as an editable draft. Use <code>/draft</code>, edit it, "
                "or relax the related .env filter after testing."
            )
            return

        if not self.cfg.live_trading_enabled:
            LOG.warning("Signal stored as draft; real Dhan orders are disabled")
            self.telegram.send(
                "<b>Editable Draft Saved</b>\n"
                "Real orders are disabled. Send <code>AUTOORDER</code> to allow Dhan orders, "
                "or keep using this as alert-only mode.\n\n"
                "Use <code>EDIT ENTRY 125</code>, <code>EDIT SL 100</code>, "
                "<code>EDIT QTY 130</code>, then <code>BUY</code>."
            )
            return

        if not self.cfg.auto_buy_enabled:
            self.telegram.send(
                "<b>Editable Draft Saved</b>\n"
                "Auto-buy is OFF. Review or edit the plan, then send <code>BUY</code>.\n\n"
                "Examples: <code>EDIT ENTRY 125</code>, <code>EDIT SL 100</code>, "
                "<code>EDIT T1 150</code>, <code>EDIT QTY 130</code>, <code>BUY</code>."
            )
            return

        self.execute_buy_order(signal, plan, source="auto-buy")

    def execute_buy_order(self, signal: RsiBbSignal, plan: OptionTradePlan, source: str) -> None:
        if self.position is not None:
            self.telegram.send("A live position is already active. New BUY skipped.")
            return
        if not self.cfg.live_trading_enabled:
            self.telegram.send("Real Dhan orders are disabled. Send AUTOORDER first if you want to place orders.")
            return
        if plan.security_id <= 0:
            self.telegram.send("Cannot place BUY: option security id is missing or invalid.")
            return
        reject_reason = trade_plan_reject_reason(plan, self.cfg)
        if reject_reason:
            self.telegram.send(f"Cannot place BUY: {tg_escape(reject_reason)}.")
            return

        entry_price = 0.0
        if self.cfg.entry_order_type == "LIMIT":
            entry_price = price_tick(plan.entry + self.cfg.entry_limit_buffer)
        entry_order = self.api.place_order(
            "BUY",
            plan.security_id,
            plan.quantity,
            self.cfg.entry_order_type,
            price=entry_price,
            correlation_id=make_correlation_id("ENTRY"),
        )
        entry_id = order_id(entry_order)
        self.telegram.send(format_order_message(f"ENTRY ORDER PLACED - {source}", plan, entry_order))
        if not entry_id:
            return

        latest = self.wait_order(entry_id)
        self.telegram.send(format_order_message("ENTRY ORDER UPDATE", plan, latest))
        filled_qty = order_filled_qty(latest)
        avg = order_avg_price(latest)
        if filled_qty <= 0:
            status = order_status(latest)
            if status not in {"REJECTED", "CANCELLED", "EXPIRED"}:
                self.pending_entry_order_id = entry_id
                self.pending_entry_plan = plan
                self.pending_entry_last_status = status
                self.pending_entry_last_filled_qty = filled_qty
                self.telegram.send(
                    "<b>Entry order is still pending</b>\n"
                    f"Order ID: <code>{tg_escape(entry_id)}</code>\n"
                    "Use <code>MODBUY 125</code> to modify limit price, "
                    "<code>CANCELBUY</code> to cancel, or <code>BUYSTATUS</code> to check."
                )
            return
        if avg > 0:
            plan.entry = price_tick(avg)
            plan.stop_loss = price_tick(avg * (1.0 - self.cfg.option_sl_pct))
            plan.risk = price_tick(plan.entry - plan.stop_loss)
            plan.target1 = price_tick(plan.entry + self.cfg.target1_r * plan.risk)
            plan.target2 = price_tick(plan.entry + self.cfg.target2_r * plan.risk)

        stop_trigger = price_tick(plan.stop_loss)
        stop_limit = 0.0
        if self.cfg.sl_order_type == "STOP_LOSS":
            stop_limit = price_tick(stop_trigger - self.cfg.stop_loss_limit_buffer)
        stop_order = self.api.place_order(
            "SELL",
            plan.security_id,
            filled_qty,
            self.cfg.sl_order_type,
            price=stop_limit,
            trigger_price=stop_trigger,
            correlation_id=make_correlation_id("SL"),
        )
        self.telegram.send(format_order_message("STOP ORDER PLACED", plan, stop_order))
        self.position = LivePosition(
            plan=plan,
            signal_time=signal.candle_time,
            opened_at=now_ist().replace(tzinfo=None),
            trigger_key=signal.trigger_key,
            current_sl=plan.stop_loss,
            remaining_qty=filled_qty,
            entry_order_id=entry_id,
            stop_order_id=order_id(stop_order),
            last_option_ts=pd.Timestamp(signal.candle_time),
        )
        self.pending_signal = None
        self.pending_plan = None
        self.pending_entry_order_id = None
        self.pending_entry_plan = None
        self.pending_entry_last_status = ""
        self.pending_entry_last_filled_qty = 0

    def manage_position(self, index_df: pd.DataFrame) -> None:
        if self.position is None:
            return
        pos = self.position
        start, end = day_start_end(now_ist().date())
        option_df = self.api.intraday_candles(
            pos.plan.security_id,
            self.cfg.fno_segment,
            self.cfg.option_instrument,
            self.cfg.candle_interval,
            start,
            end,
            oi=True,
        )
        option_df = closed_candles_only(option_df, self.cfg.candle_interval)
        if option_df.empty:
            return

        new_rows = option_df[option_df["timestamp"] > (pos.last_option_ts or pd.Timestamp(pos.signal_time))]
        if new_rows.empty:
            return

        index_by_ts = {pd.Timestamp(row.timestamp): i for i, row in index_df.iterrows()}
        for _, row in new_rows.iterrows():
            pos.last_option_ts = pd.Timestamp(row.timestamp)
            high = float(row.high)
            low = float(row.low)
            close = float(row.close)

            if low <= pos.current_sl:
                self.telegram.send(format_position_message(pos, "SL TOUCHED", "Broker stop-loss should protect this trade.", pos.current_sl, row.timestamp))
                return

            if row.timestamp.time() >= self.cfg.square_off_time:
                self.exit_position("square off", close)
                return

            if not pos.t1_hit and high >= pos.plan.target1:
                book_qty = min(t1_book_quantity(pos.plan.quantity, self.cfg), pos.remaining_qty)
                if book_qty >= pos.remaining_qty:
                    self.exit_position("target 1 full", pos.plan.target1)
                    return
                new_remaining = pos.remaining_qty - book_qty
                pos.t1_hit = True
                pos.current_sl = max(pos.current_sl, pos.plan.entry)
                self.modify_stop(new_remaining, pos.current_sl)
                self.sell_quantity(book_qty, "target 1", pos.plan.target1)
                pos.remaining_qty = new_remaining
                self.telegram.send(format_position_message(pos, "T1 HIT", "Booked partial quantity and moved SL to entry.", pos.plan.target1, row.timestamp))

            if pos.t1_hit and high >= pos.plan.target2:
                self.exit_position("target 2", pos.plan.target2)
                return

            if pos.t1_hit and self.cfg.trail_after_t1:
                prev = option_df[option_df["timestamp"] < row.timestamp]
                if not prev.empty:
                    new_sl = max(pos.current_sl, price_tick(float(prev.iloc[-1].low) - self.cfg.option_sl_buffer), pos.plan.entry)
                    if new_sl > pos.current_sl:
                        pos.current_sl = new_sl
                        self.modify_stop(pos.remaining_qty, pos.current_sl)
                        self.telegram.send(format_position_message(pos, "TRAIL SL", "Trailed to previous option candle low.", new_sl, row.timestamp))

            idx = index_by_ts.get(pd.Timestamp(row.timestamp))
            if idx is not None and opposite_exit(index_df, idx, pos.plan.side, self.cfg):
                self.exit_position("opposite RSI/BB exit", close)
                return

    def modify_stop(self, quantity: int, trigger_price: float) -> None:
        if self.position is None or not self.position.stop_order_id or quantity <= 0:
            return
        stop_limit = 0.0
        if self.cfg.sl_order_type == "STOP_LOSS":
            stop_limit = price_tick(trigger_price - self.cfg.stop_loss_limit_buffer)
        order = self.api.modify_order(
            self.position.stop_order_id,
            quantity,
            self.cfg.sl_order_type,
            price=stop_limit,
            trigger_price=trigger_price,
        )
        self.telegram.send(format_order_message("STOP ORDER MODIFIED", self.position.plan, order))

    def sell_quantity(self, quantity: int, reason: str, ref_price: float) -> None:
        if self.position is None or quantity <= 0:
            return
        exit_price = 0.0
        if self.cfg.exit_order_type == "LIMIT":
            exit_price = price_tick(ref_price - self.cfg.exit_limit_buffer)
        order = self.api.place_order(
            "SELL",
            self.position.plan.security_id,
            quantity,
            self.cfg.exit_order_type,
            price=exit_price,
            correlation_id=make_correlation_id("EXIT"),
        )
        self.telegram.send(format_order_message(f"EXIT ORDER PLACED - {reason}", self.position.plan, order))

    def exit_position(self, reason: str, ref_price: float) -> None:
        if self.position is None:
            return
        pos = self.position
        if pos.stop_order_id:
            try:
                cancelled = self.api.cancel_order(pos.stop_order_id)
                self.telegram.send(format_order_message("STOP ORDER CANCELLED", pos.plan, cancelled))
            except Exception as exc:
                self.telegram.send(f"<b>Stop cancel failed</b>\n{tg_escape(exc)}")
        self.sell_quantity(pos.remaining_qty, reason, ref_price)
        self.telegram.send(format_position_message(pos, "EXIT", reason, ref_price, now_ist().replace(tzinfo=None)))
        self.position = None

    def draft_message(self) -> str:
        if self.pending_plan is None:
            return "No editable trade draft is available."
        plan = self.pending_plan
        signal_time = self.pending_signal.candle_time if self.pending_signal else "-"
        return "\n".join(
            [
                "<b>Editable Trade Draft</b>",
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
                "<code>EDIT QTY 130</code>",
                "<code>EDIT STRIKE 22500</code>",
                "<code>EDIT SECURITY 123456</code>",
                "<code>RECALC</code>",
                "<code>BUY</code>",
            ]
        )

    def _resolve_security_for_draft(self, plan: OptionTradePlan) -> None:
        if not plan.expiry or plan.expiry.startswith("ROLLING"):
            plan.expiry = self.ensure_expiry()
        chain = self.api.option_chain(plan.expiry)
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
            self.recalculate_plan_targets(plan)
        plan.selection_reason = "manual strike resolved from live Dhan option chain"

    def recalculate_plan_targets(self, plan: OptionTradePlan) -> None:
        plan.entry = price_tick(plan.entry)
        plan.stop_loss = price_tick(plan.stop_loss)
        plan.risk = price_tick(max(plan.entry - plan.stop_loss, 0.05))
        plan.target1 = price_tick(plan.entry + self.cfg.target1_r * plan.risk)
        plan.target2 = price_tick(plan.entry + self.cfg.target2_r * plan.risk)

    def edit_draft(self, field: str, value: str) -> str:
        if self.pending_plan is None:
            return "No draft to edit. Wait for a signal or create one with the scanner."
        plan = self.pending_plan
        key = field.strip().lower().replace("_", "")
        raw_value = value.strip()

        if key in {"side"}:
            side = raw_value.upper()
            if side not in {"CE", "PE"}:
                return "SIDE must be CE or PE."
            plan.side = side
            self._resolve_security_for_draft(plan)
        elif key in {"strike"}:
            plan.strike = float(raw_value)
            self._resolve_security_for_draft(plan)
        elif key in {"security", "securityid"}:
            plan.security_id = int(float(raw_value))
            plan.selection_reason = "manual security id"
        elif key in {"expiry"}:
            plan.expiry = raw_value
            self._resolve_security_for_draft(plan)
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

    def modify_pending_buy(self, text: str) -> None:
        if not self.pending_entry_order_id or self.pending_entry_plan is None:
            self.telegram.send("No pending entry BUY order is stored.")
            return
        parts = text.split()
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
        self.pending_entry_plan = None
        self.pending_entry_last_status = ""
        self.pending_entry_last_filled_qty = 0

    def activate_filled_pending_buy(self, latest: Dict[str, Any]) -> None:
        if not self.pending_entry_order_id or self.pending_entry_plan is None or self.pending_signal is None:
            return
        display_order_id = self.pending_entry_order_id
        plan = self.pending_entry_plan
        filled_qty = order_filled_qty(latest)
        if filled_qty <= 0:
            return
        if order_status(latest) == "PART_TRADED":
            try:
                cancelled = self.api.cancel_order(display_order_id)
                self.telegram.send(format_order_message("PENDING BUY REMAINDER CANCELLED", plan, cancelled))
            except Exception as exc:
                self.telegram.send(f"<b>Pending buy cancel warning</b>\n{tg_escape(exc)}")
        avg = order_avg_price(latest)
        if avg > 0:
            plan.entry = price_tick(avg)
            plan.stop_loss = price_tick(avg * (1.0 - self.cfg.option_sl_pct))
            self.recalculate_plan_targets(plan)
        stop_trigger = price_tick(plan.stop_loss)
        stop_limit = 0.0
        if self.cfg.sl_order_type == "STOP_LOSS":
            stop_limit = price_tick(stop_trigger - self.cfg.stop_loss_limit_buffer)
        stop_order = self.api.place_order(
            "SELL",
            plan.security_id,
            filled_qty,
            self.cfg.sl_order_type,
            price=stop_limit,
            trigger_price=stop_trigger,
            correlation_id=make_correlation_id("SL"),
        )
        self.position = LivePosition(
            plan=plan,
            signal_time=self.pending_signal.candle_time,
            opened_at=now_ist().replace(tzinfo=None),
            trigger_key=self.pending_signal.trigger_key,
            current_sl=plan.stop_loss,
            remaining_qty=filled_qty,
            entry_order_id=display_order_id,
            stop_order_id=order_id(stop_order),
            last_option_ts=pd.Timestamp(now_ist().replace(tzinfo=None)),
        )
        self.telegram.send(format_order_message("STOP ORDER PLACED AFTER ENTRY FILL", plan, stop_order))
        self.telegram.send(format_position_message(self.position, "ENTRY FILLED", "Dhan buy filled; protective SL placed.", plan.entry, now_ist().replace(tzinfo=None)))
        self.pending_signal = None
        self.pending_plan = None
        self.pending_entry_order_id = None
        self.pending_entry_plan = None
        self.pending_entry_last_status = ""
        self.pending_entry_last_filled_qty = 0

    def sync_pending_buy(self) -> None:
        if not self.pending_entry_order_id or self.pending_entry_plan is None:
            return
        display_order_id = self.pending_entry_order_id
        latest = self.api.get_order(display_order_id)
        status = order_status(latest)
        filled_qty = order_filled_qty(latest)
        changed = status != self.pending_entry_last_status or filled_qty != self.pending_entry_last_filled_qty
        if changed:
            self.telegram.send(format_order_message("PENDING BUY UPDATE", self.pending_entry_plan, latest))
            self.pending_entry_last_status = status
            self.pending_entry_last_filled_qty = filled_qty
        if filled_qty > 0 and self.position is None:
            self.activate_filled_pending_buy(latest)
        elif status in {"REJECTED", "CANCELLED", "EXPIRED"}:
            self.telegram.send(f"Pending BUY order closed with status: <b>{tg_escape(status)}</b>")
            self.pending_entry_order_id = None
            self.pending_entry_plan = None
            self.pending_entry_last_status = ""
            self.pending_entry_last_filled_qty = 0

    def pending_buy_status(self) -> str:
        if not self.pending_entry_order_id or self.pending_entry_plan is None:
            return "No pending entry BUY order is stored."
        display_order_id = self.pending_entry_order_id
        latest = self.api.get_order(display_order_id)
        filled_qty = order_filled_qty(latest)
        status = order_status(latest)
        self.pending_entry_last_status = status
        self.pending_entry_last_filled_qty = filled_qty
        if filled_qty > 0 and self.position is None:
            self.activate_filled_pending_buy(latest)
        return "\n".join(
            [
                "<b>Pending BUY Status</b>",
                f"Order ID : {tg_escape(display_order_id)}",
                f"Status   : {tg_escape(status)}",
                f"Filled   : {filled_qty}",
                f"Avg      : Rs {fmt(order_avg_price(latest))}",
            ]
        )

    def help_text(self) -> str:
        mode = "REAL ORDERS" if self.cfg.live_trading_enabled else "DRY RUN ALERTS"
        buy_mode = "AUTO BUY" if self.cfg.auto_buy_enabled else "EDIT THEN BUY"
        status = "ON" if self.live_enabled else "OFF"
        return "\n".join(
            [
                "<b>NIFTY RSI+BB Bot Commands</b>",
                "",
                "<b>Scanner</b>",
                "LIVE - enable live scanner",
                "STOP - pause live scanner",
                "DRYRUN - alerts only, no real orders",
                "AUTOORDER or AUTOBUY - enable real Dhan orders + auto-buy",
                "MANUALBUY - real Dhan orders, but wait for BUY command",
                "",
                "<b>Editable Buy Draft</b>",
                "/draft - show latest draft",
                "EDIT ENTRY 125",
                "EDIT SL 100",
                "EDIT T1 150",
                "EDIT T2 175",
                "EDIT QTY 130",
                "EDIT STRIKE 22500",
                "EDIT SECURITY 123456",
                "RECALC - recalculate T1/T2 from entry and SL",
                "BUY - place Dhan BUY order from draft",
                "MODBUY 125 - modify pending limit buy price",
                "CANCELBUY - cancel pending buy",
                "BUYSTATUS - check pending buy",
                "",
                "<b>Backtest</b>",
                "SCAN 2026-05-01 2026-05-15",
                "",
                "<b>Info</b>",
                "/status - latest NIFTY RSI/BB context",
                "/chain - option chain around ATM",
                "/position - active live position",
                "/expiry - selected expiry",
                "/help - this message",
                "",
                f"Scanner: {status}",
                f"Mode: {mode}",
                f"Buy flow: {buy_mode}",
            ]
        )

    def status_message(self) -> str:
        raw = closed_candles_only(self.current_index(), self.cfg.candle_interval)
        if raw.empty:
            return "No closed NIFTY candles available yet."
        df = add_indicators(raw, self.cfg)
        row = df.iloc[-1]
        mode = "REAL ORDERS" if self.cfg.live_trading_enabled else "DRY RUN ALERTS"
        return "\n".join(
            [
                f"<b>{tg_escape(self.cfg.index_name)} RSI+BB Status</b>",
                f"Scanner    : {'ON' if self.live_enabled else 'OFF'}",
                f"Mode       : {mode}",
                f"Candle     : {tg_escape(row.timestamp)}",
                f"Close      : {fmt(row.close)}",
                f"RSI 5m     : {fmt(row.rsi)}",
                f"RSI {self.cfg.htf_interval}m    : {fmt(row.htf_rsi)}",
                f"HTF slope  : {fmt(row.htf_rsi_slope)}",
                f"BB lower   : {fmt(row.bb_lower)}",
                f"BB mid     : {fmt(row.bb_mid)}",
                f"BB upper   : {fmt(row.bb_upper)}",
                f"VWAP       : {fmt(row.vwap)}",
            ]
        )

    def chain_message(self) -> str:
        expiry = self.ensure_expiry()
        chain = self.api.option_chain(expiry)
        oc = get_oc(chain)
        spot = get_chain_spot(chain)
        if not oc:
            return "Option chain is empty."
        strikes = sorted(float(k) for k in oc.keys())
        atm = nearest_strike(strikes, spot, self.cfg.strike_step)
        nearby = sorted(strikes, key=lambda strike: abs(strike - atm))[:11]
        lines = [
            f"<b>{tg_escape(self.cfg.index_name)} Option Chain</b>",
            f"Expiry: {tg_escape(expiry)} | Spot: {fmt(spot)} | ATM: {fmt(atm, 0)}",
            "<pre>",
            f"{'Strike':<8} {'CE LTP':>8} {'CE Ask':>8} | {'PE LTP':>8} {'PE Ask':>8}",
            "-" * 48,
        ]
        for strike in sorted(nearby):
            row = get_row(oc, strike)
            ce = row.get("ce") or {}
            pe = row.get("pe") or {}
            lines.append(
                f"{strike:<8.0f} {num(ce.get('last_price')):>8.2f} {num(ce.get('top_ask_price')):>8.2f} | "
                f"{num(pe.get('last_price')):>8.2f} {num(pe.get('top_ask_price')):>8.2f}"
            )
        lines.append("</pre>")
        return "\n".join(lines)

    def position_message(self) -> str:
        if self.position is None:
            return "No active live position is being tracked."
        pos = self.position
        plan = pos.plan
        return "\n".join(
            [
                "<b>Active Live Position</b>",
                f"Option    : {plan.side} {fmt(plan.strike, 0)}",
                f"Expiry    : {tg_escape(plan.expiry)}",
                f"Security  : {plan.security_id}",
                f"Entry     : Rs {fmt(plan.entry)}",
                f"SL        : Rs {fmt(pos.current_sl)}",
                f"T1 / T2   : Rs {fmt(plan.target1)} / Rs {fmt(plan.target2)}",
                f"Remaining : {pos.remaining_qty}",
                f"T1 hit    : {'yes' if pos.t1_hit else 'no'}",
                f"Entry ID  : {tg_escape(pos.entry_order_id or '-')}",
                f"SL ID     : {tg_escape(pos.stop_order_id or '-')}",
            ]
        )

    def run_backtest_from_telegram(self, start_date: str, end_date: str) -> None:
        self.telegram.send(
            f"<b>RSI+BB backtest started</b>\nPeriod: {tg_escape(start_date)} to {tg_escape(end_date)}\n"
            "This may take a while because it fetches historical option candles."
        )
        result = RsiBbBacktester(self.cfg, self.api).run(start_date, end_date)
        csv_path, json_path = save_backtest_outputs(result, Path("backtest_results"))
        summary = format_backtest_summary(result)
        self.telegram.send(f"<pre>{tg_escape(summary[:3500])}</pre>")
        self.telegram.send(
            f"Saved CSV: <code>{tg_escape(csv_path)}</code>\n"
            f"Saved JSON: <code>{tg_escape(json_path)}</code>"
        )

    def dispatch_telegram_command(self, text: str) -> None:
        command = text.strip()
        lower = command.split("@")[0].strip().lower()
        match = SCAN_RE.match(command)
        if match:
            self.run_backtest_from_telegram(match.group(1), match.group(2))
            return
        edit_match = re.match(r"^(?:EDIT|SET)\s+([A-Za-z_]+)\s+(.+)$", command, re.IGNORECASE)
        if edit_match:
            self.telegram.send(self.edit_draft(edit_match.group(1), edit_match.group(2)))
            return
        if lower in {"/help", "help", "/start", "start"}:
            self.telegram.send(self.help_text())
        elif lower in {"/draft", "draft", "plan"}:
            self.telegram.send(self.draft_message())
        elif lower in {"/status", "status"}:
            self.telegram.send(self.status_message())
        elif lower in {"/chain", "chain"}:
            self.telegram.send(self.chain_message())
        elif lower in {"/position", "position"}:
            self.telegram.send(self.position_message())
        elif lower in {"/expiry", "expiry"}:
            self.telegram.send(f"Selected expiry: <b>{tg_escape(self.ensure_expiry())}</b>")
        elif lower in {"buy", "/buy", "confirm", "/confirm", "placebuy", "/placebuy"}:
            self.buy_draft()
        elif lower in {"canceldraft", "/canceldraft"}:
            self.pending_signal = None
            self.pending_plan = None
            self.telegram.send("Editable trade draft cleared.")
        elif lower in {"recalc", "/recalc"}:
            if self.pending_plan is None:
                self.telegram.send("No draft is available to recalculate.")
            else:
                self.recalculate_plan_targets(self.pending_plan)
                self.telegram.send(self.draft_message())
        elif lower.startswith("modbuy"):
            self.modify_pending_buy(command)
        elif lower in {"cancelbuy", "/cancelbuy"}:
            self.cancel_pending_buy()
        elif lower in {"buystatus", "/buystatus"}:
            self.telegram.send(self.pending_buy_status())
        elif lower in {"live", "/live"}:
            self.live_enabled = True
            self.telegram.send("Live scanner enabled.")
        elif lower in {"stop", "/stop"}:
            self.live_enabled = False
            self.telegram.send("Live scanner paused. Send LIVE to enable it again.")
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
        else:
            self.telegram.send(f"Unknown command: <code>{tg_escape(command)}</code>\n\n{self.help_text()}")

    def process_telegram_messages(self) -> None:
        for message in self.telegram.get_messages():
            try:
                self.dispatch_telegram_command(message)
            except Exception as exc:
                LOG.exception("Telegram command failed | message=%s", message)
                self.telegram.send(f"<b>Command error</b>\n{tg_escape(exc)}")

    def tick(self) -> None:
        if not self.live_enabled:
            return
        if not market_session_open():
            return
        self.sync_pending_buy()
        raw = closed_candles_only(self.current_index(), self.cfg.candle_interval)
        if len(raw) < max(60, self.cfg.bb_period + self.cfg.setup_lookback):
            return
        index_df = add_indicators(raw, self.cfg)
        self.manage_position(index_df)

        latest = index_df.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_candle_time:
            return
        self.last_candle_time = candle_time
        if self.position is not None:
            return

        signal = build_rsi_bb_signal(index_df, len(index_df) - 1, self.cfg)
        if signal is None or signal.trigger_key == self.last_trigger:
            return
        plan = self.build_live_plan(signal)
        self.open_position(signal, plan)
        self.last_trigger = signal.trigger_key

    def run(self) -> None:
        mode = "REAL ORDERS" if self.cfg.live_trading_enabled else "DRY RUN ALERTS"
        buy_mode = "AUTO BUY" if self.cfg.auto_buy_enabled else "EDIT THEN BUY"
        self.telegram.send(
            f"<b>{tg_escape(self.cfg.index_name)} RSI+BB option bot online</b>\n"
            f"Scanner: {'ON' if self.live_enabled else 'OFF'}\n"
            f"Mode: {mode}\n"
            f"Buy flow: {buy_mode}\n"
            "Send <code>/help</code> for commands."
        )
        while True:
            try:
                self.process_telegram_messages()
                self.tick()
            except KeyboardInterrupt:
                self.telegram.send("RSI+BB option bot stopped.")
                raise
            except Exception as exc:
                LOG.exception("Live tick failed")
                self.telegram.send(f"<b>Live tick error</b>\n{tg_escape(exc)}")
            time.sleep(max(1.0, self.cfg.poll_seconds))


# -----------------------------------------------------------------------------
# Formatting
# -----------------------------------------------------------------------------


def format_signal_message(signal: RsiBbSignal, plan: OptionTradePlan, live_trading: bool, auto_buy: bool = False) -> str:
    reasons = "\n".join(f"- {tg_escape(reason)}" for reason in signal.reasons)
    mode = "AUTO BUY" if live_trading and auto_buy else ("EDIT THEN BUY" if live_trading else "DRY RUN")
    return "\n".join(
        [
            "<b>RSI+BB NIFTY OPTION SIGNAL</b>",
            f"Mode       : {mode}",
            f"Direction  : {signal.direction}",
            f"Signal time: {tg_escape(signal.candle_time)}",
            f"Spot       : {fmt(signal.spot)}",
            f"RSI 5m     : {fmt(signal.rsi)}",
            f"RSI HTF    : {fmt(signal.htf_rsi)}",
            f"VWAP       : {fmt(signal.vwap)}",
            "",
            "<b>Option Plan</b>",
            f"Buy        : {plan.side} {fmt(plan.strike, 0)}",
            f"Expiry     : {tg_escape(plan.expiry)}",
            f"Security ID: {plan.security_id}",
            f"Entry ref  : Rs {fmt(plan.entry)}",
            f"SL         : Rs {fmt(plan.stop_loss)}",
            f"T1 / T2    : Rs {fmt(plan.target1)} / Rs {fmt(plan.target2)}",
            f"Qty        : {plan.quantity}",
            f"Selector   : {tg_escape(plan.selection_reason)}",
            "",
            "<b>Reasons</b>",
            reasons,
            "",
            "<b>Next</b>",
            "Use <code>/draft</code>, <code>EDIT ENTRY 125</code>, <code>EDIT SL 100</code>, "
            "and <code>BUY</code> unless auto-buy is enabled.",
        ]
    )


def format_order_message(title: str, plan: OptionTradePlan, order: Dict[str, Any]) -> str:
    req = order.get("_request", {}) if isinstance(order, dict) else {}
    return "\n".join(
        [
            f"<b>{tg_escape(title)}</b>",
            f"Option   : {plan.side} {fmt(plan.strike, 0)}",
            f"Order ID : {tg_escape(order.get('orderId') or order.get('order_id') or '-')}",
            f"Status   : {tg_escape(order_status(order))}",
            f"Txn      : {tg_escape(order.get('transactionType') or req.get('transactionType') or '-')}",
            f"Qty      : {tg_escape(order.get('quantity') or req.get('quantity') or '-')}",
            f"Price    : Rs {fmt(order.get('price') or req.get('price') or 0)}",
            f"Trigger  : Rs {fmt(order.get('triggerPrice') or req.get('triggerPrice') or 0)}",
            f"Error    : {tg_escape(order.get('omsErrorDescription') or '-')}",
        ]
    )


def format_position_message(position: LivePosition, action: str, reason: str, price: float, event_time: Any) -> str:
    plan = position.plan
    return "\n".join(
        [
            "<b>LIVE POSITION UPDATE</b>",
            f"Action   : {tg_escape(action)}",
            f"Reason   : {tg_escape(reason)}",
            f"Time     : {tg_escape(event_time)}",
            f"Option   : {plan.side} {fmt(plan.strike, 0)}",
            f"Entry    : Rs {fmt(plan.entry)}",
            f"Ref price: Rs {fmt(price)}",
            f"SL       : Rs {fmt(position.current_sl)}",
            f"T1 / T2  : Rs {fmt(plan.target1)} / Rs {fmt(plan.target2)}",
            f"Remain   : {position.remaining_qty}",
        ]
    )


def extract_rolling_offset(selection_reason: str) -> Optional[int]:
    match = re.search(r"\bATM([+-]\d+)\b", selection_reason or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def entry_time_bucket(entry_time: str) -> str:
    try:
        ts = pd.Timestamp(entry_time)
        if ts.time() < dt.time(13, 30):
            return "before 13:30"
        if ts.time() < dt.time(14, 0):
            return "13:30-14:00"
        return "14:00+"
    except Exception:
        return "unknown"


def grouped_backtest_stats(trades: List[BacktestTrade], key_func: Any) -> List[Tuple[str, int, float, float, float]]:
    buckets: Dict[str, List[BacktestTrade]] = {}
    for trade in trades:
        key = str(key_func(trade))
        buckets.setdefault(key, []).append(trade)
    rows: List[Tuple[str, int, float, float, float]] = []
    for key, bucket in buckets.items():
        pnl = float(sum(t.pnl for t in bucket))
        wins = sum(1 for t in bucket if t.pnl > 0)
        win_rate = wins / len(bucket) * 100.0 if bucket else 0.0
        avg = pnl / len(bucket) if bucket else 0.0
        rows.append((key, len(bucket), pnl, win_rate, avg))
    return rows


def format_grouped_stats(title: str, rows: List[Tuple[str, int, float, float, float]], limit: int = 8) -> List[str]:
    if not rows:
        return []
    out = ["", title, f"{'Group':<20} {'N':>3} {'PnL':>11} {'WR':>6} {'Avg':>10}", "-" * 56]
    for key, count, pnl, win_rate, avg in rows[:limit]:
        out.append(f"{key:<20} {count:>3} {pnl:>11.2f} {win_rate:>5.1f}% {avg:>10.2f}")
    return out


def format_backtest_summary(result: BacktestResult) -> str:
    trades = result.trades
    avg_win = statistics.mean([t.pnl for t in trades if t.pnl > 0]) if any(t.pnl > 0 for t in trades) else 0.0
    avg_loss = statistics.mean([t.pnl for t in trades if t.pnl <= 0]) if any(t.pnl <= 0 for t in trades) else 0.0
    best = max((t.pnl for t in trades), default=0.0)
    worst = min((t.pnl for t in trades), default=0.0)
    lines = [
        "RSI+BB NIFTY Option Backtest",
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
        side_rows = sorted(grouped_backtest_stats(trades, lambda t: t.side), key=lambda row: row[0])
        reason_rows = sorted(grouped_backtest_stats(trades, lambda t: t.exit_reason), key=lambda row: row[2])
        offset_rows = sorted(
            grouped_backtest_stats(trades, lambda t: extract_rolling_offset(t.selection_reason)),
            key=lambda row: (999 if row[0] == "None" else int(row[0])),
        )
        time_rows = grouped_backtest_stats(trades, lambda t: entry_time_bucket(t.entry_time))
        month_rows = sorted(grouped_backtest_stats(trades, lambda t: str(t.entry_time)[:7]), key=lambda row: row[0])
        lines.extend(format_grouped_stats("By side:", side_rows))
        lines.extend(format_grouped_stats("By exit reason:", reason_rows))
        lines.extend(format_grouped_stats("By rolling offset:", offset_rows, limit=20))
        lines.extend(format_grouped_stats("By entry time:", time_rows))
        lines.extend(format_grouped_stats("By month:", month_rows, limit=18))

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


def save_backtest_outputs(result: BacktestResult, output_dir: Path) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{result.start_date}_to_{result.end_date}"
    csv_path = output_dir / f"rsi_bb_backtest_{stamp}.csv"
    json_path = output_dir / f"rsi_bb_backtest_{stamp}.json"
    pd.DataFrame([trade.__dict__ for trade in result.trades]).to_csv(csv_path, index=False)
    payload = {
        "start_date": result.start_date,
        "end_date": result.end_date,
        "trades": [trade.__dict__ for trade in result.trades],
        "errors": result.errors,
        "summary": {
            "total_pnl": result.total_pnl,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": result.win_rate,
            "max_drawdown": result.max_drawdown,
            "gross_profit": float(sum(t.pnl for t in result.trades if t.pnl > 0)),
            "gross_loss": float(sum(t.pnl for t in result.trades if t.pnl <= 0)),
        },
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return csv_path, json_path


# -----------------------------------------------------------------------------
# Self-test
# -----------------------------------------------------------------------------


def synthetic_index_data() -> pd.DataFrame:
    start = dt.datetime(2026, 5, 18, 9, 15)
    rows: List[Dict[str, Any]] = []
    price = 22500.0
    for i in range(75):
        ts = start + dt.timedelta(minutes=5 * i)
        if i < 25:
            price += 4
        elif i < 36:
            price += 18
        elif i < 44:
            price -= 8
        elif i == 44:
            price += 55
        elif i < 50:
            price += 14
        else:
            price += 3
        open_ = price - 5
        close = price
        high = max(open_, close) + 8
        low = min(open_, close) - 8
        rows.append({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": 1000 + i * 10})
    return pd.DataFrame(rows)


def synthetic_option_data(index_df: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    premium = 100.0
    for i, row in index_df.iterrows():
        if i < 44:
            premium += 0.1
        elif i < 50:
            premium += 6.0
        else:
            premium += 2.0
        open_ = premium
        high = premium + 6
        low = max(0.05, premium - 4)
        close = premium + 2
        rows.append({"timestamp": row.timestamp, "open": open_, "high": high, "low": low, "close": close, "volume": 1000})
    return pd.DataFrame(rows)


def run_self_test() -> None:
    cfg = Config(
        require_vwap=False,
        setup_lookback=30,
        preferred_premium_min=80,
        preferred_premium_max=180,
        htf_interval=5,
    )
    index_df = add_indicators(synthetic_index_data(), cfg)
    signals = [build_rsi_bb_signal(index_df, i, cfg) for i in range(len(index_df))]
    signals = [s for s in signals if s is not None]
    if not signals:
        raise AssertionError("Synthetic data did not produce a signal.")

    signal = signals[0]
    entry_idx = int(index_df.index[index_df["timestamp"] > pd.Timestamp(signal.candle_time)][0])
    option_df = synthetic_option_data(index_df)
    entry_pos = find_exact_candle(option_df, index_df.iloc[entry_idx].timestamp)
    assert entry_pos is not None
    contract = OptionContract(signal.side, 22500.0, "SYNTHETIC", 0, float(option_df.iloc[entry_pos].open), "synthetic")
    plan = make_trade_plan(contract, option_df, entry_pos, cfg)
    trade = RsiBbBacktester(cfg, api=None).simulate_trade(index_df, entry_idx, signal, option_df, entry_pos, plan)  # type: ignore[arg-type]
    if trade.pnl <= 0:
        raise AssertionError(f"Synthetic trade should be profitable, got {trade.pnl}")
    print("Self-test passed")
    print(f"Signal: {signal.side} at {signal.candle_time}, entry {trade.entry}, exit {trade.exit_price}, pnl {trade.pnl}")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> None:
    cfg = Config.from_env(require_dhan=True)
    setup_logging(cfg)
    api = DhanApiClient(cfg)
    result = RsiBbBacktester(cfg, api).run(args.start, args.end)
    print(format_backtest_summary(result))
    csv_path, json_path = save_backtest_outputs(result, Path(args.output_dir))
    print(f"\nSaved CSV : {csv_path}")
    print(f"Saved JSON: {json_path}")


def cmd_live(_: argparse.Namespace) -> None:
    cfg = Config.from_env(require_dhan=True)
    setup_logging(cfg)
    api = DhanApiClient(cfg)
    telegram = TelegramBot(cfg)
    RsiBbLiveBot(cfg, api, telegram).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NIFTY RSI+Bollinger option-buying bot")
    sub = parser.add_subparsers(dest="command")

    backtest = sub.add_parser("backtest", help="Run Dhan rolling-option backtest")
    backtest.add_argument("--start", required=True, help="YYYY-MM-DD")
    backtest.add_argument("--end", required=True, help="YYYY-MM-DD")
    backtest.add_argument("--output-dir", default="backtest_results")
    backtest.set_defaults(func=cmd_backtest)

    live = sub.add_parser("live", help="Run live scanner. Dry-run unless LIVE_TRADING_ENABLED=true")
    live.set_defaults(func=cmd_live)

    self_test = sub.add_parser("self-test", help="Run local synthetic test without Dhan credentials")
    self_test.set_defaults(func=lambda _: run_self_test())
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
