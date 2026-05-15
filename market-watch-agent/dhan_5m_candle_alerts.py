# """NIFTY50 5-minute candlestick scanner with Telegram commands.

# Features:
# - Live monitoring runs continuously and alerts when a NEW 5-minute candle forms a pattern.
# - SCAN YYYY-MM-DD YYYY-MM-DD runs a backtest on that date range.
# - STOP cancels a running scan.
# - LIVE re-enables live monitoring.
# - Telegram commands work in the same style as your reference bot.
# - Timestamps are converted to IST.

# Notes:
# - This is a starter trading assistant, not financial advice.
# - Uses Dhan option chain for PCR / support / resistance / strike suggestion.
# - Uses Dhan intraday candles for NIFTY50 only.
# """

# from __future__ import annotations

# import dataclasses
# import datetime as dt
# import json
# import os
# import re
# import time
# from typing import Any, Dict, List, Optional, Tuple

# import numpy as np
# import pandas as pd
# import requests
# from dotenv import load_dotenv

# load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# DHAN_BASE = "https://api.dhan.co/v2"
# TELEGRAM_BASE = "https://api.telegram.org"

# NIFTY50_SECURITY_ID = 13
# NIFTY50_SEGMENT = "IDX_I"
# NIFTY50_NAME = "NIFTY 50"
# IST_TZ = "Asia/Kolkata"

# SCAN_RE = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
# STOP_RE = re.compile(r"^STOP$", re.IGNORECASE)
# LIVE_RE = re.compile(r"^LIVE$", re.IGNORECASE)
# STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)


# # ─────────────────────────────────────────────────────────────────────────────
# # Config
# # ─────────────────────────────────────────────────────────────────────────────

# @dataclasses.dataclass
# class Config:
#     dhan_client_id: str
#     dhan_access_token: str
#     telegram_bot_token: str
#     telegram_chat_id: str
#     http_timeout: int = 15
#     tg_poll_interval: float = 1.5
#     strikes_window: int = 5

#     @staticmethod
#     def from_env() -> "Config":
#         required = {
#             "DHAN_CLIENT_ID": os.getenv("DHAN_CLIENT_ID", "").strip(),
#             "DHAN_ACCESS_TOKEN": os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
#             "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
#             "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
#         }
#         missing = [k for k, v in required.items() if not v]
#         if missing:
#             raise SystemExit("Missing env vars: " + ", ".join(missing))

#         def _int(name: str, default: int) -> int:
#             try:
#                 return int(os.getenv(name, str(default)).strip())
#             except Exception:
#                 return default

#         def _float(name: str, default: float) -> float:
#             try:
#                 return float(os.getenv(name, str(default)).strip())
#             except Exception:
#                 return default

#         return Config(
#             dhan_client_id=required["DHAN_CLIENT_ID"],
#             dhan_access_token=required["DHAN_ACCESS_TOKEN"],
#             telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
#             telegram_chat_id=required["TELEGRAM_CHAT_ID"],
#             http_timeout=_int("HTTP_TIMEOUT", 15),
#             tg_poll_interval=_float("TG_POLL_INTERVAL", 1.5),
#             strikes_window=_int("STRIKES_WINDOW", 5),
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Data classes
# # ─────────────────────────────────────────────────────────────────────────────

# @dataclasses.dataclass
# class Candle:
#     ts: dt.datetime
#     open: float
#     high: float
#     low: float
#     close: float
#     volume: float


# @dataclasses.dataclass
# class IntradayLevels:
#     day_open: float
#     day_high: float
#     day_low: float
#     last_close: float
#     vwap: float
#     trend: str
#     candle_count: int
#     last_candle: Candle
#     intraday_support: float
#     intraday_resistance: float
#     prev_candle_high: float
#     prev_candle_low: float


# @dataclasses.dataclass
# class TradeLevels:
#     buy: float
#     target1: float
#     target2: float
#     stop_loss: float
#     risk: float
#     reward1: float
#     reward2: float
#     rr1: float
#     rr2: float
#     note: str


# @dataclasses.dataclass
# class Alert:
#     timestamp: str
#     underlying_symbol: str
#     candle_time: str
#     direction: str
#     pattern_names: List[str]
#     vwap: float
#     underlying_ltp: float
#     pcr: Optional[float]
#     option_side: str
#     strike: Optional[float]
#     option_security_id: Optional[int]
#     option_ltp: Optional[float]
#     entry: Optional[float]
#     stop_loss: Optional[float]
#     target: Optional[float]
#     support: Optional[float]
#     resistance: Optional[float]
#     notes: str

#     def to_json(self) -> str:
#         return json.dumps(dataclasses.asdict(self), indent=2, default=str)


# # ─────────────────────────────────────────────────────────────────────────────
# # Dhan API
# # ─────────────────────────────────────────────────────────────────────────────

# class DhanApiClient:
#     def __init__(self, cfg: Config):
#         self.cfg = cfg
#         self.session = requests.Session()
#         self.session.headers.update(
#             {
#                 "Content-Type": "application/json",
#                 "Accept": "application/json",
#                 "access-token": cfg.dhan_access_token,
#                 "client-id": cfg.dhan_client_id,
#             }
#         )

#     def expiry_list(self) -> List[str]:
#         payload = {
#             "UnderlyingScrip": NIFTY50_SECURITY_ID,
#             "UnderlyingSeg": NIFTY50_SEGMENT,
#         }
#         r = self.session.post(
#             f"{DHAN_BASE}/optionchain/expirylist",
#             data=json.dumps(payload),
#             timeout=self.cfg.http_timeout,
#         )
#         r.raise_for_status()
#         return [str(x) for x in r.json().get("data", [])]

#     def pick_expiry(self) -> str:
#         expiries = self.expiry_list()
#         if not expiries:
#             raise RuntimeError("No expiry dates returned by Dhan.")

#         today = dt.date.today()

#         def _parsed(x: str) -> dt.date:
#             for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
#                 try:
#                     return dt.datetime.strptime(x, fmt).date()
#                 except Exception:
#                     continue
#             return dt.date.max

#         future = sorted(expiries, key=_parsed)
#         for item in future:
#             d = _parsed(item)
#             if d >= today or d == dt.date.max:
#                 return item
#         return future[0]

#     def option_chain(self, expiry: str) -> Dict[str, Any]:
#         payload = {
#             "UnderlyingScrip": NIFTY50_SECURITY_ID,
#             "UnderlyingSeg": NIFTY50_SEGMENT,
#             "Expiry": expiry,
#         }
#         r = self.session.post(
#             f"{DHAN_BASE}/optionchain",
#             data=json.dumps(payload),
#             timeout=self.cfg.http_timeout,
#         )
#         r.raise_for_status()
#         return r.json()

#     def intraday_candles(
#         self,
#         security_id: int,
#         exchange_segment: str,
#         instrument: str,
#         interval: int,
#         from_date: str,
#         to_date: str,
#         oi: bool = True,
#     ) -> pd.DataFrame:
#         payload = {
#             "securityId": str(security_id),
#             "exchangeSegment": exchange_segment,
#             "instrument": instrument,
#             "interval": str(interval),
#             "oi": bool(oi),
#             "fromDate": from_date,
#             "toDate": to_date,
#         }
#         r = self.session.post(
#             f"{DHAN_BASE}/charts/intraday",
#             data=json.dumps(payload),
#             timeout=self.cfg.http_timeout,
#         )
#         r.raise_for_status()
#         raw = r.json()
#         if isinstance(raw, dict) and "data" in raw:
#             raw = raw["data"]
#         if isinstance(raw, list):
#             return self._list_to_df(raw)
#         if isinstance(raw, dict):
#             return self._dict_to_df(raw)
#         raise ValueError(f"Unexpected intraday response shape: {type(raw)}")

#     @staticmethod
#     def _list_to_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
#         df = pd.DataFrame(rows)
#         return DhanApiClient._normalize_df(df)

#     @staticmethod
#     def _dict_to_df(data: Dict[str, Any]) -> pd.DataFrame:
#         keys = {k.lower(): k for k in data.keys()}
#         required = ["open", "high", "low", "close", "volume", "timestamp"]
#         missing = [k for k in required if k not in keys]
#         if missing:
#             raise ValueError(f"Intraday response missing keys: {missing}; got {list(data.keys())}")

#         df = pd.DataFrame(
#             {
#                 "timestamp": data[keys["timestamp"]],
#                 "open": data[keys["open"]],
#                 "high": data[keys["high"]],
#                 "low": data[keys["low"]],
#                 "close": data[keys["close"]],
#                 "volume": data[keys["volume"]],
#             }
#         )
#         if "open_interest" in keys:
#             df["open_interest"] = data[keys["open_interest"]]
#         return DhanApiClient._normalize_df(df)

#     @staticmethod
#     def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
#         df = df.copy()
#         if "timestamp" not in df.columns:
#             raise ValueError("Intraday data has no timestamp column.")

#         ts = pd.to_numeric(df["timestamp"], errors="coerce")
#         if ts.notna().any():
#             df["timestamp"] = (
#                 pd.to_datetime(ts, unit="s", utc=True)
#                 .dt.tz_convert(IST_TZ)
#                 .dt.tz_localize(None)
#             )
#         else:
#             df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
#             if getattr(df["timestamp"].dt, "tz", None) is not None:
#                 df["timestamp"] = df["timestamp"].dt.tz_convert(IST_TZ).dt.tz_localize(None)

