"""
BTCUSDT Session ORB long/short signal bot for Shark Exchange.

What this program does
- Monitors BTCUSDT futures price action from Shark public market endpoints.
- Uses the same Session ORB strategy:
  first 15 minutes define the opening range, then a 5m close breakout creates a signal.
- Sends LONG/SHORT levels with entry, stop loss, T1, and T2.
- Sends Telegram updates and alerts, but never places buy/sell orders.
- SCAN YYYY-MM-DD YYYY-MM-DD runs a candle backtest.
- STOP cancels a running scan.
- LIVE enables live monitoring again.
- Uses IST timestamps.

Important:
- This bot no longer uses Shark option-chain data.
- T2 is the configured strategy target, default 1:2 risk-reward.
- T1 is a 1R checkpoint level.
- This is a signal assistant, not financial advice.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

IST = ZoneInfo("Asia/Kolkata")
TELEGRAM_BASE = "https://api.telegram.org"

SCAN_RE = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
STOP_RE = re.compile(r"^STOP$", re.IGNORECASE)
LIVE_RE = re.compile(r"^LIVE$", re.IGNORECASE)

LOGGER = logging.getLogger("shark_btcusdt_orb_levels_bot")
DEFAULT_ORB_SESSIONS = "London|13:30|13:45|14:45;NY1|18:30|18:45|19:45;NY2|20:30|20:45|21:45"


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str

    shark_public_base: str = "https://api.sharkexchange.in"
    contract_pair: str = "BTCUSDT"

    candle_interval: str = "5m"
    candle_limit: int = 180
    http_timeout: int = 15
    tg_poll_interval: float = 5.0
    live_check_interval: float = 30.0

    strategy_name: str = "SESSION_ORB_15M_TO_5M"
    orb_sessions: str = DEFAULT_ORB_SESSIONS
    risk_reward: float = 2.0
    live_close_buffer_seconds: int = 10
    min_signal_score: int = 8
    trade_size_btc: float = 0.01
    price_tick: float = 0.01

    log_level: str = "INFO"
    log_file: str = "logs/shark_btcusdt_orb_levels_bot.log"
    backtest_results_dir: str = "backtests"
    backtest_send_each_alert: bool = True
    backtest_progress_every: int = 250

    @staticmethod
    def from_env() -> "Config":
        required = {
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

        return Config(
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            shark_public_base=os.getenv("SHARK_PUBLIC_BASE", "https://api.sharkexchange.in").strip().rstrip("/"),
            contract_pair=os.getenv("SHARK_CONTRACT_PAIR", "BTCUSDT").strip().upper(),
            candle_interval=os.getenv("CANDLE_INTERVAL", "5m").strip(),
            candle_limit=_int("CANDLE_LIMIT", 180),
            http_timeout=_int("HTTP_TIMEOUT", 15),
            tg_poll_interval=_float("TG_POLL_INTERVAL", 5.0),
            live_check_interval=_float("LIVE_CHECK_INTERVAL", 30.0),
            strategy_name=os.getenv("STRATEGY_NAME", "SESSION_ORB_15M_TO_5M").strip().upper(),
            orb_sessions=os.getenv("ORB_SESSIONS", DEFAULT_ORB_SESSIONS).strip(),
            risk_reward=_float("RISK_REWARD", 2.0),
            live_close_buffer_seconds=_int("LIVE_CLOSE_BUFFER_SECONDS", 10),
            min_signal_score=_int("MIN_SIGNAL_SCORE", 8),
            trade_size_btc=_float("TRADE_SIZE_BTC", 0.01),
            price_tick=_float("PRICE_TICK", 0.01),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_file=os.getenv("LOG_FILE", "logs/shark_btcusdt_orb_levels_bot.log").strip(),
            backtest_results_dir=os.getenv("BACKTEST_RESULTS_DIR", "backtests").strip(),
            backtest_send_each_alert=_bool("BACKTEST_SEND_EACH_ALERT", True),
            backtest_progress_every=_int("BACKTEST_PROGRESS_EVERY", 250),
        )


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    if LOGGER.handlers:
        LOGGER.setLevel(level)
        return

    base_dir = Path(__file__).resolve().parent
    log_path = Path(cfg.log_file)
    if not log_path.is_absolute():
        log_path = base_dir / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.setLevel(level)
    LOGGER.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    LOGGER.addHandler(console_handler)
    LOGGER.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    LOGGER.info("Logging initialized | level=%s | file=%s", cfg.log_level.upper(), log_path)


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
    first_open: float
    period_high: float
    period_low: float
    last_close: float
    vwap: float
    trend: str
    htf_trend: str
    candle_count: int
    last_candle: Candle
    recent_support: float
    recent_resistance: float
    prev_candle_high: float
    prev_candle_low: float
    recent_avg_volume: float
    atr: float


@dataclass(frozen=True)
class OrbSession:
    name: str
    range_start: dt.time
    trade_start: dt.time
    trade_end: dt.time


@dataclass
class OrbSetup:
    session_name: str
    range_start: dt.datetime
    range_end: dt.datetime
    trade_end: dt.datetime
    range_high: float
    range_low: float
    range_width: float
    breakout_time: dt.datetime
    direction: str
    underlying_entry: float
    underlying_stop: float
    underlying_target: float
    rr: float


@dataclass
class TradePlan:
    action: str
    side: str
    entry_type: str
    entry_price: float
    stop_loss: float
    target1: float
    target2: float
    risk: float
    reward1: float
    reward2: float
    rr1: float
    rr2: float
    current_price: float
    invalidation_price: float


@dataclass
class Signal:
    timestamp: str
    candle_time: str
    underlying_symbol: str
    direction: str
    score: int
    max_score: int
    confidence: str
    pattern_names: List[str]
    reasons: List[str]
    underlying_price: float
    vwap: float
    trend: str
    htf_trend: str
    support: float
    resistance: float
    atr: float
    trade_plan: TradePlan
    strategy: str = "SESSION_ORB_15M_TO_5M"
    session_name: Optional[str] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    trade_end: Optional[str] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    range_width: Optional[float] = None
    underlying_entry: Optional[float] = None
    underlying_stop: Optional[float] = None
    underlying_target: Optional[float] = None
    underlying_rr: Optional[float] = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, default=str)


class SharkApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.public = requests.Session()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "btcusdt-orb-levels-signal-bot/1.0",
        }
        self.public.headers.update(headers)

    def ticker24h(self, pair: Optional[str] = None) -> Dict[str, Any]:
        pair = pair or self.cfg.contract_pair
        LOGGER.info("Fetching 24h ticker | pair=%s", pair)
        r = self.public.get(
            f"{self.cfg.shark_public_base}/v1/market/ticker24Hr/{pair}",
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        raw = r.json()
        data = raw.get("data", raw)
        LOGGER.info(
            "Fetched 24h ticker | pair=%s | last=%s | change_pct=%s",
            pair,
            data.get("c") or data.get("lastPrice"),
            data.get("P") or data.get("priceChangePercent"),
        )
        return data

    def klines(
        self,
        pair: Optional[str] = None,
        interval: Optional[str] = None,
        limit: Optional[int] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        price_type: str = "LAST_PRICE",
    ) -> pd.DataFrame:
        payload: Dict[str, Any] = {
            "pair": pair or self.cfg.contract_pair,
            "interval": interval or self.cfg.candle_interval,
            "limit": limit or self.cfg.candle_limit,
        }
        if start_ms is not None:
            payload["startTime"] = int(start_ms)
        if end_ms is not None:
            payload["endTime"] = int(end_ms)

        LOGGER.info(
            "Fetching klines | pair=%s | interval=%s | limit=%s | start_ms=%s | end_ms=%s | price_type=%s",
            payload["pair"],
            payload["interval"],
            payload["limit"],
            payload.get("startTime"),
            payload.get("endTime"),
            price_type,
        )
        r = self.public.post(
            f"{self.cfg.shark_public_base}/v1/market/klines",
            params={"priceType": price_type},
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        df = self._normalize_klines(r.json())
        LOGGER.info(
            "Fetched klines | rows=%s | first=%s | last=%s",
            len(df),
            df.iloc[0].timestamp if not df.empty else None,
            df.iloc[-1].timestamp if not df.empty else None,
        )
        return df

    def historical_klines(self, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        cursor = start_ms
        frames: List[pd.DataFrame] = []
        batch = 0
        LOGGER.info("Fetching historical klines | start=%s | end=%s", start, end)

        while cursor < end_ms:
            batch += 1
            df = self.klines(start_ms=cursor, end_ms=end_ms, limit=1000, price_type="LAST_PRICE")
            if df.empty:
                LOGGER.info("Historical kline batch empty | batch=%s | cursor=%s", batch, cursor)
                break
            frames.append(df)
            LOGGER.info("Historical kline batch fetched | batch=%s | rows=%s", batch, len(df))
            last_end = int(df["end_ms"].max())
            next_cursor = last_end + 1
            if next_cursor <= cursor:
                LOGGER.warning("Historical kline cursor did not advance | cursor=%s | next_cursor=%s", cursor, next_cursor)
                break
            cursor = next_cursor
            if len(df) < 1000:
                break
            time.sleep(0.12)

        if not frames:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "end_ms"])
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        out = out.reset_index(drop=True)
        LOGGER.info("Historical klines complete | rows=%s | start=%s | end=%s", len(out), out.iloc[0].timestamp, out.iloc[-1].timestamp)
        return out

    @staticmethod
    def _normalize_klines(raw: Any) -> pd.DataFrame:
        rows = raw.get("data", raw) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise ValueError(f"Unexpected kline response shape: {type(raw)}")

        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "end_ms"])

        if "startTime" not in df.columns:
            raise ValueError(f"Kline response missing startTime; got {list(df.columns)}")

        df["timestamp"] = (
            pd.to_datetime(pd.to_numeric(df["startTime"], errors="coerce"), unit="ms", utc=True)
            .dt.tz_convert(IST)
            .dt.tz_localize(None)
        )
        df["end_ms"] = pd.to_numeric(df.get("endTime", df["startTime"]), errors="coerce")
        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                df[col] = 0.0
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["timestamp", "open", "high", "low", "close"]).sort_values("timestamp")
        return df[["timestamp", "open", "high", "low", "close", "volume", "end_ms"]].reset_index(drop=True)


class TelegramBot:
    def __init__(self, token: str, chat_id: str, timeout: int = 15):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.session = requests.Session()
        self._offset = 0

    def send(self, text: str) -> None:
        LOGGER.info("Sending Telegram message | chars=%s", len(text))
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
        LOGGER.info("Telegram message sent")

    def get_messages(self) -> List[str]:
        try:
            LOGGER.debug("Polling Telegram updates | offset=%s", self._offset)
            r = self.session.get(
                f"{TELEGRAM_BASE}/bot{self.token}/getUpdates",
                params={"offset": self._offset, "timeout": 0},
                timeout=self.timeout + 5,
            )
            r.raise_for_status()
        except Exception:
            LOGGER.exception("Failed to poll Telegram updates")
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
            LOGGER.info("Received Telegram commands | count=%s", len(texts))
        return texts


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None and v != "" else default
    except Exception:
        return default


def _fmt(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{decimals}f}"
    except Exception:
        return str(v)


def _now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _quote_currency_for_pair(pair: str) -> str:
    pair = pair.upper()
    for quote in ("USDT", "USD", "INR"):
        if pair.endswith(quote):
            return quote
    return ""


def _format_price(pair: str, value: Any) -> str:
    quote = _quote_currency_for_pair(pair)
    if quote == "INR":
        return f"INR {_fmt(value)}"
    if quote in {"USDT", "USD"}:
        return f"{_fmt(value)} {quote}"
    return _fmt(value)


def _parse_date_ist(value: str, end_of_day: bool = False) -> dt.datetime:
    date_value = dt.datetime.strptime(value, "%Y-%m-%d").date()
    if end_of_day:
        return dt.datetime.combine(date_value, dt.time(23, 59, 59), tzinfo=IST)
    return dt.datetime.combine(date_value, dt.time(0, 0, 0), tzinfo=IST)


def _parse_hhmm(value: str) -> dt.time:
    return dt.datetime.strptime(value.strip(), "%H:%M").time()


def _parse_orb_sessions(raw: str) -> List[OrbSession]:
    sessions: List[OrbSession] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 4:
            raise ValueError(
                "Invalid ORB_SESSIONS format. Use Name|range_start|trade_start|trade_end; "
                "example: London|13:30|13:45|14:45"
            )
        sessions.append(
            OrbSession(
                name=parts[0],
                range_start=_parse_hhmm(parts[1]),
                trade_start=_parse_hhmm(parts[2]),
                trade_end=_parse_hhmm(parts[3]),
            )
        )
    if not sessions:
        raise ValueError("ORB_SESSIONS must contain at least one session.")
    return sessions


def _as_naive_ist(value: Any) -> dt.datetime:
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is not None:
            return value.tz_convert(IST).tz_localize(None).to_pydatetime()
        return value.to_pydatetime()
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return value.astimezone(IST).replace(tzinfo=None)
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Could not parse timestamp: {value}")
    return _as_naive_ist(parsed)


def _session_datetimes(session: OrbSession, day: dt.date) -> Tuple[dt.datetime, dt.datetime, dt.datetime]:
    return (
        dt.datetime.combine(day, session.range_start),
        dt.datetime.combine(day, session.trade_start),
        dt.datetime.combine(day, session.trade_end),
    )


def _active_orb_session(ts: dt.datetime, cfg: Config, include_range: bool = False) -> Optional[OrbSession]:
    ts = _as_naive_ist(ts)
    for session in _parse_orb_sessions(cfg.orb_sessions):
        range_start, trade_start, trade_end = _session_datetimes(session, ts.date())
        start = range_start if include_range else trade_start
        if start <= ts < trade_end:
            return session
    return None


def _orb_sessions_text(cfg: Config) -> str:
    parts = []
    for session in _parse_orb_sessions(cfg.orb_sessions):
        parts.append(
            f"{session.name} {session.range_start.strftime('%H:%M')}-"
            f"{session.trade_start.strftime('%H:%M')} range, "
            f"{session.trade_start.strftime('%H:%M')}-{session.trade_end.strftime('%H:%M')} trade"
        )
    return "; ".join(parts)


def _next_orb_session_text(cfg: Config, now: Optional[dt.datetime] = None) -> str:
    now_naive = _as_naive_ist(now or _now_ist())
    candidates: List[Tuple[dt.datetime, OrbSession]] = []
    sessions = _parse_orb_sessions(cfg.orb_sessions)
    for day_offset in range(2):
        day = now_naive.date() + dt.timedelta(days=day_offset)
        for session in sessions:
            range_start, _, trade_end = _session_datetimes(session, day)
            if now_naive < trade_end:
                candidates.append((range_start, session))
    if not candidates:
        return "-"
    start, session = sorted(candidates, key=lambda item: item[0])[0]
    _, trade_start, trade_end = _session_datetimes(session, start.date())
    return f"{session.name}: range {start.strftime('%H:%M')}-{trade_start.strftime('%H:%M')}, trade {trade_start.strftime('%H:%M')}-{trade_end.strftime('%H:%M')} IST"


def _interval_delta(interval: str) -> dt.timedelta:
    m = re.match(r"^(\d+)\s*([mhd])$", interval.strip().lower())
    if not m:
        return dt.timedelta(minutes=5)
    qty = int(m.group(1))
    unit = m.group(2)
    if unit == "h":
        return dt.timedelta(hours=qty)
    if unit == "d":
        return dt.timedelta(days=qty)
    return dt.timedelta(minutes=qty)


def _latest_closed_candle_index(candles: pd.DataFrame, cfg: Config, now: Optional[dt.datetime] = None) -> Optional[int]:
    if candles.empty:
        return None
    now_naive = _as_naive_ist(now or _now_ist())
    interval = _interval_delta(cfg.candle_interval)
    buffer = dt.timedelta(seconds=max(0, cfg.live_close_buffer_seconds))
    for idx in range(len(candles) - 1, -1, -1):
        candle_start = _as_naive_ist(candles.iloc[idx].timestamp)
        if candle_start + interval + buffer <= now_naive:
            return idx
    return None


def _live_orb_active_now(cfg: Config, now: Optional[dt.datetime] = None) -> bool:
    now_naive = _as_naive_ist(now or _now_ist())
    return _active_orb_session(now_naive, cfg, include_range=True) is not None


def _round_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return round(value, 2)
    return round(round(value / tick) * tick, 8)


def _calc_vwap(candles: Iterable[Candle]) -> float:
    candle_list = list(candles)
    num = sum(((c.high + c.low + c.close) / 3.0) * max(c.volume, 0.0) for c in candle_list)
    den = sum(max(c.volume, 0.0) for c in candle_list)
    if den > 0:
        return num / den
    return sum(c.close for c in candle_list) / max(len(candle_list), 1)


def _atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    true_ranges: List[float] = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        true_ranges.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    recent = true_ranges[-period:]
    return float(sum(recent) / max(len(recent), 1))


def _htf_trend(df: pd.DataFrame) -> str:
    if len(df) < 12:
        return "sideways"
    data = df.copy()
    data = data.set_index("timestamp")
    htf = data.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    htf = htf.dropna(subset=["open", "high", "low", "close"])
    if len(htf) < 3:
        return "sideways"
    ema_fast = htf["close"].ewm(span=5, adjust=False).mean().iloc[-1]
    ema_slow = htf["close"].ewm(span=13, adjust=False).mean().iloc[-1]
    close = htf["close"].iloc[-1]
    if close > ema_fast > ema_slow:
        return "uptrend"
    if close < ema_fast < ema_slow:
        return "downtrend"
    return "sideways"


def _analyse_intraday(df: pd.DataFrame) -> Optional[IntradayContext]:
    if df.empty:
        return None

    candles = [
        Candle(
            ts=(row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in df.itertuples(index=False)
    ]

    first_open = candles[0].open
    period_high = max(c.high for c in candles)
    period_low = min(c.low for c in candles)
    last_close = candles[-1].close
    vwap = _calc_vwap(candles)

    ema_fast = df["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema_slow = df["close"].ewm(span=21, adjust=False).mean().iloc[-1]
    if last_close > vwap and ema_fast > ema_slow:
        trend = "uptrend"
    elif last_close < vwap and ema_fast < ema_slow:
        trend = "downtrend"
    else:
        trend = "sideways"

    recent = candles[-12:] if len(candles) >= 12 else candles
    prev_candle = candles[-2] if len(candles) >= 2 else candles[-1]
    recent_avg_volume = float(sum(c.volume for c in recent) / max(len(recent), 1))

    return IntradayContext(
        first_open=first_open,
        period_high=period_high,
        period_low=period_low,
        last_close=last_close,
        vwap=vwap,
        trend=trend,
        htf_trend=_htf_trend(df),
        candle_count=len(candles),
        last_candle=candles[-1],
        recent_support=min(c.low for c in recent),
        recent_resistance=max(c.high for c in recent),
        prev_candle_high=prev_candle.high,
        prev_candle_low=prev_candle.low,
        recent_avg_volume=recent_avg_volume,
        atr=_atr(candles),
    )


def _signal_confidence(score: int, max_score: int) -> str:
    pct = (score / max_score) * 100 if max_score > 0 else 0
    if pct >= 85:
        return "Strong"
    if pct >= 70:
        return "Good"
    return "Weak"


def find_orb_setup(candles: pd.DataFrame, cfg: Config, idx: int) -> Optional[OrbSetup]:
    """Return the first 5m close breakout/breakdown for the active ORB session."""
    if candles.empty or idx < 0 or idx >= len(candles):
        return None

    window = candles.iloc[: idx + 1].copy().reset_index(drop=True)
    if window.empty:
        return None
    window["_ts"] = window["timestamp"].map(_as_naive_ist)

    latest = window.iloc[-1]
    candle_time = latest["_ts"]
    session = _active_orb_session(candle_time, cfg, include_range=False)
    if session is None:
        LOGGER.debug("ORB skipped | outside trade windows | candle_time=%s", candle_time)
        return None

    range_start, trade_start, trade_end = _session_datetimes(session, candle_time.date())
    range_df = window[(window["_ts"] >= range_start) & (window["_ts"] < trade_start)]
    if range_df.empty:
        LOGGER.info("ORB skipped | opening range missing | session=%s | candle_time=%s", session.name, candle_time)
        return None

    trade_df = window[(window["_ts"] >= trade_start) & (window["_ts"] <= candle_time)]
    if trade_df.empty:
        return None

    range_high = float(range_df["high"].max())
    range_low = float(range_df["low"].min())
    range_width = range_high - range_low
    if range_width <= 0:
        LOGGER.info("ORB skipped | invalid range | session=%s | high=%s | low=%s", session.name, range_high, range_low)
        return None

    previous_trade_df = trade_df.iloc[:-1]
    previous_breakout = (previous_trade_df["close"] > range_high).any() if not previous_trade_df.empty else False
    previous_breakdown = (previous_trade_df["close"] < range_low).any() if not previous_trade_df.empty else False
    if previous_breakout or previous_breakdown:
        LOGGER.debug("ORB skipped | session already triggered | session=%s | candle_time=%s", session.name, candle_time)
        return None

    close = float(latest.close)
    if close > range_high:
        direction = "BULLISH"
        stop = range_low
        risk = close - stop
        target = close + (risk * cfg.risk_reward)
    elif close < range_low:
        direction = "BEARISH"
        stop = range_high
        risk = stop - close
        target = close - (risk * cfg.risk_reward)
    else:
        LOGGER.debug(
            "ORB skipped | no close breakout | session=%s | close=%s | high=%s | low=%s",
            session.name,
            close,
            range_high,
            range_low,
        )
        return None

    if risk <= 0:
        LOGGER.info("ORB skipped | invalid risk | session=%s | direction=%s | risk=%s", session.name, direction, risk)
        return None

    LOGGER.info(
        "ORB setup found | session=%s | candle_time=%s | direction=%s | close=%.2f | range_high=%.2f | range_low=%.2f | target=%.2f",
        session.name,
        candle_time,
        direction,
        close,
        range_high,
        range_low,
        target,
    )
    return OrbSetup(
        session_name=session.name,
        range_start=range_start,
        range_end=trade_start,
        trade_end=trade_end,
        range_high=range_high,
        range_low=range_low,
        range_width=range_width,
        breakout_time=candle_time,
        direction=direction,
        underlying_entry=close,
        underlying_stop=stop,
        underlying_target=target,
        rr=cfg.risk_reward,
    )


def trade_plan_from_orb(orb: OrbSetup, ctx: IntradayContext, cfg: Config) -> TradePlan:
    tick = cfg.price_tick
    entry = _round_to_tick(orb.underlying_entry, tick)
    stop = _round_to_tick(orb.underlying_stop, tick)
    risk = max(abs(entry - stop), tick)

    if orb.direction == "BULLISH":
        target1 = _round_to_tick(entry + risk, tick)
        target2 = _round_to_tick(orb.underlying_target, tick)
        action = f"LONG {cfg.contract_pair}"
        side = "LONG"
        entry_type = "Buy after confirmed 5m close"
    else:
        target1 = _round_to_tick(entry - risk, tick)
        target2 = _round_to_tick(orb.underlying_target, tick)
        action = f"SHORT {cfg.contract_pair}"
        side = "SHORT"
        entry_type = "Sell after confirmed 5m close"

    reward1 = abs(target1 - entry)
    reward2 = abs(target2 - entry)
    return TradePlan(
        action=action,
        side=side,
        entry_type=entry_type,
        entry_price=round(entry, 2),
        stop_loss=round(stop, 2),
        target1=round(target1, 2),
        target2=round(target2, 2),
        risk=round(risk, 2),
        reward1=round(reward1, 2),
        reward2=round(reward2, 2),
        rr1=round(reward1 / risk, 1) if risk > 0 else 0.0,
        rr2=round(reward2 / risk, 1) if risk > 0 else 0.0,
        current_price=round(ctx.last_close, 2),
        invalidation_price=round(stop, 2),
    )


def preview_plan_from_range(
    side: str,
    entry: float,
    stop: float,
    current_price: float,
    cfg: Config,
) -> TradePlan:
    tick = cfg.price_tick
    entry = _round_to_tick(entry, tick)
    stop = _round_to_tick(stop, tick)
    risk = max(abs(entry - stop), tick)
    if side == "LONG":
        target1 = _round_to_tick(entry + risk, tick)
        target2 = _round_to_tick(entry + risk * cfg.risk_reward, tick)
        action = f"LONG {cfg.contract_pair}"
        entry_type = "Needs 5m close above"
    else:
        target1 = _round_to_tick(entry - risk, tick)
        target2 = _round_to_tick(entry - risk * cfg.risk_reward, tick)
        action = f"SHORT {cfg.contract_pair}"
        entry_type = "Needs 5m close below"

    return TradePlan(
        action=action,
        side=side,
        entry_type=entry_type,
        entry_price=round(entry, 2),
        stop_loss=round(stop, 2),
        target1=round(target1, 2),
        target2=round(target2, 2),
        risk=round(risk, 2),
        reward1=round(abs(target1 - entry), 2),
        reward2=round(abs(target2 - entry), 2),
        rr1=1.0,
        rr2=round(cfg.risk_reward, 1),
        current_price=round(current_price, 2),
        invalidation_price=round(stop, 2),
    )


def build_signal(api: SharkApiClient, candles: pd.DataFrame, idx: Optional[int] = None) -> Optional[Signal]:
    if candles.empty:
        LOGGER.debug("Signal skipped | candles empty")
        return None
    if idx is None:
        idx = len(candles) - 1
    window = candles.iloc[: idx + 1].copy().reset_index(drop=True)
    if len(window) < 4:
        LOGGER.debug("Signal skipped | insufficient candles=%s", len(window))
        return None

    ctx = _analyse_intraday(window)
    if ctx is None:
        LOGGER.debug("Signal skipped | context unavailable")
        return None

    orb = find_orb_setup(candles, api.cfg, idx)
    if orb is None:
        return None

    direction = orb.direction
    score = 8
    max_score = 10
    patterns = ["SESSION_ORB_BREAKOUT" if direction == "BULLISH" else "SESSION_ORB_BREAKDOWN"]
    reasons = [
        f"{orb.session_name} golden-hour opening range completed",
        "5-minute candle closed above opening range high" if direction == "BULLISH" else "5-minute candle closed below opening range low",
        f"Strategy target is fixed 1:{orb.rr:g} risk-reward",
    ]

    plan = trade_plan_from_orb(orb, ctx, api.cfg)
    signal = Signal(
        timestamp=_now_ist().isoformat(timespec="seconds"),
        candle_time=str(ctx.last_candle.ts),
        underlying_symbol=api.cfg.contract_pair,
        direction=direction,
        score=score,
        max_score=max_score,
        confidence=_signal_confidence(score, max_score),
        pattern_names=patterns,
        reasons=reasons,
        underlying_price=float(window.iloc[-1].close),
        vwap=ctx.vwap,
        trend=ctx.trend,
        htf_trend=ctx.htf_trend,
        support=ctx.recent_support,
        resistance=ctx.recent_resistance,
        atr=ctx.atr,
        trade_plan=plan,
        strategy=api.cfg.strategy_name,
        session_name=orb.session_name,
        range_start=orb.range_start.strftime("%Y-%m-%d %H:%M:%S"),
        range_end=orb.range_end.strftime("%Y-%m-%d %H:%M:%S"),
        trade_end=orb.trade_end.strftime("%Y-%m-%d %H:%M:%S"),
        range_high=orb.range_high,
        range_low=orb.range_low,
        range_width=orb.range_width,
        underlying_entry=orb.underlying_entry,
        underlying_stop=orb.underlying_stop,
        underlying_target=orb.underlying_target,
        underlying_rr=orb.rr,
    )
    LOGGER.info(
        "ORB signal built | session=%s | candle_time=%s | direction=%s | score=%s/%s | entry=%s | sl=%s | t2=%s",
        signal.session_name,
        signal.candle_time,
        signal.direction,
        signal.score,
        signal.max_score,
        signal.trade_plan.entry_price,
        signal.trade_plan.stop_loss,
        signal.trade_plan.target2,
    )
    return signal


def format_trade_plan(plan: TradePlan, pair: str) -> List[str]:
    return [
        f"Action      : {html.escape(plan.action)}",
        f"Entry       : {html.escape(plan.entry_type)} at {_format_price(pair, plan.entry_price)}",
        f"Stop Loss   : {_format_price(pair, plan.stop_loss)}",
        f"Target 1    : {_format_price(pair, plan.target1)}",
        f"Target 2    : {_format_price(pair, plan.target2)}",
        f"Risk        : {_fmt(plan.risk)} {_quote_currency_for_pair(pair)}",
        f"R:R         : 1:{plan.rr1} / 1:{plan.rr2}",
    ]


def format_signal(signal: Signal, alert: bool = True) -> str:
    plan = signal.trade_plan
    title = f"{signal.underlying_symbol} TRADE SIGNAL" if alert else f"{signal.underlying_symbol} TRADE SETUP"
    reasons_text = "\n".join(f"- {html.escape(r)}" for r in signal.reasons[:8]) or "- No extra reasons"
    patterns_text = ", ".join(signal.pattern_names) if signal.pattern_names else "-"

    lines = [
        f"<b>{title}</b>",
        f"<b>Strategy:</b> {html.escape(signal.strategy)}",
        f"<b>Session:</b> {html.escape(signal.session_name or '-')}",
        f"<b>Confidence:</b> {html.escape(signal.confidence)}",
        f"<b>Score:</b> {signal.score}/{signal.max_score}",
        f"<b>Candle Time:</b> {html.escape(signal.candle_time)} IST",
        f"<b>Direction:</b> {signal.direction}",
        f"<b>Patterns:</b> {html.escape(patterns_text)}",
        "",
        f"<b>Suggested {signal.underlying_symbol} Levels</b>",
        *format_trade_plan(plan, signal.underlying_symbol),
        "",
        "<b>BTC Context</b>",
        f"{signal.underlying_symbol:<11}: {_format_price(signal.underlying_symbol, signal.underlying_price)}",
        f"VWAP        : {_format_price(signal.underlying_symbol, signal.vwap)}",
        f"Trend       : {signal.trend} / 15m {signal.htf_trend}",
        f"Support     : {_format_price(signal.underlying_symbol, signal.support)}",
        f"Resistance  : {_format_price(signal.underlying_symbol, signal.resistance)}",
        f"Invalidation: {signal.underlying_symbol} around {_format_price(signal.underlying_symbol, plan.invalidation_price)}",
    ]
    if signal.range_high is not None and signal.range_low is not None:
        lines += [
            "",
            "<b>Opening Range</b>",
            f"Range candle: {html.escape(signal.range_start or '-')} to {html.escape(signal.range_end or '-')} IST",
            f"High / Low  : {_format_price(signal.underlying_symbol, signal.range_high)} / {_format_price(signal.underlying_symbol, signal.range_low)}",
            f"Range width : {_fmt(signal.range_width)} {_quote_currency_for_pair(signal.underlying_symbol)}",
            f"T2 target   : strategy 1:{signal.underlying_rr:g}" if signal.underlying_rr is not None else "T2 target   : strategy target",
        ]
    lines += [
        "",
        "<b>Why this signal</b>",
        reasons_text,
        "",
        "No auto order placed. Wait for the confirmed 5m close and manage risk.",
        f"<i>Updated: {_now_ist().strftime('%H:%M:%S IST')}</i>",
    ]
    return "\n".join(lines)


def format_levels_message(candles: pd.DataFrame, cfg: Config) -> str:
    if candles.empty:
        return f"Could not fetch {cfg.contract_pair} candles."

    idx = len(candles) - 1
    window = candles.iloc[: idx + 1].copy().reset_index(drop=True)
    window["_ts"] = window["timestamp"].map(_as_naive_ist)
    latest = window.iloc[-1]
    candle_time = latest["_ts"]
    current = float(latest.close)
    ctx = _analyse_intraday(window)

    session = _active_orb_session(candle_time, cfg, include_range=True)
    if session is None:
        return "\n".join(
            [
                f"<b>{cfg.contract_pair} Session ORB Levels</b>",
                f"Current : {_format_price(cfg.contract_pair, current)}",
                f"Candle  : {html.escape(str(candle_time))} IST",
                f"Next    : {_next_orb_session_text(cfg, candle_time)}",
                "",
                "No active ORB window right now.",
            ]
        )

    range_start, trade_start, trade_end = _session_datetimes(session, candle_time.date())
    range_so_far = window[(window["_ts"] >= range_start) & (window["_ts"] < min(candle_time + dt.timedelta(microseconds=1), trade_start))]
    range_df = window[(window["_ts"] >= range_start) & (window["_ts"] < trade_start)]

    if candle_time < trade_start:
        high = float(range_so_far["high"].max()) if not range_so_far.empty else current
        low = float(range_so_far["low"].min()) if not range_so_far.empty else current
        return "\n".join(
            [
                f"<b>{cfg.contract_pair} Session ORB Levels</b>",
                f"Session : {html.escape(session.name)}",
                f"Current : {_format_price(cfg.contract_pair, current)}",
                f"Range   : {range_start.strftime('%H:%M')}-{trade_start.strftime('%H:%M')} IST building",
                f"High/Low: {_format_price(cfg.contract_pair, high)} / {_format_price(cfg.contract_pair, low)}",
                "",
                "Trade levels activate after the 15-minute opening range closes.",
            ]
        )

    if range_df.empty:
        return f"Opening range data is missing for {session.name}."

    range_high = float(range_df["high"].max())
    range_low = float(range_df["low"].min())
    range_width = range_high - range_low
    if range_width <= 0:
        return f"Opening range is invalid for {session.name}."

    long_plan = preview_plan_from_range("LONG", range_high, range_low, current, cfg)
    short_plan = preview_plan_from_range("SHORT", range_low, range_high, current, cfg)

    lines = [
        f"<b>{cfg.contract_pair} Session ORB Levels</b>",
        f"Session : {html.escape(session.name)}",
        f"Current : {_format_price(cfg.contract_pair, current)}",
        f"Candle  : {html.escape(str(candle_time))} IST",
        f"Trade   : until {trade_end.strftime('%H:%M')} IST",
        f"Range   : {_format_price(cfg.contract_pair, range_high)} / {_format_price(cfg.contract_pair, range_low)}",
        f"Width   : {_fmt(range_width)} {_quote_currency_for_pair(cfg.contract_pair)}",
    ]
    if ctx:
        lines += [
            f"VWAP    : {_format_price(cfg.contract_pair, ctx.vwap)}",
            f"Trend   : {ctx.trend} / 15m {ctx.htf_trend}",
        ]
    lines += [
        "",
        "<b>Long Plan</b>",
        *format_trade_plan(long_plan, cfg.contract_pair),
        "",
        "<b>Short Plan</b>",
        *format_trade_plan(short_plan, cfg.contract_pair),
        "",
        "These are trigger levels. The strategy still requires a 5m close outside the range.",
        f"<i>Updated: {_now_ist().strftime('%H:%M:%S IST')}</i>",
    ]
    return "\n".join(lines)


def format_status_message(ticker: Dict[str, Any], candles: pd.DataFrame, signal: Optional[Signal], cfg: Config) -> str:
    ctx = _analyse_intraday(candles)
    last_price = _num(ticker.get("c") or ticker.get("lastPrice"))
    change_pct = _num(ticker.get("P") or ticker.get("priceChangePercent"))
    lines = [
        f"<b>{cfg.contract_pair} Session ORB Watch</b>",
        f"Underlying : {cfg.contract_pair}",
        f"{cfg.contract_pair:<11}: {_format_price(cfg.contract_pair, last_price)}",
        f"24h Change : {change_pct:.3f}%",
        f"Strategy   : {cfg.strategy_name}",
        f"Windows    : {_orb_sessions_text(cfg)}",
        f"Next window: {_next_orb_session_text(cfg)}",
    ]
    if ctx:
        lines += [
            f"VWAP       : {_format_price(cfg.contract_pair, ctx.vwap)}",
            f"Trend      : {ctx.trend} / 15m {ctx.htf_trend}",
            f"Support    : {_format_price(cfg.contract_pair, ctx.recent_support)}",
            f"Resistance : {_format_price(cfg.contract_pair, ctx.recent_resistance)}",
            f"ATR        : {_fmt(ctx.atr)} {_quote_currency_for_pair(cfg.contract_pair)}",
        ]
    if signal:
        lines += [
            "",
            f"Current setup: {signal.trade_plan.side} {signal.score}/{signal.max_score}",
            f"Session      : {signal.session_name or '-'}",
            f"Entry        : {_format_price(cfg.contract_pair, signal.trade_plan.entry_price)}",
            f"SL / T1 / T2 : {_format_price(cfg.contract_pair, signal.trade_plan.stop_loss)} / "
            f"{_format_price(cfg.contract_pair, signal.trade_plan.target1)} / "
            f"{_format_price(cfg.contract_pair, signal.trade_plan.target2)}",
        ]
    else:
        lines += ["", "Current setup: No ORB breakout setup."]
    lines.append(f"<i>Updated: {_now_ist().strftime('%H:%M:%S IST')}</i>")
    return "\n".join(lines)


HELP_TEXT = """<b>BTCUSDT Session ORB Bot Commands</b>

