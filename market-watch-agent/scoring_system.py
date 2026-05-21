"""NIFTY50 signal bot with score-based BUY alerts and live auto orders.

What this program does
- NIFTY50 only
- Live monitoring runs continuously in the background
- Sends Telegram alerts only when a setup score is strong enough
- Places real Dhan orders on live alerts
- SCAN YYYY-MM-DD YYYY-MM-DD runs a backtest on a date range
- STOP cancels a running scan
- LIVE enables live monitoring again
- Uses IST timestamps
- Keeps Telegram command style similar to your reference bot

Scoring idea
- Candlestick pattern confirmation
- VWAP alignment
- Breakout / breakdown confirmation
- Volume confirmation
- Trend alignment
- PCR confirmation
- Candle body quality

Live order behavior
- Live alert sends the same signal notification first.
- Then the bot places a real BUY LIMIT order for the suggested option.
- After fill, it places a SELL STOP_LOSS order with trigger + limit price.
- At T1, it books 50%, moves SL to entry, and trails the rest.
- At T2 or square-off, it cancels SL and sends a SELL LIMIT exit.
- SCAN/backtest and manual strike snapshots do not place orders.

Important note
- This is a signal assistant, not financial advice.
- Historical scan uses historical candles, but option-chain suggestion is based
  on the current Dhan option chain snapshot.

Logging optional variables
- LOG_LEVEL=INFO
- LOG_DIR=logs
- LOG_FILE=nifty50_signal_order_bot.log
- LOG_TO_CONSOLE=true
- LOG_HTTP_PAYLOADS=false
- LOG_LIVE_SCAN_EVERY_SECONDS=0
"""

from __future__ import annotations

import dataclasses
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
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DHAN_BASE = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"

NIFTY50_SECURITY_ID = int((os.getenv("UNDERLYING_SECURITY_ID") or os.getenv("DHAN_UNDERLYING_SECURITY_ID") or "13").strip())
NIFTY50_SEGMENT = (os.getenv("UNDERLYING_SEGMENT") or os.getenv("DHAN_UNDERLYING_SEG") or "IDX_I").strip().upper()
NIFTY50_NAME = (os.getenv("INDEX_NAME") or "NIFTY 50").strip()
FNO_SEGMENT = (os.getenv("FNO_SEGMENT") or "NSE_FNO").strip().upper()
OPTION_INSTRUMENT = (os.getenv("OPTION_INSTRUMENT") or "OPTIDX").strip().upper()
IST = ZoneInfo("Asia/Kolkata")

SCAN_RE = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
STOP_RE = re.compile(r"^STOP$", re.IGNORECASE)
LIVE_RE = re.compile(r"^LIVE$", re.IGNORECASE)
STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)

# Score threshold for BUY alerts.
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "8"))
# Live polling interval in seconds. Keep >= 5 to stay comfortable with Dhan API rate limits.
TG_POLL_INTERVAL_DEFAULT = float(os.getenv("TG_POLL_INTERVAL") or os.getenv("DHAN_POLL_SECONDS") or "5")

ORDER_FILLED_STATUSES = {"TRADED"}
ORDER_DEAD_STATUSES = {"REJECTED", "CANCELLED", "EXPIRED"}