#         for col in ["open", "high", "low", "close", "volume"]:
#             df[col] = pd.to_numeric(df[col], errors="coerce")

#         if "open_interest" not in df.columns:
#             df["open_interest"] = np.nan
#         else:
#             df["open_interest"] = pd.to_numeric(df["open_interest"], errors="coerce")

#         df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"]).sort_values("timestamp")
#         return df.reset_index(drop=True)


# # ─────────────────────────────────────────────────────────────────────────────
# # Telegram bot
# # ─────────────────────────────────────────────────────────────────────────────

# class TelegramBot:
#     def __init__(self, token: str, chat_id: str, timeout: int = 15):
#         self.token = token
#         self.chat_id = str(chat_id)
#         self.timeout = timeout
#         self.session = requests.Session()
#         self._offset = 0

#     def send(self, text: str) -> None:
#         self.session.post(
#             f"{TELEGRAM_BASE}/bot{self.token}/sendMessage",
#             data={
#                 "chat_id": self.chat_id,
#                 "text": text,
#                 "parse_mode": "HTML",
#                 "disable_web_page_preview": True,
#             },
#             timeout=self.timeout,
#         ).raise_for_status()

#     def get_messages(self) -> List[str]:
#         try:
#             r = self.session.get(
#                 f"{TELEGRAM_BASE}/bot{self.token}/getUpdates",
#                 params={"offset": self._offset, "timeout": 0},
#                 timeout=self.timeout + 5,
#             )
#             r.raise_for_status()
#         except Exception:
#             return []

#         texts: List[str] = []
#         for update in r.json().get("result", []):
#             self._offset = update["update_id"] + 1
#             msg = update.get("message") or update.get("channel_post") or {}
#             chat_id = str((msg.get("chat") or {}).get("id", ""))
#             text = (msg.get("text") or "").strip()
#             if chat_id == self.chat_id and text:
#                 texts.append(text)
#         return texts


# # ─────────────────────────────────────────────────────────────────────────────
# # Helpers
# # ─────────────────────────────────────────────────────────────────────────────

# def _num(v: Any, default: float = 0.0) -> float:
#     try:
#         return float(v) if v is not None else default
#     except Exception:
#         return default


# def _fmt(v: Any, decimals: int = 2) -> str:
#     return "—" if v is None else f"{float(v):,.{decimals}f}"


# def _nearest_strike(strikes: List[float], spot: float) -> float:
#     return min(strikes, key=lambda s: abs(s - spot)) if strikes else 0.0


# def _support_resistance_oi(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
#     support = None
#     resistance = None
#     best_pe = -1.0
#     best_ce = -1.0
#     for k, row in oc.items():
#         try:
#             strike = float(k)
#         except Exception:
#             continue
#         pe_oi = _num((row.get("pe") or {}).get("oi"))
#         ce_oi = _num((row.get("ce") or {}).get("oi"))
#         if pe_oi > best_pe:
#             best_pe = pe_oi
#             support = strike
#         if ce_oi > best_ce:
#             best_ce = ce_oi
#             resistance = strike
#     return support, resistance


# def _pcr(oc: Dict[str, Any], center: float, window: int) -> Optional[float]:
#     strikes = sorted(float(k) for k in oc.keys())
#     if not strikes:
#         return None
#     band = sorted(strikes, key=lambda s: abs(s - center))[: max(2, window * 2)]
#     call_oi = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")) for s in band)
#     put_oi = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")) for s in band)
#     return (put_oi / call_oi) if call_oi > 0 else None


# def _max_pain(oc: Dict[str, Any]) -> Optional[float]:
#     strikes = sorted(float(k) for k in oc.keys())
#     if not strikes:
#         return None
#     oi_map = {
#         s: (
#             _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")),
#             _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")),
#         )
#         for s in strikes
#     }
#     best = None
#     best_pain = None
#     for settle in strikes:
#         pain = sum(max(0.0, settle - s) * ce + max(0.0, s - settle) * pe for s, (ce, pe) in oi_map.items())
#         if best_pain is None or pain < best_pain:
#             best_pain = pain
#             best = settle
#     return best


# def _get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
#     return (
#         oc.get(f"{strike:.6f}")
#         or oc.get(f"{strike:.2f}")
#         or oc.get(f"{strike:.0f}")
#         or oc.get(str(int(strike)))
#         or {}
#     )


# def _top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
#     items: List[Tuple[float, float]] = []
#     for k, v in oc.items():
#         try:
#             items.append((float(k), _num(v.get(side, {}).get("oi"))))
#         except Exception:
#             continue
#     return sorted(items, key=lambda x: x[1], reverse=True)[:n]


# def _strikes_around_atm(oc: Dict[str, Any], spot: float, window: int) -> List[Tuple[float, Dict[str, Any]]]:
#     all_strikes = sorted(float(k) for k in oc.keys())
#     if not all_strikes:
#         return []
#     atm = _nearest_strike(all_strikes, spot)
#     nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: window * 2 + 1]
#     return [(s, oc.get(f"{s:.6f}") or oc.get(str(s)) or {}) for s in sorted(nearby)]


# def _calc_vwap(candles: List[Candle]) -> float:
#     num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in candles)
#     den = sum(c.volume for c in candles)
#     return (num / den) if den > 0 else 0.0


# def _analyse_intraday(candles: List[Candle]) -> Optional[IntradayLevels]:
#     if not candles:
#         return None

#     day_open = candles[0].open
#     day_high = max(c.high for c in candles)
#     day_low = min(c.low for c in candles)
#     last_close = candles[-1].close
#     vwap = _calc_vwap(candles)

#     if last_close > vwap and last_close > day_open:
#         trend = "uptrend"
#     elif last_close < vwap and last_close < day_open:
#         trend = "downtrend"
#     else:
#         trend = "sideways"

#     recent = candles[-6:] if len(candles) >= 6 else candles
#     intraday_support = min(c.low for c in recent)
#     intraday_resistance = max(c.high for c in recent)
#     prev_candle = candles[-2] if len(candles) >= 2 else candles[-1]

#     return IntradayLevels(
#         day_open=day_open,
#         day_high=day_high,
#         day_low=day_low,
#         last_close=last_close,
#         vwap=vwap,
#         trend=trend,
#         candle_count=len(candles),
#         last_candle=candles[-1],
#         intraday_support=intraday_support,
#         intraday_resistance=intraday_resistance,
#         prev_candle_high=prev_candle.high,
#         prev_candle_low=prev_candle.low,
#     )


# def _intraday_trade_levels(side: str, ltp: float, il: IntradayLevels) -> TradeLevels:
#     if side == "CE":
#         aligned = il.trend == "uptrend"
#         against = il.trend == "downtrend"
#     else:
#         aligned = il.trend == "downtrend"
#         against = il.trend == "uptrend"

#     if aligned:
#         sl_pct = 0.15
#         note = f"✅ Trade is trend-aligned ({il.trend}). Tighter SL used."
#     elif against:
#         sl_pct = 0.25
#         note = f"⚠️ Trade is counter-trend ({il.trend}). Wider SL used."
#     else:
#         sl_pct = 0.20
#         note = "➡️ Market is sideways. Standard SL used."

#     buy = round(ltp, 2)
#     stop_loss = round(max(ltp * (1 - sl_pct), 1.0), 2)
#     risk = round(max(buy - stop_loss, 0.01), 2)
#     target1 = round(buy + risk * 1.0, 2)
#     target2 = round(buy + risk * 2.0, 2)
#     reward1 = round(target1 - buy, 2)
#     reward2 = round(target2 - buy, 2)
#     rr1 = round(reward1 / risk, 1) if risk > 0 else 0.0
#     rr2 = round(reward2 / risk, 1) if risk > 0 else 0.0

#     return TradeLevels(
#         buy=buy,
#         target1=target1,
#         target2=target2,
#         stop_loss=stop_loss,
#         risk=risk,
#         reward1=reward1,
#         reward2=reward2,
#         rr1=rr1,
#         rr2=rr2,
#         note=note,
#     )


# # ─────────────────────────────────────────────────────────────────────────────
# # Candlestick patterns
# # ─────────────────────────────────────────────────────────────────────────────

# BULLISH_PATTERNS = {
#     "CDLHAMMER",
#     "CDLINVERTEDHAMMER",
#     "CDLENGULFING",
#     "CDLPIERCING",
#     "CDLMORNINGSTAR",
#     "CDLMORNINGDOJISTAR",
#     "CDL3WHITESOLDIERS",
#     "CDLTAKURI",
#     "CDLDRAGONFLYDOJI",
# }

# BEARISH_PATTERNS = {
#     "CDLSHOOTINGSTAR",
#     "CDLHANGINGMAN",
#     "CDLENGULFING",
#     "CDLDARKCLOUDCOVER",
#     "CDLEVENINGSTAR",
#     "CDLEVENINGDOJISTAR",
#     "CDL3BLACKCROWS",
#     "CDLGRAVESTONEDOJI",
#     "CDLADVANCEBLOCK",
# }


# def _pattern_function_names() -> List[str]:
#     try:
#         import talib  # type: ignore

