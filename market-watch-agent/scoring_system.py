"""NIFTY50 signal bot with score-based BUY alerts.

What this program does
- NIFTY50 only
- Live monitoring runs continuously in the background
- Sends Telegram alerts only when a setup score is strong enough
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

Important note
- This is a signal assistant, not financial advice.
- Historical scan uses historical candles, but option-chain suggestion is based on the current Dhan option chain snapshot.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DHAN_BASE = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"

NIFTY50_SECURITY_ID = 13
NIFTY50_SEGMENT = "IDX_I"
NIFTY50_NAME = "NIFTY 50"
IST = ZoneInfo("Asia/Kolkata")

SCAN_RE = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
STOP_RE = re.compile(r"^STOP$", re.IGNORECASE)
LIVE_RE = re.compile(r"^LIVE$", re.IGNORECASE)
STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)

# Score threshold for BUY alerts.
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "8"))
# Live polling interval in seconds. Keep >= 5 to stay comfortable with Dhan API rate limits.
TG_POLL_INTERVAL_DEFAULT = float(os.getenv("TG_POLL_INTERVAL", "5"))


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    http_timeout: int = 15
    tg_poll_interval: float = TG_POLL_INTERVAL_DEFAULT
    strikes_window: int = 5

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

        return Config(
            dhan_client_id=required["DHAN_CLIENT_ID"],
            dhan_access_token=required["DHAN_ACCESS_TOKEN"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            http_timeout=_int("HTTP_TIMEOUT", 15),
            tg_poll_interval=_float("TG_POLL_INTERVAL", TG_POLL_INTERVAL_DEFAULT),
            strikes_window=_int("STRIKES_WINDOW", 5),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────


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


# ─────────────────────────────────────────────────────────────────────────────
# API clients
# ─────────────────────────────────────────────────────────────────────────────


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
        r.raise_for_status()
        return [str(x) for x in r.json().get("data", [])]

    def pick_expiry(self) -> str:
        expiries = self.expiry_list()
        if not expiries:
            raise RuntimeError("No expiry dates returned by Dhan.")

        today = dt.date.today()

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
                return exp
        return future[0]

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
        r.raise_for_status()
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
        r.raise_for_status()
        raw = r.json()
        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]
        if isinstance(raw, list):
            return self._rows_to_df(raw)
        if isinstance(raw, dict):
            return self._dict_to_df(raw)
        raise ValueError(f"Unexpected intraday response shape: {type(raw)}")

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
            df["timestamp"] = (
                pd.to_datetime(ts, unit="s", utc=True)
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
        return df.reset_index(drop=True)


class TelegramBot:
    def __init__(self, token: str, chat_id: str, timeout: int = 15):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.session = requests.Session()
        self._offset = 0

    def send(self, text: str) -> None:
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


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def _fmt(v: Any, decimals: int = 2) -> str:
    return "—" if v is None else f"{float(v):,.{decimals}f}"


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


def _nearest_strike(strikes: List[float], spot: float) -> float:
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else 0.0


def _infer_step(strikes: List[float]) -> int:
    if len(strikes) < 2:
        return 50
    diffs = sorted(abs(b - a) for a, b in zip(strikes[:-1], strikes[1:]) if abs(b - a) > 0)
    if not diffs:
        return 50
    return int(round(statistics.median(diffs))) or 50


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
    call_oi = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")) for s in band)
    put_oi = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")) for s in band)
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
            _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")),
            _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")),
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


def _get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
    return (
        oc.get(f"{strike:.6f}")
        or oc.get(f"{strike:.2f}")
        or oc.get(f"{strike:.0f}")
        or oc.get(str(int(strike)))
        or {}
    )


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
    return [(s, oc.get(f"{s:.6f}") or oc.get(str(s)) or {}) for s in sorted(nearby)]


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


# ─────────────────────────────────────────────────────────────────────────────
# Candlestick patterns
# ─────────────────────────────────────────────────────────────────────────────

# Stronger patterns get a bigger influence in the score.
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
        return matches
    except Exception:
        # Minimal fallback if TA-Lib is not installed.
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
        return matches


def infer_direction(patterns: List[str]) -> str:
    bullish = sum(1 for p in patterns if p in BULLISH_PATTERNS)
    bearish = sum(1 for p in patterns if p in BEARISH_PATTERNS)
    if bullish > bearish:
        return "BULLISH"
    if bearish > bullish:
        return "BEARISH"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# Scoring + trade planning
# ─────────────────────────────────────────────────────────────────────────────


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

    # 1) Pattern confirmation
    pattern_side = "bullish" if bullish else "bearish"
    side_patterns = [p for p in patterns if (p in BULLISH_PATTERNS if bullish else p in BEARISH_PATTERNS)]
    if side_patterns:
        score += 3
        reasons.append(f"Pattern confirmation: {', '.join(side_patterns[:3])}")

    # 2) VWAP alignment
    if bullish and last.close > ctx.vwap:
        score += 2
        reasons.append("Price above VWAP")
    elif (not bullish) and last.close < ctx.vwap:
        score += 2
        reasons.append("Price below VWAP")

    # 3) Breakout / breakdown confirmation
    if bullish and last.close > prev.high:
        score += 2
        reasons.append("Close above previous high")
    elif (not bullish) and last.close < prev.low:
        score += 2
        reasons.append("Close below previous low")

    # 4) Volume confirmation
    if ctx.recent_avg_volume > 0 and last.volume >= ctx.recent_avg_volume * 1.15:
        score += 1
        reasons.append("Volume above recent average")

    # 5) Trend alignment
    if bullish and ctx.trend == "uptrend":
        score += 1
        reasons.append("Trend aligned to upside")
    elif (not bullish) and ctx.trend == "downtrend":
        score += 1
        reasons.append("Trend aligned to downside")

    # 6) Candle quality
    candle_range = max(last.high - last.low, 1e-9)
    body = abs(last.close - last.open)
    body_ratio = body / candle_range
    if body_ratio >= 0.55:
        score += 1
        reasons.append("Strong candle body")

    # 7) PCR confirmation
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
        else:
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
    key = min(oc.keys(), key=lambda k: abs(float(k) - strike))
    row = oc.get(key) or {}
    opt = row.get("ce" if side == "CE" else "pe") or {}

    option_security_id = opt.get("security_id")
    option_ltp = _num(opt.get("last_price"), default=0.0)
    if option_ltp <= 0:
        option_ltp = max(1.0, round(abs(spot - strike) / 4.0, 2))

    # Premium trade levels.
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
        strike=strike,
        option_security_id=int(option_security_id) if option_security_id is not None else None,
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


# ─────────────────────────────────────────────────────────────────────────────
# Signal builder
# ─────────────────────────────────────────────────────────────────────────────


def build_signal(
    expiry: str,
    candles: pd.DataFrame,
    idx: int,
    chain: Dict[str, Any],
) -> Optional[Signal]:
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
        return None
    if score < MIN_SIGNAL_SCORE:
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


# ─────────────────────────────────────────────────────────────────────────────
# Telegram formatting
# ─────────────────────────────────────────────────────────────────────────────


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
  LIVE   — enable live monitoring
  STOP   — stop running scan

<b>Market overview</b>
  /chain   — ±5 strike option chain table
  /status  — Spot, PCR, bias, support, resistance
  /expiry  — Current weekly expiry
  /help    — This help message

Alerts are sent only when score >= {MIN_SIGNAL_SCORE}.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main engine
# ─────────────────────────────────────────────────────────────────────────────


class MarketWatchAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = DhanApiClient(cfg)
        self.bot = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        self.expiry: Optional[str] = None

        self.live_enabled = True
        self.last_live_key: Optional[str] = None
        self.last_live_candle_time: Optional[str] = None

        self.scan_thread: Optional[threading.Thread] = None
        self.scan_stop_event = threading.Event()
        self.scan_lock = threading.Lock()

    def _ensure_expiry(self) -> str:
        if self.expiry is None:
            self.expiry = self.api.pick_expiry()
        return self.expiry

    def _fetch_chain(self) -> Tuple[str, float, Dict[str, Any]]:
        expiry = self._ensure_expiry()
        snapshot = self.api.option_chain(expiry)
        data = snapshot.get("data", {})
        spot = _num(data.get("last_price"))
        oc: Dict[str, Any] = data.get("oc") or {}
        if not oc:
            raise RuntimeError("Empty option chain — market may be closed.")
        return expiry, spot, oc

    def _fetch_intraday(self, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        return self.api.intraday_candles(
            security_id=NIFTY50_SECURITY_ID,
            exchange_segment=NIFTY50_SEGMENT,
            instrument="INDEX",
            interval=5,
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            oi=True,
        )

    def _current_intraday(self) -> pd.DataFrame:
        now = _now_ist()
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return self._fetch_intraday(start, end)

    # ───────────────────────────────
    # Signal helpers
    # ───────────────────────────────

    def _evaluate_dataframe(self, candles: pd.DataFrame, expiry: str, chain: Dict[str, Any], idx: int) -> Optional[Signal]:
        return build_signal(expiry, candles, idx, chain)

    def _send_signal(self, signal: Signal, prefix: str = "") -> None:
        msg = format_signal(signal)
        if prefix:
            msg = f"{prefix}\n\n{msg}"
        self.bot.send(msg)

    # ───────────────────────────────
    # Live monitoring
    # ───────────────────────────────

    def _live_check(self) -> None:
        if not self.live_enabled:
            return
        if not _market_session_open():
            return

        expiry = self._ensure_expiry()
        candles = self._current_intraday()
        if candles.empty or len(candles) < 5:
            return

        latest = candles.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_live_candle_time:
            return

        chain = self.api.option_chain(expiry)
        signal = build_signal(expiry, candles.reset_index(drop=True), len(candles) - 1, chain)
        self.last_live_candle_time = candle_time

        if signal is None:
            return

        key = f"{signal.candle_time}|{signal.direction}|{','.join(signal.pattern_names)}"
        if key == self.last_live_key:
            return

        self._send_signal(signal, prefix="<b>LIVE ALERT</b>")
        self.last_live_key = key

    # ───────────────────────────────
    # Scan / backtest
    # ───────────────────────────────

    def _scan_worker(self, start_date: str, end_date: str) -> None:
        try:
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
                self.bot.send("⚠️ No candles returned for that range.")
                return

            chain = self.api.option_chain(expiry)
            last_key: Optional[str] = None
            sent = 0

            for idx in range(4, len(candles)):
                if self.scan_stop_event.is_set():
                    self.bot.send("🛑 Scan stopped by user.")
                    return

                window = candles.iloc[: idx + 1].copy().reset_index(drop=True)
                signal = self._evaluate_dataframe(window, expiry, chain, idx)
                if signal is None:
                    continue

                key = f"{signal.candle_time}|{signal.direction}|{','.join(signal.pattern_names)}"
                if key == last_key:
                    continue

                self._send_signal(signal, prefix="<b>BACKTEST ALERT</b>")
                last_key = key
                sent += 1
                time.sleep(0.3)

            self.bot.send(f"✅ Scan completed. Sent {sent} alert(s).")
        except Exception as e:
            self.bot.send(f"⚠️ Scan error: {e}")
        finally:
            self.scan_stop_event.clear()
            with self.scan_lock:
                self.scan_thread = None

    def _start_scan(self, start_date: str, end_date: str) -> None:
        with self.scan_lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                self.bot.send("⚠️ A scan is already running.")
                return
            self.scan_stop_event.clear()
            self.scan_thread = threading.Thread(
                target=self._scan_worker,
                args=(start_date, end_date),
                daemon=True,
            )
            self.scan_thread.start()

    # ───────────────────────────────
    # Commands
    # ───────────────────────────────

    def _handle_chain(self) -> None:
        expiry, spot, oc = self._fetch_chain()
        all_strikes = sorted(float(k) for k in oc.keys())
        atm = _nearest_strike(all_strikes, spot)
        rows = _strikes_around_atm(oc, spot, self.cfg.strikes_window)
        support, resistance = _support_resistance_oi(oc)
        pcr_val = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val = _max_pain(oc)
        self.bot.send(format_chain_message(rows, spot, expiry, support, resistance, atm, pcr_val, max_pain_val))

    def _handle_status(self) -> None:
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
        expiry, spot, oc = self._fetch_chain()
        row = _get_row(oc, strike)
        option_data = row.get(side.lower(), {})
        if not row:
            strikes = sorted(float(k) for k in oc.keys())
            self.bot.send(
                f"⚠️ Strike <b>{strike:,.0f}</b> not found in the option chain.\n"
                f"Range: {strikes[0]:,.0f} – {strikes[-1]:,.0f}"
            )
            return

        support, resistance = _support_resistance_oi(oc)
        pcr_val = _pcr(oc, spot, self.cfg.strikes_window)
        all_strikes = sorted(float(k) for k in oc.keys())
        atm = _nearest_strike(all_strikes, spot)

        candles = self._current_intraday()
        if candles.empty or len(candles) < 5:
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
            self.bot.send("⚠️ Intraday context unavailable.")
            return

        ltp = _num(option_data.get("last_price"))
        if ltp <= 0:
            ltp = max(1.0, abs(spot - strike) / 4.0)
        _, _, _, plan = _option_trade_plan({"data": {"oc": oc}}, spot, side, MIN_SIGNAL_SCORE)
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
            option_plan=dataclasses.replace(plan, option_ltp=ltp, strike=strike, side=side),
            confidence="Manual",
        )
        self._send_signal(signal, prefix="<b>STRIKE SNAPSHOT</b>")

    def _dispatch(self, raw: str) -> None:
        text = raw.strip()
        cmd = text.split("@")[0].lower()

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
            self.bot.send("🛑 Stop requested.")
            return

        if LIVE_RE.match(text):
            self.live_enabled = True
            self.bot.send("✅ Live monitoring enabled.")
            return

        if cmd in ("/chain", "chain"):
            self._handle_chain()
        elif cmd in ("/status", "status"):
            self._handle_status()
        elif cmd in ("/expiry", "expiry"):
            self.bot.send(f"Current weekly expiry: <b>{self._ensure_expiry()}</b>")
        elif cmd in ("/help", "help", "/start", "start"):
            self.bot.send(HELP_TEXT)
        else:
            self.bot.send(f"Unknown command: <code>{text}</code>\n\n{HELP_TEXT}")

    # ───────────────────────────────
    # Run loop
    # ───────────────────────────────

    def run(self) -> None:
        print(f"Market Watch Agent started | {NIFTY50_NAME}")
        self.bot.send(
            f"<b>{NIFTY50_NAME} Signal Bot is online!</b>\n\n"
            f"Live monitoring is ON.\n"
            f"Send <code>SCAN YYYY-MM-DD YYYY-MM-DD</code> for backtest.\n"
            f"Send <code>STOP</code> to stop a scan.\n"
            f"Alerts fire only when score >= {MIN_SIGNAL_SCORE}."
        )

        while True:
            try:
                for msg in self.bot.get_messages():
                    print(f"Message: {msg}")
                    try:
                        self._dispatch(msg)
                    except Exception as e:
                        err = f"⚠️ Error: {e}"
                        print(err)
                        self.bot.send(err)

                try:
                    self._live_check()
                except Exception as e:
                    print(f"Live check error: {e}")

                time.sleep(self.cfg.tg_poll_interval)

            except KeyboardInterrupt:
                self.bot.send(f"{NIFTY50_NAME} Signal Bot stopped.")
                print("Stopped.")
                return
            except requests.HTTPError as e:
                print(f"HTTP error: {e}")
                time.sleep(5)
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    cfg = Config.from_env()
    MarketWatchAgent(cfg).run()


if __name__ == "__main__":
    main()