<b>Strategy</b>
  Session ORB: mark first 15 minutes, trade 5m close breakout only
  Default IST windows: London 13:45-14:45, NY1 18:45-19:45, NY2 20:45-21:45
  T1: 1R checkpoint | T2: configured strategy target, default 1:2

<b>Live control</b>
  LIVE   - enable live monitoring
  STOP   - stop running scan

<b>Signals</b>
  /signal - current ORB breakout setup
  /levels - current long and short ORB trigger levels
  /status - BTCUSDT price, trend, window, current setup

<b>Backtest scan</b>
  SCAN 2026-05-01 2026-05-03

This bot sends alerts only when the ORB setup triggers. It does not place orders.
"""


def _signal_to_backtest_row(signal: Signal, outcome: Optional[Dict[str, Any]] = None, qty_btc: float = 0.01) -> Dict[str, Any]:
    plan = signal.trade_plan
    row = {
        "timestamp": signal.timestamp,
        "candle_time": signal.candle_time,
        "strategy": signal.strategy,
        "session": signal.session_name,
        "direction": signal.direction,
        "score": signal.score,
        "max_score": signal.max_score,
        "confidence": signal.confidence,
        "range_start": signal.range_start,
        "range_end": signal.range_end,
        "range_high": signal.range_high,
        "range_low": signal.range_low,
        "range_width": signal.range_width,
        "underlying_symbol": signal.underlying_symbol,
        "underlying_price": signal.underlying_price,
        "entry": plan.entry_price,
        "stop_loss": plan.stop_loss,
        "target1": plan.target1,
        "target2": plan.target2,
        "risk": plan.risk,
        "rr1": plan.rr1,
        "rr2": plan.rr2,
        "qty_btc": qty_btc,
        "vwap": signal.vwap,
        "trend": signal.trend,
        "htf_trend": signal.htf_trend,
        "support": signal.support,
        "resistance": signal.resistance,
        "atr": signal.atr,
        "patterns": ", ".join(signal.pattern_names),
        "reasons": " | ".join(signal.reasons),
    }
    if outcome:
        row.update({f"backtest_{k}": v for k, v in outcome.items()})
        r_multiple = _num(outcome.get("r_multiple"))
        est_pnl_usdt = plan.risk * r_multiple * qty_btc
        row["backtest_est_pnl_usdt"] = round(est_pnl_usdt, 4)
    return row


def _simulate_orb_outcome(candles: pd.DataFrame, signal: Signal) -> Dict[str, Any]:
    entry_time = _as_naive_ist(signal.candle_time)
    trade_end = _as_naive_ist(signal.trade_end) if signal.trade_end else entry_time
    data = candles.copy()
    data["_ts"] = data["timestamp"].map(_as_naive_ist)
    after = data[(data["_ts"] > entry_time) & (data["_ts"] < trade_end)]

    entry = float(signal.trade_plan.entry_price)
    stop = float(signal.trade_plan.stop_loss)
    target = float(signal.trade_plan.target2)
    risk = abs(entry - stop)
    if risk <= 0:
        return {"result": "NO_SIM", "exit_time": None, "exit_price": None, "r_multiple": 0.0, "bars_held": 0}

    bars = 0
    for _, row in after.iterrows():
        bars += 1
        high = float(row.high)
        low = float(row.low)
        ts = row["_ts"]
        if signal.direction == "BULLISH":
            stop_hit = low <= stop
            target_hit = high >= target
            if stop_hit:
                return {"result": "LOSS", "exit_time": str(ts), "exit_price": stop, "r_multiple": -1.0, "bars_held": bars}
            if target_hit:
                return {"result": "WIN", "exit_time": str(ts), "exit_price": target, "r_multiple": signal.trade_plan.rr2, "bars_held": bars}
        else:
            stop_hit = high >= stop
            target_hit = low <= target
            if stop_hit:
                return {"result": "LOSS", "exit_time": str(ts), "exit_price": stop, "r_multiple": -1.0, "bars_held": bars}
            if target_hit:
                return {"result": "WIN", "exit_time": str(ts), "exit_price": target, "r_multiple": signal.trade_plan.rr2, "bars_held": bars}

    if after.empty:
        return {"result": "OPEN", "exit_time": None, "exit_price": None, "r_multiple": 0.0, "bars_held": 0}

    last = after.iloc[-1]
    exit_price = float(last.close)
    if signal.direction == "BULLISH":
        r_multiple = (exit_price - entry) / risk
    else:
        r_multiple = (entry - exit_price) / risk
    return {
        "result": "TIME_EXIT",
        "exit_time": str(last["_ts"]),
        "exit_price": round(exit_price, 2),
        "r_multiple": round(r_multiple, 2),
        "bars_held": bars,
    }


def _save_backtest_results(records: List[Dict[str, Any]], cfg: Config, start_date: str, end_date: str) -> Optional[Path]:
    if not records:
        return None
    base_dir = Path(__file__).resolve().parent
    results_dir = Path(cfg.backtest_results_dir)
    if not results_dir.is_absolute():
        results_dir = base_dir / results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_ist().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"btcusdt_orb_levels_scan_{start_date}_to_{end_date}_{stamp}.csv"
    pd.DataFrame(records).to_csv(path, index=False)
    LOGGER.info("Backtest CSV saved | path=%s | rows=%s", path, len(records))
    return path


def _format_backtest_summary(
    start_date: str,
    end_date: str,
    total_candles: int,
    evaluated: int,
    alerts: List[Signal],
    duplicate_count: int,
    result_path: Optional[Path],
    records: Optional[List[Dict[str, Any]]] = None,
) -> str:
    bullish = sum(1 for s in alerts if s.direction == "BULLISH")
    bearish = sum(1 for s in alerts if s.direction == "BEARISH")
    avg_score = sum(s.score for s in alerts) / len(alerts) if alerts else 0.0
    top = max(alerts, key=lambda s: s.score, default=None)
    records = records or []
    wins = sum(1 for r in records if r.get("backtest_result") == "WIN")
    losses = sum(1 for r in records if r.get("backtest_result") == "LOSS")
    time_exits = sum(1 for r in records if r.get("backtest_result") == "TIME_EXIT")
    total_r = sum(_num(r.get("backtest_r_multiple")) for r in records)
    total_pnl_usdt = sum(_num(r.get("backtest_est_pnl_usdt")) for r in records)
    qty_btc = _num(records[0].get("qty_btc"), 0.01) if records else 0.01
    overall = "PROFIT" if total_r > 0 else ("LOSS" if total_r < 0 else "BREAKEVEN")
    decided = wins + losses
    win_rate = (wins / decided * 100) if decided else 0.0

    lines = [
        "<b>Session ORB Backtest Completed</b>",
        f"Range      : {html.escape(start_date)} to {html.escape(end_date)}",
        f"Overall   : {overall}",
        f"Candles    : {total_candles}",
        f"Evaluated  : {evaluated}",
        f"Alerts     : {len(alerts)}",
        f"Long / Short: {bullish} / {bearish}",
        f"Wins/Losses: {wins} / {losses}",
        f"Time exits : {time_exits}",
        f"Win rate   : {win_rate:.1f}%",
        f"Total R    : {total_r:.2f}",
        f"Est P&L    : {total_pnl_usdt:,.4f} USDT for {qty_btc:g} BTC basis",
        f"Duplicates : {duplicate_count}",
        f"Avg Score  : {avg_score:.2f}",
    ]
    if top:
        lines += [
            "",
            "<b>Top Setup</b>",
            f"Time   : {html.escape(top.candle_time)} IST",
            f"Side   : {top.trade_plan.side}",
            f"Score  : {top.score}/{top.max_score}",
            f"Entry  : {_format_price(top.underlying_symbol, top.trade_plan.entry_price)}",
            f"SL/T1/T2: {_format_price(top.underlying_symbol, top.trade_plan.stop_loss)} / "
            f"{_format_price(top.underlying_symbol, top.trade_plan.target1)} / "
            f"{_format_price(top.underlying_symbol, top.trade_plan.target2)}",
        ]
    if result_path:
        lines += ["", f"CSV saved: <code>{html.escape(str(result_path))}</code>"]
    lines += [
        "",
        "Note: win/loss is based on the underlying opening-range SL and T2 target.",
    ]
    return "\n".join(lines)


class BtcOrbLevelsSignalAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = SharkApiClient(cfg)
        self.bot = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        LOGGER.info(
            "Agent initialized | pair=%s | interval=%s | min_score=%s | live_interval=%ss",
            cfg.contract_pair,
            cfg.candle_interval,
            cfg.min_signal_score,
            cfg.live_check_interval,
        )

        self.live_enabled = True
        self.last_live_key: Optional[str] = None
        self.last_live_candle_time: Optional[str] = None
        self.last_live_check_ts = 0.0

        self.scan_thread: Optional[threading.Thread] = None
        self.scan_stop_event = threading.Event()
        self.scan_lock = threading.Lock()

    def _current_candles(self) -> pd.DataFrame:
        LOGGER.info("Fetching current candles")
        candles = self.api.klines(limit=self.cfg.candle_limit, price_type="LAST_PRICE")
        LOGGER.info("Current candles ready | rows=%s", len(candles))
        return candles

    def _current_signal(self, allow_weak: bool = False) -> Optional[Signal]:
        LOGGER.info("Evaluating current signal | allow_weak=%s", allow_weak)
        candles = self._current_candles()
        idx = _latest_closed_candle_index(candles, self.cfg)
        if idx is None:
            LOGGER.info("Current signal unavailable | no closed candle yet")
            return None
        signal = build_signal(self.api, candles, idx=idx)
        if signal is None:
            LOGGER.info("Current signal unavailable | no ORB breakout setup")
            return None
        if not allow_weak and signal.score < self.cfg.min_signal_score:
            LOGGER.info("Current signal below threshold | score=%s | min=%s", signal.score, self.cfg.min_signal_score)
            return None
        LOGGER.info(
            "Current signal ready | direction=%s | score=%s/%s | entry=%s",
            signal.direction,
            signal.score,
            signal.max_score,
            signal.trade_plan.entry_price,
        )
        return signal

    def _send_signal(self, signal: Signal, prefix: str = "", alert: bool = True) -> None:
        LOGGER.info(
            "Sending signal | prefix=%s | alert=%s | direction=%s | score=%s/%s | entry=%s",
            prefix,
            alert,
            signal.direction,
            signal.score,
            signal.max_score,
            signal.trade_plan.entry_price,
        )
        msg = format_signal(signal, alert=alert)
        if prefix:
            msg = f"{prefix}\n\n{msg}"
        self.bot.send(msg)

    def _live_check(self) -> None:
        if not self.live_enabled:
            LOGGER.debug("Live check skipped | live disabled")
            return
        if time.time() - self.last_live_check_ts < self.cfg.live_check_interval:
            LOGGER.debug("Live check skipped | waiting interval")
            return
        self.last_live_check_ts = time.time()
        if not _live_orb_active_now(self.cfg):
            LOGGER.info("Live check skipped | outside configured ORB windows | next=%s", _next_orb_session_text(self.cfg))
            return
        LOGGER.info("Live check started")

        candles = self._current_candles()
        if candles.empty or len(candles) < 4:
            LOGGER.warning("Live check skipped | insufficient candles=%s", len(candles))
            return
        idx = _latest_closed_candle_index(candles, self.cfg)
        if idx is None:
            LOGGER.info("Live check skipped | no closed candle yet")
            return
        latest = candles.iloc[idx]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_live_candle_time:
            LOGGER.info("Live check skipped | duplicate candle_time=%s", candle_time)
            return

        signal = build_signal(self.api, candles.reset_index(drop=True), idx=idx)
        self.last_live_candle_time = candle_time

        if signal is None or signal.score < self.cfg.min_signal_score:
            LOGGER.info(
                "Live check finished | no alert | candle_time=%s | signal_score=%s",
                candle_time,
                None if signal is None else signal.score,
            )
            return

        key = f"{signal.candle_time}|{signal.direction}|{signal.trade_plan.entry_price}|{signal.score}"
        if key == self.last_live_key:
            LOGGER.info("Live alert skipped | duplicate key=%s", key)
            return

        self._send_signal(signal, prefix="<b>LIVE ALERT</b>")
        self.last_live_key = key
        LOGGER.info("Live alert sent | key=%s", key)

    def _handle_status(self) -> None:
        LOGGER.info("Handling status command")
        candles = self._current_candles()
        ticker = self.api.ticker24h()
        signal = None
        try:
            signal = self._current_signal(allow_weak=True)
        except Exception:
            LOGGER.exception("Failed to evaluate current signal while handling status")
            signal = None
        self.bot.send(format_status_message(ticker, candles, signal, self.cfg))

    def _handle_levels(self) -> None:
        LOGGER.info("Handling levels command")
        candles = self._current_candles()
        self.bot.send(format_levels_message(candles, self.cfg))

    def _handle_signal(self) -> None:
        LOGGER.info("Handling signal command")
        signal = self._current_signal(allow_weak=True)
        if signal is None:
            candles = self._current_candles()
            self.bot.send(f"No Session ORB BTCUSDT setup right now.\nNext: {_next_orb_session_text(self.cfg)}\n\n" + format_levels_message(candles, self.cfg))
            return
        alert = signal.score >= self.cfg.min_signal_score
        prefix = "<b>CURRENT SETUP</b>" if alert else "<b>WEAK SETUP - WATCH ONLY</b>"
        self._send_signal(signal, prefix=prefix, alert=alert)

    def _scan_worker(self, start_date: str, end_date: str) -> None:
        try:
            LOGGER.info("Backtest scan started | start=%s | end=%s", start_date, end_date)
            self.bot.send(
                f"Backtesting {self.cfg.contract_pair} {self.cfg.candle_interval} Session ORB candles\n"
                f"From: <b>{start_date}</b>\n"
                f"To: <b>{end_date}</b>\n\n"
                f"Windows: <code>{html.escape(_orb_sessions_text(self.cfg))}</code>\n"
                f"T2 target: 1:{self.cfg.risk_reward:g} | P&L basis: {self.cfg.trade_size_btc:g} BTC"
            )

            start = _parse_date_ist(start_date)
            end = _parse_date_ist(end_date, end_of_day=True)
            candles = self.api.historical_klines(start, end)
            if candles.empty:
                LOGGER.warning("Backtest scan returned no candles | start=%s | end=%s", start_date, end_date)
                self.bot.send(f"No {self.cfg.contract_pair} candles returned for that range.")
                return

            last_key: Optional[str] = None
            alerts: List[Signal] = []
            records: List[Dict[str, Any]] = []
            duplicate_count = 0
            evaluated = 0
            below_threshold = 0

            for idx in range(3, len(candles)):
                if self.scan_stop_event.is_set():
                    LOGGER.info("Backtest scan stopped | evaluated=%s | alerts=%s", evaluated, len(alerts))
                    result_path = _save_backtest_results(records, self.cfg, start_date, end_date)
                    self.bot.send(
                        "Scan stopped by user.\n\n"
                        + _format_backtest_summary(start_date, end_date, len(candles), evaluated, alerts, duplicate_count, result_path, records)
                    )
                    return

                evaluated += 1
                if evaluated % max(1, self.cfg.backtest_progress_every) == 0:
                    LOGGER.info("Backtest progress | evaluated=%s/%s | alerts=%s", evaluated, max(len(candles) - 3, 0), len(alerts))

                signal = build_signal(self.api, candles, idx=idx)
                if signal is None:
                    continue
                if signal.score < self.cfg.min_signal_score:
                    below_threshold += 1
                    LOGGER.info("Backtest setup below threshold | time=%s | score=%s", signal.candle_time, signal.score)
                    continue

                key = f"{signal.candle_time}|{signal.direction}|{signal.trade_plan.entry_price}"
                if key == last_key:
                    duplicate_count += 1
                    LOGGER.info("Backtest duplicate skipped | key=%s", key)
                    continue

                alerts.append(signal)
                outcome = _simulate_orb_outcome(candles, signal)
                records.append(_signal_to_backtest_row(signal, outcome=outcome, qty_btc=self.cfg.trade_size_btc))
                if self.cfg.backtest_send_each_alert:
                    self._send_signal(
                        signal,
                        prefix=f"<b>BACKTEST ALERT</b>\nResult: <b>{html.escape(str(outcome.get('result')))}</b> | R: {float(outcome.get('r_multiple') or 0):.2f}",
                    )
                LOGGER.info("Backtest alert recorded | key=%s | result=%s | r=%s | alerts=%s", key, outcome.get("result"), outcome.get("r_multiple"), len(alerts))
                last_key = key
                time.sleep(0.05)

            result_path = _save_backtest_results(records, self.cfg, start_date, end_date)
            LOGGER.info(
                "Backtest scan completed | candles=%s | evaluated=%s | alerts=%s | below_threshold=%s | duplicates=%s | csv=%s",
                len(candles),
                evaluated,
                len(alerts),
                below_threshold,
                duplicate_count,
                result_path,
            )
            self.bot.send(_format_backtest_summary(start_date, end_date, len(candles), evaluated, alerts, duplicate_count, result_path, records))
        except Exception as e:
            LOGGER.exception("Backtest scan error")
            self.bot.send(f"Scan error: {html.escape(str(e))}")
        finally:
            LOGGER.info("Backtest scan cleanup complete")
            self.scan_stop_event.clear()
            with self.scan_lock:
                self.scan_thread = None

    def _start_scan(self, start_date: str, end_date: str) -> None:
        with self.scan_lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                LOGGER.info("Scan start rejected | scan already running")
                self.bot.send("A scan is already running.")
                return
            self.scan_stop_event.clear()
            LOGGER.info("Starting scan thread | start=%s | end=%s", start_date, end_date)
            self.scan_thread = threading.Thread(
                target=self._scan_worker,
                args=(start_date, end_date),
                daemon=True,
            )
            self.scan_thread.start()

    def _dispatch(self, raw: str) -> None:
        text = raw.strip()
        cmd = text.split("@")[0].lower()
        LOGGER.info("Dispatching command | text=%s", text)

        m = SCAN_RE.match(text)
        if m:
            self._start_scan(m.group(1), m.group(2))
            return

        if STOP_RE.match(text):
            LOGGER.info("Stop command received")
            self.scan_stop_event.set()
            self.bot.send("Stop requested.")
            return

        if LIVE_RE.match(text):
            LOGGER.info("Live command received")
            self.live_enabled = True
            self.bot.send(f"Live monitoring enabled for Session ORB windows only.\nNext: {_next_orb_session_text(self.cfg)}")
            return

        if cmd in ("/status", "status"):
            self._handle_status()
        elif cmd in ("/levels", "levels"):
            self._handle_levels()
        elif cmd in ("/signal", "signal"):
            self._handle_signal()
        elif cmd in ("/help", "help", "/start", "start"):
            self.bot.send(HELP_TEXT + f"\nAlerts fire only when score >= {self.cfg.min_signal_score}.")
        else:
            LOGGER.info("Unknown command received | text=%s", text)
            self.bot.send(f"Unknown command: <code>{html.escape(text)}</code>\n\n{HELP_TEXT}")

    def run(self) -> None:
        LOGGER.info("Bot run loop starting | pair=%s", self.cfg.contract_pair)
        print(f"BTCUSDT ORB Levels Signal Agent started | {self.cfg.contract_pair}")
        self.bot.send(
            "<b>BTCUSDT ORB Levels Signal Bot is online.</b>\n\n"
            "Live monitoring is ON for Session ORB windows only.\n"
            f"Windows: <code>{html.escape(_orb_sessions_text(self.cfg))}</code>\n"
            f"T2 Target: 1:{self.cfg.risk_reward:g}\n"
            "Send <code>/signal</code> for the current ORB setup.\n"
            "Send <code>/levels</code> for long and short trigger levels.\n"
            "Send <code>SCAN YYYY-MM-DD YYYY-MM-DD</code> for a backtest.\n"
            "This bot does not place orders."
        )

        while True:
            try:
                for msg in self.bot.get_messages():
                    LOGGER.info("Telegram command received | msg=%s", msg)
                    print(f"Message: {msg}")
                    try:
                        self._dispatch(msg)
                    except Exception as e:
                        err = f"Error: {html.escape(str(e))}"
                        LOGGER.exception("Command handling error | msg=%s", msg)
                        print(err)
                        self.bot.send(err)

                try:
                    self._live_check()
                except Exception as e:
                    LOGGER.exception("Live check error")
                    print(f"Live check error: {e}")

                time.sleep(self.cfg.tg_poll_interval)

            except KeyboardInterrupt:
                LOGGER.info("KeyboardInterrupt received; stopping bot")
                self.bot.send("BTCUSDT ORB Levels Signal Bot stopped.")
                print("Stopped.")
                return
            except requests.HTTPError as e:
                LOGGER.exception("HTTP error in run loop")
                print(f"HTTP error: {e}")
                time.sleep(5)
            except Exception as e:
                LOGGER.exception("Unhandled error in run loop")
                print(f"Error: {e}")
                time.sleep(5)


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg)
    safe_cfg = {k: v for k, v in dataclasses.asdict(cfg).items() if "token" not in k.lower() and "secret" not in k.lower()}
    LOGGER.info("Configuration loaded | %s", safe_cfg)
    BtcOrbLevelsSignalAgent(cfg).run()


if __name__ == "__main__":
    main()