#         return sorted([n for n in dir(talib) if n.startswith("CDL") and callable(getattr(talib, n))])
#     except Exception:
#         return []


# def detect_patterns(df: pd.DataFrame) -> List[str]:
#     if len(df) < 5:
#         return []

#     try:
#         import talib  # type: ignore

#         open_ = df["open"].astype(float).to_numpy()
#         high = df["high"].astype(float).to_numpy()
#         low = df["low"].astype(float).to_numpy()
#         close = df["close"].astype(float).to_numpy()

#         matches: List[str] = []
#         for name in _pattern_function_names():
#             fn = getattr(talib, name)
#             try:
#                 out = fn(open_, high, low, close)
#                 if len(out) and int(out[-1]) != 0:
#                     matches.append(name)
#             except Exception:
#                 continue
#         return matches
#     except Exception:
#         last = df.iloc[-1]
#         prev = df.iloc[-2]
#         body = abs(last.close - last.open)
#         rng = max(last.high - last.low, 1e-9)
#         upper = last.high - max(last.open, last.close)
#         lower = min(last.open, last.close) - last.low
#         matches: List[str] = []

#         if lower >= 2 * body and upper <= body * 0.3:
#             matches.append("CDLHAMMER")
#         if upper >= 2 * body and lower <= body * 0.3:
#             matches.append("CDLSHOOTINGSTAR")
#         if last.close > last.open and prev.close < prev.open and last.close >= prev.open and last.open <= prev.close:
#             matches.append("CDLENGULFING")
#         if last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
#             matches.append("CDLENGULFING")
#         if body / rng <= 0.1:
#             matches.append("CDLDOJI")
#         return matches


# def infer_direction(patterns: List[str]) -> str:
#     bullish = any(p in BULLISH_PATTERNS for p in patterns)
#     bearish = any(p in BEARISH_PATTERNS for p in patterns)
#     if bullish and not bearish:
#         return "BULLISH"
#     if bearish and not bullish:
#         return "BEARISH"
#     if bullish and bearish:
#         return "MIXED"
#     return "NEUTRAL"


# # ─────────────────────────────────────────────────────────────────────────────
# # Alert building
# # ─────────────────────────────────────────────────────────────────────────────


# def compute_pcr_from_chain(chain_json: Dict[str, Any]) -> Optional[float]:
#     try:
#         data = chain_json.get("data", chain_json)
#         oc = data["oc"]
#         ce_oi = 0.0
#         pe_oi = 0.0
#         for strike_data in oc.values():
#             ce = strike_data.get("ce") or {}
#             pe = strike_data.get("pe") or {}
#             ce_oi += float(ce.get("oi") or 0)
#             pe_oi += float(pe.get("oi") or 0)
#         if ce_oi <= 0:
#             return None
#         return float(pe_oi / ce_oi)
#     except Exception:
#         return None


# def choose_option_from_chain(chain_json: Dict[str, Any], direction: str, underlying_ltp: float) -> Tuple[Optional[float], Optional[int], Optional[float]]:
#     try:
#         data = chain_json.get("data", chain_json)
#         oc = data["oc"]
#         strikes = sorted(float(k) for k in oc.keys())
#         if not strikes:
#             return None, None, None
#         strike = _nearest_strike(strikes, underlying_ltp)
#         candidate_key = min(oc.keys(), key=lambda k: abs(float(k) - strike))
#         side = "ce" if direction == "BULLISH" else "pe" if direction == "BEARISH" else "ce"
#         info = oc[candidate_key].get(side) or {}
#         sid = info.get("security_id")
#         ltp = info.get("last_price")
#         return float(candidate_key), int(sid) if sid is not None else None, float(ltp) if ltp is not None else None
#     except Exception:
#         return None, None, None


# def build_alert(
#     expiry: str,
#     candles: pd.DataFrame,
#     idx: int,
#     chain: Dict[str, Any],
# ) -> Optional[Alert]:
#     window = candles.iloc[: idx + 1].reset_index(drop=True)
#     pats = detect_patterns(window)
#     if not pats:
#         return None

#     direction = infer_direction(pats)
#     last = window.iloc[-1]
#     candle_list = [
#         Candle(
#             ts=row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp,
#             open=float(row.open),
#             high=float(row.high),
#             low=float(row.low),
#             close=float(row.close),
#             volume=float(row.volume),
#         )
#         for row in window.itertuples(index=False)
#     ]
#     il = _analyse_intraday(candle_list)
#     if il is None:
#         return None

#     pcr = compute_pcr_from_chain(chain)
#     data = chain.get("data", chain)
#     oc: Dict[str, Any] = data.get("oc") or {}
#     if not oc:
#         return None

#     spot = float(last.close)
#     all_strikes = sorted(float(k) for k in oc.keys())
#     atm = _nearest_strike(all_strikes, spot)
#     support, resistance = _support_resistance_oi(oc)

#     option_side = "CE" if direction == "BULLISH" else "PE" if direction == "BEARISH" else "CE"
#     strike, opt_security_id, opt_ltp = choose_option_from_chain(chain, direction, spot)

#     if opt_ltp is None or opt_ltp <= 0:
#         opt_ltp = float(last.close)

#     levels = _intraday_trade_levels(option_side, opt_ltp, il)

#     notes = (
#         "Signal built from 5-minute NIFTY50 candles. "
#         "Bullish patterns map to CE; bearish patterns map to PE. "
#         "Check liquidity and spreads before any trade."
#     )

#     return Alert(
#         timestamp=dt.datetime.now().isoformat(timespec="seconds"),
#         underlying_symbol=NIFTY50_NAME,
#         candle_time=str(il.last_candle.ts),
#         direction=direction,
#         pattern_names=pats,
#         vwap=il.vwap,
#         underlying_ltp=spot,
#         pcr=pcr,
#         option_side=option_side,
#         strike=strike if strike is not None else atm,
#         option_security_id=opt_security_id,
#         option_ltp=opt_ltp,
#         entry=levels.buy,
#         stop_loss=levels.stop_loss,
#         target=levels.target1,
#         support=support,
#         resistance=resistance,
#         notes=notes,
#     )


# # ─────────────────────────────────────────────────────────────────────────────
# # Formatting
# # ─────────────────────────────────────────────────────────────────────────────


# def format_alert(alert: Alert) -> str:
#     patterns = ", ".join(alert.pattern_names)
#     emoji = "🟢" if alert.direction == "BULLISH" else "🔴" if alert.direction == "BEARISH" else "🟡"

#     lines = [
#         f"{emoji} <b>NIFTY50 5-Min Candle Pattern Alert</b>",
#         "",
#         f"<b>Candle Time:</b> {alert.candle_time}",
#         f"<b>Direction:</b> {alert.direction}",
#         f"<b>Patterns:</b> {patterns}",
#         "",
#         "━━ Market Context ━━",
#         f"Spot Price : ₹{alert.underlying_ltp:,.2f}",
#         f"VWAP       : ₹{alert.vwap:,.2f}",
#         f"PCR        : {alert.pcr if alert.pcr is not None else '—'}",
#         f"Support    : {_fmt(alert.support, 0)}",
#         f"Resistance : {_fmt(alert.resistance, 0)}",
#         "",
#         "━━ Suggested Trade ━━",
#         f"Option Side : {alert.option_side}",
#         f"Strike      : {_fmt(alert.strike, 0)}",
#         f"Option LTP  : ₹{alert.option_ltp if alert.option_ltp else 0:.2f}",
#         f"Entry       : ₹{alert.entry if alert.entry else 0:.2f}",
#         f"Stop Loss   : ₹{alert.stop_loss if alert.stop_loss else 0:.2f}",
#         f"Target      : ₹{alert.target if alert.target else 0:.2f}",
#         "",
#         f"<i>{alert.notes}</i>",
#         f"<i>Updated: {dt.datetime.now().strftime('%H:%M:%S')}</i>",
#     ]
#     return "\n".join(lines)


# def _build_chain_message(rows: List[Tuple[float, Dict[str, Any]]], spot: float, expiry: str, support: Optional[float], resistance: Optional[float], atm: float, pcr: Optional[float], max_pain: Optional[float]) -> str:
#     bias = "neutral"
#     if pcr is not None:
#         bias = "bearish" if pcr < 0.9 else ("bullish" if pcr > 1.1 else "neutral")
#     lines = [
#         f"<b>{NIFTY50_NAME} — Option Chain</b>",
#         f"Expiry: {expiry}  |  Spot: {_fmt(spot)}  |  ATM: {_fmt(atm, 0)}",
#         f"PCR: {_fmt(pcr)}  |  Bias: {bias}  |  Max Pain: {_fmt(max_pain, 0)}",
#         f"Support S: {_fmt(support, 0)}   |   Resistance R: {_fmt(resistance, 0)}",
#         "",
#         "<b>±5 strikes around ATM</b>",
#         "<pre>",
#         f"{'Strike':<8} {'Tag':<7} {'CE OI':>9} {'CE LTP':>7} | {'PE LTP':>7} {'PE OI':>9}",
#         "-" * 54,
#     ]
#     for strike, row in rows:
#         ce = row.get("ce") or {}
#         pe = row.get("pe") or {}
#         tag = ("ATM" if strike == atm else "") + (" S" if strike == support else "") + (" R" if strike == resistance else "")
#         lines.append(
#             f"{strike:<8,.0f} {tag.strip():<7} {_num(ce.get('oi')):>9,.0f} {_num(ce.get('last_price')):>7.2f} | {_num(pe.get('last_price')):>7.2f} {_num(pe.get('oi')):>9,.0f}"
#         )
#     lines += ["</pre>", "S=Support  R=Resistance  ATM=At-the-money", f"<i>Updated: {dt.datetime.now().strftime('%H:%M:%S')}</i>"]
#     return "\n".join(lines)


