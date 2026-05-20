"""NIFTY Brahmastra option-buying bot with live orders, alerts, and backtests.

What this program does
- Monitors NIFTY 50 on 5-minute candles.
- Uses Supertrend 20,2 + MACD 12,26,9 + daily VWAP.
- Sends Telegram alerts when the full Brahmastra setup appears.
- Selects ATM / slightly ITM NIFTY CE or PE using live premium filters.
- Places live Dhan option BUY orders immediately after live signals.
- Places and modifies broker-side SELL STOP_LOSS_MARKET orders for protection.
- Books 50% at T1, moves SL to entry, trails the rest, and exits on T2,
  SL, opposite signal, or square-off.
- Runs historical backtests with option entry/exit from historical option candles.

Important backtest rule
- This script never uses current option LTP for backtest trades.
- A backtest signal enters on the next 5-minute candle open.
- The option premium must come from that contract's historical candle at that
  same timestamp.
- If a Dhan master CSV is available, historical option security ids are resolved
  from it. Otherwise, for still-listed expiries, the live option chain may be
  used only as a security-id map. Prices still come only from historical candles.
- Expired contracts require DHAN_SCRIP_MASTER_CSV with those contracts.

Important live-order rule
- Live mode places real Dhan orders. There is no paper-trading guard.
- Entry orders are BUY MARKET by default.
- Exit orders are SELL MARKET, while protection is maintained through a pending
  SELL STOP_LOSS_MARKET order that the bot modifies as SL changes.
- Dhan may convert API market orders to limit orders with MPP as per its rules.

Telegram commands
- SCAN YYYY-MM-DD YYYY-MM-DD  Run historical backtest.
- STOP                       Stop running scan.
- LIVE                       Enable live monitoring.
- /chain                     Show current option chain around ATM.
- /status                    Show current NIFTY context.
- /position                  Show active live positions.
- /expiry                    Show selected weekly expiry.
- /help                      Show help.

Environment variables
- DHAN_CLIENT_ID
- DHAN_ACCESS_TOKEN
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

Useful optional variables
- DHAN_SCRIP_MASTER_CSV=/path/to/api-scrip-master.csv
- DOWNLOAD_DHAN_MASTER=true
- DHAN_SCRIP_MASTER_URL=https://images.dhan.co/api-data/api-scrip-master.csv
- DHAN_MASTER_REFRESH_DAYS=1
- USE_DHAN_EXPIRED_OPTIONS_API=true
- EXPIRED_OPTIONS_EXPIRY_FLAG=WEEK
- EXPIRED_OPTIONS_EXPIRY_CODE=0
- MAX_BACKTEST_EXPIRY_GAP_DAYS=10
- MIN_PREMIUM=60
- PREFERRED_PREMIUM_MIN=80
- PREFERRED_PREMIUM_MAX=180
- MAX_PREMIUM=250
- LOT_SIZE=65
- LOTS=2
- ORDER_PRODUCT_TYPE=INTRADAY
- ENTRY_ORDER_TYPE=MARKET
- EXIT_ORDER_TYPE=MARKET
- SL_ORDER_TYPE=STOP_LOSS_MARKET
- ORDER_VALIDITY=DAY

Logging optional variables
- LOG_LEVEL=INFO
- LOG_DIR=logs
- LOG_FILE=brahmastra_bot.log
- LOG_TO_CONSOLE=true
- LOG_HTTP_PAYLOADS=false
- LOG_LIVE_SCAN_EVERY_SECONDS=0
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import math
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DHAN_BASE = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
DHAN_MASTER_DETAILED_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

NIFTY50_SECURITY_ID = 13
NIFTY50_SEGMENT = "IDX_I"
NIFTY50_INSTRUMENT = "INDEX"
NIFTY50_NAME = "NIFTY 50"

FNO_SEGMENT = "NSE_FNO"
OPTION_INSTRUMENT = "OPTIDX"

IST = ZoneInfo("Asia/Kolkata")

SCAN_RE = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
STOP_RE = re.compile(r"^STOP$", re.IGNORECASE)
LIVE_RE = re.compile(r"^LIVE$", re.IGNORECASE)
STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)

ORDER_FILLED_STATUSES = {"TRADED"}
ORDER_DEAD_STATUSES = {"REJECTED", "CANCELLED", "EXPIRED"}
ORDER_WORKING_STATUSES = {"TRANSIT", "PENDING", "PART_TRADED"}

LOGGER_NAME = "nifty_brahmastra"
LOG = logging.getLogger(LOGGER_NAME)
SENSITIVE_KEY_RE = re.compile(r"(token|access|authorization|password|secret|chat[_-]?id|client[_-]?id|clientid|dhanclientid)", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Config and logging
# -----------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


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


def log_json(value: Any, max_len: int = 3000) -> str:
    try:
        text = json.dumps(redact_for_log(value), ensure_ascii=True, default=str)
    except Exception:
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "...<truncated>"
    return text


@dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str

    http_timeout: int = 20
    tg_poll_interval: float = 5.0

    candle_interval: int = 5
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
    avoid_first_minutes: int = 5

    strike_step: int = 50
    option_search_depth: int = 4
    strikes_window: int = 5
    max_backtest_expiry_gap_days: int = 10

    min_premium: float = 60.0
    preferred_premium_min: float = 80.0
    preferred_premium_max: float = 180.0
    max_premium: float = 250.0
    allow_premium_fallback: bool = False

    option_sl_buffer: float = 0.50
    nifty_sl_buffer: float = 10.0
    fallback_option_sl_pct: float = 0.20

    lot_size: int = 65
    lots: int = 2
    brokerage_per_order: float = 0.0

    square_off_time: dt.time = dt.time(15, 20)
    live_enabled_at_start: bool = True

    order_product_type: str = "INTRADAY"
    entry_order_type: str = "MARKET"
    exit_order_type: str = "MARKET"
    sl_order_type: str = "STOP_LOSS_MARKET"
    order_validity: str = "DAY"
    order_status_poll_attempts: int = 15
    order_status_poll_seconds: float = 1.0

    dhan_scrip_master_csv: Optional[str] = None
    download_dhan_master: bool = False
    dhan_scrip_master_url: str = DHAN_MASTER_URL
    dhan_master_refresh_days: int = 1
    use_dhan_expired_options_api: bool = True
    expired_options_expiry_flag: str = "WEEK"
    expired_options_expiry_code: int = 0

    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "brahmastra_bot.log"
    log_to_console: bool = True
    log_http_payloads: bool = False
    log_live_scan_every_seconds: float = 0.0

    @property
    def quantity(self) -> int:
        return max(1, self.lot_size * self.lots)

    @staticmethod
    def from_env() -> "Config":
        required = {
            "DHAN_CLIENT_ID": os.getenv("DHAN_CLIENT_ID", "").strip(),
            "DHAN_ACCESS_TOKEN": os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise SystemExit("Missing env vars: " + ", ".join(missing))

        cfg = Config(
            dhan_client_id=required["DHAN_CLIENT_ID"],
            dhan_access_token=required["DHAN_ACCESS_TOKEN"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            http_timeout=_env_int("HTTP_TIMEOUT", 20),
            tg_poll_interval=_env_float("TG_POLL_INTERVAL", 5.0),
            candle_interval=_env_int("CANDLE_INTERVAL", 5),
            supertrend_period=_env_int("SUPERTREND_PERIOD", 20),
            supertrend_multiplier=_env_float("SUPERTREND_MULTIPLIER", 2.0),
            signal_lookback=_env_int("SIGNAL_LOOKBACK", 3),
            swing_lookback=_env_int("SWING_LOOKBACK", 6),
            volume_lookback=_env_int("VOLUME_LOOKBACK", 20),
            volume_multiplier=_env_float("VOLUME_MULTIPLIER", 1.10),
            min_body_ratio=_env_float("MIN_BODY_RATIO", 0.45),
            require_volume=_env_bool("REQUIRE_VOLUME", True),
            require_market_structure=_env_bool("REQUIRE_MARKET_STRUCTURE", False),
            strike_step=_env_int("STRIKE_STEP", 50),
            option_search_depth=_env_int("OPTION_SEARCH_DEPTH", 4),
            strikes_window=_env_int("STRIKES_WINDOW", 5),
            max_backtest_expiry_gap_days=_env_int("MAX_BACKTEST_EXPIRY_GAP_DAYS", 10),
            min_premium=_env_float("MIN_PREMIUM", 60.0),
            preferred_premium_min=_env_float("PREFERRED_PREMIUM_MIN", 80.0),
            preferred_premium_max=_env_float("PREFERRED_PREMIUM_MAX", 180.0),
            max_premium=_env_float("MAX_PREMIUM", 250.0),
            allow_premium_fallback=_env_bool("ALLOW_PREMIUM_FALLBACK", False),
            option_sl_buffer=_env_float("OPTION_SL_BUFFER", 0.50),
            nifty_sl_buffer=_env_float("NIFTY_SL_BUFFER", 10.0),
            fallback_option_sl_pct=_env_float("FALLBACK_OPTION_SL_PCT", 0.20),
            lot_size=_env_int("LOT_SIZE", 65),
            lots=_env_int("LOTS", 2),
            brokerage_per_order=_env_float("BROKERAGE_PER_ORDER", 0.0),
            live_enabled_at_start=_env_bool("LIVE_ENABLED", True),
            order_product_type=_env_str("ORDER_PRODUCT_TYPE", "INTRADAY").upper(),
            entry_order_type=_env_str("ENTRY_ORDER_TYPE", "MARKET").upper(),
            exit_order_type=_env_str("EXIT_ORDER_TYPE", "MARKET").upper(),
            sl_order_type=_env_str("SL_ORDER_TYPE", "STOP_LOSS_MARKET").upper(),
            order_validity=_env_str("ORDER_VALIDITY", "DAY").upper(),
            order_status_poll_attempts=_env_int("ORDER_STATUS_POLL_ATTEMPTS", 15),
            order_status_poll_seconds=_env_float("ORDER_STATUS_POLL_SECONDS", 1.0),
            dhan_scrip_master_csv=(os.getenv("DHAN_SCRIP_MASTER_CSV") or "").strip() or None,
            download_dhan_master=_env_bool("DOWNLOAD_DHAN_MASTER", False),
            dhan_scrip_master_url=_env_str("DHAN_SCRIP_MASTER_URL", DHAN_MASTER_URL),
            dhan_master_refresh_days=_env_int("DHAN_MASTER_REFRESH_DAYS", 1),
            use_dhan_expired_options_api=_env_bool("USE_DHAN_EXPIRED_OPTIONS_API", True),
            expired_options_expiry_flag=_env_str("EXPIRED_OPTIONS_EXPIRY_FLAG", "WEEK").upper(),
            expired_options_expiry_code=_env_int("EXPIRED_OPTIONS_EXPIRY_CODE", 0),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            log_dir=_env_str("LOG_DIR", "logs"),
            log_file=_env_str("LOG_FILE", "brahmastra_bot.log"),
            log_to_console=_env_bool("LOG_TO_CONSOLE", True),
            log_http_payloads=_env_bool("LOG_HTTP_PAYLOADS", False),
            log_live_scan_every_seconds=_env_float("LOG_LIVE_SCAN_EVERY_SECONDS", 0.0),
        )
        return cfg


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s [%(threadName)s] %(name)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_dir = Path(cfg.log_dir)
    if not log_dir.is_absolute():
        log_dir = Path(__file__).resolve().parent / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / cfg.log_file

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    if cfg.log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    logger.info("Logging initialized | level=%s | file=%s", cfg.log_level.upper(), log_path)
    logger.info(
        "Runtime config | candle_interval=%s | qty=%s | live_enabled=%s | product=%s | entry=%s | exit=%s | sl=%s",
        cfg.candle_interval,
        cfg.quantity,
        cfg.live_enabled_at_start,
        cfg.order_product_type,
        cfg.entry_order_type,
        cfg.exit_order_type,
        cfg.sl_order_type,
    )


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------


@dataclass
class OptionContract:
    side: str
    strike: float
    expiry: str
    security_id: int
    entry_premium: float
    selection_reason: str


@dataclass
class OptionTradePlan:
    side: str
    strike: float
    expiry: str
    option_security_id: int
    option_entry: float
    option_stop_loss: float
    option_target1: float
    option_target2: float
    option_risk: float
    option_rr1: float
    option_rr2: float
    underlying_entry: float
    underlying_stop_loss: float
    underlying_target1: float
    underlying_target2: float
    selection_reason: str


@dataclass
class BrahmastraSignal:
    candle_time: dt.datetime
    direction: str
    side: str
    spot: float
    vwap: float
    supertrend: float
    macd: float
    macd_signal: float
    macd_hist: float
    volume: float
    avg_volume: float
    body_ratio: float
    trigger_key: str
    reasons: List[str]
    option_plan: Optional[OptionTradePlan] = None


@dataclass
class BacktestTrade:
    trade_date: str
    signal_time: str
    entry_time: str
    exit_time: str
    side: str
    strike: float
    expiry: str
    security_id: int
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
class LivePosition:
    plan: OptionTradePlan
    signal_time: dt.datetime
    opened_at: dt.datetime
    trigger_key: str
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
    last_checked_option_ts: Optional[pd.Timestamp] = None


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


# -----------------------------------------------------------------------------
# API clients
# -----------------------------------------------------------------------------


class DhanApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "access-token": cfg.dhan_access_token,
                "client-id": cfg.dhan_client_id,
            }
        )
        LOG.info("Dhan API client initialized | base=%s | timeout=%ss", DHAN_BASE, cfg.http_timeout)

    @staticmethod
    def _response_body(r: requests.Response) -> str:
        try:
            return json.dumps(r.json(), ensure_ascii=True)
        except Exception:
            return (r.text or "").strip()

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = f"{DHAN_BASE}{endpoint}"
        started = time.perf_counter()
        if self.cfg.log_http_payloads:
            LOG.debug(
                "Dhan request | method=%s | endpoint=%s | payload=%s | params=%s",
                method.upper(),
                endpoint,
                log_json(payload),
                log_json(params),
            )
        else:
            LOG.debug("Dhan request | method=%s | endpoint=%s", method.upper(), endpoint)
        try:
            r = self.session.request(
                method=method.upper(),
                url=url,
                data=json.dumps(payload) if payload is not None else None,
                params=params,
                timeout=self.cfg.http_timeout,
            )
        except requests.RequestException:
            LOG.exception("Dhan request failed before response | method=%s | endpoint=%s", method.upper(), endpoint)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        LOG.info("Dhan response | method=%s | endpoint=%s | status=%s | elapsed_ms=%.0f", method.upper(), endpoint, r.status_code, elapsed_ms)
        self._raise_for_status(r, endpoint)
        return r

    def _raise_for_status(self, r: requests.Response, endpoint: str) -> None:
        if r.status_code < 400:
            return

        body = self._response_body(r)
        hint = ""
        if r.status_code == 401:
            hint = (
                "\nHint: Dhan rejected authentication. Check DHAN_CLIENT_ID and "
                "DHAN_ACCESS_TOKEN in .env, regenerate the access token if it is "
                "expired, and confirm your Dhan account has API access for charts/orders data."
            )
        if r.status_code == 403:
            hint = (
                "\nHint: Dhan order APIs may require static IP whitelisting for order "
                "placement/modification/cancellation. Confirm this in your Dhan API settings."
            )

        LOG.error("Dhan HTTP error | endpoint=%s | status=%s | body=%s", endpoint, r.status_code, body)
        raise requests.HTTPError(
            f"{r.status_code} Dhan error on {endpoint}. Response: {body or '-'}{hint}",
            response=r,
        )

    def expiry_list(self) -> List[str]:
        payload = {
            "UnderlyingScrip": NIFTY50_SECURITY_ID,
            "UnderlyingSeg": NIFTY50_SEGMENT,
        }
        r = self._request("POST", "/optionchain/expirylist", payload=payload)
        expiries = [str(x) for x in r.json().get("data", [])]
        LOG.info("Expiry list loaded | count=%s | first=%s", len(expiries), expiries[0] if expiries else "-")
        return expiries

    def pick_expiry_for_date(self, trade_date: dt.date) -> str:
        LOG.info("Picking expiry from Dhan live expiry list | trade_date=%s", trade_date)
        expiries = self.expiry_list()
        if not expiries:
            LOG.error("No expiry dates returned by Dhan")
            raise RuntimeError("No expiry dates returned by Dhan.")

        parsed = []
        for expiry in expiries:
            expiry_date = parse_date_flexible(expiry)
            if expiry_date is not None:
                parsed.append((expiry_date, expiry))

        future = sorted((d, x) for d, x in parsed if d >= trade_date)
        if future:
            expiry_date, expiry = future[0]
            gap_days = (expiry_date - trade_date).days
            if gap_days <= self.cfg.max_backtest_expiry_gap_days:
                LOG.info("Picked Dhan expiry | trade_date=%s | expiry=%s | gap_days=%s", trade_date, expiry, gap_days)
                return expiry
            LOG.error(
                "Nearest Dhan expiry outside configured gap | trade_date=%s | expiry=%s | gap_days=%s | max_gap=%s",
                trade_date,
                expiry,
                gap_days,
                self.cfg.max_backtest_expiry_gap_days,
            )
            raise RuntimeError(
                f"Nearest Dhan expiry for {trade_date} is {expiry} ({gap_days} days later), "
                f"which is outside MAX_BACKTEST_EXPIRY_GAP_DAYS={self.cfg.max_backtest_expiry_gap_days}. "
                "For older backtests, set DHAN_SCRIP_MASTER_CSV to a historical contract master "
                "that contains the actual NIFTY option expiries for that period."
            )

        LOG.error("No Dhan expiry >= trade date | trade_date=%s", trade_date)
        raise RuntimeError(
            f"No Dhan expiry >= {trade_date}. For historical expired-option backtests, "
            "set DHAN_SCRIP_MASTER_CSV to a contract master that contains expired contracts."
        )

    def option_chain(self, expiry: str) -> Dict[str, Any]:
        payload = {
            "UnderlyingScrip": NIFTY50_SECURITY_ID,
            "UnderlyingSeg": NIFTY50_SEGMENT,
            "Expiry": expiry,
        }
        LOG.info("Fetching option chain | expiry=%s", expiry)
        r = self._request("POST", "/optionchain", payload=payload)
        data = r.json()
        oc = get_oc(data) if isinstance(data, dict) else {}
        LOG.info("Option chain loaded | expiry=%s | strikes=%s", expiry, len(oc))
        return data

    def intraday_candles(
        self,
        security_id: int,
        exchange_segment: str,
        instrument: str,
        interval: int,
        from_date: str,
        to_date: str,
        oi: bool = True,
    ) -> pd.DataFrame:
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval),
            "oi": bool(oi),
            "fromDate": from_date,
            "toDate": to_date,
        }
        LOG.info(
            "Fetching intraday candles | security_id=%s | segment=%s | instrument=%s | interval=%s | from=%s | to=%s",
            security_id,
            exchange_segment,
            instrument,
            interval,
            from_date,
            to_date,
        )
        r = self._request("POST", "/charts/intraday", payload=payload)
        raw = r.json()
        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]
        if isinstance(raw, list):
            df = self._rows_to_df(raw)
        elif isinstance(raw, dict):
            df = self._dict_to_df(raw)
        else:
            LOG.error("Unexpected intraday response shape | type=%s", type(raw))
            raise ValueError(f"Unexpected intraday response shape: {type(raw)}")
        LOG.info(
            "Intraday candles normalized | security_id=%s | rows=%s | first=%s | last=%s",
            security_id,
            len(df),
            df["timestamp"].iloc[0] if not df.empty else "-",
            df["timestamp"].iloc[-1] if not df.empty else "-",
        )
        return df

    def index_intraday(self, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        LOG.info("Loading NIFTY index intraday candles | start=%s | end=%s", start, end)
        return self.intraday_candles(
            security_id=NIFTY50_SECURITY_ID,
            exchange_segment=NIFTY50_SEGMENT,
            instrument=NIFTY50_INSTRUMENT,
            interval=self.cfg.candle_interval,
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            oi=True,
        )

    def option_intraday(self, security_id: int, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        LOG.info("Loading option intraday candles | security_id=%s | start=%s | end=%s", security_id, start, end)
        return self.intraday_candles(
            security_id=security_id,
            exchange_segment=FNO_SEGMENT,
            instrument=OPTION_INSTRUMENT,
            interval=self.cfg.candle_interval,
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
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
            LOG.error("Rolling option offset outside Dhan limit | side=%s | offset=%s", side, strike_offset)
            raise ValueError("Dhan expired-options API supports index option offsets only up to ATM+10/ATM-10.")

        strike = "ATM" if strike_offset == 0 else f"ATM{strike_offset:+d}"
        option_type = "CALL" if side.upper() == "CE" else "PUT"
        payload = {
            "exchangeSegment": FNO_SEGMENT,
            "interval": str(self.cfg.candle_interval),
            "securityId": NIFTY50_SECURITY_ID,
            "instrument": OPTION_INSTRUMENT,
            "expiryFlag": self.cfg.expired_options_expiry_flag,
            "expiryCode": self.cfg.expired_options_expiry_code,
            "strike": strike,
            "drvOptionType": option_type,
            "requiredData": ["open", "high", "low", "close", "volume", "oi", "strike", "spot"],
            "fromDate": start.strftime("%Y-%m-%d"),
            "toDate": (end.date() + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        LOG.info("Fetching rolling option candles | side=%s | strike=%s | start=%s | end=%s", side, strike, start, end)
        r = self._request("POST", "/charts/rollingoption", payload=payload)

        raw = r.json()
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        side_key = "ce" if side.upper() == "CE" else "pe"
        series = data.get(side_key)
        if not isinstance(series, dict):
            LOG.warning("Rolling option response had no series | side=%s | strike=%s", side, strike)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "open_interest"])

        df = self._dict_to_df(series)
        if "oi" in series and len(series["oi"]) == len(df):
            df["open_interest"] = pd.to_numeric(series["oi"], errors="coerce")
        if "strike" in series and len(series["strike"]) == len(df):
            df["strike"] = pd.to_numeric(series["strike"], errors="coerce")
        if "spot" in series and len(series["spot"]) == len(df):
            df["spot"] = pd.to_numeric(series["spot"], errors="coerce")
        out = df[(df["timestamp"] >= pd.Timestamp(start)) & (df["timestamp"] <= pd.Timestamp(end))].reset_index(drop=True)
        LOG.info("Rolling option candles loaded | side=%s | strike=%s | rows=%s", side, strike, len(out))
        return out

    def place_order(
        self,
        *,
        transaction_type: str,
        security_id: int,
        quantity: int,
        order_type: str,
        product_type: Optional[str] = None,
        validity: Optional[str] = None,
        price: float = 0.0,
        trigger_price: float = 0.0,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "dhanClientId": self.cfg.dhan_client_id,
            "correlationId": correlation_id or make_correlation_id("ORD"),
            "transactionType": transaction_type.upper(),
            "exchangeSegment": FNO_SEGMENT,
            "productType": (product_type or self.cfg.order_product_type).upper(),
            "orderType": order_type.upper(),
            "validity": (validity or self.cfg.order_validity).upper(),
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
        LOG.warning(
            "Placing LIVE Dhan order | txn=%s | security_id=%s | qty=%s | type=%s | product=%s | trigger=%s | correlation=%s",
            payload["transactionType"],
            security_id,
            quantity,
            payload["orderType"],
            payload["productType"],
            trigger_price,
            payload["correlationId"],
        )
        r = self._request("POST", "/orders", payload=payload)
        data = r.json()
        if isinstance(data, dict):
            data["_request"] = payload
        LOG.warning("Dhan order response | order_id=%s | status=%s | response=%s", order_id(data), order_status(data), log_json(data))
        return data

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: int,
        order_type: str,
        price: float = 0.0,
        trigger_price: float = 0.0,
        validity: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "dhanClientId": self.cfg.dhan_client_id,
            "orderId": str(order_id),
            "orderType": order_type.upper(),
            "legName": "",
            "quantity": int(quantity),
            "price": float(price or 0.0),
            "disclosedQuantity": 0,
            "triggerPrice": float(trigger_price or 0.0),
            "validity": (validity or self.cfg.order_validity).upper(),
        }
        LOG.warning(
            "Modifying LIVE Dhan order | order_id=%s | qty=%s | type=%s | trigger=%s",
            order_id,
            quantity,
            order_type,
            trigger_price,
        )
        r = self._request("PUT", f"/orders/{order_id}", payload=payload)
        data = r.json()
        if isinstance(data, dict):
            data["_request"] = payload
        LOG.warning("Dhan modify response | order_id=%s | status=%s | response=%s", order_id, order_status(data), log_json(data))
        return data

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        LOG.warning("Cancelling LIVE Dhan order | order_id=%s", order_id)
        r = self._request("DELETE", f"/orders/{order_id}")
        if not (r.text or "").strip():
            data = {"orderId": str(order_id), "orderStatus": "CANCELLED"}
        else:
            data = r.json()
        LOG.warning("Dhan cancel response | order_id=%s | status=%s | response=%s", order_id, order_status(data), log_json(data))
        return data

    def get_order(self, order_id: str) -> Dict[str, Any]:
        LOG.debug("Fetching Dhan order status | order_id=%s", order_id)
        r = self._request("GET", f"/orders/{order_id}")
        data = r.json()
        LOG.info("Dhan order status | order_id=%s | status=%s | filled=%s | remaining=%s", order_id, order_status(data), order_filled_qty(data), order_remaining_qty(data))
        return data

    def trades_for_order(self, order_id: str) -> List[Dict[str, Any]]:
        LOG.info("Fetching Dhan trades for order | order_id=%s", order_id)
        r = self._request("GET", f"/trades/{order_id}")
        raw = r.json()
        if isinstance(raw, list):
            trades = raw
        elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
            trades = raw["data"]
        elif isinstance(raw, dict):
            trades = [raw]
        else:
            trades = []
        LOG.info("Dhan trades loaded | order_id=%s | count=%s", order_id, len(trades))
        return trades

    @staticmethod
    def _rows_to_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        return DhanApiClient._normalize_df(df)

    @staticmethod
    def _dict_to_df(data: Dict[str, Any]) -> pd.DataFrame:
        keys = {k.lower(): k for k in data.keys()}
        required = ["open", "high", "low", "close", "volume", "timestamp"]
        missing = [k for k in required if k not in keys]
        if missing:
            LOG.error("Intraday response missing required keys | missing=%s | got=%s", missing, list(data.keys()))
            raise ValueError(f"Intraday response missing keys: {missing}; got {list(data.keys())}")

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
            LOG.error("Intraday data has no timestamp column | columns=%s", list(df.columns))
            raise ValueError("Intraday data has no timestamp column.")

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

        before = len(df)
        df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        out = df.reset_index(drop=True)
        dropped = before - len(out)
        if dropped:
            LOG.debug("Normalized candle DataFrame | before=%s | after=%s | dropped=%s", before, len(out), dropped)
        return out


class TelegramBot:
    MAX_HTML_LEN = 3900
    MAX_TEXT_LEN = 3900

    def __init__(self, token: str, chat_id: str, timeout: int = 20):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.session = requests.Session()
        self._offset = 0
        LOG.info("Telegram bot initialized | chat_id=<redacted> | timeout=%ss", timeout)

    def _redact(self, text: Any) -> str:
        cleaned = str(text)
        if self.token:
            cleaned = cleaned.replace(self.token, "<telegram-token-redacted>")
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
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode

        LOG.info("Telegram sendMessage | parse_mode=%s | length=%s", parse_mode or "plain", len(text))
        try:
            r = self.session.post(
                f"{TELEGRAM_BASE}/bot{self.token}/sendMessage",
                data=data,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            LOG.exception("Telegram sendMessage request failed")
            raise RuntimeError(f"Telegram sendMessage request failed: {self._redact(exc)}") from exc

        if r.status_code >= 400:
            try:
                body = json.dumps(r.json(), ensure_ascii=True)
            except Exception:
                body = r.text or ""
            LOG.error("Telegram sendMessage failed | status=%s | response=%s", r.status_code, self._redact(body))
            raise RuntimeError(
                f"Telegram sendMessage failed with HTTP {r.status_code}. "
                f"Response: {self._redact(body)}"
            )
        LOG.info("Telegram sendMessage ok | status=%s", r.status_code)

    def send(self, text: str) -> None:
        text = self._redact(text)
        LOG.debug("Telegram send called | html_length=%s", len(text))
        if len(text) > self.MAX_HTML_LEN:
            plain = self._plain_text(text)
            chunks = self._split_text(plain, self.MAX_TEXT_LEN)
            LOG.info("Telegram message too long; sending as plain chunks | chunks=%s", len(chunks))
            for chunk in chunks:
                self._post_message(chunk, parse_mode=None)
            return

        try:
            self._post_message(text, parse_mode="HTML")
        except RuntimeError as exc:
            if "HTTP 400" not in str(exc):
                raise
            LOG.warning("Telegram HTML send failed with HTTP 400; retrying as plain text")
            plain = self._plain_text(text)
            for chunk in self._split_text(plain, self.MAX_TEXT_LEN):
                self._post_message(chunk, parse_mode=None)

    def get_messages(self) -> List[str]:
        try:
            r = self.session.get(
                f"{TELEGRAM_BASE}/bot{self.token}/getUpdates",
                params={"offset": self._offset, "timeout": 0},
                timeout=self.timeout + 5,
            )
            r.raise_for_status()
        except Exception:
            LOG.exception("Telegram getUpdates failed")
            return []

        texts: List[str] = []
        for update in r.json().get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message") or update.get("channel_post") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if chat_id == self.chat_id and text:
                texts.append(text)
        if texts:
            LOG.info("Telegram messages received | count=%s", len(texts))
        else:
            LOG.debug("Telegram poll completed | no messages | offset=%s", self._offset)
        return texts


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    return now_ist().date()


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


def num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        out = float(v)
        if math.isnan(out):
            return default
        return out
    except Exception:
        return default


def fmt(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{decimals}f}"
    except Exception:
        return "-"


def tg_escape(v: Any) -> str:
    return html.escape(str(v), quote=False)


def make_correlation_id(kind: str) -> str:
    stamp = now_ist().strftime("%y%m%d%H%M%S")
    tail = int(time.time() * 1000) % 100000
    raw = f"BRM{stamp}{kind.upper()}{tail}"
    correlation = re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:30]
    LOG.debug("Generated correlation id | kind=%s | correlation=%s", kind, correlation)
    return correlation


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
    return num(raw.get("averageTradedPrice") or raw.get("avgPrice") or raw.get("price"), 0.0)


def market_session_open(now: Optional[dt.datetime] = None) -> bool:
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt.time(9, 15) <= t <= dt.time(15, 30)


def day_start_end(day: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    return (
        dt.datetime.combine(day, dt.time(9, 15)),
        dt.datetime.combine(day, dt.time(15, 30)),
    )


def closed_candles_only(df: pd.DataFrame, interval_minutes: int, at_time: Optional[dt.datetime] = None) -> pd.DataFrame:
    if df.empty:
        LOG.debug("closed_candles_only called with empty DataFrame")
        return df
    at_time = (at_time or now_ist()).replace(tzinfo=None)
    latest_allowed_end = at_time - dt.timedelta(seconds=5)
    candle_end = df["timestamp"] + pd.to_timedelta(interval_minutes, unit="m")
    out = df[candle_end <= latest_allowed_end].reset_index(drop=True)
    LOG.debug("Closed candles filtered | before=%s | after=%s | at_time=%s", len(df), len(out), at_time)
    return out


def nearest_strike(strikes: List[float], spot: float) -> float:
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else round(spot / 50.0) * 50


def infer_step(strikes: List[float], fallback: int = 50) -> int:
    if len(strikes) < 2:
        return fallback
    diffs = sorted(abs(b - a) for a, b in zip(strikes[:-1], strikes[1:]) if abs(b - a) > 0)
    if not diffs:
        return fallback
    return int(round(statistics.median(diffs))) or fallback


def get_oc(chain_json: Dict[str, Any]) -> Dict[str, Any]:
    data = chain_json.get("data", chain_json)
    return data.get("oc") or {}


def get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
    return (
        oc.get(f"{strike:.6f}")
        or oc.get(f"{strike:.2f}")
        or oc.get(f"{strike:.0f}")
        or oc.get(str(int(round(strike))))
        or {}
    )


def support_resistance_oi(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    support = None
    resistance = None
    best_pe = -1.0
    best_ce = -1.0
    for k, row in oc.items():
        try:
            strike = float(k)
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
    LOG.debug("Support/resistance from OI | support=%s | resistance=%s | strikes=%s", support, resistance, len(oc))
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
    pcr = (put_oi / call_oi) if call_oi > 0 else None
    LOG.debug("PCR near ATM | center=%s | window=%s | call_oi=%s | put_oi=%s | pcr=%s", center, window, call_oi, put_oi, pcr)
    return pcr


def top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
    items: List[Tuple[float, float]] = []
    for k, v in oc.items():
        try:
            items.append((float(k), num(v.get(side, {}).get("oi"))))
        except Exception:
            continue
    top = sorted(items, key=lambda x: x[1], reverse=True)[:n]
    LOG.debug("Top OI | side=%s | top=%s", side, top)
    return top


# -----------------------------------------------------------------------------
# Contract resolver
# -----------------------------------------------------------------------------


class OptionContractResolver:
    """Resolve option security ids from live chain or an optional Dhan master CSV."""

    def __init__(self, cfg: Config, api: DhanApiClient):
        self.cfg = cfg
        self.api = api
        self.master: Optional[pd.DataFrame] = None
        LOG.info("OptionContractResolver initializing")
        self._load_master_if_available()

    def _master_needs_download(self, path: Path) -> bool:
        if not path.exists():
            LOG.info("Dhan master download needed | reason=missing | path=%s", path)
            return True
        refresh_days = self.cfg.dhan_master_refresh_days
        if refresh_days <= 0:
            return False
        age_seconds = time.time() - path.stat().st_mtime
        needs = age_seconds >= refresh_days * 24 * 60 * 60
        LOG.info("Dhan master age check | path=%s | age_hours=%.2f | refresh_days=%s | needs_download=%s", path, age_seconds / 3600.0, refresh_days, needs)
        return needs

    def _load_master_if_available(self) -> None:
        path = self.cfg.dhan_scrip_master_csv
        if not path and self.cfg.download_dhan_master:
            path = str(Path(__file__).with_name("dhan_api_scrip_master.csv"))
            csv_path = Path(path)
            if self._master_needs_download(csv_path):
                LOG.info("Downloading Dhan master CSV | url=%s | path=%s", self.cfg.dhan_scrip_master_url, csv_path)
                r = requests.get(self.cfg.dhan_scrip_master_url, timeout=self.cfg.http_timeout)
                r.raise_for_status()
                csv_path.write_bytes(r.content)
                LOG.info("Dhan master CSV downloaded | bytes=%s", len(r.content))

        if not path:
            LOG.info("No Dhan master CSV configured; resolver will use chain/API fallback where possible")
            return

        csv_path = Path(path)
        if not csv_path.exists():
            LOG.error("DHAN_SCRIP_MASTER_CSV does not exist | path=%s", csv_path)
            raise RuntimeError(f"DHAN_SCRIP_MASTER_CSV does not exist: {csv_path}")

        LOG.info("Loading Dhan master CSV | path=%s", csv_path)
        df = pd.read_csv(csv_path, low_memory=False)
        df.columns = [self._clean_col(c) for c in df.columns]
        self.master = df
        LOG.info("Dhan master loaded | rows=%s | columns=%s", len(df), len(df.columns))

    @staticmethod
    def _clean_col(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(name).lower())

    def _col(self, candidates: Iterable[str]) -> Optional[str]:
        if self.master is None:
            return None
        cols = set(self.master.columns)
        for candidate in candidates:
            clean = self._clean_col(candidate)
            if clean in cols:
                return clean
        return None

    def pick_expiry_for_date(self, trade_date: dt.date) -> str:
        LOG.info("Resolver pick_expiry_for_date | trade_date=%s | has_master=%s", trade_date, self.master is not None)
        if self.master is not None:
            expiry_col = self._col(["expirydate", "semexpirydate", "smexpirydate", "expiry", "expiry_date"])
            symbol_col = self._col(["underlyingsymbol", "semunderlyingsymbol", "underlying_symbol", "symbol", "symbolname", "smsymbolname", "semtrading_symbol", "semcustomsymbol", "customsymbol", "displayname"])
            if expiry_col:
                df = self.master
                if symbol_col:
                    df = df[df[symbol_col].astype(str).str.upper().str.contains("NIFTY", na=False)]
                parsed = pd.to_datetime(df[expiry_col], errors="coerce").dt.date
                expiries = sorted({d for d in parsed.dropna().tolist() if d >= trade_date})
                if expiries:
                    expiry_date = expiries[0]
                    gap_days = (expiry_date - trade_date).days
                    if gap_days <= self.cfg.max_backtest_expiry_gap_days:
                        picked = expiry_date.strftime("%Y-%m-%d")
                        LOG.info("Picked expiry from master | trade_date=%s | expiry=%s | gap_days=%s", trade_date, picked, gap_days)
                        return picked
                    LOG.error(
                        "Nearest master expiry outside configured gap | trade_date=%s | expiry=%s | gap_days=%s | max_gap=%s",
                        trade_date,
                        expiry_date,
                        gap_days,
                        self.cfg.max_backtest_expiry_gap_days,
                    )
                    raise RuntimeError(
                        f"Nearest Dhan master expiry for {trade_date} is {expiry_date} ({gap_days} days later), "
                        f"which is outside MAX_BACKTEST_EXPIRY_GAP_DAYS={self.cfg.max_backtest_expiry_gap_days}. "
                        "Your current master appears not to contain the historical NIFTY option contracts "
                        "for this backtest period. Provide DHAN_SCRIP_MASTER_CSV with those expired contracts."
                    )

        LOG.info("Falling back to Dhan expiry API | trade_date=%s", trade_date)
        return self.api.pick_expiry_for_date(trade_date)

    def resolve_from_chain(self, oc: Dict[str, Any], strike: float, side: str) -> Optional[int]:
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        security_id = option.get("security_id") or option.get("securityId")
        if security_id is None:
            LOG.debug("Security id not found in option chain | strike=%s | side=%s", strike, side)
            return None
        resolved = int(float(security_id))
        LOG.info("Resolved contract from chain | strike=%s | side=%s | security_id=%s", strike, side, resolved)
        return resolved

    def resolve(self, expiry: str, strike: float, side: str, oc: Optional[Dict[str, Any]] = None) -> int:
        LOG.info("Resolving option security id | expiry=%s | strike=%s | side=%s | chain_given=%s", expiry, strike, side, bool(oc))
        if oc:
            from_chain = self.resolve_from_chain(oc, strike, side)
            if from_chain is not None:
                return from_chain

        from_master = self.resolve_from_master(expiry, strike, side)
        if from_master is not None:
            return from_master

        LOG.error("Option security id resolution failed | expiry=%s | strike=%s | side=%s", expiry, strike, side)
        raise RuntimeError(
            f"Could not resolve NIFTY {expiry} {int(strike)} {side} security id. "
            "For exact historical option backtests, provide DHAN_SCRIP_MASTER_CSV with the contract."
        )

    def resolve_from_master(self, expiry: str, strike: float, side: str) -> Optional[int]:
        if self.master is None:
            return None

        id_col = self._col(["securityid", "security_id", "semsmstsecurityid", "instrumenttoken"])
        expiry_col = self._col(["expirydate", "semexpirydate", "smexpirydate", "expiry", "expiry_date"])
        strike_col = self._col(["strikeprice", "semstrikeprice", "strike", "strike_price"])
        option_col = self._col(["optiontype", "semoptiontype", "option_type"])
        symbol_col = self._col(["underlyingsymbol", "semunderlyingsymbol", "underlying_symbol", "symbol", "symbolname", "smsymbolname", "semtrading_symbol", "semcustomsymbol", "customsymbol", "displayname"])
        custom_col = self._col(["customsymbol", "semcustomsymbol", "displayname", "tradingsymbol", "trading_symbol", "symbolname", "smsymbolname"])
        instrument_col = self._col(["instrument", "instrumentname", "seminstrumentname"])

        if not id_col or not expiry_col or not strike_col:
            LOG.warning("Master CSV missing required columns | id_col=%s | expiry_col=%s | strike_col=%s", id_col, expiry_col, strike_col)
            return None

        expiry_date = parse_date_flexible(expiry)
        if expiry_date is None:
            LOG.warning("Could not parse expiry for master resolution | expiry=%s", expiry)
            return None

        df = self.master
        mask = pd.Series(True, index=df.index)

        if symbol_col:
            mask &= df[symbol_col].astype(str).str.upper().str.contains("NIFTY", na=False)
            mask &= ~df[symbol_col].astype(str).str.upper().str.contains("BANKNIFTY|FINNIFTY|MIDCPNIFTY", na=False)
        elif custom_col:
            mask &= df[custom_col].astype(str).str.upper().str.contains("NIFTY", na=False)
            mask &= ~df[custom_col].astype(str).str.upper().str.contains("BANKNIFTY|FINNIFTY|MIDCPNIFTY", na=False)

        if instrument_col:
            mask &= df[instrument_col].astype(str).str.upper().str.contains("OPT", na=False)

        expiry_values = pd.to_datetime(df[expiry_col], errors="coerce").dt.date
        mask &= expiry_values == expiry_date

        strikes = pd.to_numeric(df[strike_col], errors="coerce")
        mask &= (strikes - float(strike)).abs() < 0.01

        if option_col:
            mask &= df[option_col].astype(str).str.upper().str.contains(side.upper(), na=False)
        elif custom_col:
            mask &= df[custom_col].astype(str).str.upper().str.contains(side.upper(), na=False)

        out = df[mask]
        if out.empty:
            LOG.debug("No master match for option | expiry=%s | strike=%s | side=%s", expiry, strike, side)
            return None

        security_id = pd.to_numeric(out.iloc[0][id_col], errors="coerce")
        if pd.isna(security_id):
            LOG.warning("Master match had invalid security id | expiry=%s | strike=%s | side=%s", expiry, strike, side)
            return None
        resolved = int(security_id)
        LOG.info("Resolved contract from master | expiry=%s | strike=%s | side=%s | security_id=%s | matches=%s", expiry, strike, side, resolved, len(out))
        return resolved


# -----------------------------------------------------------------------------
# Indicators
# -----------------------------------------------------------------------------


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    LOG.info("Adding indicators | rows=%s | supertrend=%s,%s | macd=%s,%s,%s", len(df), cfg.supertrend_period, cfg.supertrend_multiplier, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    close = out["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
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

    typical = (high + low + close) / 3.0
    out["trade_date"] = out["timestamp"].dt.date
    pv = typical * out["volume"].astype(float)
    out["vwap"] = pv.groupby(out["trade_date"]).cumsum() / out["volume"].astype(float).groupby(out["trade_date"]).cumsum()

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

    LOG.info(
        "Indicators added | rows=%s | latest_ts=%s | latest_close=%s | latest_st_dir=%s | latest_macd_hist=%s",
        len(out),
        out["timestamp"].iloc[-1] if not out.empty else "-",
        fmt(out["close"].iloc[-1]) if not out.empty else "-",
        int(out["supertrend_dir"].iloc[-1]) if not out.empty else "-",
        fmt(out["macd_hist"].iloc[-1]) if not out.empty else "-",
    )
    return out


def recent_event(df: pd.DataFrame, idx: int, column: str, lookback: int) -> Tuple[bool, Optional[dt.datetime]]:
    start = max(0, idx - lookback + 1)
    for j in range(idx, start - 1, -1):
        if bool(df.iloc[j].get(column, False)):
            return True, df.iloc[j].timestamp
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
        ok = float(last.close) > float(prev["high"].max()) or (
            float(df.iloc[idx].low) > float(prev["low"].min()) and float(last.close) > float(df.iloc[idx - 1].close)
        )
    else:
        ok = float(last.close) < float(prev["low"].min()) or (
            float(df.iloc[idx].high) < float(prev["high"].max()) and float(last.close) < float(df.iloc[idx - 1].close)
        )
    LOG.debug("Market structure check | idx=%s | side=%s | ok=%s", idx, side, ok)
    return ok


# -----------------------------------------------------------------------------
# Strategy engine
# -----------------------------------------------------------------------------


def build_brahmastra_signal(df: pd.DataFrame, idx: int, cfg: Config) -> Optional[BrahmastraSignal]:
    if idx < max(cfg.supertrend_period + 2, cfg.macd_slow + cfg.macd_signal):
        LOG.debug("Signal check skipped: warmup | idx=%s | rows=%s", idx, len(df))
        return None

    row = df.iloc[idx]
    if pd.isna(row.supertrend) or pd.isna(row.macd) or pd.isna(row.macd_signal) or pd.isna(row.vwap):
        LOG.debug("Signal check skipped: indicator NaN | idx=%s | time=%s", idx, row.timestamp)
        return None

    market_open = dt.datetime.combine(row.timestamp.date(), dt.time(9, 15))
    if row.timestamp < market_open + dt.timedelta(minutes=cfg.avoid_first_minutes):
        LOG.debug("Signal check skipped: avoid first minutes | idx=%s | time=%s", idx, row.timestamp)
        return None

    candle_range = max(float(row.high) - float(row.low), 1e-9)
    body_ratio = abs(float(row.close) - float(row.open)) / candle_range
    strong_body = body_ratio >= cfg.min_body_ratio
    avg_volume = float(row.avg_volume) if not pd.isna(row.avg_volume) else float(row.volume)
    volume_ok = (float(row.volume) >= avg_volume * cfg.volume_multiplier) if cfg.require_volume else True

    st_up, st_up_time = recent_event(df, idx, "st_flip_up", cfg.signal_lookback)
    st_down, st_down_time = recent_event(df, idx, "st_flip_down", cfg.signal_lookback)
    macd_up, macd_up_time = recent_event(df, idx, "macd_cross_up", cfg.signal_lookback)
    macd_down, macd_down_time = recent_event(df, idx, "macd_cross_down", cfg.signal_lookback)

    base_reasons = [
        f"Supertrend 20,2: {fmt(row.supertrend)}",
        f"MACD: {float(row.macd):.2f}, Signal: {float(row.macd_signal):.2f}, Hist: {float(row.macd_hist):.2f}",
        f"VWAP: {fmt(row.vwap)}",
        f"Body ratio: {body_ratio * 100:.0f}%",
    ]

    bullish_conditions = [
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
    bearish_conditions = [
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

    if cfg.require_market_structure:
        bullish_conditions.append(market_structure_ok(df, idx, "CE"))
        bearish_conditions.append(market_structure_ok(df, idx, "PE"))

    LOG.debug(
        "Signal check | idx=%s | time=%s | close=%s | bull=%s | bear=%s | body=%.3f | volume_ok=%s | st_dir=%s | macd_hist=%.2f",
        idx,
        row.timestamp,
        fmt(row.close),
        bullish_conditions,
        bearish_conditions,
        body_ratio,
        volume_ok,
        int(row.supertrend_dir),
        float(row.macd_hist),
    )

    if all(bullish_conditions):
        trigger_key = f"CE|ST:{st_up_time}|MACD:{macd_up_time}"
        reasons = [
            "Supertrend flipped red to green",
            "MACD bullish crossover confirmed",
            "Price closed above VWAP",
            "Confirmation candle closed bullish",
        ] + base_reasons
        if cfg.require_volume:
            reasons.append(f"Volume {fmt(row.volume, 0)} >= required {fmt(avg_volume * cfg.volume_multiplier, 0)}")
        LOG.warning("Brahmastra signal generated | direction=BULLISH | side=CE | time=%s | spot=%s | trigger=%s", row.timestamp, fmt(row.close), trigger_key)
        return BrahmastraSignal(
            candle_time=row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp,
            direction="BULLISH",
            side="CE",
            spot=float(row.close),
            vwap=float(row.vwap),
            supertrend=float(row.supertrend),
            macd=float(row.macd),
            macd_signal=float(row.macd_signal),
            macd_hist=float(row.macd_hist),
            volume=float(row.volume),
            avg_volume=avg_volume,
            body_ratio=body_ratio,
            trigger_key=trigger_key,
            reasons=reasons,
        )

    if all(bearish_conditions):
        trigger_key = f"PE|ST:{st_down_time}|MACD:{macd_down_time}"
        reasons = [
            "Supertrend flipped green to red",
            "MACD bearish crossover confirmed",
            "Price closed below VWAP",
            "Confirmation candle closed bearish",
        ] + base_reasons
        if cfg.require_volume:
            reasons.append(f"Volume {fmt(row.volume, 0)} >= required {fmt(avg_volume * cfg.volume_multiplier, 0)}")
        LOG.warning("Brahmastra signal generated | direction=BEARISH | side=PE | time=%s | spot=%s | trigger=%s", row.timestamp, fmt(row.close), trigger_key)
        return BrahmastraSignal(
            candle_time=row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp,
            direction="BEARISH",
            side="PE",
            spot=float(row.close),
            vwap=float(row.vwap),
            supertrend=float(row.supertrend),
            macd=float(row.macd),
            macd_signal=float(row.macd_signal),
            macd_hist=float(row.macd_hist),
            volume=float(row.volume),
            avg_volume=avg_volume,
            body_ratio=body_ratio,
            trigger_key=trigger_key,
            reasons=reasons,
        )

    return None


def opposite_exit_signal(df: pd.DataFrame, idx: int, side: str) -> bool:
    if idx <= 0:
        return False
    row = df.iloc[idx]
    if side == "CE":
        result = bool(row.st_flip_down) or bool(row.macd_cross_down) or (
            int(row.supertrend_dir) == -1 and float(row.close) < float(row.vwap)
        )
    else:
        result = bool(row.st_flip_up) or bool(row.macd_cross_up) or (
            int(row.supertrend_dir) == 1 and float(row.close) > float(row.vwap)
        )
    if result:
        LOG.warning("Opposite exit signal | idx=%s | side=%s | time=%s | close=%s", idx, side, row.timestamp, fmt(row.close))
    return result


def underlying_levels(df: pd.DataFrame, idx: int, side: str, cfg: Config) -> Tuple[float, float, float]:
    row = df.iloc[idx]
    entry = float(row.close)
    if side == "CE":
        structure = min(recent_swing_low(df, idx, cfg.swing_lookback), float(row.vwap), float(row.supertrend))
        sl = round(structure - cfg.nifty_sl_buffer, 2)
        risk = max(entry - sl, 0.01)
        levels = (sl, round(entry + risk, 2), round(entry + 2.0 * risk, 2))
    else:
        structure = max(recent_swing_high(df, idx, cfg.swing_lookback), float(row.vwap), float(row.supertrend))
        sl = round(structure + cfg.nifty_sl_buffer, 2)
        risk = max(sl - entry, 0.01)
        levels = (sl, round(entry - risk, 2), round(entry - 2.0 * risk, 2))
    LOG.info("Underlying levels | side=%s | entry=%s | sl=%s | t1=%s | t2=%s", side, fmt(entry), fmt(levels[0]), fmt(levels[1]), fmt(levels[2]))
    return levels


# -----------------------------------------------------------------------------
# Option selection and trade levels
# -----------------------------------------------------------------------------


def strike_candidates(spot: float, side: str, cfg: Config, strikes: Optional[List[float]] = None) -> List[float]:
    if strikes:
        atm = nearest_strike(sorted(strikes), spot)
        step = infer_step(sorted(strikes), cfg.strike_step)
    else:
        step = cfg.strike_step
        atm = round(spot / step) * step

    offsets = [0]
    for i in range(1, cfg.option_search_depth + 1):
        if side == "CE":
            offsets.extend([-i, i])
        else:
            offsets.extend([i, -i])

    candidates: List[float] = []
    for offset in offsets:
        strike = float(atm + offset * step)
        if strikes:
            strike = nearest_strike(sorted(strikes), strike)
        if strike not in candidates:
            candidates.append(strike)
    LOG.info("Strike candidates | spot=%s | side=%s | atm=%s | step=%s | candidates=%s", fmt(spot), side, atm, step, [int(x) for x in candidates])
    return candidates


def score_premium_choice(premium: float, strike: float, atm: float, cfg: Config) -> Tuple[int, float, float]:
    preferred_mid = (cfg.preferred_premium_min + cfg.preferred_premium_max) / 2.0
    if cfg.preferred_premium_min <= premium <= cfg.preferred_premium_max:
        band = 0
    elif cfg.min_premium <= premium <= cfg.max_premium:
        band = 1
    else:
        band = 2
    score = (band, abs(premium - preferred_mid), abs(strike - atm))
    LOG.debug("Premium score | premium=%s | strike=%s | atm=%s | score=%s", premium, strike, atm, score)
    return score


def select_option_from_chain(
    oc: Dict[str, Any],
    expiry: str,
    spot: float,
    side: str,
    cfg: Config,
    resolver: OptionContractResolver,
) -> OptionContract:
    LOG.info("Selecting live option from chain | expiry=%s | spot=%s | side=%s | strikes=%s", expiry, fmt(spot), side, len(oc))
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        LOG.error("Option chain is empty during selection")
        raise RuntimeError("Option chain is empty.")

    atm = nearest_strike(strikes, spot)
    choices: List[Tuple[Tuple[int, float, float], OptionContract]] = []

    for strike in strike_candidates(spot, side, cfg, strikes):
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        premium = num(option.get("last_price"))
        LOG.info("Live option candidate | side=%s | strike=%s | premium=%s", side, strike, premium)
        if premium <= 0:
            LOG.info("Skipping candidate: premium <= 0 | strike=%s | side=%s", strike, side)
            continue
        if not cfg.allow_premium_fallback and not (cfg.min_premium <= premium <= cfg.max_premium):
            LOG.info(
                "Skipping candidate: premium outside range | strike=%s | side=%s | premium=%s | allowed=%s-%s",
                strike,
                side,
                premium,
                cfg.min_premium,
                cfg.max_premium,
            )
            continue
        security_id = resolver.resolve_from_chain(oc, strike, side)
        if security_id is None:
            LOG.info("Skipping candidate: no security id | strike=%s | side=%s", strike, side)
            continue
        reason = "preferred premium" if cfg.preferred_premium_min <= premium <= cfg.preferred_premium_max else "nearest liquid premium"
        contract = OptionContract(
            side=side,
            strike=float(strike),
            expiry=expiry,
            security_id=security_id,
            entry_premium=float(premium),
            selection_reason=reason,
        )
        choices.append((score_premium_choice(premium, strike, atm, cfg), contract))

    if not choices and cfg.allow_premium_fallback:
        strike = atm
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        premium = num(option.get("last_price"), default=1.0)
        security_id = resolver.resolve(expiry, strike, side, oc)
        LOG.warning("Using fallback ATM option | side=%s | strike=%s | premium=%s | security_id=%s", side, strike, premium, security_id)
        return OptionContract(
            side=side,
            strike=float(strike),
            expiry=expiry,
            security_id=security_id,
            entry_premium=float(premium),
            selection_reason="fallback ATM because no premium-filter candidate was available",
        )
    if not choices:
        LOG.error("No live option found inside premium range | side=%s | min=%s | max=%s", side, cfg.min_premium, cfg.max_premium)
        raise RuntimeError(
            f"No {side} option found inside premium range Rs {cfg.min_premium:g}-{cfg.max_premium:g}. "
            "Signal skipped."
        )

    selected = sorted(choices, key=lambda x: x[0])[0][1]
    LOG.warning(
        "Selected live option | side=%s | strike=%s | expiry=%s | security_id=%s | premium=%s | reason=%s",
        selected.side,
        selected.strike,
        selected.expiry,
        selected.security_id,
        selected.entry_premium,
        selected.selection_reason,
    )
    return selected


def option_sl_from_history(option_df: pd.DataFrame, entry_pos: int, entry: float, cfg: Config) -> float:
    start = max(0, entry_pos - cfg.swing_lookback)
    prior = option_df.iloc[start:entry_pos]
    if len(prior) >= 2:
        sl = float(prior["low"].min()) - cfg.option_sl_buffer
        source = "history swing low"
    else:
        sl = entry * (1.0 - cfg.fallback_option_sl_pct)
        source = "fallback percentage"
    sl = round(max(sl, 0.05), 2)
    if sl >= entry:
        sl = round(max(entry * (1.0 - cfg.fallback_option_sl_pct), 0.05), 2)
        source = "fallback percentage because computed SL >= entry"
    LOG.info("Option SL calculated | entry=%s | entry_pos=%s | sl=%s | source=%s", entry, entry_pos, sl, source)
    return sl


def make_option_plan(
    signal: BrahmastraSignal,
    index_df: pd.DataFrame,
    signal_idx: int,
    contract: OptionContract,
    option_df: Optional[pd.DataFrame],
    cfg: Config,
) -> OptionTradePlan:
    entry = contract.entry_premium
    entry_pos = len(option_df) - 1 if option_df is not None and not option_df.empty else 0
    option_sl = option_sl_from_history(option_df, entry_pos, entry, cfg) if option_df is not None and not option_df.empty else round(entry * (1 - cfg.fallback_option_sl_pct), 2)
    risk = round(max(entry - option_sl, 0.05), 2)
    target1 = round(entry + risk, 2)
    target2 = round(entry + 2 * risk, 2)
    underlying_sl, underlying_t1, underlying_t2 = underlying_levels(index_df, signal_idx, signal.side, cfg)
    plan = OptionTradePlan(
        side=signal.side,
        strike=contract.strike,
        expiry=contract.expiry,
        option_security_id=contract.security_id,
        option_entry=entry,
        option_stop_loss=option_sl,
        option_target1=target1,
        option_target2=target2,
        option_risk=risk,
        option_rr1=round((target1 - entry) / risk, 2) if risk > 0 else 0.0,
        option_rr2=round((target2 - entry) / risk, 2) if risk > 0 else 0.0,
        underlying_entry=signal.spot,
        underlying_stop_loss=underlying_sl,
        underlying_target1=underlying_t1,
        underlying_target2=underlying_t2,
        selection_reason=contract.selection_reason,
    )
    LOG.warning(
        "Option trade plan created | side=%s | strike=%s | expiry=%s | security_id=%s | entry=%s | sl=%s | t1=%s | t2=%s | risk=%s",
        plan.side,
        plan.strike,
        plan.expiry,
        plan.option_security_id,
        plan.option_entry,
        plan.option_stop_loss,
        plan.option_target1,
        plan.option_target2,
        plan.option_risk,
    )
    return plan


def find_candle_at_or_after(df: pd.DataFrame, timestamp: dt.datetime) -> Optional[int]:
    if df.empty:
        return None
    ts = pd.Timestamp(timestamp)
    matches = df.index[df["timestamp"] >= ts].tolist()
    found = int(matches[0]) if matches else None
    LOG.debug("find_candle_at_or_after | timestamp=%s | found=%s", timestamp, found)
    return found


def find_exact_candle(df: pd.DataFrame, timestamp: dt.datetime) -> Optional[int]:
    if df.empty:
        return None
    ts = pd.Timestamp(timestamp)
    matches = df.index[df["timestamp"] == ts].tolist()
    found = int(matches[0]) if matches else None
    LOG.debug("find_exact_candle | timestamp=%s | found=%s", timestamp, found)
    return found


# -----------------------------------------------------------------------------
# Backtesting
# -----------------------------------------------------------------------------


class BrahmastraBacktester:
    def __init__(self, cfg: Config, api: DhanApiClient, resolver: OptionContractResolver, stop_event: threading.Event):
        self.cfg = cfg
        self.api = api
        self.resolver = resolver
        self.stop_event = stop_event
        self.option_cache: Dict[Tuple[int, str], pd.DataFrame] = {}
        self.rolling_option_cache: Dict[Tuple[str, int, str], pd.DataFrame] = {}
        self.option_chain_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self.option_chain_errors: Dict[str, str] = {}
        LOG.info("Backtester initialized")

    def run(self, start_date: str, end_date: str) -> BacktestResult:
        LOG.warning("Backtest run started | start=%s | end=%s", start_date, end_date)
        start_day = dt.datetime.strptime(start_date, "%Y-%m-%d").date()
        end_day = dt.datetime.strptime(end_date, "%Y-%m-%d").date()
        start_dt = dt.datetime.combine(start_day, dt.time(9, 15))
        end_dt = dt.datetime.combine(end_day, dt.time(15, 30))

        raw = self.api.index_intraday(start_dt, end_dt)
        if raw.empty:
            LOG.warning("Backtest stopped: no NIFTY candles returned")
            return BacktestResult(start_date, end_date, [], ["No NIFTY candles returned."])

        index_df = add_indicators(raw, self.cfg)
        trades: List[BacktestTrade] = []
        errors: List[str] = []
        last_trigger_key: Optional[str] = None
        active_until: Optional[pd.Timestamp] = None

        LOG.info("Backtest candles ready | rows=%s | first=%s | last=%s", len(index_df), index_df.iloc[0].timestamp, index_df.iloc[-1].timestamp)
        for idx in range(len(index_df) - 1):
            if self.stop_event.is_set():
                LOG.warning("Backtest stop_event set | idx=%s", idx)
                errors.append("Scan stopped by user.")
                break

            if active_until is not None and pd.Timestamp(index_df.iloc[idx].timestamp) <= active_until:
                LOG.debug("Backtest skipping candle inside active trade window | idx=%s | ts=%s | active_until=%s", idx, index_df.iloc[idx].timestamp, active_until)
                continue

            signal = build_brahmastra_signal(index_df, idx, self.cfg)
            if signal is None:
                continue
            if signal.trigger_key == last_trigger_key:
                LOG.info("Backtest duplicate trigger skipped | idx=%s | trigger=%s", idx, signal.trigger_key)
                continue

            entry_idx = idx + 1
            entry_row = index_df.iloc[entry_idx]
            entry_time = entry_row.timestamp.to_pydatetime() if hasattr(entry_row.timestamp, "to_pydatetime") else entry_row.timestamp
            LOG.warning("Backtest signal accepted | signal_time=%s | entry_time=%s | side=%s | spot_open=%s", signal.candle_time, entry_time, signal.side, fmt(entry_row.open))

            try:
                trade = self._build_and_simulate_trade(index_df, idx, entry_idx, signal, entry_time)
                trades.append(trade)
                active_until = pd.Timestamp(trade.exit_time)
                last_trigger_key = signal.trigger_key
                LOG.warning("Backtest trade completed | entry=%s | exit=%s | side=%s | strike=%s | pnl=%s | reason=%s", trade.entry_time, trade.exit_time, trade.side, trade.strike, trade.pnl, trade.exit_reason)
            except Exception as exc:
                LOG.exception("Backtest trade build/sim failed | signal_time=%s | side=%s", signal.candle_time, signal.side)
                errors.append(f"{signal.candle_time} {signal.side}: {exc}")
                last_trigger_key = signal.trigger_key

        result = BacktestResult(start_date, end_date, trades, errors)
        LOG.warning(
            "Backtest run finished | start=%s | end=%s | trades=%s | wins=%s | losses=%s | pnl=%s | errors=%s",
            start_date,
            end_date,
            len(trades),
            result.wins,
            result.losses,
            result.total_pnl,
            len(errors),
        )
        return result

    def _build_and_simulate_trade(
        self,
        index_df: pd.DataFrame,
        signal_idx: int,
        entry_idx: int,
        signal: BrahmastraSignal,
        entry_time: dt.datetime,
    ) -> BacktestTrade:
        trade_date = entry_time.date()
        LOG.info("Building historical trade | trade_date=%s | side=%s | entry_time=%s", trade_date, signal.side, entry_time)
        try:
            expiry = self.resolver.pick_expiry_for_date(trade_date)
            contract, option_df, entry_pos = self._select_historical_option(expiry, signal.side, float(index_df.iloc[entry_idx].open), entry_time)
        except Exception as exact_exc:
            LOG.exception("Exact historical option selection failed | side=%s | entry_time=%s", signal.side, entry_time)
            if not self.cfg.use_dhan_expired_options_api:
                raise
            try:
                LOG.warning("Trying Dhan expired-options rolling fallback | side=%s | entry_time=%s", signal.side, entry_time)
                contract, option_df, entry_pos = self._select_rolling_historical_option(
                    signal.side,
                    float(index_df.iloc[entry_idx].open),
                    entry_time,
                )
            except Exception as rolling_exc:
                LOG.exception("Rolling historical option fallback failed | side=%s | entry_time=%s", signal.side, entry_time)
                raise RuntimeError(
                    f"{exact_exc}; Dhan expired-options rolling fallback also failed: {rolling_exc}"
                ) from rolling_exc
        plan = make_option_plan(signal, index_df, signal_idx, contract, option_df.iloc[: entry_pos + 1], self.cfg)
        return self._simulate_option_trade(index_df, entry_idx, signal, option_df, entry_pos, plan)

    def _option_day_df(self, security_id: int, day: dt.date) -> pd.DataFrame:
        cache_key = (security_id, day.isoformat())
        if cache_key in self.option_cache:
            LOG.info("Option candle cache hit | security_id=%s | day=%s | rows=%s", security_id, day, len(self.option_cache[cache_key]))
            return self.option_cache[cache_key]
        LOG.info("Option candle cache miss | security_id=%s | day=%s", security_id, day)
        start, end = day_start_end(day)
        df = self.api.option_intraday(security_id, start, end)
        self.option_cache[cache_key] = df
        return df

    def _rolling_option_day_df(self, side: str, strike_offset: int, day: dt.date) -> pd.DataFrame:
        cache_key = (side.upper(), strike_offset, day.isoformat())
        if cache_key in self.rolling_option_cache:
            LOG.info("Rolling option candle cache hit | side=%s | offset=%s | day=%s | rows=%s", side, strike_offset, day, len(self.rolling_option_cache[cache_key]))
            return self.rolling_option_cache[cache_key]
        LOG.info("Rolling option candle cache miss | side=%s | offset=%s | day=%s", side, strike_offset, day)
        start, end = day_start_end(day)
        df = self.api.rolling_option_intraday(side, strike_offset, start, end)
        self.rolling_option_cache[cache_key] = df
        return df

    def _option_chain_for_security_ids(self, expiry: str, trade_date: dt.date) -> Optional[Dict[str, Any]]:
        if expiry in self.option_chain_cache:
            LOG.info("Option-chain id cache hit | expiry=%s | available=%s", expiry, self.option_chain_cache[expiry] is not None)
            return self.option_chain_cache[expiry]

        expiry_date = parse_date_flexible(expiry)
        if expiry_date is not None and (expiry_date - trade_date).days > 10:
            self.option_chain_cache[expiry] = None
            self.option_chain_errors[expiry] = (
                f"live option-chain id fallback skipped because expiry {expiry} "
                f"is too far from trade date {trade_date}"
            )
            LOG.warning(self.option_chain_errors[expiry])
            return None

        try:
            LOG.info("Fetching live option-chain id fallback | expiry=%s | trade_date=%s", expiry, trade_date)
            snapshot = self.api.option_chain(expiry)
            data = snapshot.get("data", {}) if isinstance(snapshot, dict) else {}
            oc = data.get("oc") or {}
            self.option_chain_cache[expiry] = oc if oc else None
            if not oc:
                self.option_chain_errors[expiry] = f"live option chain for expiry {expiry} was empty"
                LOG.warning(self.option_chain_errors[expiry])
            return self.option_chain_cache[expiry]
        except Exception as exc:
            self.option_chain_cache[expiry] = None
            self.option_chain_errors[expiry] = f"live option-chain id fallback failed: {exc}"
            LOG.exception("Live option-chain id fallback failed | expiry=%s", expiry)
            return None

    def _select_historical_option(
        self,
        expiry: str,
        side: str,
        spot_at_entry: float,
        entry_time: dt.datetime,
    ) -> Tuple[OptionContract, pd.DataFrame, int]:
        LOG.info("Selecting exact historical option | expiry=%s | side=%s | spot_at_entry=%s | entry_time=%s", expiry, side, spot_at_entry, entry_time)
        candidates = strike_candidates(spot_at_entry, side, self.cfg, None)
        atm = round(spot_at_entry / self.cfg.strike_step) * self.cfg.strike_step
        choices: List[Tuple[Tuple[int, float, float], OptionContract, pd.DataFrame, int]] = []
        failures: List[str] = []
        oc_for_ids: Optional[Dict[str, Any]] = None
        oc_for_ids_loaded = False

        for strike in candidates:
            try:
                try:
                    security_id = self.resolver.resolve(expiry, strike, side)
                except Exception as resolve_exc:
                    LOG.warning("Master resolve failed; trying option-chain id fallback | strike=%s | side=%s | error=%s", strike, side, resolve_exc)
                    if not oc_for_ids_loaded:
                        oc_for_ids = self._option_chain_for_security_ids(expiry, entry_time.date())
                        oc_for_ids_loaded = True
                    if oc_for_ids is None:
                        raise resolve_exc
                    security_id = self.resolver.resolve(expiry, strike, side, oc_for_ids)

                option_df = self._option_day_df(security_id, entry_time.date())
                entry_pos = find_exact_candle(option_df, entry_time)
                if entry_pos is None:
                    failure = f"{int(strike)} {side}: no exact candle at {entry_time.time()}"
                    LOG.info("Historical candidate rejected | %s", failure)
                    failures.append(failure)
                    continue
                entry_premium = float(option_df.iloc[entry_pos].open)
                if entry_premium <= 0:
                    failure = f"{int(strike)} {side}: invalid premium {entry_premium}"
                    LOG.info("Historical candidate rejected | %s", failure)
                    failures.append(failure)
                    continue
                if not self.cfg.allow_premium_fallback and not (self.cfg.min_premium <= entry_premium <= self.cfg.max_premium):
                    failure = f"{int(strike)} {side}: premium {entry_premium:.2f} outside allowed range"
                    LOG.info("Historical candidate rejected | %s", failure)
                    failures.append(failure)
                    continue

                reason = "historical preferred premium" if self.cfg.preferred_premium_min <= entry_premium <= self.cfg.preferred_premium_max else "historical nearest premium"
                contract = OptionContract(
                    side=side,
                    strike=float(strike),
                    expiry=expiry,
                    security_id=security_id,
                    entry_premium=entry_premium,
                    selection_reason=reason,
                )
                choices.append((score_premium_choice(entry_premium, strike, atm, self.cfg), contract, option_df, entry_pos))
                LOG.info("Historical candidate accepted | strike=%s | side=%s | security_id=%s | premium=%s | entry_pos=%s", strike, side, security_id, entry_premium, entry_pos)
            except Exception as exc:
                failure = f"{int(strike)} {side}: {exc}"
                LOG.exception("Historical candidate failed | strike=%s | side=%s", strike, side)
                failures.append(failure)

        if not choices:
            detail = "; ".join(failures[:4])
            chain_error = self.option_chain_errors.get(expiry)
            if chain_error and chain_error not in detail:
                detail = f"{detail}; {chain_error}" if detail else chain_error
            LOG.error("No historical option candidate found | expiry=%s | time=%s | details=%s", expiry, entry_time, detail)
            raise RuntimeError(
                "No historical option candidate had an exact entry candle. "
                f"Expiry={expiry}, time={entry_time}. Details: {detail}"
            )

        _, contract, option_df, entry_pos = sorted(choices, key=lambda x: x[0])[0]
        LOG.warning("Selected historical option | side=%s | strike=%s | expiry=%s | security_id=%s | premium=%s | entry_pos=%s", contract.side, contract.strike, contract.expiry, contract.security_id, contract.entry_premium, entry_pos)
        return contract, option_df, entry_pos

    def _select_rolling_historical_option(
        self,
        side: str,
        spot_at_entry: float,
        entry_time: dt.datetime,
    ) -> Tuple[OptionContract, pd.DataFrame, int]:
        LOG.info("Selecting rolling historical option | side=%s | spot_at_entry=%s | entry_time=%s", side, spot_at_entry, entry_time)
        candidates = strike_candidates(spot_at_entry, side, self.cfg, None)
        atm = round(spot_at_entry / self.cfg.strike_step) * self.cfg.strike_step
        choices: List[Tuple[Tuple[int, float, float], OptionContract, pd.DataFrame, int]] = []
        failures: List[str] = []

        for strike in candidates:
            strike_offset = int(round((float(strike) - float(atm)) / self.cfg.strike_step))
            if abs(strike_offset) > 10:
                failure = f"{int(strike)} {side}: rolling offset ATM{strike_offset:+d} outside Dhan limit"
                LOG.info("Rolling candidate rejected | %s", failure)
                failures.append(failure)
                continue

            try:
                option_df = self._rolling_option_day_df(side, strike_offset, entry_time.date())
                entry_pos = find_exact_candle(option_df, entry_time)
                if entry_pos is None:
                    failure = f"{int(strike)} {side}: no rolling candle at {entry_time.time()}"
                    LOG.info("Rolling candidate rejected | %s", failure)
                    failures.append(failure)
                    continue

                entry_premium = float(option_df.iloc[entry_pos].open)
                if entry_premium <= 0:
                    failure = f"{int(strike)} {side}: invalid rolling premium {entry_premium}"
                    LOG.info("Rolling candidate rejected | %s", failure)
                    failures.append(failure)
                    continue
                if not self.cfg.allow_premium_fallback and not (self.cfg.min_premium <= entry_premium <= self.cfg.max_premium):
                    failure = f"{int(strike)} {side}: rolling premium {entry_premium:.2f} outside allowed range"
                    LOG.info("Rolling candidate rejected | %s", failure)
                    failures.append(failure)
                    continue

                actual_strike = float(strike)
                if "strike" in option_df.columns:
                    api_strike = pd.to_numeric(pd.Series([option_df.iloc[entry_pos].strike]), errors="coerce").iloc[0]
                    if pd.notna(api_strike) and float(api_strike) > 0:
                        actual_strike = float(api_strike)

                offset_label = "ATM" if strike_offset == 0 else f"ATM{strike_offset:+d}"
                reason = (
                    f"Dhan expired rolling option API {self.cfg.expired_options_expiry_flag} "
                    f"expiryCode={self.cfg.expired_options_expiry_code} {offset_label}"
                )
                contract = OptionContract(
                    side=side,
                    strike=actual_strike,
                    expiry=f"ROLLING-{self.cfg.expired_options_expiry_flag}-{self.cfg.expired_options_expiry_code}",
                    security_id=0,
                    entry_premium=entry_premium,
                    selection_reason=reason,
                )
                choices.append((score_premium_choice(entry_premium, actual_strike, atm, self.cfg), contract, option_df, entry_pos))
                LOG.info("Rolling candidate accepted | strike=%s | actual_strike=%s | side=%s | premium=%s | entry_pos=%s", strike, actual_strike, side, entry_premium, entry_pos)
            except Exception as exc:
                failure = f"{int(strike)} {side}: {exc}"
                LOG.exception("Rolling candidate failed | strike=%s | side=%s", strike, side)
                failures.append(failure)

        if not choices:
            detail = "; ".join(failures[:4])
            LOG.error("No rolling historical option candidate found | time=%s | details=%s", entry_time, detail)
            raise RuntimeError(
                "No Dhan expired-options rolling candidate had an exact entry candle. "
                f"time={entry_time}. Details: {detail}"
            )

        _, contract, option_df, entry_pos = sorted(choices, key=lambda x: x[0])[0]
        LOG.warning("Selected rolling historical option | side=%s | strike=%s | expiry=%s | premium=%s | entry_pos=%s", contract.side, contract.strike, contract.expiry, contract.entry_premium, entry_pos)
        return contract, option_df, entry_pos

    def _simulate_option_trade(
        self,
        index_df: pd.DataFrame,
        entry_idx: int,
        signal: BrahmastraSignal,
        option_df: pd.DataFrame,
        entry_pos: int,
        plan: OptionTradePlan,
    ) -> BacktestTrade:
        qty = self.cfg.quantity
        half_qty = max(1, qty // 2)
        remaining_qty = qty
        realized = 0.0
        entry = plan.option_entry
        sl = plan.option_stop_loss
        initial_sl = sl
        t1 = plan.option_target1
        t2 = plan.option_target2
        t1_hit = False
        exit_price = entry
        exit_reason = "end"
        exit_time = option_df.iloc[entry_pos].timestamp

        index_by_ts = {pd.Timestamp(row.timestamp): i for i, row in index_df.iterrows()}

        LOG.warning(
            "Simulating option trade | side=%s | strike=%s | entry_time=%s | entry=%s | sl=%s | t1=%s | t2=%s | qty=%s",
            plan.side,
            plan.strike,
            option_df.iloc[entry_pos].timestamp,
            entry,
            sl,
            t1,
            t2,
            qty,
        )
        for pos in range(entry_pos, len(option_df)):
            row = option_df.iloc[pos]
            row_ts = pd.Timestamp(row.timestamp)
            low = float(row.low)
            high = float(row.high)
            close = float(row.close)
            exit_time = row.timestamp
            LOG.debug("Trade sim candle | pos=%s | ts=%s | high=%s | low=%s | close=%s | sl=%s | remaining=%s | t1_hit=%s", pos, row.timestamp, high, low, close, sl, remaining_qty, t1_hit)

            if row.timestamp.time() >= self.cfg.square_off_time:
                exit_price = close
                exit_reason = "square off"
                LOG.warning("Backtest exit: square off | time=%s | price=%s", row.timestamp, exit_price)
                break

            if low <= sl:
                exit_price = sl
                exit_reason = "stop loss" if not t1_hit else "trailing/breakeven stop"
                LOG.warning("Backtest exit: stop hit | time=%s | price=%s | reason=%s", row.timestamp, exit_price, exit_reason)
                break

            if not t1_hit and high >= t1:
                realized += half_qty * (t1 - entry)
                remaining_qty -= half_qty
                t1_hit = True
                sl = max(sl, entry)
                LOG.warning("Backtest T1 hit | time=%s | t1=%s | booked_qty=%s | remaining=%s | new_sl=%s | realized=%s", row.timestamp, t1, half_qty, remaining_qty, sl, realized)
                if remaining_qty <= 0:
                    exit_price = t1
                    exit_reason = "target 1 full"
                    break

            if t1_hit and high >= t2:
                exit_price = t2
                exit_reason = "target 2"
                LOG.warning("Backtest exit: target 2 | time=%s | price=%s", row.timestamp, exit_price)
                break

            if pos > entry_pos and t1_hit:
                prev_low = float(option_df.iloc[pos - 1].low)
                new_sl = max(sl, round(prev_low - self.cfg.option_sl_buffer, 2), entry)
                if new_sl > sl:
                    LOG.warning("Backtest trailing SL raised | time=%s | old_sl=%s | new_sl=%s | prev_low=%s", row.timestamp, sl, new_sl, prev_low)
                sl = new_sl

            idx = index_by_ts.get(row_ts)
            if idx is not None and idx > entry_idx and opposite_exit_signal(index_df, idx, signal.side):
                exit_price = close
                exit_reason = "opposite signal"
                LOG.warning("Backtest exit: opposite signal | time=%s | price=%s", row.timestamp, exit_price)
                break

        realized += remaining_qty * (exit_price - entry)
        orders = 2 + (1 if t1_hit else 0)
        net_pnl = realized - orders * self.cfg.brokerage_per_order
        pnl_points = net_pnl / max(qty, 1)
        signal_time = signal.candle_time
        entry_time = option_df.iloc[entry_pos].timestamp

        trade = BacktestTrade(
            trade_date=str(pd.Timestamp(entry_time).date()),
            signal_time=str(signal_time),
            entry_time=str(entry_time),
            exit_time=str(exit_time),
            side=plan.side,
            strike=plan.strike,
            expiry=plan.expiry,
            security_id=plan.option_security_id,
            entry=entry,
            initial_sl=initial_sl,
            target1=t1,
            target2=t2,
            exit_price=round(exit_price, 2),
            quantity=qty,
            pnl=round(net_pnl, 2),
            pnl_points=round(pnl_points, 2),
            exit_reason=exit_reason,
            t1_hit=t1_hit,
            selection_reason=plan.selection_reason,
        )
        LOG.warning("Backtest trade simulated | %s", log_json(trade.__dict__))
        return trade


# -----------------------------------------------------------------------------
# Telegram formatting
# -----------------------------------------------------------------------------


def format_signal(signal: BrahmastraSignal) -> str:
    plan = signal.option_plan
    reasons = "\n".join(f"- {tg_escape(r)}" for r in signal.reasons[:10])
    side_text = f"{plan.side} {fmt(plan.strike, 0)}" if plan else signal.side

    lines = [
        "<b>LIVE BRAHMASTRA ALERT</b>",
        f"<b>Direction:</b> {signal.direction}",
        f"<b>Suggested:</b> BUY {side_text}",
        f"<b>Candle Time:</b> {signal.candle_time}",
        "",
        "<b>Signal Conditions</b>",
        reasons,
        "",
        "<b>NIFTY Context</b>",
        f"Spot close : {fmt(signal.spot)}",
        f"VWAP       : {fmt(signal.vwap)}",
        f"Supertrend : {fmt(signal.supertrend)}",
        f"MACD Hist  : {signal.macd_hist:.2f}",
    ]

    if plan:
        lines += [
            "",
            "<b>Option Trade Plan</b>",
            f"Expiry      : {tg_escape(plan.expiry)}",
            f"Side/Strike : {plan.side} {fmt(plan.strike, 0)}",
            f"Security ID : {plan.option_security_id}",
            f"Entry       : Rs {fmt(plan.option_entry)}",
            f"Stop Loss   : Rs {fmt(plan.option_stop_loss)}",
            f"Target 1    : Rs {fmt(plan.option_target1)}",
            f"Target 2    : Rs {fmt(plan.option_target2)}",
            f"Risk        : Rs {fmt(plan.option_risk)}",
            f"RR          : 1:{plan.option_rr1} / 1:{plan.option_rr2}",
            f"Selector    : {tg_escape(plan.selection_reason)}",
            "",
            "<b>Underlying Structure Levels</b>",
            f"NIFTY SL    : {fmt(plan.underlying_stop_loss)}",
            f"NIFTY T1    : {fmt(plan.underlying_target1)}",
            f"NIFTY T2    : {fmt(plan.underlying_target2)}",
            "",
            "Exit rule: book 50% at T1, move SL to entry, trail rest with previous option candle low.",
        ]

    lines.append(f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>")
    return "\n".join(lines)


def format_order_update(
    title: str,
    position: Optional[LivePosition],
    order: Optional[Dict[str, Any]],
    note: Optional[str] = None,
) -> str:
    plan = position.plan if position else None
    lines = [f"<b>{tg_escape(title)}</b>"]
    if plan:
        lines += [
            f"Option      : {plan.side} {fmt(plan.strike, 0)}",
            f"Expiry      : {tg_escape(plan.expiry)}",
            f"Security ID : {plan.option_security_id}",
        ]
    if order:
        req = order.get("_request", {}) if isinstance(order, dict) else {}
        lines += [
            f"Order ID    : {tg_escape(order.get('orderId') or order.get('order_id') or '-')}",
            f"Status      : {tg_escape(order_status(order))}",
        ]
        txn = order.get("transactionType") or req.get("transactionType")
        qty = order.get("quantity") or req.get("quantity")
        filled = order.get("filledQty")
        avg = order.get("averageTradedPrice")
        trigger = order.get("triggerPrice") or req.get("triggerPrice")
        if txn:
            lines.append(f"Txn         : {tg_escape(txn)}")
        if qty:
            lines.append(f"Qty         : {tg_escape(qty)}")
        if filled is not None:
            lines.append(f"Filled Qty  : {tg_escape(filled)}")
        if avg is not None:
            lines.append(f"Avg Price   : Rs {fmt(avg)}")
        if trigger:
            lines.append(f"Trigger     : Rs {fmt(trigger)}")
        if order.get("omsErrorDescription"):
            lines.append(f"Error       : {tg_escape(order.get('omsErrorDescription'))}")
    if position:
        lines += [
            f"Remaining   : {position.remaining_qty}",
            f"Current SL  : Rs {fmt(position.current_sl)}",
        ]
    if note:
        lines += ["", tg_escape(note)]
    lines.append(f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>")
    return "\n".join(lines)


def format_live_position_update(
    position: LivePosition,
    action: str,
    reason: str,
    price: Optional[float],
    event_time: Any,
    note: Optional[str] = None,
) -> str:
    plan = position.plan
    lines = [
        "<b>LIVE POSITION UPDATE</b>",
        f"<b>Action:</b> {tg_escape(action)}",
        f"<b>Reason:</b> {tg_escape(reason)}",
        f"Time        : {tg_escape(event_time)}",
        f"Option      : {plan.side} {fmt(plan.strike, 0)}",
        f"Opened at   : {tg_escape(position.opened_at)}",
        f"Entry       : Rs {fmt(plan.option_entry)}",
        f"Current SL  : Rs {fmt(position.current_sl)}",
        f"Target 1    : Rs {fmt(plan.option_target1)}",
        f"Target 2    : Rs {fmt(plan.option_target2)}",
    ]
    if position.entry_order_id:
        lines.append(f"Entry Order : {tg_escape(position.entry_order_id)}")
    if position.stop_order_id:
        lines.append(f"SL Order    : {tg_escape(position.stop_order_id)}")
    if price is not None:
        lines.append(f"Ref Price   : Rs {fmt(price)}")
    lines.append(f"Remaining   : {position.remaining_qty} qty")
    if note:
        lines += ["", tg_escape(note)]
    return "\n".join(lines)


def format_live_position_status(positions: List[LivePosition]) -> str:
    if not positions:
        return "No active live positions being tracked."

    lines = ["<b>Active Live Positions</b>", f"Count: {len(positions)}"]
    for i, position in enumerate(positions, start=1):
        plan = position.plan
        lines += [
            "",
            f"<b>#{i} {plan.side} {fmt(plan.strike, 0)}</b>",
            f"Expiry      : {tg_escape(plan.expiry)}",
            f"Security ID : {plan.option_security_id}",
            f"Entry       : Rs {fmt(plan.option_entry)}",
            f"Entry Order : {tg_escape(position.entry_order_id or '-')}",
            f"Entry Status: {tg_escape(position.entry_order_status)}",
            f"SL Order    : {tg_escape(position.stop_order_id or '-')}",
            f"SL Status   : {tg_escape(position.stop_order_status)}",
            f"Current SL  : Rs {fmt(position.current_sl)}",
            f"Target 1    : Rs {fmt(plan.option_target1)}",
            f"Target 2    : Rs {fmt(plan.option_target2)}",
            f"T1 Hit      : {'yes' if position.t1_hit else 'no'}",
            f"Remaining   : {position.remaining_qty} qty",
            f"Opened at   : {tg_escape(position.opened_at)}",
        ]
    return "\n".join(lines)


def format_chain_message(rows: List[Tuple[float, Dict[str, Any]]], spot: float, expiry: str, support: Optional[float], resistance: Optional[float], atm: float, pcr: Optional[float]) -> str:
    lines = [
        f"<b>{NIFTY50_NAME} Option Chain</b>",
        f"Expiry: {expiry} | Spot: {fmt(spot)} | ATM: {fmt(atm, 0)} | PCR: {fmt(pcr)}",
        f"Support: {fmt(support, 0)} | Resistance: {fmt(resistance, 0)}",
        "",
        "<pre>",
        f"{'Strike':<8} {'Tag':<6} {'CE OI':>10} {'CE LTP':>8} | {'PE LTP':>8} {'PE OI':>10}",
        "-" * 58,
    ]
    for strike, row in rows:
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        tag = ("ATM" if strike == atm else "") + (" S" if strike == support else "") + (" R" if strike == resistance else "")
        lines.append(
            f"{strike:<8,.0f} {tag.strip():<6} {num(ce.get('oi')):>10,.0f} {num(ce.get('last_price')):>8.2f} | {num(pe.get('last_price')):>8.2f} {num(pe.get('oi')):>10,.0f}"
        )
    lines += ["</pre>", "S=Support R=Resistance", f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>"]
    return "\n".join(lines)


def format_status_message(spot: float, expiry: str, support: Optional[float], resistance: Optional[float], atm: float, pcr: Optional[float], call_top: List[Tuple[float, float]], put_top: List[Tuple[float, float]]) -> str:
    bias = "neutral"
    if pcr is not None:
        bias = "bullish pressure" if pcr > 1.1 else ("bearish pressure" if pcr < 0.9 else "neutral")
    lines = [
        f"<b>{NIFTY50_NAME} Status</b>",
        f"Expiry: {expiry}",
        f"Spot: {fmt(spot)} | ATM: {fmt(atm, 0)} | PCR: {fmt(pcr)}",
        f"Bias: {bias}",
        f"Support: {fmt(support, 0)} | Resistance: {fmt(resistance, 0)}",
    ]
    if call_top:
        lines.append("Top Call OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in call_top))
    if put_top:
        lines.append("Top Put OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in put_top))
    lines.append(f"<i>Updated: {now_ist().strftime('%H:%M:%S IST')}</i>")
    return "\n".join(lines)


def format_backtest_summary(result: BacktestResult) -> str:
    trades = result.trades
    avg_win = statistics.mean([t.pnl for t in trades if t.pnl > 0]) if any(t.pnl > 0 for t in trades) else 0.0
    avg_loss = statistics.mean([t.pnl for t in trades if t.pnl <= 0]) if any(t.pnl <= 0 for t in trades) else 0.0
    best = max((t.pnl for t in trades), default=0.0)
    worst = min((t.pnl for t in trades), default=0.0)

    lines = [
        "<b>Brahmastra Backtest Result</b>",
        f"Period: {result.start_date} to {result.end_date}",
        "",
        f"Trades     : {len(trades)}",
        f"Wins       : {result.wins}",
        f"Losses     : {result.losses}",
        f"Win rate   : {result.win_rate:.1f}%",
        f"Total PnL  : Rs {fmt(result.total_pnl)}",
        f"Max DD     : Rs {fmt(result.max_drawdown)}",
        f"Avg win    : Rs {fmt(avg_win)}",
        f"Avg loss   : Rs {fmt(avg_loss)}",
        f"Best trade : Rs {fmt(best)}",
        f"Worst trade: Rs {fmt(worst)}",
    ]

    if trades:
        day_pnl: Dict[str, float] = {}
        for trade in trades:
            day_pnl[trade.trade_date] = day_pnl.get(trade.trade_date, 0.0) + trade.pnl
        lines += ["", "<b>Day-wise PnL</b>"]
        for day, pnl in sorted(day_pnl.items()):
            lines.append(f"{day}: Rs {fmt(pnl)}")

        lines += ["", "<b>Recent Trades</b>", "<pre>"]
        lines.append(f"{'Time':<16} {'Opt':<10} {'Entry':>7} {'SL':>7} {'T1':>7} {'T2':>7} {'Exit':>7} {'PnL':>9} {'Reason':<14}")
        lines.append("-" * 96)
        for trade in trades[-8:]:
            entry_time = str(trade.entry_time)[5:16]
            opt = f"{int(trade.strike)}{trade.side}"
            lines.append(
                f"{entry_time:<16} {opt:<10} {trade.entry:>7.2f} {trade.initial_sl:>7.2f} "
                f"{trade.target1:>7.2f} {trade.target2:>7.2f} {trade.exit_price:>7.2f} "
                f"{trade.pnl:>9.2f} {trade.exit_reason:<14}"
            )
        lines.append("</pre>")
        lines.append("SL shown is the initial option stop loss used at entry.")

    if result.errors:
        lines += ["", "<b>Warnings</b>"]
        for err in result.errors[:6]:
            lines.append(f"- {tg_escape(err)}")
        if len(result.errors) > 6:
            lines.append(f"- plus {len(result.errors) - 6} more warning(s)")

    return "\n".join(lines)


HELP_TEXT = """<b>NIFTY Brahmastra Bot Commands</b>

