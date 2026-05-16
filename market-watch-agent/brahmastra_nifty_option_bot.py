"""NIFTY Brahmastra option-buying bot with live alerts and premium backtests.

What this program does
- Monitors NIFTY 50 on 5-minute candles.
- Uses Supertrend 20,2 + MACD 12,26,9 + daily VWAP.
- Sends Telegram alerts when the full Brahmastra setup appears.
- Selects ATM / slightly ITM NIFTY CE or PE using live premium filters.
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

Telegram commands
- SCAN YYYY-MM-DD YYYY-MM-DD  Run historical backtest.
- STOP                       Stop running scan.
- LIVE                       Enable live monitoring.
- /chain                     Show current option chain around ATM.
- /status                    Show current NIFTY context.
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
- MAX_BACKTEST_EXPIRY_GAP_DAYS=10
- MIN_PREMIUM=60
- PREFERRED_PREMIUM_MIN=80
- PREFERRED_PREMIUM_MAX=180
- MAX_PREMIUM=250
- LOT_SIZE=75
- LOTS=1
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass
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
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


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

    lot_size: int = 75
    lots: int = 1
    brokerage_per_order: float = 0.0

    square_off_time: dt.time = dt.time(15, 20)
    live_enabled_at_start: bool = True

    dhan_scrip_master_csv: Optional[str] = None
    download_dhan_master: bool = False

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

        return Config(
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
            lot_size=_env_int("LOT_SIZE", 75),
            lots=_env_int("LOTS", 1),
            brokerage_per_order=_env_float("BROKERAGE_PER_ORDER", 0.0),
            live_enabled_at_start=_env_bool("LIVE_ENABLED", True),
            dhan_scrip_master_csv=(os.getenv("DHAN_SCRIP_MASTER_CSV") or "").strip() or None,
            download_dhan_master=_env_bool("DOWNLOAD_DHAN_MASTER", False),
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
        hint = ""
        if r.status_code == 401:
            hint = (
                "\nHint: Dhan rejected authentication. Check DHAN_CLIENT_ID and "
                "DHAN_ACCESS_TOKEN in .env, regenerate the access token if it is "
                "expired, and confirm your Dhan account has API access for charts data."
            )

        raise requests.HTTPError(
            f"{r.status_code} Dhan error on {endpoint}. Response: {body or '-'}{hint}",
            response=r,
        )

    def expiry_list(self) -> List[str]:
        payload = {
            "UnderlyingScrip": NIFTY50_SECURITY_ID,
            "UnderlyingSeg": NIFTY50_SEGMENT,
        }
        r = self.session.post(
            f"{DHAN_BASE}/optionchain/expirylist",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, "/optionchain/expirylist")
        return [str(x) for x in r.json().get("data", [])]

    def pick_expiry_for_date(self, trade_date: dt.date) -> str:
        expiries = self.expiry_list()
        if not expiries:
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
                return expiry
            raise RuntimeError(
                f"Nearest Dhan expiry for {trade_date} is {expiry} ({gap_days} days later), "
                f"which is outside MAX_BACKTEST_EXPIRY_GAP_DAYS={self.cfg.max_backtest_expiry_gap_days}. "
                "For older backtests, set DHAN_SCRIP_MASTER_CSV to a historical contract master "
                "that contains the actual NIFTY option expiries for that period."
            )

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
        r = self.session.post(
            f"{DHAN_BASE}/optionchain",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        self._raise_for_status(r, "/optionchain")
        return r.json()

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
            return self._rows_to_df(raw)
        if isinstance(raw, dict):
            return self._dict_to_df(raw)
        raise ValueError(f"Unexpected intraday response shape: {type(raw)}")

    def index_intraday(self, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
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
        return self.intraday_candles(
            security_id=security_id,
            exchange_segment=FNO_SEGMENT,
            instrument=OPTION_INSTRUMENT,
            interval=self.cfg.candle_interval,
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            oi=True,
        )

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

        df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        return df.reset_index(drop=True)


class TelegramBot:
    MAX_HTML_LEN = 3900
    MAX_TEXT_LEN = 3900

    def __init__(self, token: str, chat_id: str, timeout: int = 20):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.session = requests.Session()
        self._offset = 0

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

        try:
            r = self.session.post(
                f"{TELEGRAM_BASE}/bot{self.token}/sendMessage",
                data=data,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Telegram sendMessage request failed: {self._redact(exc)}") from exc

        if r.status_code >= 400:
            try:
                body = json.dumps(r.json(), ensure_ascii=True)
            except Exception:
                body = r.text or ""
            raise RuntimeError(
                f"Telegram sendMessage failed with HTTP {r.status_code}. "
                f"Response: {self._redact(body)}"
            )

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
            # Telegram returns 400 for HTML parse issues. Fall back to plain text
            # so important scan results still reach the chat.
            if "HTTP 400" not in str(exc):
                raise
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
            return []

        texts: List[str] = []
        for update in r.json().get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message") or update.get("channel_post") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if chat_id == self.chat_id and text:
                texts.append(text)
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
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value[:10], fmt).date()
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
        return df
    at_time = (at_time or now_ist()).replace(tzinfo=None)
    latest_allowed_end = at_time - dt.timedelta(seconds=5)
    candle_end = df["timestamp"] + pd.to_timedelta(interval_minutes, unit="m")
    return df[candle_end <= latest_allowed_end].reset_index(drop=True)


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


def top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
    items: List[Tuple[float, float]] = []
    for k, v in oc.items():
        try:
            items.append((float(k), num(v.get(side, {}).get("oi"))))
        except Exception:
            continue
    return sorted(items, key=lambda x: x[1], reverse=True)[:n]


# -----------------------------------------------------------------------------
# Contract resolver
# -----------------------------------------------------------------------------


class OptionContractResolver:
    """Resolve option security ids from live chain or an optional Dhan master CSV."""

    def __init__(self, cfg: Config, api: DhanApiClient):
        self.cfg = cfg
        self.api = api
        self.master: Optional[pd.DataFrame] = None
        self._load_master_if_available()

    def _load_master_if_available(self) -> None:
        path = self.cfg.dhan_scrip_master_csv
        if not path and self.cfg.download_dhan_master:
            path = str(Path(__file__).with_name("dhan_api_scrip_master.csv"))
            if not Path(path).exists():
                r = requests.get(DHAN_MASTER_URL, timeout=self.cfg.http_timeout)
                r.raise_for_status()
                Path(path).write_bytes(r.content)

        if not path:
            return

        csv_path = Path(path)
        if not csv_path.exists():
            raise RuntimeError(f"DHAN_SCRIP_MASTER_CSV does not exist: {csv_path}")

        df = pd.read_csv(csv_path, low_memory=False)
        df.columns = [self._clean_col(c) for c in df.columns]
        self.master = df

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
        if self.master is not None:
            expiry_col = self._col(["expirydate", "semexpirydate", "expiry", "expiry_date"])
            symbol_col = self._col(["underlyingsymbol", "semunderlyingsymbol", "symbol", "semtrading_symbol", "semcustomsymbol", "customsymbol"])
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
                        return expiry_date.strftime("%Y-%m-%d")
                    raise RuntimeError(
                        f"Nearest Dhan master expiry for {trade_date} is {expiry_date} ({gap_days} days later), "
                        f"which is outside MAX_BACKTEST_EXPIRY_GAP_DAYS={self.cfg.max_backtest_expiry_gap_days}. "
                        "Your current master appears not to contain the historical NIFTY option contracts "
                        "for this backtest period. Provide DHAN_SCRIP_MASTER_CSV with those expired contracts."
                    )

        return self.api.pick_expiry_for_date(trade_date)

    def resolve_from_chain(self, oc: Dict[str, Any], strike: float, side: str) -> Optional[int]:
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        security_id = option.get("security_id") or option.get("securityId")
        if security_id is None:
            return None
        return int(float(security_id))

    def resolve(self, expiry: str, strike: float, side: str, oc: Optional[Dict[str, Any]] = None) -> int:
        if oc:
            from_chain = self.resolve_from_chain(oc, strike, side)
            if from_chain is not None:
                return from_chain

        from_master = self.resolve_from_master(expiry, strike, side)
        if from_master is not None:
            return from_master

        raise RuntimeError(
            f"Could not resolve NIFTY {expiry} {int(strike)} {side} security id. "
            "For exact historical option backtests, provide DHAN_SCRIP_MASTER_CSV with the contract."
        )

    def resolve_from_master(self, expiry: str, strike: float, side: str) -> Optional[int]:
        if self.master is None:
            return None

        id_col = self._col(["securityid", "security_id", "semsmstsecurityid", "instrumenttoken"])
        expiry_col = self._col(["expirydate", "semexpirydate", "expiry", "expiry_date"])
        strike_col = self._col(["strikeprice", "semstrikeprice", "strike", "strike_price"])
        option_col = self._col(["optiontype", "semoptiontype", "option_type"])
        symbol_col = self._col(["underlyingsymbol", "semunderlyingsymbol", "symbol", "semtrading_symbol", "semcustomsymbol", "customsymbol"])
        custom_col = self._col(["customsymbol", "semcustomsymbol", "tradingsymbol", "trading_symbol", "symbolname"])
        instrument_col = self._col(["instrument", "instrumentname", "seminstrumentname"])

        if not id_col or not expiry_col or not strike_col:
            return None

        expiry_date = parse_date_flexible(expiry)
        if expiry_date is None:
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
            return None

        security_id = pd.to_numeric(out.iloc[0][id_col], errors="coerce")
        if pd.isna(security_id):
            return None
        return int(security_id)


# -----------------------------------------------------------------------------
# Indicators
# -----------------------------------------------------------------------------


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
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
        return float(last.close) > float(prev["high"].max()) or (
            float(df.iloc[idx].low) > float(prev["low"].min()) and float(last.close) > float(df.iloc[idx - 1].close)
        )
    return float(last.close) < float(prev["low"].min()) or (
        float(df.iloc[idx].high) < float(prev["high"].max()) and float(last.close) < float(df.iloc[idx - 1].close)
    )


# -----------------------------------------------------------------------------
# Strategy engine
# -----------------------------------------------------------------------------


def build_brahmastra_signal(df: pd.DataFrame, idx: int, cfg: Config) -> Optional[BrahmastraSignal]:
    if idx < max(cfg.supertrend_period + 2, cfg.macd_slow + cfg.macd_signal):
        return None

    row = df.iloc[idx]
    if pd.isna(row.supertrend) or pd.isna(row.macd) or pd.isna(row.macd_signal) or pd.isna(row.vwap):
        return None

    market_open = dt.datetime.combine(row.timestamp.date(), dt.time(9, 15))
    if row.timestamp < market_open + dt.timedelta(minutes=cfg.avoid_first_minutes):
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
        return bool(row.st_flip_down) or bool(row.macd_cross_down) or (
            int(row.supertrend_dir) == -1 and float(row.close) < float(row.vwap)
        )
    return bool(row.st_flip_up) or bool(row.macd_cross_up) or (
        int(row.supertrend_dir) == 1 and float(row.close) > float(row.vwap)
    )


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
    return candidates


def score_premium_choice(premium: float, strike: float, atm: float, cfg: Config) -> Tuple[int, float, float]:
    preferred_mid = (cfg.preferred_premium_min + cfg.preferred_premium_max) / 2.0
    if cfg.preferred_premium_min <= premium <= cfg.preferred_premium_max:
        band = 0
    elif cfg.min_premium <= premium <= cfg.max_premium:
        band = 1
    else:
        band = 2
    return (band, abs(premium - preferred_mid), abs(strike - atm))


def select_option_from_chain(
    oc: Dict[str, Any],
    expiry: str,
    spot: float,
    side: str,
    cfg: Config,
    resolver: OptionContractResolver,
) -> OptionContract:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        raise RuntimeError("Option chain is empty.")

    atm = nearest_strike(strikes, spot)
    choices: List[Tuple[Tuple[int, float, float], OptionContract]] = []

    for strike in strike_candidates(spot, side, cfg, strikes):
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        premium = num(option.get("last_price"))
        if premium <= 0:
            continue
        if not cfg.allow_premium_fallback and not (cfg.min_premium <= premium <= cfg.max_premium):
            continue
        security_id = resolver.resolve_from_chain(oc, strike, side)
        if security_id is None:
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
        return OptionContract(
            side=side,
            strike=float(strike),
            expiry=expiry,
            security_id=security_id,
            entry_premium=float(premium),
            selection_reason="fallback ATM because no premium-filter candidate was available",
        )
    if not choices:
        raise RuntimeError(
            f"No {side} option found inside premium range Rs {cfg.min_premium:g}-{cfg.max_premium:g}. "
            "Signal skipped."
        )

    return sorted(choices, key=lambda x: x[0])[0][1]


def option_sl_from_history(option_df: pd.DataFrame, entry_pos: int, entry: float, cfg: Config) -> float:
    start = max(0, entry_pos - cfg.swing_lookback)
    prior = option_df.iloc[start:entry_pos]
    if len(prior) >= 2:
        sl = float(prior["low"].min()) - cfg.option_sl_buffer
    else:
        sl = entry * (1.0 - cfg.fallback_option_sl_pct)
    sl = round(max(sl, 0.05), 2)
    if sl >= entry:
        sl = round(max(entry * (1.0 - cfg.fallback_option_sl_pct), 0.05), 2)
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
    return OptionTradePlan(
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


def find_candle_at_or_after(df: pd.DataFrame, timestamp: dt.datetime) -> Optional[int]:
    if df.empty:
        return None
    ts = pd.Timestamp(timestamp)
    matches = df.index[df["timestamp"] >= ts].tolist()
    return int(matches[0]) if matches else None


def find_exact_candle(df: pd.DataFrame, timestamp: dt.datetime) -> Optional[int]:
    if df.empty:
        return None
    ts = pd.Timestamp(timestamp)
    matches = df.index[df["timestamp"] == ts].tolist()
    return int(matches[0]) if matches else None


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
        self.option_chain_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self.option_chain_errors: Dict[str, str] = {}

    def run(self, start_date: str, end_date: str) -> BacktestResult:
        start_day = dt.datetime.strptime(start_date, "%Y-%m-%d").date()
        end_day = dt.datetime.strptime(end_date, "%Y-%m-%d").date()
        start_dt = dt.datetime.combine(start_day, dt.time(9, 15))
        end_dt = dt.datetime.combine(end_day, dt.time(15, 30))

        raw = self.api.index_intraday(start_dt, end_dt)
        if raw.empty:
            return BacktestResult(start_date, end_date, [], ["No NIFTY candles returned."])

        index_df = add_indicators(raw, self.cfg)
        trades: List[BacktestTrade] = []
        errors: List[str] = []
        last_trigger_key: Optional[str] = None
        active_until: Optional[pd.Timestamp] = None

        for idx in range(len(index_df) - 1):
            if self.stop_event.is_set():
                errors.append("Scan stopped by user.")
                break

            if active_until is not None and pd.Timestamp(index_df.iloc[idx].timestamp) <= active_until:
                continue

            signal = build_brahmastra_signal(index_df, idx, self.cfg)
            if signal is None:
                continue
            if signal.trigger_key == last_trigger_key:
                continue

            entry_idx = idx + 1
            entry_row = index_df.iloc[entry_idx]
            entry_time = entry_row.timestamp.to_pydatetime() if hasattr(entry_row.timestamp, "to_pydatetime") else entry_row.timestamp

            try:
                trade = self._build_and_simulate_trade(index_df, idx, entry_idx, signal, entry_time)
                trades.append(trade)
                active_until = pd.Timestamp(trade.exit_time)
                last_trigger_key = signal.trigger_key
            except Exception as exc:
                errors.append(f"{signal.candle_time} {signal.side}: {exc}")
                last_trigger_key = signal.trigger_key

        return BacktestResult(start_date, end_date, trades, errors)

    def _build_and_simulate_trade(
        self,
        index_df: pd.DataFrame,
        signal_idx: int,
        entry_idx: int,
        signal: BrahmastraSignal,
        entry_time: dt.datetime,
    ) -> BacktestTrade:
        trade_date = entry_time.date()
        expiry = self.resolver.pick_expiry_for_date(trade_date)
        contract, option_df, entry_pos = self._select_historical_option(expiry, signal.side, float(index_df.iloc[entry_idx].open), entry_time)
        plan = make_option_plan(signal, index_df, signal_idx, contract, option_df.iloc[: entry_pos + 1], self.cfg)
        return self._simulate_option_trade(index_df, entry_idx, signal, option_df, entry_pos, plan)

    def _option_day_df(self, security_id: int, day: dt.date) -> pd.DataFrame:
        cache_key = (security_id, day.isoformat())
        if cache_key in self.option_cache:
            return self.option_cache[cache_key]
        start, end = day_start_end(day)
        df = self.api.option_intraday(security_id, start, end)
        self.option_cache[cache_key] = df
        return df

    def _option_chain_for_security_ids(self, expiry: str, trade_date: dt.date) -> Optional[Dict[str, Any]]:
        if expiry in self.option_chain_cache:
            return self.option_chain_cache[expiry]

        expiry_date = parse_date_flexible(expiry)
        if expiry_date is not None and (expiry_date - trade_date).days > 10:
            self.option_chain_cache[expiry] = None
            self.option_chain_errors[expiry] = (
                f"live option-chain id fallback skipped because expiry {expiry} "
                f"is too far from trade date {trade_date}"
            )
            return None

        try:
            snapshot = self.api.option_chain(expiry)
            data = snapshot.get("data", {}) if isinstance(snapshot, dict) else {}
            oc = data.get("oc") or {}
            self.option_chain_cache[expiry] = oc if oc else None
            if not oc:
                self.option_chain_errors[expiry] = f"live option chain for expiry {expiry} was empty"
            return self.option_chain_cache[expiry]
        except Exception as exc:
            self.option_chain_cache[expiry] = None
            self.option_chain_errors[expiry] = f"live option-chain id fallback failed: {exc}"
            return None

    def _select_historical_option(
        self,
        expiry: str,
        side: str,
        spot_at_entry: float,
        entry_time: dt.datetime,
    ) -> Tuple[OptionContract, pd.DataFrame, int]:
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
                    if not oc_for_ids_loaded:
                        oc_for_ids = self._option_chain_for_security_ids(expiry, entry_time.date())
                        oc_for_ids_loaded = True
                    if oc_for_ids is None:
                        raise resolve_exc
                    security_id = self.resolver.resolve(expiry, strike, side, oc_for_ids)

                option_df = self._option_day_df(security_id, entry_time.date())
                entry_pos = find_exact_candle(option_df, entry_time)
                if entry_pos is None:
                    failures.append(f"{int(strike)} {side}: no exact candle at {entry_time.time()}")
                    continue
                entry_premium = float(option_df.iloc[entry_pos].open)
                if entry_premium <= 0:
                    failures.append(f"{int(strike)} {side}: invalid premium {entry_premium}")
                    continue
                if not self.cfg.allow_premium_fallback and not (self.cfg.min_premium <= entry_premium <= self.cfg.max_premium):
                    failures.append(f"{int(strike)} {side}: premium {entry_premium:.2f} outside allowed range")
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
            except Exception as exc:
                failures.append(f"{int(strike)} {side}: {exc}")

        if not choices:
            detail = "; ".join(failures[:4])
            chain_error = self.option_chain_errors.get(expiry)
            if chain_error and chain_error not in detail:
                detail = f"{detail}; {chain_error}" if detail else chain_error
            raise RuntimeError(
                "No historical option candidate had an exact entry candle. "
                f"Expiry={expiry}, time={entry_time}. Details: {detail}"
            )

        _, contract, option_df, entry_pos = sorted(choices, key=lambda x: x[0])[0]
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

        for pos in range(entry_pos, len(option_df)):
            row = option_df.iloc[pos]
            row_ts = pd.Timestamp(row.timestamp)
            low = float(row.low)
            high = float(row.high)
            close = float(row.close)
            exit_time = row.timestamp

            if row.timestamp.time() >= self.cfg.square_off_time:
                exit_price = close
                exit_reason = "square off"
                break

            # Conservative intrabar assumption: if SL and target both touch in
            # one candle, SL is counted first.
            if low <= sl:
                exit_price = sl
                exit_reason = "stop loss" if not t1_hit else "trailing/breakeven stop"
                break

            if not t1_hit and high >= t1:
                realized += half_qty * (t1 - entry)
                remaining_qty -= half_qty
                t1_hit = True
                sl = max(sl, entry)
                if remaining_qty <= 0:
                    exit_price = t1
                    exit_reason = "target 1 full"
                    break

            if t1_hit and high >= t2:
                exit_price = t2
                exit_reason = "target 2"
                break

            if pos > entry_pos and t1_hit:
                prev_low = float(option_df.iloc[pos - 1].low)
                sl = max(sl, round(prev_low - self.cfg.option_sl_buffer, 2), entry)

            idx = index_by_ts.get(row_ts)
            if idx is not None and idx > entry_idx and opposite_exit_signal(index_df, idx, signal.side):
                exit_price = close
                exit_reason = "opposite signal"
                break

        realized += remaining_qty * (exit_price - entry)
        orders = 2 + (1 if t1_hit else 0)
        net_pnl = realized - orders * self.cfg.brokerage_per_order
        pnl_points = net_pnl / max(qty, 1)
        signal_time = signal.candle_time
        entry_time = option_df.iloc[entry_pos].timestamp

        return BacktestTrade(
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
        lines.append(f"{'Time':<16} {'Opt':<10} {'Entry':>7} {'Exit':>7} {'PnL':>9} {'Reason':<16}")
        lines.append("-" * 72)
        for trade in trades[-8:]:
            entry_time = str(trade.entry_time)[5:16]
            opt = f"{int(trade.strike)}{trade.side}"
            lines.append(
                f"{entry_time:<16} {opt:<10} {trade.entry:>7.2f} {trade.exit_price:>7.2f} {trade.pnl:>9.2f} {trade.exit_reason:<16}"
            )
        lines.append("</pre>")

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
  LIVE   - enable live monitoring
  STOP   - stop running scan

<b>Market overview</b>
  /chain   - option chain around ATM
  /status  - spot, PCR, support, resistance
  /expiry  - selected weekly expiry
  /help    - this help message

Strategy:
Supertrend 20,2 + MACD 12,26,9 + VWAP on 5-minute NIFTY candles.
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

        self.scan_thread: Optional[threading.Thread] = None
        self.scan_stop_event = threading.Event()
        self.scan_lock = threading.Lock()

    def ensure_expiry(self) -> str:
        today = today_ist()
        if self.expiry is None:
            self.expiry = self.resolver.pick_expiry_for_date(today)
        return self.expiry

    def current_intraday(self) -> pd.DataFrame:
        now = now_ist().replace(tzinfo=None)
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return self.api.index_intraday(start, end)

    def current_chain(self) -> Tuple[str, float, Dict[str, Any]]:
        expiry = self.ensure_expiry()
        snapshot = self.api.option_chain(expiry)
        data = snapshot.get("data", {})
        spot = num(data.get("last_price"))
        oc = data.get("oc") or {}
        if not oc:
            raise RuntimeError("Empty option chain. Market may be closed.")
        return expiry, spot, oc

    def build_live_plan(self, signal: BrahmastraSignal, index_df: pd.DataFrame, signal_idx: int) -> BrahmastraSignal:
        expiry, _, oc = self.current_chain()
        contract = select_option_from_chain(oc, expiry, signal.spot, signal.side, self.cfg, self.resolver)
        option_df: Optional[pd.DataFrame] = None
        try:
            start, end = day_start_end(today_ist())
            option_df = self.api.option_intraday(contract.security_id, start, end)
            option_df = closed_candles_only(option_df, self.cfg.candle_interval)
        except Exception:
            option_df = None
        signal.option_plan = make_option_plan(signal, index_df, signal_idx, contract, option_df, self.cfg)
        return signal

    def live_check(self) -> None:
        if not self.live_enabled or not market_session_open():
            return

        raw = self.current_intraday()
        raw = closed_candles_only(raw, self.cfg.candle_interval)
        if raw.empty or len(raw) < 40:
            return

        index_df = add_indicators(raw, self.cfg)
        latest = index_df.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_live_candle_time:
            return
        self.last_live_candle_time = candle_time

        signal = build_brahmastra_signal(index_df, len(index_df) - 1, self.cfg)
        if signal is None:
            return
        if signal.trigger_key == self.last_live_trigger:
            return

        signal = self.build_live_plan(signal, index_df, len(index_df) - 1)
        self.bot.send(format_signal(signal))
        self.last_live_trigger = signal.trigger_key

    def scan_worker(self, start_date: str, end_date: str) -> None:
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
        except Exception as exc:
            self.bot.send(f"<b>Backtest error</b>\n{tg_escape(exc)}")
        finally:
            self.scan_stop_event.clear()
            with self.scan_lock:
                self.scan_thread = None

    def start_scan(self, start_date: str, end_date: str) -> None:
        with self.scan_lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                self.bot.send("A scan is already running. Send STOP first.")
                return
            self.scan_stop_event.clear()
            self.scan_thread = threading.Thread(target=self.scan_worker, args=(start_date, end_date), daemon=True)
            self.scan_thread.start()

    def handle_chain(self) -> None:
        expiry, spot, oc = self.current_chain()
        all_strikes = sorted(float(k) for k in oc.keys())
        atm = nearest_strike(all_strikes, spot)
        nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: self.cfg.strikes_window * 2 + 1]
        rows = [(s, get_row(oc, s)) for s in sorted(nearby)]
        support, resistance = support_resistance_oi(oc)
        pcr = pcr_near_atm(oc, spot, self.cfg.strikes_window)
        self.bot.send(format_chain_message(rows, spot, expiry, support, resistance, atm, pcr))

    def handle_status(self) -> None:
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
        expiry, spot, oc = self.current_chain()
        row = get_row(oc, strike)
        option = row.get(side.lower(), {}) if row else {}
        if not option:
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

        m = STRIKE_RE.match(text.replace(" ", ""))
        if m:
            self.handle_strike(m.group(1).upper(), float(m.group(2)))
            return

        m = SCAN_RE.match(text)
        if m:
            self.start_scan(m.group(1), m.group(2))
            return

        if STOP_RE.match(text):
            self.scan_stop_event.set()
            self.bot.send("Stop requested.")
            return

        if LIVE_RE.match(text):
            self.live_enabled = True
            self.bot.send("Live monitoring enabled.")
            return

        if cmd in ("/chain", "chain"):
            self.handle_chain()
        elif cmd in ("/status", "status"):
            self.handle_status()
        elif cmd in ("/expiry", "expiry"):
            self.bot.send(f"Current weekly expiry: <b>{self.ensure_expiry()}</b>")
        elif cmd in ("/help", "help", "/start", "start"):
            self.bot.send(HELP_TEXT)
        else:
            self.bot.send(f"Unknown command: <code>{tg_escape(text)}</code>\n\n{HELP_TEXT}")

    def run(self) -> None:
        print(f"Brahmastra bot started | {NIFTY50_NAME}")
        self.bot.send(
            f"<b>{NIFTY50_NAME} Brahmastra Bot is online</b>\n\n"
            f"Live monitoring: {'ON' if self.live_enabled else 'OFF'}\n"
            f"Send <code>SCAN YYYY-MM-DD YYYY-MM-DD</code> for backtest.\n"
            f"Send <code>/help</code> for commands."
        )

        while True:
            try:
                for msg in self.bot.get_messages():
                    print(f"Message: {msg}")
                    try:
                        self.dispatch(msg)
                    except Exception as exc:
                        err = f"Error: {exc}"
                        print(err)
                        self.bot.send(tg_escape(err))

                try:
                    self.live_check()
                except Exception as exc:
                    print(f"Live check error: {exc}")

                time.sleep(self.cfg.tg_poll_interval)

            except KeyboardInterrupt:
                self.bot.send(f"{NIFTY50_NAME} Brahmastra Bot stopped.")
                print("Stopped.")
                return
            except requests.HTTPError as exc:
                print(f"HTTP error: {exc}")
                time.sleep(5)
            except Exception as exc:
                print(f"Error: {exc}")
                time.sleep(5)


def main() -> None:
    cfg = Config.from_env()
    BrahmastraBot(cfg).run()


if __name__ == "__main__":
    main()