# def _build_status_message(spot: float, expiry: str, support: Optional[float], resistance: Optional[float], atm: float, pcr: Optional[float], max_pain: Optional[float], call_top: List[Tuple[float, float]], put_top: List[Tuple[float, float]]) -> str:
#     bias = "neutral"
#     if pcr is not None:
#         bias = "bearish pressure" if pcr < 0.9 else ("bullish pressure" if pcr > 1.1 else "neutral")
#     lines = [
#         f"<b>{NIFTY50_NAME} Weekly Watch</b> | Expiry {expiry}",
#         f"Spot: {_fmt(spot)} | ATM: {_fmt(atm, 0)} | PCR: {_fmt(pcr)}",
#         f"Bias: {bias}",
#         f"Support: {_fmt(support, 0)} | Resistance: {_fmt(resistance, 0)} | Max Pain: {_fmt(max_pain, 0)}",
#     ]
#     if call_top:
#         lines.append("Top Call OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in call_top))
#     if put_top:
#         lines.append("Top Put OI:  " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in put_top))
#     lines.append(f"<i>Updated: {dt.datetime.now().strftime('%H:%M:%S')}</i>")
#     return "\n".join(lines)


# HELP_TEXT = f"""<b>{NIFTY50_NAME} Bot Commands</b>

# <b>Scan a date range</b>
#   SCAN 2026-05-01 2026-05-14

# <b>Live control</b>
#   LIVE   — enable live monitoring
#   STOP   — stop current scan

# <b>Market Overview</b>
#   /chain   — ±5 strike option chain table
#   /status  — Spot, PCR, bias, support, resistance
#   /expiry  — Current weekly expiry
#   /help    — This help message

# Send a SCAN command with two dates in YYYY-MM-DD format."""


# # ─────────────────────────────────────────────────────────────────────────────
# # Scanner
# # ─────────────────────────────────────────────────────────────────────────────

# class MarketWatchAgent:
#     def __init__(self, cfg: Config):
#         self.cfg = cfg
#         self.api = DhanApiClient(cfg)
#         self.bot = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
#         self.expiry: Optional[str] = None
#         self.stop_scan = False
#         self.live_enabled = True
#         self.last_live_key: Optional[str] = None
#         self.last_live_candle_time: Optional[str] = None

#     def _ensure_expiry(self) -> str:
#         if self.expiry is None:
#             self.expiry = self.api.pick_expiry()
#         return self.expiry

#     def _fetch_chain(self) -> Tuple[str, float, Dict[str, Any]]:
#         expiry = self._ensure_expiry()
#         snapshot = self.api.option_chain(expiry)
#         data = snapshot.get("data", {})
#         spot = _num(data.get("last_price"))
#         oc: Dict[str, Any] = data.get("oc") or {}
#         if not oc:
#             raise RuntimeError("Empty option chain — market may be closed.")
#         return expiry, spot, oc

#     def _current_intraday(self) -> pd.DataFrame:
#         today = dt.datetime.now().date().strftime("%Y-%m-%d")
#         return self.api.intraday_candles(
#             security_id=NIFTY50_SECURITY_ID,
#             exchange_segment=NIFTY50_SEGMENT,
#             instrument="INDEX",
#             interval=5,
#             from_date=f"{today} 09:15:00",
#             to_date=f"{today} 15:30:00",
#             oi=True,
#         )

#     def _handle_chain(self) -> None:
#         expiry, spot, oc = self._fetch_chain()
#         all_strikes = sorted(float(k) for k in oc.keys())
#         atm = _nearest_strike(all_strikes, spot)
#         rows = _strikes_around_atm(oc, spot, self.cfg.strikes_window)
#         support, resistance = _support_resistance_oi(oc)
#         pcr_val = _pcr(oc, spot, self.cfg.strikes_window)
#         max_pain_val = _max_pain(oc)
#         self.bot.send(_build_chain_message(rows, spot, expiry, support, resistance, atm, pcr_val, max_pain_val))

#     def _handle_status(self) -> None:
#         expiry, spot, oc = self._fetch_chain()
#         all_strikes = sorted(float(k) for k in oc.keys())
#         atm = _nearest_strike(all_strikes, spot)
#         support, resistance = _support_resistance_oi(oc)
#         pcr_val = _pcr(oc, spot, self.cfg.strikes_window)
#         max_pain_val = _max_pain(oc)
#         call_top = _top_oi(oc, "ce", 3)
#         put_top = _top_oi(oc, "pe", 3)
#         self.bot.send(_build_status_message(spot, expiry, support, resistance, atm, pcr_val, max_pain_val, call_top, put_top))

#     def _handle_strike(self, side: str, strike: float) -> None:
#         expiry, spot, oc = self._fetch_chain()
#         all_strikes = sorted(float(k) for k in oc.keys())
#         atm = _nearest_strike(all_strikes, spot)
#         support, resistance = _support_resistance_oi(oc)
#         pcr_val = _pcr(oc, spot, self.cfg.strikes_window)
#         row = _get_row(oc, strike)
#         option_data = row.get(side.lower(), {})
#         if not row:
#             self.bot.send(
#                 f"⚠️ Strike <b>{strike:,.0f}</b> not found in the option chain.\n"
#                 f"Range: {all_strikes[0]:,.0f} – {all_strikes[-1]:,.0f}"
#             )
#             return

#         candles = self._current_intraday()
#         if candles.empty:
#             self.bot.send("⚠️ Could not fetch intraday candles.")
#             return

#         il = _analyse_intraday([
#             Candle(
#                 ts=row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp,
#                 open=float(row.open),
#                 high=float(row.high),
#                 low=float(row.low),
#                 close=float(row.close),
#                 volume=float(row.volume),
#             )
#             for row in candles.itertuples(index=False)
#         ])
#         if il is None:
#             self.bot.send("⚠️ Could not build intraday levels yet.")
#             return

#         ltp = _num(option_data.get("last_price"))
#         levels = _intraday_trade_levels(side, ltp if ltp > 0 else il.last_close, il)
#         alert = Alert(
#             timestamp=dt.datetime.now().isoformat(timespec="seconds"),
#             underlying_symbol=NIFTY50_NAME,
#             candle_time=str(il.last_candle.ts),
#             direction="BULLISH" if side == "CE" else "BEARISH",
#             pattern_names=[f"Manual query: {side}{int(strike)}"],
#             vwap=il.vwap,
#             underlying_ltp=spot,
#             pcr=pcr_val,
#             option_side=side,
#             strike=strike,
#             option_security_id=int(option_data.get("security_id")) if option_data.get("security_id") else None,
#             option_ltp=ltp,
#             entry=levels.buy,
#             stop_loss=levels.stop_loss,
#             target=levels.target1,
#             support=support,
#             resistance=resistance,
#             notes="Manual strike query using current option chain.",
#         )
#         self.bot.send(format_alert(alert))

#     def _scan_date_range(self, start_date: str, end_date: str) -> None:
#         expiry = self._ensure_expiry()
#         self.stop_scan = False
#         self.bot.send(
#             f"⏳ Scanning {NIFTY50_NAME} 5-minute candles\n"
#             f"From: <b>{start_date}</b>\n"
#             f"To: <b>{end_date}</b>\n\n"
#             f"Send <code>STOP</code> to cancel."
#         )

#         candles = self.api.intraday_candles(
#             security_id=NIFTY50_SECURITY_ID,
#             exchange_segment=NIFTY50_SEGMENT,
#             instrument="INDEX",
#             interval=5,
#             from_date=f"{start_date} 09:15:00",
#             to_date=f"{end_date} 15:30:00",
#             oi=True,
#         )

#         if candles.empty:
#             self.bot.send("⚠️ No candles returned for that range.")
#             return

#         chain = self.api.option_chain(expiry)
#         last_key: Optional[str] = None
#         count = 0

#         for idx in range(4, len(candles)):
#             if self.stop_scan:
#                 self.bot.send("🛑 Scan stopped by user.")
#                 self.stop_scan = False
#                 return

#             window = candles.iloc[: idx + 1].copy().reset_index(drop=True)
#             alert = build_alert(expiry, window, idx, chain)
#             if alert is None:
#                 continue
#             key = f"{alert.candle_time}|{','.join(alert.pattern_names)}"
#             if key == last_key:
#                 continue
#             self.bot.send(format_alert(alert))
#             last_key = key
#             count += 1
#             time.sleep(0.4)

#         if count == 0:
#             self.bot.send("No candlestick patterns detected in that date range.")
#         else:
#             self.bot.send(f"✅ Scan completed. Sent {count} alert(s).")