LOGGER_NAME = "nifty50_signal_order_bot"
LOG = logging.getLogger(LOGGER_NAME)
SENSITIVE_KEY_RE = re.compile(r"(token|access|authorization|password|secret|chat[_-]?id|client[_-]?id|clientid|dhanclientid)", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


@dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    http_timeout: int = 15
    tg_poll_interval: float = TG_POLL_INTERVAL_DEFAULT
    strikes_window: int = 5

    # Optional live-order settings.
    lot_size: int = 65
    lots: int = 2
    live_enabled_at_start: bool = False
    order_product_type: str = "MARGIN"
    entry_order_type: str = "LIMIT"
    exit_order_type: str = "LIMIT"
    sl_order_type: str = "STOP_LOSS"
    order_validity: str = "DAY"
    order_status_poll_attempts: int = 15
    order_status_poll_seconds: float = 1.0
    preferred_expiry: str = ""
    entry_limit_buffer: float = 1.0
    exit_limit_buffer: float = 1.0
    stop_loss_limit_buffer: float = 0.50
    option_sl_buffer: float = 0.50
    square_off_time: dt.time = dt.time(15, 20)
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "nifty50_signal_order_bot.log"
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

        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)).strip())
            except Exception:
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)).strip())
            except Exception:
                return default

        def _bool(name: str, default: bool) -> bool:
            value = os.getenv(name)
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}

        def _str(name: str, default: str) -> str:
            value = os.getenv(name)
            if value is None:
                return default
            value = value.strip()
            return value or default

        return Config(
            dhan_client_id=required["DHAN_CLIENT_ID"],
            dhan_access_token=required["DHAN_ACCESS_TOKEN"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            http_timeout=_int("HTTP_TIMEOUT", 15),
            tg_poll_interval=_float("TG_POLL_INTERVAL", _float("DHAN_POLL_SECONDS", TG_POLL_INTERVAL_DEFAULT)),
            strikes_window=_int("STRIKES_WINDOW", 5),
            lot_size=_int("LOT_SIZE", 65),
            lots=_int("LOTS", 2),
            live_enabled_at_start=_bool("LIVE_ENABLED", False),
            order_product_type=_str("ORDER_PRODUCT_TYPE", "MARGIN").upper(),
            entry_order_type=_str("ENTRY_ORDER_TYPE", "LIMIT").upper(),
            exit_order_type=_str("EXIT_ORDER_TYPE", "LIMIT").upper(),
            sl_order_type=_str("SL_ORDER_TYPE", "STOP_LOSS").upper(),
            order_validity=_str("ORDER_VALIDITY", "DAY").upper(),
            order_status_poll_attempts=_int("ORDER_STATUS_POLL_ATTEMPTS", 15),
            order_status_poll_seconds=_float("ORDER_STATUS_POLL_SECONDS", 1.0),
            preferred_expiry=_str("PREFERRED_EXPIRY", _str("DHAN_EXPIRY", "")),
            entry_limit_buffer=_float("ENTRY_LIMIT_BUFFER", 1.0),
            exit_limit_buffer=_float("EXIT_LIMIT_BUFFER", 1.0),
            stop_loss_limit_buffer=_float("STOP_LOSS_LIMIT_BUFFER", 0.50),
            option_sl_buffer=_float("OPTION_SL_BUFFER", 0.50),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            log_dir=_str("LOG_DIR", "logs"),
            log_file=_str("LOG_FILE", "nifty50_signal_order_bot.log"),
            log_to_console=_bool("LOG_TO_CONSOLE", True),
            log_http_payloads=_bool("LOG_HTTP_PAYLOADS", False),
            log_live_scan_every_seconds=_float("LOG_LIVE_SCAN_EVERY_SECONDS", 0.0),
        )


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


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    LOG.setLevel(level)
    LOG.propagate = False

    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
        handler.close()

    log_dir = Path(cfg.log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = Path(__file__).resolve().parent / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / cfg.log_file

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(name)s | %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    LOG.addHandler(file_handler)

    if cfg.log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        LOG.addHandler(console_handler)

    LOG.info(
        "Logging initialized | file=%s level=%s console=%s http_payloads=%s",
        log_path,
        cfg.log_level.upper(),
        cfg.log_to_console,
        cfg.log_http_payloads,
    )


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------


@dataclass
class Candle:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class IntradayContext:
    day_open: float
    day_high: float
    day_low: float
    last_close: float
    vwap: float
    trend: str
    candle_count: int
    last_candle: Candle
    intraday_support: float
    intraday_resistance: float
    prev_candle_high: float
    prev_candle_low: float
    recent_avg_volume: float


@dataclass
class OptionTradePlan:
    side: str
    strike: float
    option_security_id: Optional[int]
    option_ltp: float
    entry: float
    stop_loss: float
    target1: float
    target2: float
    risk: float
    reward1: float
    reward2: float
    rr1: float
    rr2: float


@dataclass
class Signal:
    timestamp: str
    underlying_symbol: str
    candle_time: str
    direction: str
    score: int
    max_score: int
    pattern_names: List[str]
    reasons: List[str]
    vwap: float
    spot: float
    pcr: Optional[float]
    support: Optional[float]
    resistance: Optional[float]
    option_plan: OptionTradePlan
    confidence: str

    def to_json(self) -> str:
        payload = dataclasses.asdict(self)
        return json.dumps(payload, indent=2, default=str)


@dataclass
class LivePosition:
    signal: Signal
    expiry: str
    opened_at: dt.datetime
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

    @property
    def plan(self) -> OptionTradePlan:
        return self.signal.option_plan


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
        LOG.debug("Dhan API client initialized | timeout=%s", cfg.http_timeout)

    @staticmethod
    def _response_body(r: requests.Response) -> str:
        try:
            return json.dumps(r.json(), ensure_ascii=True)
        except Exception:
            return (r.text or "").strip()

    def _raise_for_status(self, r: requests.Response, endpoint: str) -> None:
        if r.status_code < 400:
            return
        body = self._response_body(r)
        LOG.error(
            "Dhan API error | endpoint=%s status=%s body=%s",
            endpoint,
            r.status_code,
            log_json(body),
        )
        hint = ""
        if r.status_code == 401:
            hint = "\nHint: Dhan rejected authentication. Check DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN."
        if r.status_code == 403:
            hint = "\nHint: Dhan order APIs may require static IP whitelisting."
        raise requests.HTTPError(
            f"{r.status_code} Dhan error on {endpoint}. Response: {body or '-'}{hint}",
            response=r,
        )

    def expiry_list(self) -> List[str]:
        payload = {
            "UnderlyingScrip": NIFTY50_SECURITY_ID,
            "UnderlyingSeg": NIFTY50_SEGMENT,
        }
        if self.cfg.log_http_payloads:
            LOG.debug("Dhan request | endpoint=/optionchain/expirylist payload=%s", log_json(payload))
        r = self.session.post(
            f"{DHAN_BASE}/optionchain/expirylist",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, "/optionchain/expirylist")
        expiries = [str(x) for x in r.json().get("data", [])]
        LOG.info("Fetched expiry list | count=%s", len(expiries))
        return expiries

    def pick_expiry(self) -> str:
        expiries = self.expiry_list()
        if not expiries:
            raise RuntimeError("No expiry dates returned by Dhan.")

        preferred_expiry = self.cfg.preferred_expiry.strip()
        if preferred_expiry:
            for expiry in expiries:
                if expiry.strip() == preferred_expiry:
                    LOG.info("Picked preferred expiry from env | expiry=%s", expiry)
                    return expiry
            LOG.warning(
                "Preferred expiry from env was not in Dhan expiry list | preferred=%s | available=%s",
                preferred_expiry,
                expiries[:5],
            )

        today = dt.datetime.now(IST).date()

        def parse_expiry(x: str) -> dt.date:
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
                try:
                    return dt.datetime.strptime(x, fmt).date()
                except Exception:
                    continue
            return dt.date.max

        future = sorted(expiries, key=parse_expiry)
        for exp in future:
            d = parse_expiry(exp)
            if d >= today or d == dt.date.max:
                LOG.info("Picked expiry | expiry=%s", exp)
                return exp
        LOG.info("Picked fallback expiry | expiry=%s", future[0])
        return future[0]

    def option_chain(self, expiry: str) -> Dict[str, Any]:
        payload = {
            "UnderlyingScrip": NIFTY50_SECURITY_ID,
            "UnderlyingSeg": NIFTY50_SEGMENT,
            "Expiry": expiry,
        }
        if self.cfg.log_http_payloads:
            LOG.debug("Dhan request | endpoint=/optionchain payload=%s", log_json(payload))
        r = self.session.post(
            f"{DHAN_BASE}/optionchain",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, "/optionchain")
        data = r.json()
        chain_data = data.get("data", {}) if isinstance(data, dict) else {}
        oc = chain_data.get("oc") or {}
        LOG.info(
            "Fetched option chain | expiry=%s spot=%s strikes=%s",
            expiry,
            chain_data.get("last_price"),
            len(oc),
        )
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
        if self.cfg.log_http_payloads:
            LOG.debug("Dhan request | endpoint=/charts/intraday payload=%s", log_json(payload))
        LOG.debug(
            "Fetching intraday candles | security_id=%s segment=%s instrument=%s interval=%s from=%s to=%s",
            security_id,
            exchange_segment,
            instrument,
            interval,
            from_date,
            to_date,
        )
        r = self.session.post(
            f"{DHAN_BASE}/charts/intraday",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, "/charts/intraday")
        raw = r.json()
        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]
        if isinstance(raw, list):
            df = self._rows_to_df(raw)
            LOG.info(
                "Fetched intraday candles | security_id=%s rows=%s from=%s to=%s",
                security_id,
                len(df),
                from_date,
                to_date,
            )
            return df
        if isinstance(raw, dict):
            df = self._dict_to_df(raw)
            LOG.info(
                "Fetched intraday candles | security_id=%s rows=%s from=%s to=%s",
                security_id,
                len(df),
                from_date,
                to_date,
            )
            return df
        raise ValueError(f"Unexpected intraday response shape: {type(raw)}")

    def option_intraday(self, security_id: int, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        return self.intraday_candles(
            security_id=security_id,
            exchange_segment=FNO_SEGMENT,
            instrument=OPTION_INSTRUMENT,
            interval=5,
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            oi=True,
        )

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
            "correlationId": correlation_id or _make_correlation_id("ORD"),
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
        LOG.info(
            "Placing order | txn=%s segment=%s security_id=%s qty=%s type=%s product=%s price=%s trigger=%s correlation=%s",
            payload["transactionType"],
            payload["exchangeSegment"],
            security_id,
            quantity,
            payload["orderType"],
            payload["productType"],
            payload["price"],
            payload["triggerPrice"],
            payload["correlationId"],
        )
        if self.cfg.log_http_payloads:
            LOG.debug("Dhan request | endpoint=/orders payload=%s", log_json(payload))
        r = self.session.post(
            f"{DHAN_BASE}/orders",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, "/orders")
        data = r.json()
        if isinstance(data, dict):
            data["_request"] = payload
        LOG.info(
            "Order placed response | order_id=%s status=%s",
            _order_id(data) if isinstance(data, dict) else None,
            _order_status(data) if isinstance(data, dict) else "UNKNOWN",
        )
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
        LOG.info(
            "Modifying order | order_id=%s qty=%s type=%s price=%s trigger=%s",
            order_id,
            quantity,
            payload["orderType"],
            payload["price"],
            payload["triggerPrice"],
        )
        if self.cfg.log_http_payloads:
            LOG.debug("Dhan request | endpoint=/orders/%s payload=%s", order_id, log_json(payload))
        r = self.session.put(
            f"{DHAN_BASE}/orders/{order_id}",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, f"/orders/{order_id}")
        data = r.json()
        if isinstance(data, dict):
            data["_request"] = payload
        LOG.info(
            "Order modify response | order_id=%s status=%s",
            _order_id(data) if isinstance(data, dict) else order_id,
            _order_status(data) if isinstance(data, dict) else "UNKNOWN",
        )
        return data

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        LOG.info("Cancelling order | order_id=%s", order_id)
        r = self.session.delete(
            f"{DHAN_BASE}/orders/{order_id}",
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, f"/orders/{order_id}")
        if not (r.text or "").strip():
            data = {"orderId": str(order_id), "orderStatus": "CANCELLED"}
            LOG.info("Order cancel response | order_id=%s status=%s", order_id, _order_status(data))
            return data
        data = r.json()
        LOG.info("Order cancel response | order_id=%s status=%s", order_id, _order_status(data))
        return data

    def get_order(self, order_id: str) -> Dict[str, Any]:
        r = self.session.get(
            f"{DHAN_BASE}/orders/{order_id}",
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, f"/orders/{order_id}")
        data = r.json()
        LOG.debug(
            "Fetched order | order_id=%s status=%s filled=%s remaining=%s",
            order_id,
            _order_status(data),
            _order_filled_qty(data),
            _order_remaining_qty(data),
        )
        return data

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
            raise ValueError("Intraday data has no timestamp column.")

        ts = pd.to_numeric(df["timestamp"], errors="coerce")
        if ts.notna().any():
            sample = float(ts.dropna().median())
            unit = "ms" if sample > 1_000_000_000_000 else "s"
            df["timestamp"] = (
                pd.to_datetime(ts, unit=unit, utc=True)
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

        df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"]).sort_values("timestamp")
        return df.drop_duplicates("timestamp", keep="last").reset_index(drop=True)


class TelegramBot:
    def __init__(self, token: str, chat_id: str, timeout: int = 15):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.session = requests.Session()
        self._offset = 0
        LOG.debug("Telegram client initialized | timeout=%s", timeout)

    def send(self, text: str) -> None:
        LOG.debug("Sending Telegram message | chars=%s", len(text))
        self.session.post(
            f"{TELEGRAM_BASE}/bot{self.token}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=self.timeout,
        ).raise_for_status()
        LOG.info("Telegram message sent | chars=%s", len(text))

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
            LOG.info("Telegram messages received | count=%s commands=%s", len(texts), texts)
        return texts


# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------


def _num(v: Any, default: float = 0.0) -> float:
    try:
        out = float(v) if v is not None else default
        if math.isnan(out):
            return default
        return out
    except Exception:
        return default


def _price_tick(value: float) -> float:
    return round(max(float(value), 0.05), 2)


def _fmt(v: Any, decimals: int = 2) -> str:
    return "—" if v is None else f"{float(v):,.{decimals}f}"


def _escape(v: Any) -> str:
    return html.escape(str(v), quote=False)


def _today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def _now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _market_session_open(now: Optional[dt.datetime] = None) -> bool:
    now = now or _now_ist()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt.time(9, 15) <= t <= dt.time(15, 30)


def _day_start_end(day: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    return (
        dt.datetime.combine(day, dt.time(9, 15)),
        dt.datetime.combine(day, dt.time(15, 30)),
    )


def _closed_candles_only(df: pd.DataFrame, interval_minutes: int = 5, at_time: Optional[dt.datetime] = None) -> pd.DataFrame:
    if df.empty:
        return df
    at_time = (at_time or _now_ist()).replace(tzinfo=None)
    latest_allowed_end = at_time - dt.timedelta(seconds=5)
    candle_end = df["timestamp"] + pd.to_timedelta(interval_minutes, unit="m")
    return df[candle_end <= latest_allowed_end].reset_index(drop=True)


def _make_correlation_id(kind: str) -> str:
    stamp = _now_ist().strftime("%y%m%d%H%M%S")
    tail = int(time.time() * 1000) % 100000
    raw = f"N50{stamp}{kind.upper()}{tail}"
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:30]


def _order_status(raw: Dict[str, Any]) -> str:
    return str(raw.get("orderStatus") or raw.get("status") or "UNKNOWN").upper()


def _order_id(raw: Dict[str, Any]) -> Optional[str]:
    value = raw.get("orderId") or raw.get("order_id")
    return str(value) if value is not None else None


def _order_filled_qty(raw: Dict[str, Any]) -> int:
    return int(_num(raw.get("filledQty") or raw.get("filledQuantity") or raw.get("tradedQuantity"), 0))


def _order_remaining_qty(raw: Dict[str, Any]) -> int:
    return int(_num(raw.get("remainingQuantity"), 0))


def _order_average_price(raw: Dict[str, Any]) -> float:
    return _num(raw.get("averageTradedPrice") or raw.get("avgPrice") or raw.get("price"), 0.0)


def _nearest_strike(strikes: List[float], spot: float) -> float:
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else 0.0


def _infer_step(strikes: List[float]) -> int:
    if len(strikes) < 2:
        return 50
    diffs = sorted(abs(b - a) for a, b in zip(strikes[:-1], strikes[1:]) if abs(b - a) > 0)
    if not diffs:
        return 50
    return int(round(statistics.median(diffs))) or 50


def _get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
    return (
        oc.get(f"{strike:.6f}")
        or oc.get(f"{strike:.2f}")
        or oc.get(f"{strike:.0f}")
        or oc.get(str(int(strike)))
        or {}
    )


def _support_resistance_oi(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    support = None
    resistance = None
    best_pe = -1.0
    best_ce = -1.0
    for k, row in oc.items():
        try:
            strike = float(k)
        except Exception:
            continue
        pe_oi = _num((row.get("pe") or {}).get("oi"))
        ce_oi = _num((row.get("ce") or {}).get("oi"))
        if pe_oi > best_pe:
            best_pe = pe_oi
            support = strike
        if ce_oi > best_ce:
            best_ce = ce_oi
            resistance = strike
    return support, resistance


def _pcr(oc: Dict[str, Any], center: float, window: int) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        return None
    band = sorted(strikes, key=lambda s: abs(s - center))[: max(2, window * 2)]
    call_oi = 0.0
    put_oi = 0.0
    for strike in band:
        row = _get_row(oc, strike)
        call_oi += _num((row.get("ce") or {}).get("oi"))
        put_oi += _num((row.get("pe") or {}).get("oi"))
    return (put_oi / call_oi) if call_oi > 0 else None


def compute_pcr_from_chain(chain_json: Dict[str, Any]) -> Optional[float]:
    """Return overall PCR from the full option-chain snapshot."""
    try:
        data = chain_json.get("data", chain_json)
        oc: Dict[str, Any] = data.get("oc") or {}
        if not oc:
            return None

        ce_oi = 0.0
        pe_oi = 0.0
        for row in oc.values():
            ce_oi += _num((row.get("ce") or {}).get("oi"))
            pe_oi += _num((row.get("pe") or {}).get("oi"))

        if ce_oi <= 0:
            return None
        return pe_oi / ce_oi
    except Exception:
        return None


def _max_pain(oc: Dict[str, Any]) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        return None
    oi_map = {
        s: (
            _num((_get_row(oc, s)).get("ce", {}).get("oi")),
            _num((_get_row(oc, s)).get("pe", {}).get("oi")),
        )
        for s in strikes
    }
    best = None
    best_pain = None
    for settle in strikes:
        pain = sum(max(0.0, settle - s) * ce + max(0.0, s - settle) * pe for s, (ce, pe) in oi_map.items())
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best = settle
    return best


def _top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
    items: List[Tuple[float, float]] = []
    for k, v in oc.items():
        try:
            items.append((float(k), _num(v.get(side, {}).get("oi"))))
        except Exception:
            continue
    return sorted(items, key=lambda x: x[1], reverse=True)[:n]


def _strikes_around_atm(oc: Dict[str, Any], spot: float, window: int) -> List[Tuple[float, Dict[str, Any]]]:
    all_strikes = sorted(float(k) for k in oc.keys())
    if not all_strikes:
        return []
    atm = _nearest_strike(all_strikes, spot)
    nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: window * 2 + 1]
    return [(s, _get_row(oc, s)) for s in sorted(nearby)]


def _calc_vwap(candles: List[Candle]) -> float:
    num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in candles)
    den = sum(c.volume for c in candles)
    return (num / den) if den > 0 else 0.0


def _analyse_intraday(candles: List[Candle]) -> Optional[IntradayContext]:
    if not candles:
        return None

    day_open = candles[0].open
    day_high = max(c.high for c in candles)
    day_low = min(c.low for c in candles)
    last_close = candles[-1].close
    vwap = _calc_vwap(candles)

    if last_close > vwap and last_close > day_open:
        trend = "uptrend"
    elif last_close < vwap and last_close < day_open:
        trend = "downtrend"
    else:
        trend = "sideways"

    recent = candles[-6:] if len(candles) >= 6 else candles
    intraday_support = min(c.low for c in recent)
    intraday_resistance = max(c.high for c in recent)
    prev_candle = candles[-2] if len(candles) >= 2 else candles[-1]
    recent_avg_volume = float(sum(c.volume for c in recent) / max(len(recent), 1))

    return IntradayContext(
        day_open=day_open,
        day_high=day_high,
        day_low=day_low,
        last_close=last_close,
        vwap=vwap,
        trend=trend,
        candle_count=len(candles),
        last_candle=candles[-1],
        intraday_support=intraday_support,
        intraday_resistance=intraday_resistance,
        prev_candle_high=prev_candle.high,
        prev_candle_low=prev_candle.low,
        recent_avg_volume=recent_avg_volume,
    )


# -----------------------------------------------------------------------------
# Candlestick patterns
# -----------------------------------------------------------------------------


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


def _pattern_function_names() -> List[str]:
    try:
        import talib  # type: ignore

        return sorted([n for n in dir(talib) if n.startswith("CDL") and callable(getattr(talib, n))])
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
        for name in _pattern_function_names():
            fn = getattr(talib, name)
            try:
                out = fn(open_, high, low, close)
                if len(out) and int(out[-1]) != 0:
                    matches.append(name)
            except Exception:
                continue
        if matches:
            LOG.debug("Detected TA-Lib patterns | candle=%s patterns=%s", df.iloc[-1].timestamp, matches)
        return matches
    except Exception:
        LOG.debug("TA-Lib unavailable or failed; using fallback pattern detection")
        last = df.iloc[-1]
        prev = df.iloc[-2]
        body = abs(last.close - last.open)
        rng = max(last.high - last.low, 1e-9)
        upper = last.high - max(last.open, last.close)
        lower = min(last.open, last.close) - last.low
        matches: List[str] = []

        if lower >= 2 * body and upper <= body * 0.3:
            matches.append("CDLHAMMER")
        if upper >= 2 * body and lower <= body * 0.3:
            matches.append("CDLSHOOTINGSTAR")
        if last.close > last.open and prev.close < prev.open and last.close >= prev.open and last.open <= prev.close:
            matches.append("CDLENGULFING")
        if last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
            matches.append("CDLENGULFING")
        if body / rng <= 0.1:
            matches.append("CDLDOJI")
        if matches:
            LOG.debug("Detected fallback patterns | candle=%s patterns=%s", df.iloc[-1].timestamp, matches)
        return matches


def infer_direction(patterns: List[str]) -> str:
    bullish = sum(1 for p in patterns if p in BULLISH_PATTERNS)
    bearish = sum(1 for p in patterns if p in BEARISH_PATTERNS)
    if bullish > bearish:
        return "BULLISH"
    if bearish > bullish:
        return "BEARISH"
    return "NEUTRAL"


# -----------------------------------------------------------------------------
# Scoring + trade planning
# -----------------------------------------------------------------------------


def score_setup(
    candles: pd.DataFrame,
    chain: Dict[str, Any],
    ctx: IntradayContext,
    patterns: List[str],
) -> Tuple[str, int, int, List[str]]:
    """Return (direction, score, max_score, reasons)."""
    max_score = 11
    reasons: List[str] = []

    direction = infer_direction(patterns)
    if direction == "NEUTRAL":
        return direction, 0, max_score, reasons

    bullish = direction == "BULLISH"
    last = candles.iloc[-1]
    prev = candles.iloc[-2] if len(candles) >= 2 else candles.iloc[-1]

    score = 0

    side_patterns = [p for p in patterns if (p in BULLISH_PATTERNS if bullish else p in BEARISH_PATTERNS)]
    if side_patterns:
        score += 3
        reasons.append(f"Pattern confirmation: {', '.join(side_patterns[:3])}")

    if bullish and last.close > ctx.vwap:
        score += 2
        reasons.append("Price above VWAP")
    elif (not bullish) and last.close < ctx.vwap:
        score += 2
        reasons.append("Price below VWAP")

    if bullish and last.close > prev.high:
        score += 2
        reasons.append("Close above previous high")
    elif (not bullish) and last.close < prev.low:
        score += 2
        reasons.append("Close below previous low")

    if ctx.recent_avg_volume > 0 and last.volume >= ctx.recent_avg_volume * 1.15:
        score += 1
        reasons.append("Volume above recent average")

    if bullish and ctx.trend == "uptrend":
        score += 1
        reasons.append("Trend aligned to upside")
    elif (not bullish) and ctx.trend == "downtrend":
        score += 1
        reasons.append("Trend aligned to downside")

    candle_range = max(last.high - last.low, 1e-9)
    body = abs(last.close - last.open)
    body_ratio = body / candle_range
    if body_ratio >= 0.55:
        score += 1
        reasons.append("Strong candle body")

    pcr = compute_pcr_from_chain(chain)
    if bullish and pcr is not None and pcr > 1.05:
        score += 1
        reasons.append(f"PCR bullish ({pcr:.2f})")
    elif (not bullish) and pcr is not None and pcr < 0.95:
        score += 1
        reasons.append(f"PCR bearish ({pcr:.2f})")

    return direction, min(score, max_score), max_score, reasons


def _signal_confidence(score: int, max_score: int) -> str:
    pct = (score / max_score) * 100 if max_score > 0 else 0
    if pct >= 85:
        return "Strong"
    if pct >= 70:
        return "Good"
    return "Weak"


def _choose_strike(spot: float, strikes: List[float], side: str, score: int) -> float:
    """Choose ATM or slightly ITM for stronger scores."""
    if not strikes:
        return round(spot / 50.0) * 50

    step = _infer_step(strikes)
    atm = _nearest_strike(strikes, spot)

    if score >= 10:
        if side == "CE":
            target = atm - step
            candidates = [s for s in strikes if s <= target]
            return max(candidates) if candidates else atm
        target = atm + step
        candidates = [s for s in strikes if s >= target]
        return min(candidates) if candidates else atm

    return atm


def _option_trade_plan(
    chain: Dict[str, Any],
    spot: float,
    side: str,
    score: int,
) -> Tuple[float, Optional[int], float, OptionTradePlan]:
    data = chain.get("data", chain)
    oc: Dict[str, Any] = data.get("oc") or {}
    strikes = sorted(float(k) for k in oc.keys())
    strike = _choose_strike(spot, strikes, side, score)
    if not oc:
        key = str(int(strike))
        row: Dict[str, Any] = {}
    else:
        key = min(oc.keys(), key=lambda k: abs(float(k) - strike))
        row = oc.get(key) or {}
    opt = row.get("ce" if side == "CE" else "pe") or {}

    option_security_id = opt.get("security_id") or opt.get("securityId")
    option_ltp = _num(opt.get("last_price"), default=0.0)
    if option_ltp <= 0:
        option_ltp = max(1.0, round(abs(spot - strike) / 4.0, 2))

    sl_pct = 0.15 if score >= 10 else 0.20
    entry = option_ltp
    stop_loss = round(max(entry * (1 - sl_pct), 1.0), 2)
    risk = round(max(entry - stop_loss, 0.01), 2)
    target1 = round(entry + risk * 1.0, 2)
    target2 = round(entry + risk * 2.0, 2)
    reward1 = round(target1 - entry, 2)
    reward2 = round(target2 - entry, 2)
    rr1 = round(reward1 / risk, 1) if risk > 0 else 0.0
    rr2 = round(reward2 / risk, 1) if risk > 0 else 0.0

    plan = OptionTradePlan(
        side=side,
        strike=float(strike),
        option_security_id=int(float(option_security_id)) if option_security_id is not None else None,
        option_ltp=option_ltp,
        entry=entry,
        stop_loss=stop_loss,
        target1=target1,
        target2=target2,
        risk=risk,
        reward1=reward1,
        reward2=reward2,
        rr1=rr1,
        rr2=rr2,
    )
    return strike, option_security_id, option_ltp, plan


# -----------------------------------------------------------------------------
# Signal builder
# -----------------------------------------------------------------------------


def build_signal(
    expiry: str,
    candles: pd.DataFrame,
    idx: int,
    chain: Dict[str, Any],
) -> Optional[Signal]:
    _ = expiry
    window = candles.iloc[: idx + 1].reset_index(drop=True)
    if len(window) < 5:
        return None

    patterns = detect_patterns(window)
    if not patterns:
        return None

    candle_list = [
        Candle(
            ts=(row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in window.itertuples(index=False)
    ]
    ctx = _analyse_intraday(candle_list)
    if ctx is None:
        return None

    direction, score, max_score, reasons = score_setup(window, chain, ctx, patterns)
    if direction == "NEUTRAL":
        LOG.debug(
            "Signal rejected | candle=%s reason=neutral_direction patterns=%s",
            ctx.last_candle.ts,
            patterns,
        )
        return None
    if score < MIN_SIGNAL_SCORE:
        LOG.debug(
            "Signal rejected | candle=%s direction=%s score=%s/%s threshold=%s patterns=%s",
            ctx.last_candle.ts,
            direction,
            score,
            max_score,
            MIN_SIGNAL_SCORE,
            patterns,
        )
        return None

    bullish = direction == "BULLISH"
    side = "CE" if bullish else "PE"
    spot = float(window.iloc[-1].close)
    pcr = compute_pcr_from_chain(chain)
    data = chain.get("data", chain)
    oc: Dict[str, Any] = data.get("oc") or {}
    support, resistance = _support_resistance_oi(oc)
    _, _, _, option_plan = _option_trade_plan(chain, spot, side, score)

    confidence = _signal_confidence(score, max_score)
    LOG.info(
        "Signal accepted | candle=%s direction=%s score=%s/%s confidence=%s side=%s strike=%s option_id=%s",
        ctx.last_candle.ts,
        direction,
        score,
        max_score,
        confidence,
        side,
        option_plan.strike,
        option_plan.option_security_id,
    )

    return Signal(
        timestamp=_now_ist().isoformat(timespec="seconds"),
        underlying_symbol=NIFTY50_NAME,
        candle_time=str(ctx.last_candle.ts),
        direction=direction,
        score=score,
        max_score=max_score,
        pattern_names=patterns,
        reasons=reasons,
        vwap=ctx.vwap,
        spot=spot,
        pcr=pcr,
        support=support,
        resistance=resistance,
        option_plan=option_plan,
        confidence=confidence,
    )


# -----------------------------------------------------------------------------
# Telegram formatting
# -----------------------------------------------------------------------------


def format_signal(signal: Signal) -> str:
    emoji = "🟢" if signal.direction == "BULLISH" else "🔴"
    reasons_text = "\n".join(f"• {r}" for r in signal.reasons[:7]) or "• No extra reasons"
    patterns_text = ", ".join(signal.pattern_names)
    plan = signal.option_plan

    lines = [
        f"{emoji} <b>NIFTY50 BUY SIGNAL</b>",
        f"<b>Confidence:</b> {signal.confidence}",
        f"<b>Score:</b> {signal.score}/{signal.max_score}",
        f"<b>Candle Time:</b> {signal.candle_time}",
        f"<b>Direction:</b> {signal.direction}",
        f"<b>Patterns:</b> {patterns_text}",
        "",
        "━━ Why this signal ━━",
        reasons_text,
        "",
        "━━ Market Context ━━",
        f"Spot Price : ₹{signal.spot:,.2f}",
        f"VWAP       : ₹{signal.vwap:,.2f}",
        f"PCR        : {signal.pcr if signal.pcr is not None else '—'}",
        f"Support    : {_fmt(signal.support, 0)}",
        f"Resistance : {_fmt(signal.resistance, 0)}",
        "",
        "━━ Suggested Buy ━━",
        f"Option Side : {plan.side}",
        f"Strike      : {_fmt(plan.strike, 0)}",
        f"Option LTP  : ₹{plan.option_ltp:,.2f}",
        f"Entry       : ₹{plan.entry:,.2f}",
        f"Stop Loss   : ₹{plan.stop_loss:,.2f}",
        f"Target 1    : ₹{plan.target1:,.2f}",
        f"Target 2    : ₹{plan.target2:,.2f}",
        f"Risk        : ₹{plan.risk:,.2f}",
        f"Reward T1   : ₹{plan.reward1:,.2f}  |  R:R = 1:{plan.rr1}",
        f"Reward T2   : ₹{plan.reward2:,.2f}  |  R:R = 1:{plan.rr2}",
        f"Option ID   : {plan.option_security_id if plan.option_security_id is not None else '—'}",
        "",
        f"<i>Updated: {_now_ist().strftime('%H:%M:%S %Z')}</i>",
    ]
    return "\n".join(lines)


def format_order_update(title: str, position: Optional[LivePosition], order: Optional[Dict[str, Any]], note: Optional[str] = None) -> str:
    plan = position.plan if position else None
    lines = [f"<b>{_escape(title)}</b>"]
    if position and plan:
        lines += [
            f"Option      : {plan.side} {_fmt(plan.strike, 0)}",
            f"Expiry      : {_escape(position.expiry)}",
            f"Security ID : {plan.option_security_id}",
        ]
    if order:
        req = order.get("_request", {}) if isinstance(order, dict) else {}
        lines += [
            f"Order ID    : {_escape(order.get('orderId') or order.get('order_id') or '-')}",
            f"Status      : {_escape(_order_status(order))}",
        ]
        txn = order.get("transactionType") or req.get("transactionType")
        qty = order.get("quantity") or req.get("quantity")
        filled = order.get("filledQty")
        avg = order.get("averageTradedPrice")
        limit_price = order.get("price") or req.get("price")
        trigger = order.get("triggerPrice") or req.get("triggerPrice")
        if txn:
            lines.append(f"Txn         : {_escape(txn)}")
        if qty:
            lines.append(f"Qty         : {_escape(qty)}")
        if filled is not None:
            lines.append(f"Filled Qty  : {_escape(filled)}")
        if avg is not None:
            lines.append(f"Avg Price   : ₹{_fmt(avg)}")
        if limit_price:
            lines.append(f"Limit Price : ₹{_fmt(limit_price)}")
        if trigger:
            lines.append(f"Trigger     : ₹{_fmt(trigger)}")
        if order.get("omsErrorDescription"):
            lines.append(f"Error       : {_escape(order.get('omsErrorDescription'))}")
    if position:
        lines += [
            f"Remaining   : {position.remaining_qty}",
            f"Current SL  : ₹{_fmt(position.current_sl)}",
        ]
    if note:
        lines += ["", _escape(note)]
    lines.append(f"<i>Updated: {_now_ist().strftime('%H:%M:%S %Z')}</i>")
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
        f"<b>Action:</b> {_escape(action)}",
        f"<b>Reason:</b> {_escape(reason)}",
        f"Time        : {_escape(event_time)}",
        f"Option      : {plan.side} {_fmt(plan.strike, 0)}",
        f"Entry       : ₹{_fmt(plan.entry)}",
        f"Current SL  : ₹{_fmt(position.current_sl)}",
        f"Target 1    : ₹{_fmt(plan.target1)}",
        f"Target 2    : ₹{_fmt(plan.target2)}",
    ]
    if position.entry_order_id:
        lines.append(f"Entry Order : {_escape(position.entry_order_id)}")
    if position.stop_order_id:
        lines.append(f"SL Order    : {_escape(position.stop_order_id)}")
    if price is not None:
        lines.append(f"Ref Price   : ₹{_fmt(price)}")
    lines.append(f"Remaining   : {position.remaining_qty} qty")
    if note:
        lines += ["", _escape(note)]
    return "\n".join(lines)


def format_live_position_status(positions: List[LivePosition]) -> str:
    if not positions:
        return "No active live positions being tracked."

    lines = ["<b>Active Live Positions</b>", f"Count: {len(positions)}"]
    for i, position in enumerate(positions, start=1):
        plan = position.plan
        lines += [
            "",
            f"<b>#{i} {plan.side} {_fmt(plan.strike, 0)}</b>",
            f"Expiry      : {_escape(position.expiry)}",
            f"Security ID : {plan.option_security_id}",
            f"Entry       : ₹{_fmt(plan.entry)}",
            f"Entry Order : {_escape(position.entry_order_id or '-')}",
            f"Entry Status: {_escape(position.entry_order_status)}",
            f"SL Order    : {_escape(position.stop_order_id or '-')}",
            f"SL Status   : {_escape(position.stop_order_status)}",
            f"Current SL  : ₹{_fmt(position.current_sl)}",
            f"Target 1    : ₹{_fmt(plan.target1)}",
            f"Target 2    : ₹{_fmt(plan.target2)}",
            f"T1 Hit      : {'yes' if position.t1_hit else 'no'}",
            f"Remaining   : {position.remaining_qty} qty",
            f"Opened at   : {_escape(position.opened_at)}",
        ]
    return "\n".join(lines)


def format_chain_message(rows: List[Tuple[float, Dict[str, Any]]], spot: float, expiry: str, support: Optional[float], resistance: Optional[float], atm: float, pcr: Optional[float], max_pain: Optional[float]) -> str:
    bias = "neutral"
    if pcr is not None:
        bias = "bearish" if pcr < 0.9 else ("bullish" if pcr > 1.1 else "neutral")
    lines = [
        f"<b>{NIFTY50_NAME} — Option Chain</b>",
        f"Expiry: {expiry}  |  Spot: {_fmt(spot)}  |  ATM: {_fmt(atm, 0)}",
        f"PCR: {_fmt(pcr)}  |  Bias: {bias}  |  Max Pain: {_fmt(max_pain, 0)}",
        f"Support S: {_fmt(support, 0)}   |   Resistance R: {_fmt(resistance, 0)}",
        "",
        "<b>±5 strikes around ATM</b>",
        "<pre>",
        f"{'Strike':<8} {'Tag':<7} {'CE OI':>9} {'CE LTP':>7} | {'PE LTP':>7} {'PE OI':>9}",
        "-" * 54,
    ]
    for strike, row in rows:
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        tag = ("ATM" if strike == atm else "") + (" S" if strike == support else "") + (" R" if strike == resistance else "")
        lines.append(
            f"{strike:<8,.0f} {tag.strip():<7} {_num(ce.get('oi')):>9,.0f} {_num(ce.get('last_price')):>7.2f} | {_num(pe.get('last_price')):>7.2f} {_num(pe.get('oi')):>9,.0f}"
        )
    lines += ["</pre>", "S=Support  R=Resistance  ATM=At-the-money", f"<i>Updated: {_now_ist().strftime('%H:%M:%S %Z')}</i>"]
    return "\n".join(lines)


def format_status_message(spot: float, expiry: str, support: Optional[float], resistance: Optional[float], atm: float, pcr: Optional[float], max_pain: Optional[float], call_top: List[Tuple[float, float]], put_top: List[Tuple[float, float]]) -> str:
    bias = "neutral"
    if pcr is not None:
        bias = "bearish pressure" if pcr < 0.9 else ("bullish pressure" if pcr > 1.1 else "neutral")
    lines = [
        f"<b>{NIFTY50_NAME} Weekly Watch</b> | Expiry {expiry}",
        f"Spot: {_fmt(spot)} | ATM: {_fmt(atm, 0)} | PCR: {_fmt(pcr)}",
        f"Bias: {bias}",
        f"Support: {_fmt(support, 0)} | Resistance: {_fmt(resistance, 0)} | Max Pain: {_fmt(max_pain, 0)}",
    ]
    if call_top:
        lines.append("Top Call OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in call_top))
    if put_top:
        lines.append("Top Put OI:  " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in put_top))
    lines.append(f"<i>Updated: {_now_ist().strftime('%H:%M:%S %Z')}</i>")
    return "\n".join(lines)


HELP_TEXT = f"""<b>{NIFTY50_NAME} Bot Commands</b>

<b>Backtest scan</b>
  SCAN 2026-05-01 2026-05-14

<b>Live control</b>
  LIVE   — enable live monitoring and real auto orders
  STOP   — stop running scan
  /position — active live order status

<b>Market overview</b>
  /chain   — ±5 strike option chain table
  /status  — Spot, PCR, bias, support, resistance
  /expiry  — Current weekly expiry
  /help    — This help message

Alerts are sent only when score >= {MIN_SIGNAL_SCORE}.
Live alerts place real Dhan intraday orders.
"""


# -----------------------------------------------------------------------------
# Main engine
# -----------------------------------------------------------------------------


class MarketWatchAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = DhanApiClient(cfg)
        self.bot = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        self.expiry: Optional[str] = None

        self.live_enabled = cfg.live_enabled_at_start
        self.last_live_key: Optional[str] = None
        self.last_live_candle_time: Optional[str] = None
        self.live_positions: List[LivePosition] = []

        self.scan_thread: Optional[threading.Thread] = None
        self.scan_stop_event = threading.Event()
        self.scan_lock = threading.Lock()
        self._last_live_scan_log_at: Optional[float] = None
        LOG.info(
            "MarketWatchAgent initialized | live_enabled=%s quantity=%s threshold=%s poll_interval=%s",
            self.live_enabled,
            self.cfg.quantity,
            MIN_SIGNAL_SCORE,
            self.cfg.tg_poll_interval,
        )

    def _ensure_expiry(self) -> str:
        if self.expiry is None:
            self.expiry = self.api.pick_expiry()
            LOG.info("Cached active expiry | expiry=%s", self.expiry)
        return self.expiry

    def _fetch_chain(self) -> Tuple[str, float, Dict[str, Any]]:
        expiry = self._ensure_expiry()
        snapshot = self.api.option_chain(expiry)
        data = snapshot.get("data", {})
        spot = _num(data.get("last_price"))
        oc: Dict[str, Any] = data.get("oc") or {}
        if not oc:
            LOG.warning("Fetched empty option chain | expiry=%s spot=%s", expiry, spot)
            raise RuntimeError("Empty option chain — market may be closed.")
        LOG.info("Chain snapshot ready | expiry=%s spot=%s strikes=%s", expiry, spot, len(oc))
        return expiry, spot, oc

    def _fetch_intraday(self, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        df = self.api.intraday_candles(
            security_id=NIFTY50_SECURITY_ID,
            exchange_segment=NIFTY50_SEGMENT,
            instrument="INDEX",
            interval=5,
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            oi=True,
        )
        LOG.debug("Index intraday fetched | rows=%s start=%s end=%s", len(df), start, end)
        return df

    def _current_intraday(self) -> pd.DataFrame:
        now = _now_ist()
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return self._fetch_intraday(start, end)

    # Signal helpers

    def _evaluate_dataframe(self, candles: pd.DataFrame, expiry: str, chain: Dict[str, Any], idx: int) -> Optional[Signal]:
        return build_signal(expiry, candles, idx, chain)

    def _send_signal(self, signal: Signal, prefix: str = "") -> None:
        msg = format_signal(signal)
        if prefix:
            msg = f"{prefix}\n\n{msg}"
        LOG.info(
            "Sending signal alert | prefix=%s candle=%s direction=%s score=%s/%s option=%s %s",
            prefix or "-",
            signal.candle_time,
            signal.direction,
            signal.score,
            signal.max_score,
            signal.option_plan.side,
            signal.option_plan.strike,
        )
        self.bot.send(msg)

    # Live order helpers

    def _wait_for_order_update(self, order_id_value: str, attempts: Optional[int] = None) -> Dict[str, Any]:
        attempts = attempts or self.cfg.order_status_poll_attempts
        LOG.info("Waiting for order update | order_id=%s attempts=%s", order_id_value, attempts)
        latest: Dict[str, Any] = {"orderId": order_id_value, "orderStatus": "UNKNOWN"}
        for attempt in range(max(1, attempts)):
            latest = self.api.get_order(order_id_value)
            status = _order_status(latest)
            LOG.debug(
                "Order poll | order_id=%s attempt=%s/%s status=%s filled=%s remaining=%s",
                order_id_value,
                attempt + 1,
                attempts,
                status,
                _order_filled_qty(latest),
                _order_remaining_qty(latest),
            )
            if status in ORDER_FILLED_STATUSES or status in ORDER_DEAD_STATUSES:
                LOG.info("Order reached terminal status | order_id=%s status=%s", order_id_value, status)
                return latest
            if status == "PART_TRADED" and _order_remaining_qty(latest) == 0:
                LOG.info("Order part-traded with zero remaining | order_id=%s", order_id_value)
                return latest
            time.sleep(max(0.1, self.cfg.order_status_poll_seconds))
        LOG.warning("Order polling ended before terminal status | order_id=%s status=%s", order_id_value, _order_status(latest))
        return latest

    def _place_market_exit(self, position: LivePosition, quantity: int, reason: str, ref_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if quantity <= 0:
            LOG.info("Market exit skipped | reason=%s quantity=%s", reason, quantity)
            return None
        exit_order_type = self.cfg.exit_order_type.upper()
        exit_price = 0.0
        if exit_order_type == "LIMIT":
            base_price = ref_price or position.entry_avg_price or position.plan.entry
            exit_price = _price_tick(base_price - self.cfg.exit_limit_buffer)
        LOG.info(
            "Placing exit | reason=%s security_id=%s quantity=%s remaining_before=%s type=%s price=%s ref_price=%s",
            reason,
            position.plan.option_security_id,
            quantity,
            position.remaining_qty,
            exit_order_type,
            exit_price,
            ref_price,
        )
        order = self.api.place_order(
            transaction_type="SELL",
            security_id=int(position.plan.option_security_id or 0),
            quantity=quantity,
            order_type=exit_order_type,
            product_type=self.cfg.order_product_type,
            validity=self.cfg.order_validity,
            price=exit_price,
            trigger_price=0.0,
            correlation_id=_make_correlation_id("EXIT"),
        )
        status = _order_status(order)
        exit_order_id = _order_id(order)
        position.last_exit_order_id = exit_order_id
        self.bot.send(format_order_update(f"EXIT ORDER PLACED - {reason}", position, order))
        if exit_order_id:
            latest = self._wait_for_order_update(exit_order_id, attempts=5)
            latest_status = _order_status(latest)
            if latest_status != status:
                self.bot.send(format_order_update(f"EXIT ORDER UPDATE - {reason}", position, latest))
            LOG.info(
                "Market exit update | reason=%s order_id=%s status=%s",
                reason,
                exit_order_id,
                latest_status,
            )
            return latest
        return order

    def _place_or_modify_stop_order(self, position: LivePosition, quantity: int, trigger_price: float, reason: str) -> None:
        trigger_price = _price_tick(trigger_price)
        sl_order_type = self.cfg.sl_order_type.upper()
        sl_limit_price = 0.0
        if sl_order_type == "STOP_LOSS":
            sl_limit_price = _price_tick(trigger_price - self.cfg.stop_loss_limit_buffer)
        if quantity <= 0:
            LOG.info(
                "Stop order not needed | reason=%s quantity=%s existing_stop=%s",
                reason,
                quantity,
                position.stop_order_id,
            )
            if position.stop_order_id and position.stop_order_status not in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
                cancelled = self.api.cancel_order(position.stop_order_id)
                position.stop_order_status = _order_status(cancelled)
                self.bot.send(format_order_update(f"SL ORDER CANCELLED - {reason}", position, cancelled))
                LOG.info("Stop order cancelled after zero quantity | stop_order_id=%s status=%s", position.stop_order_id, position.stop_order_status)
            return

        if position.stop_order_id and position.stop_order_status not in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
            LOG.info(
                "Modifying stop order | reason=%s stop_order_id=%s quantity=%s trigger=%s",
                reason,
                position.stop_order_id,
                quantity,
                trigger_price,
            )
            try:
                modified = self.api.modify_order(
                    position.stop_order_id,
                    quantity=quantity,
                    order_type=sl_order_type,
                    price=sl_limit_price,
                    trigger_price=trigger_price,
                    validity=self.cfg.order_validity,
                )
            except Exception as exc:
                LOG.exception("Stop order modify failed | stop_order_id=%s reason=%s", position.stop_order_id, reason)
                failed = {
                    "orderId": position.stop_order_id or "-",
                    "orderStatus": "REJECTED",
                    "omsErrorDescription": str(exc),
                    "_request": {
                        "transactionType": "SELL",
                        "orderType": sl_order_type,
                        "quantity": quantity,
                        "price": sl_limit_price,
                        "triggerPrice": trigger_price,
                    },
                }
                self.bot.send(
                    format_order_update(
                        f"SL ORDER FAILED - {reason}",
                        position,
                        failed,
                        "The position is still tracked, but the broker-side protective SL order was not updated.",
                    )
                )
                return
            position.stop_order_status = _order_status(modified)
            position.current_sl = trigger_price
            self.bot.send(format_order_update(f"SL ORDER MODIFIED - {reason}", position, modified))
            LOG.info("Stop order modified | stop_order_id=%s status=%s", position.stop_order_id, position.stop_order_status)
            return

        LOG.info(
            "Placing stop order | reason=%s security_id=%s quantity=%s trigger=%s",
            reason,
            position.plan.option_security_id,
            quantity,
            trigger_price,
        )
        try:
            placed = self.api.place_order(
                transaction_type="SELL",
                security_id=int(position.plan.option_security_id or 0),
                quantity=quantity,
                order_type=sl_order_type,
                product_type=self.cfg.order_product_type,
                validity=self.cfg.order_validity,
                price=sl_limit_price,
                trigger_price=trigger_price,
                correlation_id=_make_correlation_id("SL"),
            )
        except Exception as exc:
            LOG.exception("Stop order placement failed | reason=%s", reason)
            failed = {
                "orderId": "-",
                "orderStatus": "REJECTED",
                "omsErrorDescription": str(exc),
                "_request": {
                    "transactionType": "SELL",
                    "orderType": sl_order_type,
                    "quantity": quantity,
                    "price": sl_limit_price,
                    "triggerPrice": trigger_price,
                },
            }
            self.bot.send(
                format_order_update(
                    f"SL ORDER FAILED - {reason}",
                    position,
                    failed,
                    "The entry remains tracked, but Dhan did not accept the broker-side protective SL order.",
                )
            )
            return
        position.stop_order_id = _order_id(placed)
        position.stop_order_status = _order_status(placed)
        position.current_sl = trigger_price
        self.bot.send(format_order_update(f"SL ORDER PLACED - {reason}", position, placed))
        LOG.info("Stop order placed | stop_order_id=%s status=%s", position.stop_order_id, position.stop_order_status)

    def _sync_stop_order(self, position: LivePosition) -> bool:
        if not position.stop_order_id:
            LOG.debug("Stop sync skipped | no stop order")
            return True
        previous_status = position.stop_order_status
        latest = self.api.get_order(position.stop_order_id)
        status = _order_status(latest)
        remaining_after_stop = _order_remaining_qty(latest)
        if status != previous_status:
            position.stop_order_status = status
            self.bot.send(format_order_update("SL ORDER STATUS UPDATE", position, latest))
            LOG.info(
                "Stop order status changed | stop_order_id=%s from=%s to=%s remaining=%s",
                position.stop_order_id,
                previous_status,
                status,
                remaining_after_stop,
            )
        if status in ORDER_FILLED_STATUSES or (status == "PART_TRADED" and remaining_after_stop == 0):
            position.remaining_qty = 0
            self.bot.send(
                format_live_position_update(
                    position,
                    "EXIT FULL",
                    "broker stop-loss order executed",
                    _order_average_price(latest) or position.current_sl,
                    _now_ist().replace(tzinfo=None),
                )
            )
            LOG.info("Stop order executed | stop_order_id=%s status=%s", position.stop_order_id, status)
            return False
        if status == "PART_TRADED" and remaining_after_stop > 0:
            position.remaining_qty = remaining_after_stop
            LOG.info("Stop order part traded | stop_order_id=%s remaining=%s", position.stop_order_id, remaining_after_stop)
        if status in ORDER_DEAD_STATUSES and position.remaining_qty > 0 and status != previous_status:
            self.bot.send(
                format_order_update(
                    "SL ORDER NOT ACTIVE",
                    position,
                    latest,
                    "The position still has remaining quantity, but the protective SL order is not active.",
                )
            )
            LOG.warning("Stop order dead while position remains | stop_order_id=%s status=%s remaining=%s", position.stop_order_id, status, position.remaining_qty)
        return True

    def _sync_entry_order(self, position: LivePosition) -> bool:
        if not position.entry_order_id:
            LOG.warning("Entry sync failed | missing entry order id")
            return False
        latest = self.api.get_order(position.entry_order_id)
        status = _order_status(latest)
        filled_qty = _order_filled_qty(latest)
        avg_price = _order_average_price(latest)

        if status != position.entry_order_status or filled_qty != position.entry_filled_qty:
            previous_filled_qty = position.entry_filled_qty
            position.entry_order_status = status
            position.entry_filled_qty = filled_qty
            if avg_price > 0:
                position.entry_avg_price = avg_price
            self.bot.send(format_order_update("ENTRY ORDER STATUS UPDATE", position, latest))
            LOG.info(
                "Entry order status changed | entry_order_id=%s status=%s filled=%s avg=%s",
                position.entry_order_id,
                status,
                filled_qty,
                avg_price,
            )
            if filled_qty > previous_filled_qty and position.stop_order_id is not None:
                additional_qty = filled_qty - previous_filled_qty
                position.remaining_qty += additional_qty
                LOG.info("Additional entry fill detected | qty=%s remaining=%s", additional_qty, position.remaining_qty)
                self._place_or_modify_stop_order(position, position.remaining_qty, position.current_sl, "additional entry fill")

        if filled_qty > 0 and position.stop_order_id is None:
            position.remaining_qty = filled_qty
            LOG.info("Entry filled; placing protective stop | filled=%s sl=%s", filled_qty, position.current_sl)
            self._place_or_modify_stop_order(position, position.remaining_qty, position.current_sl, "entry filled")

        if status in ORDER_DEAD_STATUSES and filled_qty <= 0:
            self.bot.send(format_order_update("ENTRY ORDER CLOSED WITHOUT FILL", position, latest))
            LOG.warning("Entry order closed without fill | entry_order_id=%s status=%s", position.entry_order_id, status)
            return False
        return True

    def _open_live_position(self, expiry: str, signal: Signal) -> None:
        plan = signal.option_plan
        if plan.option_security_id is None:
            LOG.warning("Live entry skipped | missing option security id candle=%s", signal.candle_time)
            self.bot.send(
                "<b>ENTRY ORDER SKIPPED</b>\n"
                "No option security id was available in the Dhan option-chain snapshot."
            )
            return

        signal_ts = pd.Timestamp(signal.candle_time)
        for position in self.live_positions:
            if pd.Timestamp(position.signal.candle_time) == signal_ts and position.signal.direction == signal.direction:
                LOG.info("Duplicate live position skipped | candle=%s direction=%s", signal.candle_time, signal.direction)
                return

        position = LivePosition(
            signal=signal,
            expiry=expiry,
            opened_at=_now_ist().replace(tzinfo=None),
            current_sl=plan.stop_loss,
            remaining_qty=0,
            last_checked_option_ts=signal_ts,
        )
        LOG.info(
            "Opening live position | expiry=%s direction=%s option=%s %s security_id=%s qty=%s",
            expiry,
            signal.direction,
            plan.side,
            plan.strike,
            plan.option_security_id,
            self.cfg.quantity,
        )

        entry_order_type = self.cfg.entry_order_type.upper()
        entry_price = 0.0
        if entry_order_type == "LIMIT":
            entry_price = _price_tick(plan.entry + self.cfg.entry_limit_buffer)
        try:
            entry_order = self.api.place_order(
                transaction_type="BUY",
                security_id=int(plan.option_security_id),
                quantity=self.cfg.quantity,
                order_type=entry_order_type,
                product_type=self.cfg.order_product_type,
                validity=self.cfg.order_validity,
                price=entry_price,
                trigger_price=0.0,
                correlation_id=_make_correlation_id("ENTRY"),
            )
        except Exception as exc:
            LOG.exception("Entry order placement failed | candle=%s", signal.candle_time)
            failed = {
                "orderId": "-",
                "orderStatus": "REJECTED",
                "omsErrorDescription": str(exc),
                "_request": {
                    "transactionType": "BUY",
                    "orderType": entry_order_type,
                    "quantity": self.cfg.quantity,
                    "price": entry_price,
                    "triggerPrice": 0.0,
                },
            }
            self.bot.send(
                format_order_update(
                    "ENTRY ORDER FAILED",
                    position,
                    failed,
                    "Dhan rejected the entry order. No live position was opened by this bot.",
                )
            )
            return
        position.entry_order_id = _order_id(entry_order)
        position.entry_order_status = _order_status(entry_order)
        self.bot.send(format_order_update("ENTRY ORDER PLACED", position, entry_order))

        if position.entry_order_id:
            latest = self._wait_for_order_update(position.entry_order_id)
            position.entry_order_status = _order_status(latest)
            position.entry_filled_qty = _order_filled_qty(latest)
            avg_price = _order_average_price(latest)
            if avg_price > 0:
                position.entry_avg_price = avg_price
            self.bot.send(format_order_update("ENTRY ORDER UPDATE", position, latest))

            if position.entry_filled_qty > 0:
                position.remaining_qty = position.entry_filled_qty
                self._place_or_modify_stop_order(position, position.remaining_qty, position.current_sl, "entry filled")
            elif position.entry_order_status in ORDER_DEAD_STATUSES:
                LOG.warning(
                    "Live position not tracked after dead entry | order_id=%s status=%s",
                    position.entry_order_id,
                    position.entry_order_status,
                )
                return

        self.live_positions.append(position)
        LOG.info("Live position tracked | entry_order_id=%s active_positions=%s", position.entry_order_id, len(self.live_positions))

    def _cancel_stop_before_exit(self, position: LivePosition, reason: str) -> None:
        if not position.stop_order_id:
            LOG.info("Stop cancel skipped | reason=%s no stop order", reason)
            return
        if position.stop_order_status in ORDER_DEAD_STATUSES | ORDER_FILLED_STATUSES:
            LOG.info("Stop cancel skipped | reason=%s stop_order_id=%s status=%s", reason, position.stop_order_id, position.stop_order_status)
            return
        latest = self.api.get_order(position.stop_order_id)
        latest_status = _order_status(latest)
        position.stop_order_status = latest_status
        if latest_status in ORDER_FILLED_STATUSES or (latest_status == "PART_TRADED" and _order_remaining_qty(latest) == 0):
            position.remaining_qty = 0
            self.bot.send(format_order_update("SL ORDER ALREADY EXECUTED", position, latest))
            LOG.info("Stop already executed before exit | stop_order_id=%s status=%s", position.stop_order_id, latest_status)
            return
        if latest_status == "PART_TRADED":
            position.remaining_qty = _order_remaining_qty(latest)
            LOG.info("Stop part traded before exit | stop_order_id=%s remaining=%s", position.stop_order_id, position.remaining_qty)
        if latest_status in ORDER_DEAD_STATUSES:
            LOG.info("Stop cancel skipped | reason=%s stop_order_id=%s latest_status=%s", reason, position.stop_order_id, latest_status)
            return
        LOG.info("Cancelling stop before exit | reason=%s stop_order_id=%s", reason, position.stop_order_id)
        cancelled = self.api.cancel_order(position.stop_order_id)
        position.stop_order_status = _order_status(cancelled)
        self.bot.send(format_order_update(f"SL ORDER CANCELLED - {reason}", position, cancelled))

    def _track_live_position(self, candles: pd.DataFrame, position: LivePosition) -> bool:
        LOG.debug(
            "Tracking live position | entry_order_id=%s option=%s %s remaining=%s",
            position.entry_order_id,
            position.plan.side,
            position.plan.strike,
            position.remaining_qty,
        )
        if not self._sync_entry_order(position):
            LOG.info("Live position closed from tracking | reason=entry_sync_false entry_order_id=%s", position.entry_order_id)
            return False
        if position.entry_filled_qty <= 0:
            return True
        if not self._sync_stop_order(position):
            LOG.info("Live position closed from tracking | reason=stop_sync_false entry_order_id=%s", position.entry_order_id)
            return False

        plan = position.plan
        if plan.option_security_id is None:
            LOG.warning("Live position missing option security id during tracking")
            return False

        start, end = _day_start_end(_today_ist())
        option_df = self.api.option_intraday(int(plan.option_security_id), start, end)
        option_df = _closed_candles_only(option_df, 5)
        if option_df.empty:
            LOG.debug("No option candles available for live position | security_id=%s", plan.option_security_id)
            return True

        index_by_ts = {pd.Timestamp(row.timestamp): i for i, row in candles.iterrows()}
        last_ts = position.last_checked_option_ts or pd.Timestamp(position.signal.candle_time)
        new_rows = option_df[option_df["timestamp"] > last_ts].reset_index(drop=True)
        if new_rows.empty:
            LOG.debug("No new option candles for live position | security_id=%s last_ts=%s", plan.option_security_id, last_ts)
            return True

        half_qty = max(1, self.cfg.quantity // 2)
        LOG.info("Processing option candles for live position | new_rows=%s last_ts=%s", len(new_rows), last_ts)

        for _, row in new_rows.iterrows():
            row_ts = pd.Timestamp(row.timestamp)
            low = float(row.low)
            high = float(row.high)
            close = float(row.close)
            position.last_checked_option_ts = row_ts

            if not self._sync_stop_order(position):
                return False

            if row.timestamp.time() >= self.cfg.square_off_time:
                LOG.info("Square-off time reached | row_time=%s remaining=%s", row.timestamp, position.remaining_qty)
                self._cancel_stop_before_exit(position, "15:20 square-off")
                if position.remaining_qty > 0:
                    self._place_market_exit(position, position.remaining_qty, "15:20 square-off", close)
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
                LOG.info("Stop-loss touched in option candle | time=%s low=%s sl=%s", row.timestamp, low, position.current_sl)
                self.bot.send(
                    format_live_position_update(
                        position,
                        "SL TOUCHED",
                        "waiting for broker stop-loss order execution",
                        position.current_sl,
                        row.timestamp,
                    )
                )
                return self._sync_stop_order(position)

            if not position.t1_hit and high >= plan.target1:
                LOG.info("Target 1 hit | time=%s high=%s target1=%s remaining=%s", row.timestamp, high, plan.target1, position.remaining_qty)
                booked_qty = min(half_qty, position.remaining_qty)
                old_remaining = position.remaining_qty
                new_remaining = position.remaining_qty - booked_qty
                new_sl = max(position.current_sl, plan.entry)
                try:
                    self._place_or_modify_stop_order(position, new_remaining, new_sl, "target 1 hit")
                    if booked_qty > 0:
                        self._place_market_exit(position, booked_qty, "target 1", plan.target1)
                        position.remaining_qty = new_remaining
                except Exception:
                    LOG.exception("Target 1 handling failed; attempting restore")
                    position.remaining_qty = old_remaining
                    try:
                        self._place_or_modify_stop_order(position, old_remaining, position.current_sl, "restore after target 1 exit error")
                    except Exception:
                        LOG.exception("Could not restore stop order after target 1 error")
                        pass
                    raise
                position.t1_hit = True
                self.bot.send(
                    format_live_position_update(
                        position,
                        f"BOOK {booked_qty} QTY",
                        "target 1 hit; SL moved to entry",
                        plan.target1,
                        row.timestamp,
                        "Keep the remaining quantity running for T2 or trailing stop.",
                    )
                )
                if position.remaining_qty <= 0:
                    return False

            if position.t1_hit and high >= plan.target2:
                LOG.info("Target 2 hit | time=%s high=%s target2=%s remaining=%s", row.timestamp, high, plan.target2, position.remaining_qty)
                self._cancel_stop_before_exit(position, "target 2")
                if position.remaining_qty > 0:
                    self._place_market_exit(position, position.remaining_qty, "target 2", plan.target2)
                    self.bot.send(
                        format_live_position_update(
                            position,
                            "EXIT REMAINING",
                            "target 2 hit",
                            plan.target2,
                            row.timestamp,
                        )
                    )
                    position.remaining_qty = 0
                return False

            if position.t1_hit:
                prev_rows = option_df[option_df["timestamp"] < row.timestamp]
                if not prev_rows.empty:
                    prev_low = float(prev_rows.iloc[-1].low)
                    new_sl = max(position.current_sl, round(prev_low - self.cfg.option_sl_buffer, 2), plan.entry)
                    if new_sl > position.current_sl:
                        LOG.info("Trailing stop | time=%s old_sl=%s new_sl=%s prev_low=%s", row.timestamp, position.current_sl, new_sl, prev_low)
                        self._place_or_modify_stop_order(position, position.remaining_qty, new_sl, "previous option candle low trail")
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
            if idx is not None and idx >= 4:
                chain = self.api.option_chain(position.expiry)
                opposite = build_signal(position.expiry, candles.reset_index(drop=True), idx, chain)
                if opposite is not None and opposite.direction != position.signal.direction:
                    LOG.info(
                        "Opposite signal exit | time=%s original=%s opposite=%s remaining=%s",
                        row.timestamp,
                        position.signal.direction,
                        opposite.direction,
                        position.remaining_qty,
                    )
                    self._cancel_stop_before_exit(position, "opposite scored signal")
                    if position.remaining_qty > 0:
                        action = "EXIT FULL" if not position.t1_hit else "EXIT REMAINING"
                        self._place_market_exit(position, position.remaining_qty, "opposite scored signal", close)
                        self.bot.send(
                            format_live_position_update(
                                position,
                                action,
                                "opposite scored signal",
                                close,
                                row.timestamp,
                                "The bot closed because a strong opposite setup appeared.",
                            )
                        )
                        position.remaining_qty = 0
                    return False

        return True

    def _track_live_positions(self, candles: pd.DataFrame) -> None:
        if not self.live_positions:
            return
        LOG.debug("Tracking live positions | count=%s", len(self.live_positions))
        still_open: List[LivePosition] = []
        for position in list(self.live_positions):
            try:
                if self._track_live_position(candles, position):
                    still_open.append(position)
            except Exception as exc:
                print(f"Live position tracking error: {exc}")
                LOG.exception("Live position tracking error")
                self.bot.send(f"<b>Live position tracking error</b>\n{_escape(exc)}")
                still_open.append(position)
        self.live_positions = still_open
        LOG.debug("Live positions after tracking | count=%s", len(self.live_positions))

    # Live monitoring

    def _live_check(self) -> None:
        if not self.live_enabled:
            LOG.debug("Live check skipped | live disabled")
            return
        if not _market_session_open():
            LOG.debug("Live check skipped | market session closed")
            return

        expiry = self._ensure_expiry()
        candles = self._current_intraday()
        candles = _closed_candles_only(candles, 5)
        if candles.empty or len(candles) < 5:
            LOG.debug("Live check skipped | insufficient candles rows=%s", len(candles))
            return

        self._track_live_positions(candles.reset_index(drop=True))

        latest = candles.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_live_candle_time:
            LOG.debug("Live check skipped | candle already processed candle=%s", candle_time)
            return

        chain = self.api.option_chain(expiry)
        signal = build_signal(expiry, candles.reset_index(drop=True), len(candles) - 1, chain)
        self.last_live_candle_time = candle_time
        now_monotonic = time.monotonic()
        if (
            self.cfg.log_live_scan_every_seconds > 0
            and (
                self._last_live_scan_log_at is None
                or now_monotonic - self._last_live_scan_log_at >= self.cfg.log_live_scan_every_seconds
            )
        ):
            self._last_live_scan_log_at = now_monotonic
            LOG.info("Live scan heartbeat | candle=%s rows=%s signal=%s", candle_time, len(candles), signal is not None)

        if signal is None:
            LOG.debug("Live check complete | no signal candle=%s", candle_time)
            return

        key = f"{signal.candle_time}|{signal.direction}|{','.join(signal.pattern_names)}"
        if key == self.last_live_key:
            LOG.info("Live signal skipped | duplicate key=%s", key)
            return

        LOG.info("Live signal firing | key=%s", key)
        self._send_signal(signal, prefix="<b>LIVE ALERT</b>")
        self._open_live_position(expiry, signal)
        self.last_live_key = key

    # Scan / backtest

    def _scan_worker(self, start_date: str, end_date: str) -> None:
        try:
            LOG.info("Scan worker started | start=%s end=%s", start_date, end_date)
            expiry = self._ensure_expiry()
            self.bot.send(
                f"⏳ Backtesting {NIFTY50_NAME} 5-minute candles\n"
                f"From: <b>{start_date}</b>\n"
                f"To: <b>{end_date}</b>\n\n"
                f"Alerts only when score >= {MIN_SIGNAL_SCORE}."
            )

            candles = self.api.intraday_candles(
                security_id=NIFTY50_SECURITY_ID,
                exchange_segment=NIFTY50_SEGMENT,
                instrument="INDEX",
                interval=5,
                from_date=f"{start_date} 09:15:00",
                to_date=f"{end_date} 15:30:00",
                oi=True,
            )

            if candles.empty:
                LOG.warning("Scan returned no candles | start=%s end=%s", start_date, end_date)
                self.bot.send("⚠️ No candles returned for that range.")
                return

            chain = self.api.option_chain(expiry)
            last_key: Optional[str] = None
            sent = 0
            LOG.info("Scan data ready | candles=%s expiry=%s", len(candles), expiry)

            for idx in range(4, len(candles)):
                if self.scan_stop_event.is_set():
                    LOG.info("Scan stopped by user | start=%s end=%s alerts_sent=%s", start_date, end_date, sent)
                    self.bot.send("🛑 Scan stopped by user.")
                    return

                window = candles.iloc[: idx + 1].copy().reset_index(drop=True)
                signal = self._evaluate_dataframe(window, expiry, chain, idx)
                if signal is None:
                    continue

                key = f"{signal.candle_time}|{signal.direction}|{','.join(signal.pattern_names)}"
                if key == last_key:
                    continue

                LOG.info(
                    "Backtest alert | candle=%s direction=%s score=%s/%s",
                    signal.candle_time,
                    signal.direction,
                    signal.score,
                    signal.max_score,
                )
                self._send_signal(signal, prefix="<b>BACKTEST ALERT</b>")
                last_key = key
                sent += 1
                time.sleep(0.3)

            self.bot.send(f"✅ Scan completed. Sent {sent} alert(s).")
            LOG.info("Scan completed | start=%s end=%s alerts_sent=%s", start_date, end_date, sent)
        except Exception as e:
            LOG.exception("Scan error | start=%s end=%s", start_date, end_date)
            self.bot.send(f"⚠️ Scan error: {_escape(e)}")
        finally:
            self.scan_stop_event.clear()
            with self.scan_lock:
                self.scan_thread = None
            LOG.info("Scan worker cleaned up | start=%s end=%s", start_date, end_date)

    def _start_scan(self, start_date: str, end_date: str) -> None:
        with self.scan_lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                LOG.warning("Scan start rejected | already running start=%s end=%s", start_date, end_date)
                self.bot.send("⚠️ A scan is already running.")
                return
            self.scan_stop_event.clear()
            self.scan_thread = threading.Thread(
                target=self._scan_worker,
                args=(start_date, end_date),
                daemon=True,
            )
            self.scan_thread.start()
            LOG.info("Scan thread started | start=%s end=%s thread=%s", start_date, end_date, self.scan_thread.name)

    # Commands

    def _handle_chain(self) -> None:
        LOG.info("Handling chain command")
        expiry, spot, oc = self._fetch_chain()
        all_strikes = sorted(float(k) for k in oc.keys())
        atm = _nearest_strike(all_strikes, spot)
        rows = _strikes_around_atm(oc, spot, self.cfg.strikes_window)
        support, resistance = _support_resistance_oi(oc)
        pcr_val = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val = _max_pain(oc)
        self.bot.send(format_chain_message(rows, spot, expiry, support, resistance, atm, pcr_val, max_pain_val))

    def _handle_status(self) -> None:
        LOG.info("Handling status command")
        expiry, spot, oc = self._fetch_chain()
        all_strikes = sorted(float(k) for k in oc.keys())
        atm = _nearest_strike(all_strikes, spot)
        support, resistance = _support_resistance_oi(oc)
        pcr_val = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val = _max_pain(oc)
        call_top = _top_oi(oc, "ce", 3)
        put_top = _top_oi(oc, "pe", 3)
        self.bot.send(format_status_message(spot, expiry, support, resistance, atm, pcr_val, max_pain_val, call_top, put_top))

    def _handle_strike(self, side: str, strike: float) -> None:
        LOG.info("Handling strike command | side=%s strike=%s", side, strike)
        expiry, spot, oc = self._fetch_chain()
        row = _get_row(oc, strike)
        option_data = row.get(side.lower(), {})
        if not row:
            strikes = sorted(float(k) for k in oc.keys())
            LOG.warning("Strike not found | side=%s strike=%s min=%s max=%s", side, strike, strikes[0], strikes[-1])
            self.bot.send(
                f"⚠️ Strike <b>{strike:,.0f}</b> not found in the option chain.\n"
                f"Range: {strikes[0]:,.0f} – {strikes[-1]:,.0f}"
            )
            return

        support, resistance = _support_resistance_oi(oc)
        pcr_val = _pcr(oc, spot, self.cfg.strikes_window)

        candles = self._current_intraday()
        if candles.empty or len(candles) < 5:
            LOG.warning("Strike analysis failed | insufficient candles rows=%s", len(candles))
            self.bot.send("⚠️ Could not fetch intraday candles for strike analysis.")
            return

        ctx = _analyse_intraday([
            Candle(
                ts=(row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
            )
            for row in candles.itertuples(index=False)
        ])
        if ctx is None:
            LOG.warning("Strike analysis failed | intraday context unavailable")
            self.bot.send("⚠️ Intraday context unavailable.")
            return

        ltp = _num(option_data.get("last_price"))
        if ltp <= 0:
            ltp = max(1.0, abs(spot - strike) / 4.0)
        _, _, _, plan = _option_trade_plan({"data": {"oc": oc}}, spot, side, MIN_SIGNAL_SCORE)
        opt_id = option_data.get("security_id") or option_data.get("securityId")
        signal = Signal(
            timestamp=_now_ist().isoformat(timespec="seconds"),
            underlying_symbol=NIFTY50_NAME,
            candle_time=str(ctx.last_candle.ts),
            direction="BULLISH" if side == "CE" else "BEARISH",
            score=MIN_SIGNAL_SCORE,
            max_score=11,
            pattern_names=[f"Manual query: {side}{int(strike)}"],
            reasons=["Manual strike lookup using current option chain"],
            vwap=ctx.vwap,
            spot=spot,
            pcr=pcr_val,
            support=support,
            resistance=resistance,
            option_plan=dataclasses.replace(
                plan,
                option_ltp=ltp,
                strike=strike,
                side=side,
                option_security_id=int(float(opt_id)) if opt_id is not None else None,
            ),
            confidence="Manual",
        )
        self._send_signal(signal, prefix="<b>STRIKE SNAPSHOT</b>")
        LOG.info("Strike snapshot sent | side=%s strike=%s option_id=%s", side, strike, opt_id)

    def _dispatch(self, raw: str) -> None:
        text = raw.strip()
        cmd = text.split("@")[0].lower()
        LOG.info("Dispatching command | text=%s", text)

        m = STRIKE_RE.match(text.replace(" ", ""))
        if m:
            self._handle_strike(m.group(1).upper(), float(m.group(2)))
            return

        m = SCAN_RE.match(text)
        if m:
            self._start_scan(m.group(1), m.group(2))
            return

        if STOP_RE.match(text):
            self.scan_stop_event.set()
            LOG.info("Stop requested via command")
            self.bot.send("🛑 Stop requested.")
            return

        if LIVE_RE.match(text):
            self.live_enabled = True
            LOG.info("Live monitoring enabled via command")
            self.bot.send("✅ Live monitoring enabled. Real Dhan orders will be placed on live alerts.")
            return

        if cmd in ("/chain", "chain"):
            self._handle_chain()
        elif cmd in ("/status", "status"):
            self._handle_status()
        elif cmd in ("/position", "position"):
            self.bot.send(format_live_position_status(self.live_positions))
        elif cmd in ("/expiry", "expiry"):
            self.bot.send(f"Current weekly expiry: <b>{self._ensure_expiry()}</b>")
        elif cmd in ("/help", "help", "/start", "start"):
            self.bot.send(HELP_TEXT)
        else:
            LOG.warning("Unknown command | text=%s", text)
            self.bot.send(f"Unknown command: <code>{_escape(text)}</code>\n\n{HELP_TEXT}")

    # Run loop

    def run(self) -> None:
        print(f"Market Watch Agent started | {NIFTY50_NAME}")
        LOG.info("Market Watch Agent started | symbol=%s", NIFTY50_NAME)
        self.bot.send(
            f"<b>{NIFTY50_NAME} Signal Bot is online!</b>\n\n"
            f"Live monitoring: {'ON' if self.live_enabled else 'OFF'}.\n"
            f"Live orders: REAL DHAN ORDERS.\n"
            f"Send <code>SCAN YYYY-MM-DD YYYY-MM-DD</code> for backtest.\n"
            f"Send <code>LIVE</code> to enable real live orders.\n"
            f"Send <code>STOP</code> to stop a scan.\n"
            f"Alerts fire only when score >= {MIN_SIGNAL_SCORE}."
        )

        while True:
            try:
                for msg in self.bot.get_messages():
                    print(f"Message: {msg}")
                    LOG.info("Telegram command received | message=%s", msg)
                    try:
                        self._dispatch(msg)
                    except Exception as e:
                        err = f"⚠️ Error: {_escape(e)}"
                        print(err)
                        LOG.exception("Command handling error | message=%s", msg)
                        self.bot.send(err)

                try:
                    self._live_check()
                except Exception as e:
                    print(f"Live check error: {e}")
                    LOG.exception("Live check error")

                time.sleep(self.cfg.tg_poll_interval)

            except KeyboardInterrupt:
                self.bot.send(f"{NIFTY50_NAME} Signal Bot stopped.")
                print("Stopped.")
                LOG.info("Market Watch Agent stopped by keyboard interrupt")
                return
            except requests.HTTPError as e:
                print(f"HTTP error: {e}")
                LOG.exception("HTTP error in main loop")
                time.sleep(5)
            except Exception as e:
                print(f"Error: {e}")
                LOG.exception("Unexpected error in main loop")
                time.sleep(5)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg)
    LOG.info("Config loaded | quantity=%s lots=%s lot_size=%s product=%s", cfg.quantity, cfg.lots, cfg.lot_size, cfg.order_product_type)
    MarketWatchAgent(cfg).run()


if __name__ == "__main__":
    main()