<b>Backtest</b>
  SCAN 2026-05-01 2026-05-14

<b>Live control</b>
  LIVE   - enable live monitoring and real order execution
  STOP   - stop running scan
  /position - active live trade levels and order ids

<b>Market overview</b>
  /chain   - option chain around ATM
  /status  - spot, PCR, support, resistance
  /expiry  - selected weekly expiry
  /help    - this help message

Strategy:
Supertrend 20,2 + MACD 12,26,9 + VWAP on 5-minute NIFTY candles.
Live mode places real Dhan intraday orders.
Backtest option entry/exit uses historical option candles at the same timestamp.
"""


# -----------------------------------------------------------------------------
# Main bot engine
# -----------------------------------------------------------------------------


class BrahmastraBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = DhanApiClient(cfg)
        self.bot = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        self.resolver = OptionContractResolver(cfg, self.api)

        self.expiry: Optional[str] = None
        self.live_enabled = cfg.live_enabled_at_start
        self.last_live_candle_time: Optional[str] = None
        self.last_live_trigger: Optional[str] = None
        self.live_positions: List[LivePosition] = []
        self._last_live_scan_log_at = 0.0

        self.scan_thread: Optional[threading.Thread] = None
        self.scan_stop_event = threading.Event()
        self.scan_lock = threading.Lock()
        LOG.info("BrahmastraBot initialized | live_enabled=%s", self.live_enabled)

    def _live_scan_log(self, message: str, *args: Any, force: bool = False) -> None:
        every = max(0.0, self.cfg.log_live_scan_every_seconds)
        now = time.time()
        if force or every <= 0.0 or now - self._last_live_scan_log_at >= every:
            LOG.info(message, *args)
            self._last_live_scan_log_at = now
        else:
            LOG.debug(message, *args)

    def ensure_expiry(self) -> str:
        today = today_ist()
        if self.expiry is None:
            LOG.info("ensure_expiry: no cached expiry; resolving | today=%s", today)
            self.expiry = self.resolver.pick_expiry_for_date(today)
        else:
            LOG.debug("ensure_expiry: using cached expiry | expiry=%s", self.expiry)
        return self.expiry

    def current_intraday(self) -> pd.DataFrame:
        now = now_ist().replace(tzinfo=None)
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        LOG.info("Loading current intraday candles | start=%s | end=%s | now=%s", start, end, now)
        return self.api.index_intraday(start, end)

    def current_chain(self) -> Tuple[str, float, Dict[str, Any]]:
        expiry = self.ensure_expiry()
        LOG.info("Loading current option chain | expiry=%s", expiry)
        snapshot = self.api.option_chain(expiry)
        data = snapshot.get("data", {})
        spot = num(data.get("last_price"))
        oc = data.get("oc") or {}
        if not oc:
            LOG.error("Current option chain empty | expiry=%s | spot=%s", expiry, spot)
            raise RuntimeError("Empty option chain. Market may be closed.")
        LOG.info("Current option chain ready | expiry=%s | spot=%s | strikes=%s", expiry, spot, len(oc))
        return expiry, spot, oc

    def build_live_plan(self, signal: BrahmastraSignal, index_df: pd.DataFrame, signal_idx: int) -> BrahmastraSignal:
        LOG.warning("Building live plan | signal_time=%s | side=%s | spot=%s", signal.candle_time, signal.side, signal.spot)
        expiry, _, oc = self.current_chain()
        contract = select_option_from_chain(oc, expiry, signal.spot, signal.side, self.cfg, self.resolver)
        option_df: Optional[pd.DataFrame] = None
        try:
            start, end = day_start_end(today_ist())
            option_df = self.api.option_intraday(contract.security_id, start, end)
            option_df = closed_candles_only(option_df, self.cfg.candle_interval)
            LOG.info("Live plan option history loaded | security_id=%s | rows=%s", contract.security_id, len(option_df))
        except Exception:
            LOG.exception("Live plan option history failed; using fallback SL | security_id=%s", contract.security_id)
            option_df = None
        signal.option_plan = make_option_plan(signal, index_df, signal_idx, contract, option_df, self.cfg)
        return signal

    def wait_for_order_update(self, order_id_value: str, attempts: Optional[int] = None) -> Dict[str, Any]:
        attempts = attempts or self.cfg.order_status_poll_attempts
        latest: Dict[str, Any] = {"orderId": order_id_value, "orderStatus": "UNKNOWN"}
        LOG.info("Waiting for order update | order_id=%s | attempts=%s | interval=%ss", order_id_value, attempts, self.cfg.order_status_poll_seconds)
        for attempt in range(1, max(1, attempts) + 1):
            latest = self.api.get_order(order_id_value)
            status = order_status(latest)
            LOG.info("Order poll | order_id=%s | attempt=%s/%s | status=%s | filled=%s | remaining=%s", order_id_value, attempt, attempts, status, order_filled_qty(latest), order_remaining_qty(latest))
            if status in ORDER_FILLED_STATUSES or status in ORDER_DEAD_STATUSES:
                return latest
            if status == "PART_TRADED" and order_remaining_qty(latest) == 0:
                return latest
            time.sleep(max(0.1, self.cfg.order_status_poll_seconds))
        LOG.warning("Order polling ended without final state | order_id=%s | latest_status=%s", order_id_value, order_status(latest))
        return latest

    def place_market_exit(self, position: LivePosition, quantity: int, reason: str) -> Optional[Dict[str, Any]]:
        if quantity <= 0:
            LOG.info("place_market_exit skipped: quantity <= 0 | reason=%s", reason)
            return None
        LOG.warning("Placing market exit | qty=%s | reason=%s | option=%s %s", quantity, reason, position.plan.side, position.plan.strike)
        order = self.api.place_order(
            transaction_type="SELL",
            security_id=position.plan.option_security_id,
            quantity=quantity,
            order_type=self.cfg.exit_order_type,
            product_type=self.cfg.order_product_type,
            validity=self.cfg.order_validity,
            price=0.0,
            trigger_price=0.0,
            correlation_id=make_correlation_id("EXIT"),
        )
        status = order_status(order)
        exit_order_id = order_id(order)
        position.last_exit_order_id = exit_order_id
        self.bot.send(format_order_update(f"EXIT ORDER PLACED - {reason}", position, order))
        if exit_order_id:
            latest = self.wait_for_order_update(exit_order_id, attempts=5)
            latest_status = order_status(latest)
            if latest_status != status:
                self.bot.send(format_order_update(f"EXIT ORDER UPDATE - {reason}", position, latest))
            return latest
        return order

    def place_or_modify_stop_order(self, position: LivePosition, quantity: int, trigger_price: float, reason: str) -> None:
        trigger_price = round(max(float(trigger_price), 0.05), 2)
        LOG.warning("place_or_modify_stop_order | qty=%s | trigger=%s | reason=%s | existing_order=%s | existing_status=%s", quantity, trigger_price, reason, position.stop_order_id, position.stop_order_status)
        if quantity <= 0:
            if position.stop_order_id and position.stop_order_status not in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
                cancelled = self.api.cancel_order(position.stop_order_id)
                position.stop_order_status = order_status(cancelled)
                self.bot.send(format_order_update(f"SL ORDER CANCELLED - {reason}", position, cancelled))
            return

        if position.stop_order_id and position.stop_order_status not in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
            modified = self.api.modify_order(
                position.stop_order_id,
                quantity=quantity,
                order_type=self.cfg.sl_order_type,
                price=0.0,
                trigger_price=trigger_price,
                validity=self.cfg.order_validity,
            )
            position.stop_order_status = order_status(modified)
            position.current_sl = trigger_price
            self.bot.send(format_order_update(f"SL ORDER MODIFIED - {reason}", position, modified))
            return

        placed = self.api.place_order(
            transaction_type="SELL",
            security_id=position.plan.option_security_id,
            quantity=quantity,
            order_type=self.cfg.sl_order_type,
            product_type=self.cfg.order_product_type,
            validity=self.cfg.order_validity,
            price=0.0,
            trigger_price=trigger_price,
            correlation_id=make_correlation_id("SL"),
        )
        position.stop_order_id = order_id(placed)
        position.stop_order_status = order_status(placed)
        position.current_sl = trigger_price
        self.bot.send(format_order_update(f"SL ORDER PLACED - {reason}", position, placed))

    def sync_stop_order(self, position: LivePosition) -> bool:
        if not position.stop_order_id:
            LOG.debug("sync_stop_order skipped: no stop order id | trigger=%s", position.trigger_key)
            return True
        LOG.info("Syncing stop order | order_id=%s | current_status=%s | remaining=%s", position.stop_order_id, position.stop_order_status, position.remaining_qty)
        latest = self.api.get_order(position.stop_order_id)
        status = order_status(latest)
        remaining_after_stop = order_remaining_qty(latest)
        if status != position.stop_order_status:
            LOG.warning("Stop order status changed | order_id=%s | old=%s | new=%s", position.stop_order_id, position.stop_order_status, status)
            position.stop_order_status = status
            self.bot.send(format_order_update("SL ORDER STATUS UPDATE", position, latest))
        if status in ORDER_FILLED_STATUSES or (status == "PART_TRADED" and order_remaining_qty(latest) == 0):
            position.remaining_qty = 0
            self.bot.send(
                format_live_position_update(
                    position,
                    "EXIT FULL",
                    "broker stop-loss order executed",
                    order_average_price(latest) or position.current_sl,
                    now_ist().replace(tzinfo=None),
                )
            )
            LOG.warning("Stop order executed; position closed | order_id=%s", position.stop_order_id)
            return False
        if status == "PART_TRADED" and remaining_after_stop > 0:
            LOG.warning("Stop order part traded | order_id=%s | remaining_after_stop=%s", position.stop_order_id, remaining_after_stop)
            position.remaining_qty = remaining_after_stop
        if status in ORDER_DEAD_STATUSES and position.remaining_qty > 0:
            LOG.error("Stop order not active while position has remaining qty | order_id=%s | status=%s | remaining=%s", position.stop_order_id, status, position.remaining_qty)
            self.bot.send(
                format_order_update(
                    "SL ORDER NOT ACTIVE",
                    position,
                    latest,
                    "The position still has remaining quantity, but the protective SL order is not active.",
                )
            )
        return True

    def sync_entry_order(self, position: LivePosition) -> bool:
        if not position.entry_order_id:
            LOG.warning("sync_entry_order failed: no entry order id")
            return False
        LOG.info("Syncing entry order | order_id=%s | current_status=%s | filled=%s", position.entry_order_id, position.entry_order_status, position.entry_filled_qty)
        latest = self.api.get_order(position.entry_order_id)
        status = order_status(latest)
        filled_qty = order_filled_qty(latest)
        avg_price = order_average_price(latest)

        if status != position.entry_order_status or filled_qty != position.entry_filled_qty:
            previous_filled_qty = position.entry_filled_qty
            LOG.warning("Entry order state changed | order_id=%s | old_status=%s | new_status=%s | old_filled=%s | new_filled=%s", position.entry_order_id, position.entry_order_status, status, previous_filled_qty, filled_qty)
            position.entry_order_status = status
            position.entry_filled_qty = filled_qty
            if avg_price > 0:
                position.entry_avg_price = avg_price
            self.bot.send(format_order_update("ENTRY ORDER STATUS UPDATE", position, latest))
            if filled_qty > previous_filled_qty and position.stop_order_id is not None:
                additional_qty = filled_qty - previous_filled_qty
                position.remaining_qty += additional_qty
                self.place_or_modify_stop_order(position, position.remaining_qty, position.current_sl, "additional entry fill")

        if filled_qty > 0 and position.stop_order_id is None:
            LOG.warning("Entry has fill and no stop order; placing protection | filled_qty=%s", filled_qty)
            position.remaining_qty = filled_qty
            self.place_or_modify_stop_order(position, position.remaining_qty, position.current_sl, "entry filled")

        if status in ORDER_DEAD_STATUSES and filled_qty <= 0:
            LOG.warning("Entry order closed without fill | order_id=%s | status=%s", position.entry_order_id, status)
            self.bot.send(format_order_update("ENTRY ORDER CLOSED WITHOUT FILL", position, latest))
            return False
        return True

    def open_live_position(self, signal: BrahmastraSignal) -> None:
        if signal.option_plan is None:
            LOG.warning("open_live_position skipped: signal has no option plan | trigger=%s", signal.trigger_key)
            return
        signal_ts = pd.Timestamp(signal.candle_time)
        for position in self.live_positions:
            if position.trigger_key == signal.trigger_key and pd.Timestamp(position.signal_time) == signal_ts:
                LOG.info("open_live_position skipped duplicate | trigger=%s | signal_time=%s", signal.trigger_key, signal.candle_time)
                return

        LOG.warning("Opening live position | trigger=%s | signal_time=%s | side=%s | strike=%s | qty=%s", signal.trigger_key, signal.candle_time, signal.option_plan.side, signal.option_plan.strike, self.cfg.quantity)
        position = LivePosition(
            plan=signal.option_plan,
            signal_time=signal.candle_time,
            opened_at=now_ist().replace(tzinfo=None),
            trigger_key=signal.trigger_key,
            current_sl=signal.option_plan.option_stop_loss,
            remaining_qty=0,
            last_checked_option_ts=signal_ts,
        )

        entry_order = self.api.place_order(
            transaction_type="BUY",
            security_id=signal.option_plan.option_security_id,
            quantity=self.cfg.quantity,
            order_type=self.cfg.entry_order_type,
            product_type=self.cfg.order_product_type,
            validity=self.cfg.order_validity,
            price=0.0,
            trigger_price=0.0,
            correlation_id=make_correlation_id("ENTRY"),
        )
        position.entry_order_id = order_id(entry_order)
        position.entry_order_status = order_status(entry_order)
        self.bot.send(format_order_update("ENTRY ORDER PLACED", position, entry_order))

        if position.entry_order_id:
            latest = self.wait_for_order_update(position.entry_order_id)
            position.entry_order_status = order_status(latest)
            position.entry_filled_qty = order_filled_qty(latest)
            avg_price = order_average_price(latest)
            if avg_price > 0:
                position.entry_avg_price = avg_price
            self.bot.send(format_order_update("ENTRY ORDER UPDATE", position, latest))

            if position.entry_filled_qty > 0:
                position.remaining_qty = position.entry_filled_qty
                self.place_or_modify_stop_order(position, position.remaining_qty, position.current_sl, "entry filled")
            elif position.entry_order_status in ORDER_DEAD_STATUSES:
                LOG.warning("Live position not tracked because entry is dead without fill | order_id=%s | status=%s", position.entry_order_id, position.entry_order_status)
                return

        self.live_positions.append(position)
        LOG.warning("Live position added to tracking | positions=%s | entry_order=%s | filled=%s | remaining=%s", len(self.live_positions), position.entry_order_id, position.entry_filled_qty, position.remaining_qty)

    def cancel_stop_before_exit(self, position: LivePosition, reason: str) -> None:
        if not position.stop_order_id:
            LOG.debug("cancel_stop_before_exit skipped: no stop order | reason=%s", reason)
            return
        if position.stop_order_status in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
            LOG.info("cancel_stop_before_exit skipped: stop not active | order_id=%s | status=%s", position.stop_order_id, position.stop_order_status)
            return
        LOG.warning("Checking/cancelling stop before market exit | order_id=%s | reason=%s", position.stop_order_id, reason)
        latest = self.api.get_order(position.stop_order_id)
        latest_status = order_status(latest)
        position.stop_order_status = latest_status
        if latest_status in ORDER_FILLED_STATUSES or (latest_status == "PART_TRADED" and order_remaining_qty(latest) == 0):
            position.remaining_qty = 0
            self.bot.send(format_order_update("SL ORDER ALREADY EXECUTED", position, latest))
            LOG.warning("Stop already executed before manual exit | order_id=%s", position.stop_order_id)
            return
        if latest_status == "PART_TRADED":
            position.remaining_qty = order_remaining_qty(latest)
            LOG.warning("Stop part traded before manual exit | remaining=%s", position.remaining_qty)
        if latest_status in ORDER_DEAD_STATUSES:
            LOG.info("Stop already dead before manual exit | order_id=%s | status=%s", position.stop_order_id, latest_status)
            return
        cancelled = self.api.cancel_order(position.stop_order_id)
        position.stop_order_status = order_status(cancelled)
        self.bot.send(format_order_update(f"SL ORDER CANCELLED - {reason}", position, cancelled))

    def track_live_position(self, index_df: pd.DataFrame, position: LivePosition) -> bool:
        """Return True while the position remains open."""

        LOG.info(
            "Tracking live position | option=%s %s | entry_order=%s | stop_order=%s | remaining=%s | sl=%s | t1_hit=%s",
            position.plan.side,
            position.plan.strike,
            position.entry_order_id,
            position.stop_order_id,
            position.remaining_qty,
            position.current_sl,
            position.t1_hit,
        )
        if not self.sync_entry_order(position):
            LOG.warning("Position no longer open after entry sync | trigger=%s", position.trigger_key)
            return False
        if position.entry_filled_qty <= 0:
            LOG.info("Position waiting for entry fill | order_id=%s | status=%s", position.entry_order_id, position.entry_order_status)
            return True
        if not self.sync_stop_order(position):
            LOG.warning("Position closed after stop sync | stop_order=%s", position.stop_order_id)
            return False

        plan = position.plan
        start, end = day_start_end(today_ist())
        option_df = self.api.option_intraday(plan.option_security_id, start, end)
        option_df = closed_candles_only(option_df, self.cfg.candle_interval)
        if option_df.empty:
            LOG.warning("No option candles available while tracking position | security_id=%s", plan.option_security_id)
            return True

        index_by_ts = {pd.Timestamp(row.timestamp): i for i, row in index_df.iterrows()}
        last_ts = position.last_checked_option_ts or pd.Timestamp(position.signal_time)
        new_rows = option_df[option_df["timestamp"] > last_ts].reset_index(drop=True)
        LOG.info("Live position candle scan | security_id=%s | option_rows=%s | new_rows=%s | last_ts=%s", plan.option_security_id, len(option_df), len(new_rows), last_ts)
        if new_rows.empty:
            return True

        half_qty = max(1, self.cfg.quantity // 2)

        for _, row in new_rows.iterrows():
            row_ts = pd.Timestamp(row.timestamp)
            low = float(row.low)
            high = float(row.high)
            close = float(row.close)
            position.last_checked_option_ts = row_ts
            LOG.info("Processing live option candle | ts=%s | high=%s | low=%s | close=%s | sl=%s | t1=%s | t2=%s | remaining=%s", row.timestamp, high, low, close, position.current_sl, plan.option_target1, plan.option_target2, position.remaining_qty)

            if not self.sync_stop_order(position):
                return False

            if row.timestamp.time() >= self.cfg.square_off_time:
                LOG.warning("Live square-off condition met | time=%s | close=%s", row.timestamp, close)
                self.cancel_stop_before_exit(position, "15:20 square-off")
                if position.remaining_qty > 0:
                    self.place_market_exit(position, position.remaining_qty, "15:20 square-off")
                    self.bot.send(
                        format_live_position_update(
                            position,
                            "EXIT FULL",
                            "15:20 square-off time",
                            close,
                            row.timestamp,
                            "No SL/target exit came first, so the bot sent a market square-off order.",
                        )
                    )
                    position.remaining_qty = 0
                return False

            if low <= position.current_sl:
                LOG.warning("Live SL touched by candle | time=%s | low=%s | sl=%s", row.timestamp, low, position.current_sl)
                self.bot.send(
                    format_live_position_update(
                        position,
                        "SL TOUCHED",
                        "waiting for broker stop-loss order execution",
                        position.current_sl,
                        row.timestamp,
                    )
                )
                if not self.sync_stop_order(position):
                    return False
                return True

            if not position.t1_hit and high >= plan.option_target1:
                booked_qty = min(half_qty, position.remaining_qty)
                new_remaining = position.remaining_qty - booked_qty
                position.current_sl = max(position.current_sl, plan.option_entry)
                LOG.warning("Live T1 hit | time=%s | high=%s | t1=%s | booked_qty=%s | new_remaining=%s | new_sl=%s", row.timestamp, high, plan.option_target1, booked_qty, new_remaining, position.current_sl)
                self.place_or_modify_stop_order(position, new_remaining, position.current_sl, "target 1 hit")
                if booked_qty > 0:
                    self.place_market_exit(position, booked_qty, "target 1")
                    position.remaining_qty = new_remaining
                position.t1_hit = True
                self.bot.send(
                    format_live_position_update(
                        position,
                        f"BOOK {booked_qty} QTY",
                        "target 1 hit; SL moved to entry",
                        plan.option_target1,
                        row.timestamp,
                        "Keep the remaining quantity running for T2 or trailing stop.",
                    )
                )
                if position.remaining_qty <= 0:
                    LOG.warning("Live position closed after T1 booking | trigger=%s", position.trigger_key)
                    return False

            if position.t1_hit and high >= plan.option_target2:
                LOG.warning("Live T2 hit | time=%s | high=%s | t2=%s", row.timestamp, high, plan.option_target2)
                self.cancel_stop_before_exit(position, "target 2")
                if position.remaining_qty > 0:
                    self.place_market_exit(position, position.remaining_qty, "target 2")
                    self.bot.send(
                        format_live_position_update(
                            position,
                            "EXIT REMAINING",
                            "target 2 hit",
                            plan.option_target2,
                            row.timestamp,
                        )
                    )
                    position.remaining_qty = 0
                return False

            if position.t1_hit:
                prev_rows = option_df[option_df["timestamp"] < row.timestamp]
                if not prev_rows.empty:
                    prev_low = float(prev_rows.iloc[-1].low)
                    new_sl = max(position.current_sl, round(prev_low - self.cfg.option_sl_buffer, 2), plan.option_entry)
                    if new_sl > position.current_sl:
                        LOG.warning("Live trailing SL update | time=%s | old_sl=%s | new_sl=%s | prev_low=%s", row.timestamp, position.current_sl, new_sl, prev_low)
                        self.place_or_modify_stop_order(position, position.remaining_qty, new_sl, "previous option candle low trail")
                        self.bot.send(
                            format_live_position_update(
                                position,
                                "TRAIL SL",
                                "previous option candle low trail",
                                None,
                                row.timestamp,
                            )
                        )

            idx = index_by_ts.get(row_ts)
            if idx is not None and pd.Timestamp(index_df.iloc[idx].timestamp) > pd.Timestamp(position.signal_time):
                if opposite_exit_signal(index_df, idx, plan.side):
                    LOG.warning("Live opposite signal exit condition | time=%s | close=%s", row.timestamp, close)
                    self.cancel_stop_before_exit(position, "opposite Brahmastra signal")
                    if position.remaining_qty > 0:
                        action = "EXIT FULL" if not position.t1_hit else "EXIT REMAINING"
                        self.place_market_exit(position, position.remaining_qty, "opposite signal")
                        self.bot.send(
                            format_live_position_update(
                                position,
                                action,
                                "opposite Brahmastra signal",
                                close,
                                row.timestamp,
                                "No SL/target exit came first; the bot closed because the setup reversed.",
                            )
                        )
                        position.remaining_qty = 0
                    return False

        LOG.info("Live position remains open after scan | remaining=%s | sl=%s", position.remaining_qty, position.current_sl)
        return True

    def track_live_positions(self, index_df: pd.DataFrame) -> None:
        if not self.live_positions:
            LOG.debug("No live positions to track")
            return

        LOG.info("Tracking live positions | count=%s", len(self.live_positions))
        still_open: List[LivePosition] = []
        for position in list(self.live_positions):
            try:
                if self.track_live_position(index_df, position):
                    still_open.append(position)
            except Exception as exc:
                LOG.exception("Live position tracking error | trigger=%s", position.trigger_key)
                print(f"Live position tracking error: {exc}")
                self.bot.send(f"<b>Live position tracking error</b>\n{tg_escape(exc)}")
                still_open.append(position)
        closed = len(self.live_positions) - len(still_open)
        self.live_positions = still_open
        LOG.info("Live position tracking completed | still_open=%s | closed=%s", len(still_open), closed)

    def live_check(self) -> None:
        scan_started = time.perf_counter()
        self._live_scan_log(
            "Live scan tick | enabled=%s | market_open=%s | positions=%s | last_candle=%s",
            self.live_enabled,
            market_session_open(),
            len(self.live_positions),
            self.last_live_candle_time,
        )
        if not self.live_enabled:
            self._live_scan_log("Live scan skipped | reason=live_disabled")
            return
        if not market_session_open():
            self._live_scan_log("Live scan skipped | reason=market_closed | now=%s", now_ist())
            return

        raw = self.current_intraday()
        LOG.info("Live scan raw candles | rows=%s", len(raw))
        raw = closed_candles_only(raw, self.cfg.candle_interval)
        LOG.info("Live scan closed candles | rows=%s", len(raw))
        if raw.empty or len(raw) < 40:
            self._live_scan_log("Live scan skipped | reason=not_enough_closed_candles | rows=%s", len(raw), force=True)
            return

        index_df = add_indicators(raw, self.cfg)
        latest = index_df.iloc[-1]
        candle_time = str(latest.timestamp)
        LOG.info(
            "Live scan latest candle | time=%s | open=%s | high=%s | low=%s | close=%s | st_dir=%s | vwap=%s | macd_hist=%s",
            latest.timestamp,
            fmt(latest.open),
            fmt(latest.high),
            fmt(latest.low),
            fmt(latest.close),
            int(latest.supertrend_dir),
            fmt(latest.vwap),
            fmt(latest.macd_hist),
        )
        self.track_live_positions(index_df)

        if candle_time == self.last_live_candle_time:
            self._live_scan_log("Live scan no new candle | candle_time=%s", candle_time)
            return
        self.last_live_candle_time = candle_time
        LOG.warning("Live scan processing new candle | candle_time=%s", candle_time)

        signal = build_brahmastra_signal(index_df, len(index_df) - 1, self.cfg)
        if signal is None:
            elapsed_ms = (time.perf_counter() - scan_started) * 1000.0
            self._live_scan_log("Live scan completed | signal=no | candle_time=%s | elapsed_ms=%.0f", candle_time, elapsed_ms, force=True)
            return
        if signal.trigger_key == self.last_live_trigger:
            LOG.info("Live signal skipped: duplicate trigger | trigger=%s", signal.trigger_key)
            return

        signal = self.build_live_plan(signal, index_df, len(index_df) - 1)
        self.bot.send(format_signal(signal))
        self.open_live_position(signal)
        self.last_live_trigger = signal.trigger_key
        elapsed_ms = (time.perf_counter() - scan_started) * 1000.0
        LOG.warning("Live scan completed | signal=yes | trigger=%s | elapsed_ms=%.0f", signal.trigger_key, elapsed_ms)

    def scan_worker(self, start_date: str, end_date: str) -> None:
        LOG.warning("Backtest worker started | start=%s | end=%s", start_date, end_date)
        try:
            self.bot.send(
                f"<b>Backtest started</b>\n"
                f"Symbol: {NIFTY50_NAME}\n"
                f"Period: {start_date} to {end_date}\n"
                f"Entry: next candle open\n"
                f"Premium: historical option candle only"
            )
            backtester = BrahmastraBacktester(self.cfg, self.api, self.resolver, self.scan_stop_event)
            result = backtester.run(start_date, end_date)
            self.bot.send(format_backtest_summary(result))
            LOG.warning("Backtest worker sent summary | trades=%s | errors=%s | pnl=%s", len(result.trades), len(result.errors), result.total_pnl)
        except Exception as exc:
            LOG.exception("Backtest worker error")
            self.bot.send(f"<b>Backtest error</b>\n{tg_escape(exc)}")
        finally:
            self.scan_stop_event.clear()
            with self.scan_lock:
                self.scan_thread = None
            LOG.warning("Backtest worker finished | start=%s | end=%s", start_date, end_date)

    def start_scan(self, start_date: str, end_date: str) -> None:
        LOG.info("start_scan requested | start=%s | end=%s", start_date, end_date)
        with self.scan_lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                LOG.warning("start_scan rejected: scan already running")
                self.bot.send("A scan is already running. Send STOP first.")
                return
            self.scan_stop_event.clear()
            self.scan_thread = threading.Thread(target=self.scan_worker, args=(start_date, end_date), daemon=True, name=f"Backtest-{start_date}-{end_date}")
            self.scan_thread.start()
            LOG.warning("Backtest thread started | name=%s", self.scan_thread.name)

    def handle_chain(self) -> None:
        LOG.info("Handling /chain command")
        expiry, spot, oc = self.current_chain()
        all_strikes = sorted(float(k) for k in oc.keys())
        atm = nearest_strike(all_strikes, spot)
        nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: self.cfg.strikes_window * 2 + 1]
        rows = [(s, get_row(oc, s)) for s in sorted(nearby)]
        support, resistance = support_resistance_oi(oc)
        pcr = pcr_near_atm(oc, spot, self.cfg.strikes_window)
        self.bot.send(format_chain_message(rows, spot, expiry, support, resistance, atm, pcr))

    def handle_status(self) -> None:
        LOG.info("Handling /status command")
        expiry, spot, oc = self.current_chain()
        all_strikes = sorted(float(k) for k in oc.keys())
        atm = nearest_strike(all_strikes, spot)
        support, resistance = support_resistance_oi(oc)
        pcr = pcr_near_atm(oc, spot, self.cfg.strikes_window)
        self.bot.send(
            format_status_message(
                spot=spot,
                expiry=expiry,
                support=support,
                resistance=resistance,
                atm=atm,
                pcr=pcr,
                call_top=top_oi(oc, "ce", 3),
                put_top=top_oi(oc, "pe", 3),
            )
        )

    def handle_strike(self, side: str, strike: float) -> None:
        LOG.info("Handling strike command | side=%s | strike=%s", side, strike)
        expiry, spot, oc = self.current_chain()
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        if not option:
            LOG.warning("Strike not found | side=%s | strike=%s", side, strike)
            self.bot.send(f"Strike not found: {side} {int(strike)}")
            return

        premium = num(option.get("last_price"))
        security_id = self.resolver.resolve(expiry, strike, side, oc)
        msg = [
            "<b>Strike Snapshot</b>",
            f"Expiry: {expiry}",
            f"Spot: {fmt(spot)}",
            f"Option: {side} {fmt(strike, 0)}",
            f"Security ID: {security_id}",
            f"LTP: Rs {fmt(premium)}",
            f"OI: {fmt(option.get('oi'), 0)}",
        ]
        self.bot.send("\n".join(msg))

    def dispatch(self, raw: str) -> None:
        text = raw.strip()
        cmd = text.split("@")[0].lower()
        LOG.info("Dispatching command | raw=%s | cmd=%s", text, cmd)

        m = STRIKE_RE.match(text.replace(" ", ""))
        if m:
            self.handle_strike(m.group(1).upper(), float(m.group(2)))
            return

        m = SCAN_RE.match(text)
        if m:
            self.start_scan(m.group(1), m.group(2))
            return

        if STOP_RE.match(text):
            LOG.warning("STOP command received")
            self.scan_stop_event.set()
            self.bot.send("Stop requested.")
            return

        if LIVE_RE.match(text):
            LOG.warning("LIVE command received; enabling live monitoring")
            self.live_enabled = True
            self.bot.send("Live monitoring enabled. Real Dhan orders will be placed on live signals.")
            return

        if cmd in ("/chain", "chain"):
            self.handle_chain()
        elif cmd in ("/status", "status"):
            self.handle_status()
        elif cmd in ("/position", "position"):
            LOG.info("Handling /position command | positions=%s", len(self.live_positions))
            self.bot.send(format_live_position_status(self.live_positions))
        elif cmd in ("/expiry", "expiry"):
            LOG.info("Handling /expiry command")
            self.bot.send(f"Current weekly expiry: <b>{self.ensure_expiry()}</b>")
        elif cmd in ("/help", "help", "/start", "start"):
            LOG.info("Handling /help command")
            self.bot.send(HELP_TEXT)
        else:
            LOG.warning("Unknown command | text=%s", text)
            self.bot.send(f"Unknown command: <code>{tg_escape(text)}</code>\n\n{HELP_TEXT}")

    def run(self) -> None:
        LOG.warning("Brahmastra bot run loop starting | symbol=%s | live_enabled=%s", NIFTY50_NAME, self.live_enabled)
        print(f"Brahmastra bot started | {NIFTY50_NAME}")
        self.bot.send(
            f"<b>{NIFTY50_NAME} Brahmastra Bot is online</b>\n\n"
            f"Live monitoring: {'ON' if self.live_enabled else 'OFF'}\n"
            f"Live orders: REAL DHAN ORDERS\n"
            f"Send <code>SCAN YYYY-MM-DD YYYY-MM-DD</code> for backtest.\n"
            f"Send <code>/help</code> for commands."
        )

        while True:
            try:
                for msg in self.bot.get_messages():
                    LOG.info("Telegram message received | msg=%s", msg)
                    print(f"Message: {msg}")
                    try:
                        self.dispatch(msg)
                    except Exception as exc:
                        LOG.exception("Command dispatch error | msg=%s", msg)
                        err = f"Error: {exc}"
                        print(err)
                        self.bot.send(tg_escape(err))

                try:
                    self.live_check()
                except Exception as exc:
                    LOG.exception("Live check error")
                    print(f"Live check error: {exc}")

                LOG.debug("Main loop sleeping | seconds=%s", self.cfg.tg_poll_interval)
                time.sleep(self.cfg.tg_poll_interval)

            except KeyboardInterrupt:
                LOG.warning("KeyboardInterrupt received; stopping bot")
                self.bot.send(f"{NIFTY50_NAME} Brahmastra Bot stopped.")
                print("Stopped.")
                return
            except requests.HTTPError as exc:
                LOG.exception("Main loop HTTP error")
                print(f"HTTP error: {exc}")
                time.sleep(5)
            except Exception as exc:
                LOG.exception("Main loop unexpected error")
                print(f"Error: {exc}")
                time.sleep(5)


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg)
    LOG.warning("Process starting | pid=%s | script=%s", os.getpid(), Path(__file__).resolve())
    BrahmastraBot(cfg).run()


if __name__ == "__main__":
    main()