#     def _live_check(self) -> None:
#         """Continuously monitor the latest completed NIFTY50 5-minute candle and alert on new patterns."""
#         expiry = self._ensure_expiry()
#         candles = self._current_intraday()
#         if candles.empty or len(candles) < 5:
#             return

#         latest = candles.iloc[-1]
#         candle_time = str(latest.timestamp)
#         if candle_time == self.last_live_candle_time:
#             return

#         chain = self.api.option_chain(expiry)
#         alert = build_alert(expiry, candles.reset_index(drop=True), len(candles) - 1, chain)
#         self.last_live_candle_time = candle_time

#         if alert is None:
#             return

#         key = f"{alert.candle_time}|{','.join(alert.pattern_names)}"
#         if key == self.last_live_key:
#             return

#         self.bot.send(format_alert(alert))
#         self.last_live_key = key

#     def _dispatch(self, raw: str) -> None:
#         text = raw.strip()
#         cmd = text.split("@")[0].lower()

#         m = STRIKE_RE.match(text.replace(" ", ""))
#         if m:
#             side = m.group(1).upper()
#             strike = float(m.group(2))
#             self._handle_strike(side, strike)
#             return

#         m = SCAN_RE.match(text)
#         if m:
#             self._scan_date_range(m.group(1), m.group(2))
#             return

#         if STOP_RE.match(text):
#             self.stop_scan = True
#             self.bot.send("🛑 Stop requested.")
#             return

#         if LIVE_RE.match(text):
#             self.live_enabled = True
#             self.stop_scan = False
#             self.bot.send("✅ Live monitoring enabled.")
#             return

#         if cmd in ("/chain", "chain"):
#             self._handle_chain()
#         elif cmd in ("/status", "status"):
#             self._handle_status()
#         elif cmd in ("/expiry", "expiry"):
#             self.bot.send(f"Current weekly expiry: <b>{self._ensure_expiry()}</b>")
#         elif cmd in ("/help", "help", "/start", "start"):
#             self.bot.send(HELP_TEXT)
#         else:
#             self.bot.send(f"Unknown command: <code>{text}</code>\n\n{HELP_TEXT}")

#     def run(self) -> None:
#         print(f"Market Watch Agent started | {NIFTY50_NAME}")
#         self.bot.send(
#             f"<b>{NIFTY50_NAME} Candle Bot is online!</b>\n\n"
#             f"Live monitoring is ON.\n"
#             f"Send <code>SCAN YYYY-MM-DD YYYY-MM-DD</code> for backtest.\n"
#             f"Send <code>STOP</code> to cancel a scan.\n"
#             f"Send <code>LIVE</code> to keep live monitoring on."
#         )

#         while True:
#             try:
#                 for msg in self.bot.get_messages():
#                     print(f"Message: {msg}")
#                     try:
#                         self._dispatch(msg)
#                     except Exception as e:
#                         err = f"⚠️ Error: {e}"
#                         print(err)
#                         self.bot.send(err)

#                 if self.live_enabled and not self.stop_scan:
#                     try:
#                         self._live_check()
#                     except Exception as e:
#                         print(f"Live check error: {e}")

#                 time.sleep(self.cfg.tg_poll_interval)

#             except KeyboardInterrupt:
#                 self.bot.send(f"{NIFTY50_NAME} Candle Bot stopped.")
#                 print("Stopped.")
#                 return
#             except requests.HTTPError as e:
#                 print(f"HTTP error: {e}")
#                 time.sleep(5)
#             except Exception as e:
#                 print(f"Error: {e}")
#                 time.sleep(5)


# # ─────────────────────────────────────────────────────────────────────────────
# # Main
# # ─────────────────────────────────────────────────────────────────────────────

# def main() -> None:
#     cfg = Config.from_env()
#     MarketWatchAgent(cfg).run()


# if __name__ == "__main__":
#     main()
"""NIFTY50 5-minute candlestick scanner with Telegram commands.

Features:
- Live monitoring runs continuously and alerts when a NEW 5-minute candle forms a pattern.
- Only BULLISH or BEARISH patterns trigger alerts. NEUTRAL / MIXED are silently skipped.
- SCAN YYYY-MM-DD YYYY-MM-DD runs a backtest on that date range.
- STOP cancels a running scan.
- LIVE re-enables live monitoring.
- Timestamps are converted to IST.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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
IST_TZ = "Asia/Kolkata"

SCAN_RE   = re.compile(r"^SCAN\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
STOP_RE   = re.compile(r"^STOP$", re.IGNORECASE)
LIVE_RE   = re.compile(r"^LIVE$", re.IGNORECASE)
STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    http_timeout: int = 15
    tg_poll_interval: float = 1.5
    strikes_window: int = 5

    @staticmethod
    def from_env() -> "Config":
        required = {
            "DHAN_CLIENT_ID":     os.getenv("DHAN_CLIENT_ID", "").strip(),
            "DHAN_ACCESS_TOKEN":  os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise SystemExit("Missing env vars: " + ", ".join(missing))

        def _int(name: str, default: int) -> int:
            try:   return int(os.getenv(name, str(default)).strip())
            except Exception: return default

        def _float(name: str, default: float) -> float:
            try:   return float(os.getenv(name, str(default)).strip())
            except Exception: return default

        return Config(
            dhan_client_id=required["DHAN_CLIENT_ID"],
            dhan_access_token=required["DHAN_ACCESS_TOKEN"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            http_timeout=_int("HTTP_TIMEOUT", 15),
            tg_poll_interval=_float("TG_POLL_INTERVAL", 1.5),
            strikes_window=_int("STRIKES_WINDOW", 5),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Candle:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclasses.dataclass
class IntradayLevels:
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


@dataclasses.dataclass
class TradeLevels:
    buy: float
    target1: float
    target2: float
    stop_loss: float
    risk: float
    reward1: float
    reward2: float
    rr1: float
    rr2: float
    note: str


@dataclasses.dataclass
class Alert:
    timestamp: str
    underlying_symbol: str
    candle_time: str
    direction: str           # "BULLISH" or "BEARISH" only
    pattern_names: List[str]
    vwap: float
    underlying_ltp: float
    pcr: Optional[float]
    option_side: str         # "CE" or "PE"
    strike: Optional[float]
    option_security_id: Optional[int]
    option_ltp: Optional[float]
    entry: Optional[float]
    stop_loss: Optional[float]
    target: Optional[float]
    support: Optional[float]
    resistance: Optional[float]
    notes: str


# ─────────────────────────────────────────────────────────────────────────────
# Dhan API
# ─────────────────────────────────────────────────────────────────────────────

class DhanApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept":        "application/json",
            "access-token":  cfg.dhan_access_token,
            "client-id":     cfg.dhan_client_id,
        })

    def expiry_list(self) -> List[str]:
        r = self.session.post(
            f"{DHAN_BASE}/optionchain/expirylist",
            data=json.dumps({
                "UnderlyingScrip": NIFTY50_SECURITY_ID,
                "UnderlyingSeg":   NIFTY50_SEGMENT,
            }),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        return [str(x) for x in r.json().get("data", [])]

    def pick_expiry(self) -> str:
        expiries = self.expiry_list()
        if not expiries:
            raise RuntimeError("No expiry dates returned by Dhan.")
        today = dt.date.today()

        def _parsed(x: str) -> dt.date:
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
                try:   return dt.datetime.strptime(x, fmt).date()
                except Exception: continue
            return dt.date.max

        for item in sorted(expiries, key=_parsed):
            if _parsed(item) >= today:
                return item
        return expiries[0]

    def option_chain(self, expiry: str) -> Dict[str, Any]:
        r = self.session.post(
            f"{DHAN_BASE}/optionchain",
            data=json.dumps({
                "UnderlyingScrip": NIFTY50_SECURITY_ID,
                "UnderlyingSeg":   NIFTY50_SEGMENT,
                "Expiry":          expiry,
            }),
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
            "securityId":      str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument":      instrument,
            "interval":        str(interval),
            "oi":              bool(oi),
            "fromDate":        from_date,
            "toDate":          to_date,
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
            return self._list_to_df(raw)
        if isinstance(raw, dict):
            return self._dict_to_df(raw)
        raise ValueError(f"Unexpected intraday response shape: {type(raw)}")

    @staticmethod
    def _list_to_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
        return DhanApiClient._normalize_df(pd.DataFrame(rows))

    @staticmethod
    def _dict_to_df(data: Dict[str, Any]) -> pd.DataFrame:
        keys = {k.lower(): k for k in data.keys()}
        required = ["open", "high", "low", "close", "volume", "timestamp"]
        missing = [k for k in required if k not in keys]
        if missing:
            raise ValueError(f"Intraday response missing keys: {missing}")
        df = pd.DataFrame({
            "timestamp": data[keys["timestamp"]],
            "open":      data[keys["open"]],
            "high":      data[keys["high"]],
            "low":       data[keys["low"]],
            "close":     data[keys["close"]],
            "volume":    data[keys["volume"]],
        })
        if "open_interest" in keys:
            df["open_interest"] = data[keys["open_interest"]]
        return DhanApiClient._normalize_df(df)

    @staticmethod
    def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        ts = pd.to_numeric(df["timestamp"], errors="coerce")
        if ts.notna().any():
            df["timestamp"] = (
                pd.to_datetime(ts, unit="s", utc=True)
                .dt.tz_convert(IST_TZ)
                .dt.tz_localize(None)
            )
        else:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            if getattr(df["timestamp"].dt, "tz", None) is not None:
                df["timestamp"] = df["timestamp"].dt.tz_convert(IST_TZ).dt.tz_localize(None)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "open_interest" not in df.columns:
            df["open_interest"] = np.nan
        else:
            df["open_interest"] = pd.to_numeric(df["open_interest"], errors="coerce")
        df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"]).sort_values("timestamp")
        return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram bot
# ─────────────────────────────────────────────────────────────────────────────

class TelegramBot:
    def __init__(self, token: str, chat_id: str, timeout: int = 15):
        self.token   = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.session = requests.Session()
        self._offset = 0

    def send(self, text: str) -> None:
        self.session.post(
            f"{TELEGRAM_BASE}/bot{self.token}/sendMessage",
            data={
                "chat_id":                  self.chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
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
            msg     = update.get("message") or update.get("channel_post") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text    = (msg.get("text") or "").strip()
            if chat_id == self.chat_id and text:
                texts.append(text)
        return texts


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _num(v: Any, default: float = 0.0) -> float:
    try:   return float(v) if v is not None else default
    except Exception: return default

def _fmt(v: Any, decimals: int = 2) -> str:
    return "—" if v is None else f"{float(v):,.{decimals}f}"

def _nearest_strike(strikes: List[float], spot: float) -> float:
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else 0.0

def _support_resistance_oi(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    support = resistance = None
    best_pe = best_ce = -1.0
    for k, row in oc.items():
        try:   strike = float(k)
        except Exception: continue
        pe_oi = _num((row.get("pe") or {}).get("oi"))
        ce_oi = _num((row.get("ce") or {}).get("oi"))
        if pe_oi > best_pe: best_pe, support    = pe_oi, strike
        if ce_oi > best_ce: best_ce, resistance = ce_oi, strike
    return support, resistance

def _pcr(oc: Dict[str, Any], center: float, window: int) -> Optional[float]:
    strikes  = sorted(float(k) for k in oc.keys())
    if not strikes: return None
    band     = sorted(strikes, key=lambda s: abs(s - center))[: max(2, window * 2)]
    call_oi  = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")) for s in band)
    put_oi   = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")) for s in band)
    return (put_oi / call_oi) if call_oi > 0 else None

def _max_pain(oc: Dict[str, Any]) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes: return None
    oi_map  = {
        s: (
            _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")),
            _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")),
        )
        for s in strikes
    }
    best = best_pain = None
    for settle in strikes:
        pain = sum(max(0.0, settle - s) * ce + max(0.0, s - settle) * pe for s, (ce, pe) in oi_map.items())
        if best_pain is None or pain < best_pain:
            best_pain, best = pain, settle
    return best

def _get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
    return (
        oc.get(f"{strike:.6f}") or oc.get(f"{strike:.2f}")
        or oc.get(f"{strike:.0f}") or oc.get(str(int(strike))) or {}
    )

def _top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
    items: List[Tuple[float, float]] = []
    for k, v in oc.items():
        try:   items.append((float(k), _num(v.get(side, {}).get("oi"))))
        except Exception: continue
    return sorted(items, key=lambda x: x[1], reverse=True)[:n]

def _strikes_around_atm(oc: Dict[str, Any], spot: float, window: int) -> List[Tuple[float, Dict[str, Any]]]:
    all_strikes = sorted(float(k) for k in oc.keys())
    if not all_strikes: return []
    atm    = _nearest_strike(all_strikes, spot)
    nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: window * 2 + 1]
    return [(s, oc.get(f"{s:.6f}") or oc.get(str(s)) or {}) for s in sorted(nearby)]

def _calc_vwap(candles: List[Candle]) -> float:
    num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in candles)
    den = sum(c.volume for c in candles)
    return (num / den) if den > 0 else 0.0

def _analyse_intraday(candles: List[Candle]) -> Optional[IntradayLevels]:
    if not candles: return None
    day_open   = candles[0].open
    day_high   = max(c.high for c in candles)
    day_low    = min(c.low  for c in candles)
    last_close = candles[-1].close
    vwap       = _calc_vwap(candles)
    if   last_close > vwap and last_close > day_open: trend = "uptrend"
    elif last_close < vwap and last_close < day_open: trend = "downtrend"
    else:                                              trend = "sideways"
    recent      = candles[-6:] if len(candles) >= 6 else candles
    prev_candle = candles[-2]  if len(candles) >= 2 else candles[-1]
    return IntradayLevels(
        day_open=day_open, day_high=day_high, day_low=day_low,
        last_close=last_close, vwap=vwap, trend=trend,
        candle_count=len(candles), last_candle=candles[-1],
        intraday_support=min(c.low  for c in recent),
        intraday_resistance=max(c.high for c in recent),
        prev_candle_high=prev_candle.high,
        prev_candle_low=prev_candle.low,
    )

def _intraday_trade_levels(side: str, ltp: float, il: IntradayLevels) -> TradeLevels:
    if side == "CE":
        aligned = il.trend == "uptrend"
        against = il.trend == "downtrend"
    else:
        aligned = il.trend == "downtrend"
        against = il.trend == "uptrend"

    if aligned:
        sl_pct = 0.15
        note   = f"✅ Trade is trend-aligned ({il.trend}). Tighter SL used."
    elif against:
        sl_pct = 0.25
        note   = f"⚠️ Trade is counter-trend ({il.trend}). Wider SL used."
    else:
        sl_pct = 0.20
        note   = "➡️ Market is sideways. Standard SL used."

    buy       = round(ltp, 2)
    stop_loss = round(max(ltp * (1 - sl_pct), 1.0), 2)
    risk      = round(max(buy - stop_loss, 0.01), 2)
    target1   = round(buy + risk * 1.0, 2)
    target2   = round(buy + risk * 2.0, 2)
    reward1   = round(target1 - buy, 2)
    reward2   = round(target2 - buy, 2)
    rr1       = round(reward1 / risk, 1) if risk > 0 else 0.0
    rr2       = round(reward2 / risk, 1) if risk > 0 else 0.0
    return TradeLevels(
        buy=buy, target1=target1, target2=target2,
        stop_loss=stop_loss, risk=risk,
        reward1=reward1, reward2=reward2,
        rr1=rr1, rr2=rr2, note=note,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Candlestick patterns
# ─────────────────────────────────────────────────────────────────────────────

BULLISH_PATTERNS = {
    "CDLHAMMER", "CDLINVERTEDHAMMER", "CDLENGULFING", "CDLPIERCING",
    "CDLMORNINGSTAR", "CDLMORNINGDOJISTAR", "CDL3WHITESOLDIERS",
    "CDLTAKURI", "CDLDRAGONFLYDOJI",
}

BEARISH_PATTERNS = {
    "CDLSHOOTINGSTAR", "CDLHANGINGMAN", "CDLENGULFING", "CDLDARKCLOUDCOVER",
    "CDLEVENINGSTAR", "CDLEVENINGDOJISTAR", "CDL3BLACKCROWS",
    "CDLGRAVESTONEDOJI", "CDLADVANCEBLOCK",
}

# Patterns that are ONLY neutral — never fire an alert
NEUTRAL_ONLY_PATTERNS = {"CDLDOJI", "CDLLONGLEGGEDDOJI", "CDLSPINNINGTOP"}


def _pattern_function_names() -> List[str]:
    try:
        import talib  # type: ignore
        return sorted([n for n in dir(talib) if n.startswith("CDL") and callable(getattr(talib, n))])
    except Exception:
        return []


def detect_patterns(df: pd.DataFrame) -> List[str]:
    """Return list of detected pattern names. Never includes neutral-only patterns."""
    if len(df) < 5:
        return []

    try:
        import talib  # type: ignore

        open_  = df["open"].astype(float).to_numpy()
        high   = df["high"].astype(float).to_numpy()
        low    = df["low"].astype(float).to_numpy()
        close  = df["close"].astype(float).to_numpy()

        matches: List[str] = []
        for name in _pattern_function_names():
            if name in NEUTRAL_ONLY_PATTERNS:
                continue                        # ← skip pure-neutral patterns
            fn = getattr(talib, name)
            try:
                out = fn(open_, high, low, close)
                if len(out) and int(out[-1]) != 0:
                    matches.append(name)
            except Exception:
                continue
        return matches

    except Exception:
        # Fallback manual detection (no talib)
        last  = df.iloc[-1]
        prev  = df.iloc[-2]
        body  = abs(last.close - last.open)
        rng   = max(last.high - last.low, 1e-9)
        upper = last.high - max(last.open, last.close)
        lower = min(last.open, last.close) - last.low
        matches: List[str] = []

        # Hammer — bullish
        if lower >= 2 * body and upper <= body * 0.3:
            matches.append("CDLHAMMER")
        # Shooting Star — bearish
        if upper >= 2 * body and lower <= body * 0.3:
            matches.append("CDLSHOOTINGSTAR")
        # Bullish Engulfing
        if (last.close > last.open and prev.close < prev.open
                and last.close >= prev.open and last.open <= prev.close):
            matches.append("CDLENGULFING")
        # Bearish Engulfing
        if (last.close < last.open and prev.close > prev.open
                and last.open >= prev.close and last.close <= prev.open):
            matches.append("CDLENGULFING")
        # Doji excluded from fallback (neutral only)
        return matches


def infer_direction(patterns: List[str]) -> Optional[str]:
    """
    Returns "BULLISH", "BEARISH", or None.
    None means skip — no alert fired (was NEUTRAL or MIXED).
    """
    bullish = any(p in BULLISH_PATTERNS for p in patterns)
    bearish = any(p in BEARISH_PATTERNS for p in patterns)

    if bullish and not bearish:
        return "BULLISH"
    if bearish and not bullish:
        return "BEARISH"
    # MIXED or no directional pattern → skip
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Alert building
# ─────────────────────────────────────────────────────────────────────────────

def compute_pcr_from_chain(chain_json: Dict[str, Any]) -> Optional[float]:
    try:
        data = chain_json.get("data", chain_json)
        oc   = data["oc"]
        ce_oi = pe_oi = 0.0
        for strike_data in oc.values():
            ce_oi += float((strike_data.get("ce") or {}).get("oi") or 0)
            pe_oi += float((strike_data.get("pe") or {}).get("oi") or 0)
        return (pe_oi / ce_oi) if ce_oi > 0 else None
    except Exception:
        return None


def choose_option_from_chain(
    chain_json: Dict[str, Any], direction: str, underlying_ltp: float
) -> Tuple[Optional[float], Optional[int], Optional[float]]:
    try:
        data    = chain_json.get("data", chain_json)
        oc      = data["oc"]
        strikes = sorted(float(k) for k in oc.keys())
        if not strikes: return None, None, None
        strike  = _nearest_strike(strikes, underlying_ltp)
        key     = min(oc.keys(), key=lambda k: abs(float(k) - strike))
        side    = "ce" if direction == "BULLISH" else "pe"
        info    = oc[key].get(side) or {}
        sid     = info.get("security_id")
        ltp     = info.get("last_price")
        return float(key), (int(sid) if sid is not None else None), (float(ltp) if ltp is not None else None)
    except Exception:
        return None, None, None


def build_alert(
    expiry: str,
    candles: pd.DataFrame,
    idx: int,
    chain: Dict[str, Any],
) -> Optional[Alert]:
    window = candles.iloc[: idx + 1].reset_index(drop=True)
    pats   = detect_patterns(window)
    if not pats:
        return None

    direction = infer_direction(pats)
    if direction is None:
        # NEUTRAL or MIXED — silently skip, no alert
        return None

    last = window.iloc[-1]
    candle_list = [
        Candle(
            ts=row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp,
            open=float(row.open), high=float(row.high),
            low=float(row.low),   close=float(row.close), volume=float(row.volume),
        )
        for row in window.itertuples(index=False)
    ]
    il = _analyse_intraday(candle_list)
    if il is None:
        return None

    pcr    = compute_pcr_from_chain(chain)
    data   = chain.get("data", chain)
    oc: Dict[str, Any] = data.get("oc") or {}
    if not oc: return None

    spot        = float(last.close)
    all_strikes = sorted(float(k) for k in oc.keys())
    atm         = _nearest_strike(all_strikes, spot)
    support, resistance = _support_resistance_oi(oc)

    option_side = "CE" if direction == "BULLISH" else "PE"
    strike, opt_security_id, opt_ltp = choose_option_from_chain(chain, direction, spot)

    if opt_ltp is None or opt_ltp <= 0:
        opt_ltp = float(last.close)

    levels = _intraday_trade_levels(option_side, opt_ltp, il)

    return Alert(
        timestamp=dt.datetime.now().isoformat(timespec="seconds"),
        underlying_symbol=NIFTY50_NAME,
        candle_time=str(il.last_candle.ts),
        direction=direction,
        pattern_names=pats,
        vwap=il.vwap,
        underlying_ltp=spot,
        pcr=pcr,
        option_side=option_side,
        strike=strike if strike is not None else atm,
        option_security_id=opt_security_id,
        option_ltp=opt_ltp,
        entry=levels.buy,
        stop_loss=levels.stop_loss,
        target=levels.target1,
        support=support,
        resistance=resistance,
        notes=levels.note,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Alert message formatter
# ─────────────────────────────────────────────────────────────────────────────

def format_alert(alert: Alert) -> str:
    patterns  = ", ".join(alert.pattern_names)
    emoji     = "🟢" if alert.direction == "BULLISH" else "🔴"
    buy_label = "🟢 BUY CE (Call)" if alert.direction == "BULLISH" else "🔴 BUY PE (Put)"

    bias = "neutral"
    if alert.pcr is not None:
        bias = "bearish" if alert.pcr < 0.9 else ("bullish" if alert.pcr > 1.1 else "neutral")

    lines = [
        f"{emoji} <b>NIFTY50 5-Min Pattern Alert — {alert.direction}</b>",
        "",
        f"<b>Pattern(s) :</b> {patterns}",
        f"<b>Candle Time:</b> {alert.candle_time}",
        "",
        "━━ Market Context ━━━━━━━━━━━━━━━━━━",
        f"Spot       : ₹{alert.underlying_ltp:,.2f}",
        f"VWAP       : ₹{alert.vwap:,.2f}",
        f"PCR        : {_fmt(alert.pcr)}  |  Bias: {bias}",
        f"Support    : {_fmt(alert.support,    0)}   (highest PE OI)",
        f"Resistance : {_fmt(alert.resistance, 0)}  (highest CE OI)",
        "",
        "━━ Suggested Trade ━━━━━━━━━━━━━━━━━",
        f"{buy_label}",
        f"Strike     : NIFTY {alert.option_side} {_fmt(alert.strike, 0)} (ATM)",
        f"Option LTP : ₹{alert.option_ltp or 0:,.2f}",
        "",
        "━━ Trade Levels ━━━━━━━━━━━━━━━━━━━━",
        f"{'🟢' if alert.direction == 'BULLISH' else '🔴'} Entry      : ₹{alert.entry or 0:,.2f}",
        f"🎯 Target 1  : ₹{alert.target or 0:,.2f}   (1:1 R:R)",
        f"🛑 Stop Loss : ₹{alert.stop_loss or 0:,.2f}",
        "",
        f"{alert.notes}",
        "",
        "<i>⚠️ Calculated levels only. Not financial advice.</i>",
        f"<i>🕐 {dt.datetime.now().strftime('%d-%b-%Y %H:%M:%S')}</i>",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Chain / status message builders (unchanged from reference)
# ─────────────────────────────────────────────────────────────────────────────

def _build_chain_message(rows, spot, expiry, support, resistance, atm, pcr, max_pain) -> str:
    bias = "neutral"
    if pcr is not None:
        bias = "bearish" if pcr < 0.9 else ("bullish" if pcr > 1.1 else "neutral")
    lines = [
        f"<b>{NIFTY50_NAME} — Option Chain</b>",
        f"Expiry: {expiry}  |  Spot: {_fmt(spot)}  |  ATM: {_fmt(atm, 0)}",
        f"PCR: {_fmt(pcr)}  |  Bias: {bias}  |  Max Pain: {_fmt(max_pain, 0)}",
        f"Support S: {_fmt(support, 0)}   |   Resistance R: {_fmt(resistance, 0)}",
        "", "<b>±5 strikes around ATM</b>", "<pre>",
        f"{'Strike':<8} {'Tag':<7} {'CE OI':>9} {'CE LTP':>7} | {'PE LTP':>7} {'PE OI':>9}",
        "-" * 54,
    ]
    for strike, row in rows:
        ce  = row.get("ce") or {}
        pe  = row.get("pe") or {}
        tag = ("ATM" if strike == atm else "") + (" S" if strike == support else "") + (" R" if strike == resistance else "")
        lines.append(
            f"{strike:<8,.0f} {tag.strip():<7} "
            f"{_num(ce.get('oi')):>9,.0f} {_num(ce.get('last_price')):>7.2f} | "
            f"{_num(pe.get('last_price')):>7.2f} {_num(pe.get('oi')):>9,.0f}"
        )
    lines += ["</pre>", "S=Support  R=Resistance  ATM=At-the-money",
              f"<i>Updated: {dt.datetime.now().strftime('%H:%M:%S')}</i>"]
    return "\n".join(lines)


def _build_status_message(spot, expiry, support, resistance, atm, pcr, max_pain, call_top, put_top) -> str:
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
    lines.append(f"<i>Updated: {dt.datetime.now().strftime('%H:%M:%S')}</i>")
    return "\n".join(lines)


HELP_TEXT = f"""<b>{NIFTY50_NAME} Bot Commands</b>

<b>Scan a date range</b>
  SCAN 2026-05-01 2026-05-14

<b>Live control</b>
  LIVE   — enable live monitoring
  STOP   — stop current scan

<b>Market Overview</b>
  /chain   — ±5 strike option chain table
  /status  — Spot, PCR, bias, support, resistance
  /expiry  — Current weekly expiry
  /help    — This help message

Alerts fire ONLY for clear Bullish or Bearish patterns.
Neutral / Mixed candles are silently ignored."""


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────

class MarketWatchAgent:
    def __init__(self, cfg: Config):
        self.cfg    = cfg
        self.api    = DhanApiClient(cfg)
        self.bot    = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        self.expiry: Optional[str] = None
        self.stop_scan             = False
        self.live_enabled          = True
        self.last_live_key:         Optional[str] = None
        self.last_live_candle_time: Optional[str] = None

    def _ensure_expiry(self) -> str:
        if self.expiry is None:
            self.expiry = self.api.pick_expiry()
        return self.expiry

    def _fetch_chain(self) -> Tuple[str, float, Dict[str, Any]]:
        expiry   = self._ensure_expiry()
        snapshot = self.api.option_chain(expiry)
        data     = snapshot.get("data", {})
        spot     = _num(data.get("last_price"))
        oc: Dict[str, Any] = data.get("oc") or {}
        if not oc:
            raise RuntimeError("Empty option chain — market may be closed.")
        return expiry, spot, oc

    def _current_intraday(self) -> pd.DataFrame:
        today = dt.datetime.now().date().strftime("%Y-%m-%d")
        return self.api.intraday_candles(
            security_id=NIFTY50_SECURITY_ID,
            exchange_segment=NIFTY50_SEGMENT,
            instrument="INDEX",
            interval=5,
            from_date=f"{today} 09:15:00",
            to_date=f"{today} 15:30:00",
            oi=True,
        )

    # ── Telegram command handlers ─────────────────────────────────────────────

    def _handle_chain(self) -> None:
        expiry, spot, oc = self._fetch_chain()
        all_strikes      = sorted(float(k) for k in oc.keys())
        atm              = _nearest_strike(all_strikes, spot)
        rows             = _strikes_around_atm(oc, spot, self.cfg.strikes_window)
        support, res     = _support_resistance_oi(oc)
        pcr_val          = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val     = _max_pain(oc)
        self.bot.send(_build_chain_message(rows, spot, expiry, support, res, atm, pcr_val, max_pain_val))

    def _handle_status(self) -> None:
        expiry, spot, oc = self._fetch_chain()
        all_strikes      = sorted(float(k) for k in oc.keys())
        atm              = _nearest_strike(all_strikes, spot)
        support, res     = _support_resistance_oi(oc)
        pcr_val          = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val     = _max_pain(oc)
        call_top         = _top_oi(oc, "ce", 3)
        put_top          = _top_oi(oc, "pe", 3)
        self.bot.send(_build_status_message(spot, expiry, support, res, atm, pcr_val, max_pain_val, call_top, put_top))

    def _handle_strike(self, side: str, strike: float) -> None:
        expiry, spot, oc = self._fetch_chain()
        all_strikes      = sorted(float(k) for k in oc.keys())
        atm              = _nearest_strike(all_strikes, spot)
        support, res     = _support_resistance_oi(oc)
        pcr_val          = _pcr(oc, spot, self.cfg.strikes_window)
        row              = _get_row(oc, strike)
        option_data      = row.get(side.lower(), {})

        if not row:
            self.bot.send(
                f"⚠️ Strike <b>{strike:,.0f}</b> not found.\n"
                f"Range: {all_strikes[0]:,.0f} – {all_strikes[-1]:,.0f}"
            )
            return

        candles = self._current_intraday()
        if candles.empty:
            self.bot.send("⚠️ Could not fetch intraday candles.")
            return

        il = _analyse_intraday([
            Candle(ts=r.timestamp.to_pydatetime() if hasattr(r.timestamp, "to_pydatetime") else r.timestamp,
                   open=float(r.open), high=float(r.high), low=float(r.low),
                   close=float(r.close), volume=float(r.volume))
            for r in candles.itertuples(index=False)
        ])
        if il is None:
            self.bot.send("⚠️ Could not build intraday levels yet.")
            return

        ltp    = _num(option_data.get("last_price"))
        levels = _intraday_trade_levels(side, ltp if ltp > 0 else il.last_close, il)

        alert = Alert(
            timestamp=dt.datetime.now().isoformat(timespec="seconds"),
            underlying_symbol=NIFTY50_NAME,
            candle_time=str(il.last_candle.ts),
            direction="BULLISH" if side == "CE" else "BEARISH",
            pattern_names=[f"Manual query: {side}{int(strike)}"],
            vwap=il.vwap,
            underlying_ltp=spot,
            pcr=pcr_val,
            option_side=side,
            strike=strike,
            option_security_id=int(option_data["security_id"]) if option_data.get("security_id") else None,
            option_ltp=ltp,
            entry=levels.buy,
            stop_loss=levels.stop_loss,
            target=levels.target1,
            support=support,
            resistance=res,
            notes=levels.note,
        )
        self.bot.send(format_alert(alert))

    # ── Backtest ──────────────────────────────────────────────────────────────

    def _scan_date_range(self, start_date: str, end_date: str) -> None:
        expiry         = self._ensure_expiry()
        self.stop_scan = False
        self.bot.send(
            f"⏳ Scanning {NIFTY50_NAME} 5-min candles\n"
            f"From: <b>{start_date}</b>  To: <b>{end_date}</b>\n\n"
            f"Only <b>Bullish</b> and <b>Bearish</b> patterns will be reported.\n"
            f"Send <code>STOP</code> to cancel."
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

        chain    = self.api.option_chain(expiry)
        last_key: Optional[str] = None
        count    = 0

        for idx in range(4, len(candles)):
            if self.stop_scan:
                self.bot.send("🛑 Scan stopped by user.")
                self.stop_scan = False
                return

            window = candles.iloc[: idx + 1].copy().reset_index(drop=True)
            alert  = build_alert(expiry, window, idx, chain)
            if alert is None:
                continue                           # neutral/mixed → skip silently

            key = f"{alert.candle_time}|{','.join(alert.pattern_names)}"
            if key == last_key:
                continue
            self.bot.send(format_alert(alert))
            last_key = key
            count   += 1
            time.sleep(0.4)

        if count == 0:
            self.bot.send("No Bullish or Bearish patterns detected in that date range.")
        else:
            self.bot.send(f"✅ Scan complete. <b>{count}</b> alert(s) sent.")

    # ── Live check ────────────────────────────────────────────────────────────

    def _live_check(self) -> None:
        expiry  = self._ensure_expiry()
        candles = self._current_intraday()
        if candles.empty or len(candles) < 5:
            return

        latest      = candles.iloc[-1]
        candle_time = str(latest.timestamp)
        if candle_time == self.last_live_candle_time:
            return

        chain = self.api.option_chain(expiry)
        alert = build_alert(expiry, candles.reset_index(drop=True), len(candles) - 1, chain)
        self.last_live_candle_time = candle_time   # always update so we don't re-check same candle

        if alert is None:
            return                                 # neutral/mixed → no alert

        key = f"{alert.candle_time}|{','.join(alert.pattern_names)}"
        if key == self.last_live_key:
            return

        self.bot.send(format_alert(alert))
        self.last_live_key = key

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def _dispatch(self, raw: str) -> None:
        text = raw.strip()
        cmd  = text.split("@")[0].lower()

        m = STRIKE_RE.match(text.replace(" ", ""))
        if m:
            self._handle_strike(m.group(1).upper(), float(m.group(2)))
            return

        m = SCAN_RE.match(text)
        if m:
            self._scan_date_range(m.group(1), m.group(2))
            return

        if STOP_RE.match(text):
            self.stop_scan = True
            self.bot.send("🛑 Stop requested.")
            return

        if LIVE_RE.match(text):
            self.live_enabled = True
            self.stop_scan    = False
            self.bot.send("✅ Live monitoring enabled.")
            return

        if cmd in ("/chain",  "chain"):  self._handle_chain()
        elif cmd in ("/status", "status"): self._handle_status()
        elif cmd in ("/expiry", "expiry"):
            self.bot.send(f"Current weekly expiry: <b>{self._ensure_expiry()}</b>")
        elif cmd in ("/help", "help", "/start", "start"):
            self.bot.send(HELP_TEXT)
        else:
            self.bot.send(f"Unknown command: <code>{text}</code>\n\n{HELP_TEXT}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        print(f"Market Watch Agent started | {NIFTY50_NAME}")
        self.bot.send(
            f"<b>{NIFTY50_NAME} Candle Bot is online!</b>\n\n"
            f"✅ Live monitoring is ON.\n"
            f"🔕 Neutral / Mixed patterns are <b>silently ignored</b>.\n"
            f"Only 🟢 Bullish and 🔴 Bearish patterns trigger alerts.\n\n"
            f"Send <code>SCAN YYYY-MM-DD YYYY-MM-DD</code> for backtest.\n"
            f"Send <code>STOP</code> to cancel a scan.\n"
            f"Send <code>LIVE</code> to re-enable live monitoring.\n"
            f"Send <code>/help</code> for all commands."
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

                if self.live_enabled and not self.stop_scan:
                    try:
                        self._live_check()
                    except Exception as e:
                        print(f"Live check error: {e}")

                time.sleep(self.cfg.tg_poll_interval)

            except KeyboardInterrupt:
                self.bot.send(f"{NIFTY50_NAME} Candle Bot stopped.")
                print("Stopped.")
                return
            except requests.HTTPError as e:
                print(f"HTTP error: {e}")
                time.sleep(5)
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = Config.from_env()
    MarketWatchAgent(cfg).run()


if __name__ == "__main__":
    main()
