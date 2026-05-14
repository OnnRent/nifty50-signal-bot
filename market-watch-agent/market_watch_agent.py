# from __future__ import annotations

# import dataclasses
# import json
# import os
# import re
# import time
# from datetime import datetime
# from typing import Any, Dict, List, Optional, Tuple

# import requests
# from dotenv import load_dotenv

# load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# DHAN_BASE     = "https://api.dhan.co/v2"
# TELEGRAM_BASE = "https://api.telegram.org"


# # ─────────────────────────────────────────────────────────────────────────────
# # Config
# # ─────────────────────────────────────────────────────────────────────────────

# @dataclasses.dataclass
# class Config:
#     dhan_client_id: str
#     dhan_access_token: str
#     telegram_bot_token: str
#     telegram_chat_id: str
#     underlying_security_id: int = 13
#     underlying_segment: str = "IDX_I"
#     underlying_name_hint: str = "NIFTY 50"
#     strikes_window: int = 5
#     http_timeout: int = 15
#     tg_poll_interval: float = 1.5

#     @staticmethod
#     def from_env() -> "Config":
#         required = {
#             "DHAN_CLIENT_ID":     os.getenv("DHAN_CLIENT_ID", "").strip(),
#             "DHAN_ACCESS_TOKEN":  os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
#             "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
#             "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", "").strip(),
#         }
#         missing = [k for k, v in required.items() if not v]
#         if missing:
#             raise SystemExit("Missing env vars: " + ", ".join(missing))

#         def _int(n: str, d: int) -> int:
#             try: return int(os.getenv(n, str(d)).strip())
#             except ValueError: return d

#         def _float(n: str, d: float) -> float:
#             try: return float(os.getenv(n, str(d)).strip())
#             except ValueError: return d

#         return Config(
#             dhan_client_id=required["DHAN_CLIENT_ID"],
#             dhan_access_token=required["DHAN_ACCESS_TOKEN"],
#             telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
#             telegram_chat_id=required["TELEGRAM_CHAT_ID"],
#             underlying_security_id=_int("DHAN_UNDERLYING_SECURITY_ID", 13),
#             underlying_segment=os.getenv("DHAN_UNDERLYING_SEGMENT", "IDX_I").strip(),
#             underlying_name_hint=os.getenv("UNDERLYING_NAME_HINT", "NIFTY 50").strip(),
#             strikes_window=_int("STRIKES_WINDOW", 5),
#             http_timeout=_int("HTTP_TIMEOUT", 15),
#             tg_poll_interval=_float("TG_POLL_INTERVAL", 1.5),
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Dhan API
# # ─────────────────────────────────────────────────────────────────────────────

# class DhanApiClient:
#     def __init__(self, cfg: Config):
#         self.cfg = cfg
#         self.session = requests.Session()
#         self.session.headers.update({
#             "Content-Type": "application/json",
#             "access-token": cfg.dhan_access_token,
#             "client-id":    cfg.dhan_client_id,
#         })

#     def expiry_list(self) -> List[str]:
#         r = self.session.post(
#             f"{DHAN_BASE}/optionchain/expirylist",
#             data=json.dumps({
#                 "UnderlyingScrip": self.cfg.underlying_security_id,
#                 "UnderlyingSeg":   self.cfg.underlying_segment,
#             }),
#             timeout=self.cfg.http_timeout,
#         )
#         r.raise_for_status()
#         return [str(x) for x in r.json().get("data", [])]

#     def option_chain(self, expiry: str) -> Dict[str, Any]:
#         r = self.session.post(
#             f"{DHAN_BASE}/optionchain",
#             data=json.dumps({
#                 "UnderlyingScrip": self.cfg.underlying_security_id,
#                 "UnderlyingSeg":   self.cfg.underlying_segment,
#                 "Expiry":          expiry,
#             }),
#             timeout=self.cfg.http_timeout,
#         )
#         r.raise_for_status()
#         return r.json()


# # ─────────────────────────────────────────────────────────────────────────────
# # Telegram bot
# # ─────────────────────────────────────────────────────────────────────────────

# class TelegramBot:
#     def __init__(self, token: str, chat_id: str, timeout: int = 15):
#         self.token   = token
#         self.chat_id = str(chat_id)
#         self.timeout = timeout
#         self.session = requests.Session()
#         self._offset = 0

#     def send(self, text: str) -> None:
#         self.session.post(
#             f"{TELEGRAM_BASE}/bot{self.token}/sendMessage",
#             data={
#                 "chat_id":                  self.chat_id,
#                 "text":                     text,
#                 "parse_mode":               "HTML",
#                 "disable_web_page_preview": True,
#             },
#             timeout=self.timeout,
#         ).raise_for_status()

#     def get_messages(self) -> List[str]:
#         """Poll and return new text messages from our chat."""
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
#             msg     = update.get("message") or update.get("channel_post") or {}
#             chat_id = str((msg.get("chat") or {}).get("id", ""))
#             text    = (msg.get("text") or "").strip()
#             if chat_id == self.chat_id and text:
#                 texts.append(text)
#         return texts


# # ─────────────────────────────────────────────────────────────────────────────
# # Helpers
# # ─────────────────────────────────────────────────────────────────────────────

# def _num(v: Any, default: float = 0.0) -> float:
#     try:   return float(v) if v is not None else default
#     except Exception: return default

# def _fmt(v: Any, decimals: int = 2) -> str:
#     return "—" if v is None else f"{float(v):,.{decimals}f}"

# def _nearest_strike(strikes: List[float], spot: float) -> float:
#     return min(strikes, key=lambda s: abs(s - spot))

# def _parse_date(s: str):
#     for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
#         try:   return datetime.strptime(s, fmt).date()
#         except Exception: continue
#     return None

# def _support_resistance(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
#     """Support = strike with max Put OI.  Resistance = strike with max Call OI."""
#     best_pe, support    = -1.0, None
#     best_ce, resistance = -1.0, None
#     for k, row in oc.items():
#         try:   strike = float(k)
#         except Exception: continue
#         pe_oi = _num(row.get("pe", {}).get("oi"))
#         ce_oi = _num(row.get("ce", {}).get("oi"))
#         if pe_oi > best_pe: best_pe, support    = pe_oi, strike
#         if ce_oi > best_ce: best_ce, resistance = ce_oi, strike
#     return support, resistance

# def _pcr(oc: Dict[str, Any], center: float, window: int) -> Optional[float]:
#     strikes = sorted(float(k) for k in oc.keys())
#     band    = sorted(strikes, key=lambda s: abs(s - center))[: max(2, window * 2)]
#     call_oi = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")) for s in band)
#     put_oi  = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")) for s in band)
#     return (put_oi / call_oi) if call_oi > 0 else None

# def _max_pain(oc: Dict[str, Any]) -> Optional[float]:
#     strikes = sorted(float(k) for k in oc.keys())
#     if not strikes: return None
#     oi_map = {
#         s: (
#             _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")),
#             _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")),
#         )
#         for s in strikes
#     }
#     best, best_pain = None, None
#     for settle in strikes:
#         pain = sum(max(0.0, settle - s) * ce + max(0.0, s - settle) * pe
#                    for s, (ce, pe) in oi_map.items())
#         if best_pain is None or pain < best_pain:
#             best_pain, best = pain, settle
#     return best

# def _get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
#     """Fetch the option chain row for a given strike (handles float key formats)."""
#     return (
#         oc.get(f"{strike:.6f}")
#         or oc.get(f"{strike:.2f}")
#         or oc.get(f"{strike:.0f}")
#         or oc.get(str(int(strike)))
#         or {}
#     )


# # ─────────────────────────────────────────────────────────────────────────────
# # Trade levels calculator
# # ─────────────────────────────────────────────────────────────────────────────

# def _trade_levels(
#     ltp: float,
#     side: str,              # "CE" or "PE"
#     support: float,
#     resistance: float,
#     spot: float,
#     strike: float,
# ) -> Dict[str, float]:
#     """
#     Calculate buy price, sell/target price and stop loss.

#     Logic:
#     ──────
#     CE (Call) — bullish trade
#         Buy   : LTP  (entry at market)
#         Target: LTP + (resistance - spot) * 0.3   (30% of move to resistance)
#         SL    : LTP * 0.80                         (20% below entry = hard SL)

#     PE (Put) — bearish trade
#         Buy   : LTP
#         Target: LTP + (spot - support) * 0.3      (30% of move to support)
#         SL    : LTP * 0.80

#     We also cap the SL floor at ₹2 to avoid unrealistic tiny SL for cheap OTM options.
#     Target is capped at 3× LTP so we don't show absurd numbers on deep OTM.
#     """
#     if side == "CE":
#         move      = max(resistance - spot, 0.0) if resistance else 0.0
#         raw_target = ltp + move * 0.30
#     else:
#         move      = max(spot - support, 0.0) if support else 0.0
#         raw_target = ltp + move * 0.30

#     target   = min(raw_target, ltp * 3.0)          # cap at 3×
#     target   = max(target, ltp * 1.05)             # at least 5% gain
#     stop_loss = max(ltp * 0.80, 2.0)               # 20% SL, floor ₹2

#     return {
#         "buy":       round(ltp,        2),
#         "target":    round(target,     2),
#         "stop_loss": round(stop_loss,  2),
#         "risk":      round(ltp - stop_loss, 2),
#         "reward":    round(target - ltp,    2),
#     }


# # ─────────────────────────────────────────────────────────────────────────────
# # Message builder for strike analysis
# # ─────────────────────────────────────────────────────────────────────────────

# def _build_strike_message(
#     side: str,                          # "CE" or "PE"
#     strike: float,
#     option_data: Dict[str, Any],        # ce or pe sub-dict from chain
#     spot: float,
#     expiry: str,
#     support: Optional[float],
#     resistance: Optional[float],
#     atm: float,
#     pcr: Optional[float],
#     max_pain: Optional[float],
# ) -> str:
#     ltp        = _num(option_data.get("last_price"))
#     prev_close = _num(option_data.get("previous_close_price"))
#     oi         = _num(option_data.get("oi"))
#     prev_oi    = _num(option_data.get("previous_oi"))
#     volume     = _num(option_data.get("volume"))
#     iv         = _num(option_data.get("implied_volatility"))
#     bid        = _num(option_data.get("bid_price"))
#     ask        = _num(option_data.get("ask_price"))

#     oi_change   = oi - prev_oi
#     price_chg   = ltp - prev_close
#     price_chg_p = (price_chg / prev_close * 100) if prev_close > 0 else 0.0

#     # ── trade levels ──────────────────────────────────────────────────────────
#     if ltp <= 0:
#         trade_note = "⚠️ LTP is 0 — option may be illiquid or market closed."
#         levels = None
#     else:
#         levels = _trade_levels(ltp, side, support or 0.0, resistance or 0.0, spot, strike)
#         rr     = (levels["reward"] / levels["risk"]) if levels["risk"] > 0 else 0.0
#         trade_note = None

#     # ── market context ────────────────────────────────────────────────────────
#     bias = "neutral"
#     if pcr is not None:
#         if pcr < 0.9:   bias = "bearish"
#         elif pcr > 1.1: bias = "bullish"

#     atm_tag  = " (ATM)" if strike == atm else (" (ITM)" if
#                (side == "CE" and strike < spot) or (side == "PE" and strike > spot)
#                else " (OTM)")

#     emoji = "📈" if side == "CE" else "📉"

#     lines = [
#         f"{emoji} <b>NIFTY {side} {strike:,.0f}{atm_tag}</b>",
#         f"Expiry: {expiry}  |  Spot: {_fmt(spot)}",
#         "",
#         "─── Option Data ───────────────────",
#         f"LTP        : ₹{_fmt(ltp)}",
#         f"Prev Close : ₹{_fmt(prev_close)}  ({price_chg:+.2f} / {price_chg_p:+.1f}%)",
#         f"Bid / Ask  : ₹{_fmt(bid)} / ₹{_fmt(ask)}",
#         f"IV         : {_fmt(iv, 1)}%",
#         f"OI         : {oi:,.0f}  (Chg: {oi_change:+,.0f})",
#         f"Volume     : {volume:,.0f}",
#         "",
#         "─── Market Context ─────────────────",
#         f"PCR        : {_fmt(pcr)}  |  Bias: {bias}",
#         f"ATM Strike : {_fmt(atm, 0)}",
#         f"Max Pain   : {_fmt(max_pain, 0)}",
#         f"Support  S : {_fmt(support, 0)}   (highest PE OI)",
#         f"Resistance R: {_fmt(resistance, 0)}  (highest CE OI)",
#         "",
#     ]

#     if trade_note:
#         lines.append(trade_note)
#     else:
#         sign      = "🟢 BUY" if side == "CE" else "🔴 BUY"
#         lines += [
#             "─── Trade Levels ───────────────────",
#             f"{sign}        : ₹{_fmt(levels['buy'])}",
#             f"🎯 Target    : ₹{_fmt(levels['target'])}",
#             f"🛑 Stop Loss : ₹{_fmt(levels['stop_loss'])}",
#             f"",
#             f"Risk         : ₹{_fmt(levels['risk'])}  per lot",
#             f"Reward       : ₹{_fmt(levels['reward'])}  per lot",
#             f"R:R Ratio    : 1 : {rr:.1f}",
#             "",
#             "<i>⚠️ These are calculated levels, not financial advice.</i>",
#             "<i>Always use your own judgment before trading.</i>",
#         ]

#     lines.append(f"\n<i>Updated: {datetime.now().strftime('%H:%M:%S')}</i>")
#     return "\n".join(lines)


# # ─────────────────────────────────────────────────────────────────────────────
# # Help text
# # ─────────────────────────────────────────────────────────────────────────────

# HELP_TEXT = """<b>NIFTY Bot Commands</b>

# <b>Strike Analysis</b>
#   CE23700   — Analyse NIFTY 23700 Call option
#   PE23700   — Analyse NIFTY 23700 Put option

# <b>Market Overview</b>
#   /chain    — ±5 strike option chain table
#   /status   — Spot, PCR, bias, support, resistance
#   /expiry   — Current weekly expiry date
#   /help     — This help message

# Just type the strike command (e.g. CE24500) and I will fetch live data instantly."""


# # ─────────────────────────────────────────────────────────────────────────────
# # Chain overview helpers (used by /chain and /status)
# # ─────────────────────────────────────────────────────────────────────────────

# def _strikes_around_atm(
#     oc: Dict[str, Any], spot: float, window: int
# ) -> List[Tuple[float, Dict[str, Any]]]:
#     all_strikes = sorted(float(k) for k in oc.keys())
#     if not all_strikes: return []
#     atm    = _nearest_strike(all_strikes, spot)
#     nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: window * 2 + 1]
#     return [(s, oc.get(f"{s:.6f}") or oc.get(str(s)) or {}) for s in sorted(nearby)]

# def _top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
#     items = [(float(k), _num(v.get(side, {}).get("oi"))) for k, v in oc.items()
#              if k.replace(".", "").lstrip("-").isdigit()]
#     return sorted(items, key=lambda x: x[1], reverse=True)[:n]

# def _build_chain_message(
#     rows: List[Tuple[float, Dict[str, Any]]],
#     spot: float, expiry: str,
#     support: Optional[float], resistance: Optional[float],
#     atm: float, pcr: Optional[float], max_pain: Optional[float],
# ) -> str:
#     bias = "neutral"
#     if pcr is not None:
#         bias = "bearish" if pcr < 0.9 else ("bullish" if pcr > 1.1 else "neutral")

#     lines = [
#         "<b>NIFTY 50 — Option Chain</b>",
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
#         ce  = row.get("ce") or {}
#         pe  = row.get("pe") or {}
#         tag = ("ATM" if strike == atm else "") + (" S" if strike == support else "") + (" R" if strike == resistance else "")
#         lines.append(
#             f"{strike:<8,.0f} {tag.strip():<7} "
#             f"{_num(ce.get('oi')):>9,.0f} {_num(ce.get('last_price')):>7.2f} | "
#             f"{_num(pe.get('last_price')):>7.2f} {_num(pe.get('oi')):>9,.0f}"
#         )
#     lines += ["</pre>", "S=Support  R=Resistance  ATM=At-the-money",
#               f"<i>Updated: {datetime.now().strftime('%H:%M:%S')}</i>"]
#     return "\n".join(lines)

# def _build_status_message(
#     spot: float, expiry: str,
#     support: Optional[float], resistance: Optional[float],
#     atm: float, pcr: Optional[float], max_pain: Optional[float],
#     call_top: List[Tuple[float, float]], put_top: List[Tuple[float, float]],
# ) -> str:
#     bias = "neutral"
#     if pcr is not None:
#         bias = "bearish pressure" if pcr < 0.9 else ("bullish pressure" if pcr > 1.1 else "neutral")
#     lines = [
#         f"<b>NIFTY Weekly Watch</b> | Expiry {expiry}",
#         f"Spot: {_fmt(spot)} | ATM: {_fmt(atm, 0)} | PCR: {_fmt(pcr)}",
#         f"Bias: {bias}",
#         f"Support: {_fmt(support, 0)} | Resistance: {_fmt(resistance, 0)} | Max Pain: {_fmt(max_pain, 0)}",
#     ]
#     if call_top:
#         lines.append("Top Call OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in call_top))
#     if put_top:
#         lines.append("Top Put OI:  " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in put_top))
#     lines.append(f"<i>Updated: {datetime.now().strftime('%H:%M:%S')}</i>")
#     return "\n".join(lines)


# # ─────────────────────────────────────────────────────────────────────────────
# # Agent
# # ─────────────────────────────────────────────────────────────────────────────

# # Regex: matches CE23700, PE 23700, ce24550, pe 24550 etc.
# STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)


# class MarketWatchAgent:
#     def __init__(self, cfg: Config):
#         self.cfg    = cfg
#         self.api    = DhanApiClient(cfg)
#         self.bot    = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
#         self.expiry: Optional[str] = None

#     # ── expiry ────────────────────────────────────────────────────────────────

#     def _pick_expiry(self) -> str:
#         expiries = self.api.expiry_list()
#         today    = datetime.now().date()
#         future   = sorted(d for x in expiries if (d := _parse_date(x)) and d >= today)
#         if not future:
#             raise RuntimeError("No valid future expiry from Dhan.")
#         return future[0].isoformat()

#     def _ensure_expiry(self) -> str:
#         if self.expiry is None:
#             self.expiry = self._pick_expiry()
#         return self.expiry

#     # ── core fetch ────────────────────────────────────────────────────────────

#     def _fetch_chain(self):
#         expiry   = self._ensure_expiry()
#         snapshot = self.api.option_chain(expiry)
#         data     = snapshot.get("data", {})
#         spot     = _num(data.get("last_price"))
#         oc: Dict[str, Any] = data.get("oc") or {}
#         if not oc:
#             raise RuntimeError("Empty option chain — market may be closed.")
#         return expiry, spot, oc

#     # ── handlers ──────────────────────────────────────────────────────────────

#     def _handle_strike(self, side: str, strike: float) -> None:
#         """Main handler: CE23700 / PE23700"""
#         expiry, spot, oc = self._fetch_chain()

#         all_strikes         = sorted(float(k) for k in oc.keys())
#         atm                 = _nearest_strike(all_strikes, spot)
#         support, resistance = _support_resistance(oc)
#         pcr_val             = _pcr(oc, spot, self.cfg.strikes_window)
#         max_pain_val        = _max_pain(oc)

#         row         = _get_row(oc, strike)
#         option_data = row.get(side.lower(), {})

#         if not row:
#             self.bot.send(
#                 f"⚠️ Strike <b>{strike:,.0f}</b> not found in the option chain.\n"
#                 f"Available strikes range: {all_strikes[0]:,.0f} – {all_strikes[-1]:,.0f}"
#             )
#             return

#         msg = _build_strike_message(
#             side=side,
#             strike=strike,
#             option_data=option_data,
#             spot=spot,
#             expiry=expiry,
#             support=support,
#             resistance=resistance,
#             atm=atm,
#             pcr=pcr_val,
#             max_pain=max_pain_val,
#         )
#         self.bot.send(msg)

#     def _handle_chain(self) -> None:
#         expiry, spot, oc    = self._fetch_chain()
#         all_strikes         = sorted(float(k) for k in oc.keys())
#         atm                 = _nearest_strike(all_strikes, spot)
#         rows                = _strikes_around_atm(oc, spot, self.cfg.strikes_window)
#         support, resistance = _support_resistance(oc)
#         pcr_val             = _pcr(oc, spot, self.cfg.strikes_window)
#         max_pain_val        = _max_pain(oc)
#         self.bot.send(_build_chain_message(rows, spot, expiry, support, resistance, atm, pcr_val, max_pain_val))

#     def _handle_status(self) -> None:
#         expiry, spot, oc    = self._fetch_chain()
#         all_strikes         = sorted(float(k) for k in oc.keys())
#         atm                 = _nearest_strike(all_strikes, spot)
#         support, resistance = _support_resistance(oc)
#         pcr_val             = _pcr(oc, spot, self.cfg.strikes_window)
#         max_pain_val        = _max_pain(oc)
#         call_top            = _top_oi(oc, "ce", 3)
#         put_top             = _top_oi(oc, "pe", 3)
#         self.bot.send(_build_status_message(spot, expiry, support, resistance, atm, pcr_val, max_pain_val, call_top, put_top))

#     # ── dispatcher ────────────────────────────────────────────────────────────

#     def _dispatch(self, raw: str) -> None:
#         text = raw.strip()
#         cmd  = text.split("@")[0].lower()

#         # CE23700 / PE23700 pattern
#         m = STRIKE_RE.match(text.replace(" ", ""))
#         if m:
#             side   = m.group(1).upper()
#             strike = float(m.group(2))
#             print(f"  Strike query: {side} {strike}")
#             self._handle_strike(side, strike)
#             return

#         # Named commands
#         if cmd in ("/chain", "chain"):
#             self._handle_chain()
#         elif cmd in ("/status", "status"):
#             self._handle_status()
#         elif cmd in ("/expiry", "expiry"):
#             expiry = self._ensure_expiry()
#             self.bot.send(f"Current weekly expiry: <b>{expiry}</b>")
#         elif cmd in ("/help", "help", "/start", "start"):
#             self.bot.send(HELP_TEXT)
#         else:
#             self.bot.send(f"Unknown command: <code>{text}</code>\n\n{HELP_TEXT}")

#     # ── main loop ─────────────────────────────────────────────────────────────

#     def run(self) -> None:
#         print(f"Market Watch Agent started | {self.cfg.underlying_name_hint}")
#         self.bot.send(f"<b>NIFTY Market Watch Bot is online!</b>\n\n{HELP_TEXT}")

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

#                 time.sleep(self.cfg.tg_poll_interval)

#             except KeyboardInterrupt:
#                 self.bot.send("NIFTY Market Watch Bot stopped.")
#                 print("Stopped.")
#                 return
#             except requests.HTTPError as e:
#                 print(f"HTTP error: {e}")
#                 time.sleep(5)
#             except Exception as e:
#                 print(f"Error: {e}")
#                 time.sleep(5)


# # ─────────────────────────────────────────────────────────────────────────────

# def main() -> None:
#     cfg = Config.from_env()
#     MarketWatchAgent(cfg).run()

# if __name__ == "__main__":
#     main()

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DHAN_BASE     = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    underlying_security_id: int = 13
    underlying_segment: str = "IDX_I"
    underlying_name_hint: str = "NIFTY 50"
    strikes_window: int = 5
    http_timeout: int = 15
    tg_poll_interval: float = 1.5

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

        def _int(n: str, d: int) -> int:
            try: return int(os.getenv(n, str(d)).strip())
            except ValueError: return d

        def _float(n: str, d: float) -> float:
            try: return float(os.getenv(n, str(d)).strip())
            except ValueError: return d

        return Config(
            dhan_client_id=required["DHAN_CLIENT_ID"],
            dhan_access_token=required["DHAN_ACCESS_TOKEN"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            underlying_security_id=_int("DHAN_UNDERLYING_SECURITY_ID", 13),
            underlying_segment=os.getenv("DHAN_UNDERLYING_SEGMENT", "IDX_I").strip(),
            underlying_name_hint=os.getenv("UNDERLYING_NAME_HINT", "NIFTY 50").strip(),
            strikes_window=_int("STRIKES_WINDOW", 5),
            http_timeout=_int("HTTP_TIMEOUT", 15),
            tg_poll_interval=_float("TG_POLL_INTERVAL", 1.5),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Candle dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


# ─────────────────────────────────────────────────────────────────────────────
# Dhan API
# ─────────────────────────────────────────────────────────────────────────────

class DhanApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "access-token": cfg.dhan_access_token,
            "client-id":    cfg.dhan_client_id,
        })

    def expiry_list(self) -> List[str]:
        r = self.session.post(
            f"{DHAN_BASE}/optionchain/expirylist",
            data=json.dumps({
                "UnderlyingScrip": self.cfg.underlying_security_id,
                "UnderlyingSeg":   self.cfg.underlying_segment,
            }),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        return [str(x) for x in r.json().get("data", [])]

    def option_chain(self, expiry: str) -> Dict[str, Any]:
        r = self.session.post(
            f"{DHAN_BASE}/optionchain",
            data=json.dumps({
                "UnderlyingScrip": self.cfg.underlying_security_id,
                "UnderlyingSeg":   self.cfg.underlying_segment,
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
        interval: int = 5,
    ) -> List[Candle]:
        """Fetch today's intraday 5-min candles for a security."""
        now      = datetime.now()
        today    = now.strftime("%Y-%m-%d")
        # Dhan needs fromDate/toDate as date strings or datetime strings
        payload  = {
            "securityId":      str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument":      instrument,
            "interval":        str(interval),
            "oi":              False,
            "fromDate":        today,
            "toDate":          today,
        }
        r = self.session.post(
            f"{DHAN_BASE}/charts/intraday",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        raw = r.json()

        opens  = raw.get("open",      [])
        highs  = raw.get("high",      [])
        lows   = raw.get("low",       [])
        closes = raw.get("close",     [])
        vols   = raw.get("volume",    [])
        tss    = raw.get("timestamp", [])

        candles: List[Candle] = []
        for i in range(min(len(opens), len(highs), len(lows), len(closes), len(vols), len(tss))):
            try:
                candles.append(Candle(
                    ts=datetime.fromtimestamp(float(tss[i]), tz=timezone.utc).astimezone(),
                    open=float(opens[i]),
                    high=float(highs[i]),
                    low=float(lows[i]),
                    close=float(closes[i]),
                    volume=float(vols[i]),
                ))
            except Exception:
                continue
        return candles

    def intraday_option_candles(
        self,
        security_id: int,
        interval: int = 5,
    ) -> List[Candle]:
        """Fetch intraday candles for an option contract (NSE FO segment)."""
        return self.intraday_candles(
            security_id=security_id,
            exchange_segment="NSE_FNO",
            instrument="OPTIDX",
            interval=interval,
        )


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
    return min(strikes, key=lambda s: abs(s - spot))

def _parse_date(s: str):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:   return datetime.strptime(s, fmt).date()
        except Exception: continue
    return None

def _get_row(oc: Dict[str, Any], strike: float) -> Dict[str, Any]:
    return (
        oc.get(f"{strike:.6f}")
        or oc.get(f"{strike:.2f}")
        or oc.get(f"{strike:.0f}")
        or oc.get(str(int(strike)))
        or {}
    )

def _support_resistance_oi(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    best_pe, support    = -1.0, None
    best_ce, resistance = -1.0, None
    for k, row in oc.items():
        try:   strike = float(k)
        except Exception: continue
        pe_oi = _num(row.get("pe", {}).get("oi"))
        ce_oi = _num(row.get("ce", {}).get("oi"))
        if pe_oi > best_pe: best_pe, support    = pe_oi, strike
        if ce_oi > best_ce: best_ce, resistance = ce_oi, strike
    return support, resistance

def _pcr(oc: Dict[str, Any], center: float, window: int) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    band    = sorted(strikes, key=lambda s: abs(s - center))[: max(2, window * 2)]
    call_oi = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")) for s in band)
    put_oi  = sum(_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")) for s in band)
    return (put_oi / call_oi) if call_oi > 0 else None

def _max_pain(oc: Dict[str, Any]) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes: return None
    oi_map = {
        s: (
            _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")),
            _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")),
        )
        for s in strikes
    }
    best, best_pain = None, None
    for settle in strikes:
        pain = sum(max(0.0, settle - s) * ce + max(0.0, s - settle) * pe
                   for s, (ce, pe) in oi_map.items())
        if best_pain is None or pain < best_pain:
            best_pain, best = pain, settle
    return best

def _top_oi(oc: Dict[str, Any], side: str, n: int = 3) -> List[Tuple[float, float]]:
    items = []
    for k, v in oc.items():
        try:   items.append((float(k), _num(v.get(side, {}).get("oi"))))
        except Exception: continue
    return sorted(items, key=lambda x: x[1], reverse=True)[:n]

def _strikes_around_atm(
    oc: Dict[str, Any], spot: float, window: int
) -> List[Tuple[float, Dict[str, Any]]]:
    all_strikes = sorted(float(k) for k in oc.keys())
    if not all_strikes: return []
    atm    = _nearest_strike(all_strikes, spot)
    nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: window * 2 + 1]
    return [(s, oc.get(f"{s:.6f}") or oc.get(str(s)) or {}) for s in sorted(nearby)]


# ─────────────────────────────────────────────────────────────────────────────
# Intraday analytics
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class IntradayLevels:
    day_open:   float
    day_high:   float
    day_low:    float
    last_close: float          # last candle close = current price proxy
    vwap:       float
    trend:      str            # "uptrend" | "downtrend" | "sideways"
    candle_count: int
    last_candle: Optional[Candle]

    # derived support / resistance from candles
    intraday_support:    float
    intraday_resistance: float

    # key price levels
    prev_candle_high: float
    prev_candle_low:  float


def _calc_vwap(candles: List[Candle]) -> float:
    """VWAP = sum(typical_price * volume) / sum(volume)"""
    num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in candles)
    den = sum(c.volume for c in candles)
    return (num / den) if den > 0 else 0.0


def _analyse_intraday(candles: List[Candle]) -> Optional[IntradayLevels]:
    if not candles:
        return None

    day_open   = candles[0].open
    day_high   = max(c.high   for c in candles)
    day_low    = min(c.low    for c in candles)
    last_close = candles[-1].close
    vwap       = _calc_vwap(candles)

    # Simple trend: compare last close vs open and vs VWAP
    if last_close > vwap and last_close > day_open:
        trend = "uptrend"
    elif last_close < vwap and last_close < day_open:
        trend = "downtrend"
    else:
        trend = "sideways"

    # Intraday support  = lowest low of last 6 candles (≈30 min)
    # Intraday resistance = highest high of last 6 candles
    recent = candles[-6:] if len(candles) >= 6 else candles
    intraday_support    = min(c.low  for c in recent)
    intraday_resistance = max(c.high for c in recent)

    prev_candle = candles[-2] if len(candles) >= 2 else candles[-1]

    return IntradayLevels(
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
    )


# ─────────────────────────────────────────────────────────────────────────────
# Intraday trade levels
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class TradeLevels:
    buy:        float
    target1:    float   # conservative  (+1 R)
    target2:    float   # aggressive    (+2 R)
    stop_loss:  float
    risk:       float
    reward1:    float
    reward2:    float
    rr1:        float
    rr2:        float
    note:       str


def _intraday_trade_levels(
    side: str,               # "CE" or "PE"
    ltp: float,
    il: IntradayLevels,
    oi_support: Optional[float],
    oi_resistance: Optional[float],
    spot: float,
) -> TradeLevels:
    """
    Intraday trade level logic
    ──────────────────────────
    Entry (buy price):
        CE → buy near VWAP if price > VWAP (bullish), else near intraday support
        PE → buy near VWAP if price < VWAP (bearish), else near intraday resistance

    Stop Loss:
        CE → below the lower of: prev candle low OR intraday support (in option price terms → 20% of LTP)
        PE → above the higher of: prev candle high OR intraday resistance

        We translate the underlying SL buffer into option price % move:
            SL = LTP * (1 - sl_pct)  where sl_pct is 15-25% depending on trend

    Target:
        T1 = entry + 1× risk  (1:1 R:R)
        T2 = entry + 2× risk  (1:2 R:R)

    Context adjustments:
        - Trend aligned  → sl_pct = 15% (tighter SL, higher confidence)
        - Counter-trend  → sl_pct = 25% (wider SL, lower confidence)
        - Sideways       → sl_pct = 20%
    """
    # Trend alignment check
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
        note   = f"⚠️ Trade is counter-trend ({il.trend}). Wider SL used. Trade with caution."
    else:
        sl_pct = 0.20
        note   = f"➡️ Market is sideways. Standard SL used."

    # Entry = LTP (market order)
    buy       = round(ltp, 2)
    stop_loss = round(max(ltp * (1 - sl_pct), 1.0), 2)   # floor ₹1
    risk      = round(buy - stop_loss, 2)

    target1 = round(buy + risk * 1.0, 2)   # 1:1
    target2 = round(buy + risk * 2.0, 2)   # 1:2

    reward1 = round(target1 - buy, 2)
    reward2 = round(target2 - buy, 2)
    rr1     = round(reward1 / risk, 1) if risk > 0 else 0.0
    rr2     = round(reward2 / risk, 1) if risk > 0 else 0.0

    return TradeLevels(
        buy=buy, target1=target1, target2=target2,
        stop_loss=stop_loss, risk=risk,
        reward1=reward1, reward2=reward2,
        rr1=rr1, rr2=rr2, note=note,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Message builder — strike analysis (intraday)
# ─────────────────────────────────────────────────────────────────────────────

def _build_strike_message(
    side: str,
    strike: float,
    option_data: Dict[str, Any],
    spot: float,
    expiry: str,
    oi_support: Optional[float],
    oi_resistance: Optional[float],
    atm: float,
    pcr: Optional[float],
    max_pain: Optional[float],
    il: Optional[IntradayLevels],
) -> str:
    ltp        = _num(option_data.get("last_price"))
    prev_close = _num(option_data.get("previous_close_price"))
    oi         = _num(option_data.get("oi"))
    prev_oi    = _num(option_data.get("previous_oi"))
    volume     = _num(option_data.get("volume"))
    iv         = _num(option_data.get("implied_volatility"))
    bid        = _num(option_data.get("bid_price"))
    ask        = _num(option_data.get("ask_price"))

    oi_change   = oi - prev_oi
    price_chg   = ltp - prev_close
    price_chg_p = (price_chg / prev_close * 100) if prev_close > 0 else 0.0

    bias = "neutral"
    if pcr is not None:
        bias = "bearish" if pcr < 0.9 else ("bullish" if pcr > 1.1 else "neutral")

    atm_tag = (
        " (ATM)" if strike == atm else
        " (ITM)" if (side == "CE" and strike < spot) or (side == "PE" and strike > spot) else
        " (OTM)"
    )
    emoji = "📈" if side == "CE" else "📉"

    lines = [
        f"{emoji} <b>NIFTY {side} {strike:,.0f}{atm_tag} — Intraday</b>",
        f"Expiry: {expiry}  |  Spot: {_fmt(spot)}",
        "",
        "━━ Option Data ━━━━━━━━━━━━━━━━━━━━",
        f"LTP        : ₹{_fmt(ltp)}",
        f"Prev Close : ₹{_fmt(prev_close)}  ({price_chg:+.2f} / {price_chg_p:+.1f}%)",
        f"Bid / Ask  : ₹{_fmt(bid)} / ₹{_fmt(ask)}",
        f"IV         : {_fmt(iv, 1)}%",
        f"OI         : {oi:,.0f}  (Chg: {oi_change:+,.0f})",
        f"Volume     : {volume:,.0f}",
    ]

    # ── Intraday section ──────────────────────────────────────────────────────
    if il:
        trend_arrow = "↑" if il.trend == "uptrend" else ("↓" if il.trend == "downtrend" else "→")
        lines += [
            "",
            "━━ Intraday Levels (5-min candles) ━",
            f"Day Open   : ₹{_fmt(il.day_open)}",
            f"Day High   : ₹{_fmt(il.day_high)}   ← Intraday Resistance",
            f"Day Low    : ₹{_fmt(il.day_low)}   ← Intraday Support",
            f"VWAP       : ₹{_fmt(il.vwap)}",
            f"Trend      : {trend_arrow} {il.trend.capitalize()}  ({il.candle_count} candles so far)",
            f"Last Candle: O={_fmt(il.last_candle.open)} H={_fmt(il.last_candle.high)} "
            f"L={_fmt(il.last_candle.low)} C={_fmt(il.last_candle.close)}",
            f"Prev Candle: H={_fmt(il.prev_candle_high)}  L={_fmt(il.prev_candle_low)}",
            f"30-min S/R : Support={_fmt(il.intraday_support)}  Resistance={_fmt(il.intraday_resistance)}",
        ]
    else:
        lines += [
            "",
            "⚠️ Intraday candle data unavailable (market may not have opened yet).",
        ]

    lines += [
        "",
        "━━ Market Context ━━━━━━━━━━━━━━━━━━",
        f"PCR        : {_fmt(pcr)}  |  Bias: {bias}",
        f"ATM Strike : {_fmt(atm, 0)}",
        f"Max Pain   : {_fmt(max_pain, 0)}",
        f"OI Support : {_fmt(oi_support, 0)}   (highest PE OI)",
        f"OI Resist  : {_fmt(oi_resistance, 0)}  (highest CE OI)",
    ]

    # ── Trade levels ──────────────────────────────────────────────────────────
    if ltp <= 0:
        lines += ["", "⚠️ LTP is 0 — option may be illiquid or market closed."]
    elif il is None:
        lines += ["", "⚠️ Trade levels need intraday data — not available yet."]
    else:
        tl = _intraday_trade_levels(side, ltp, il, oi_support, oi_resistance, spot)
        buy_emoji = "🟢" if side == "CE" else "🔴"
        lines += [
            "",
            "━━ Intraday Trade Levels ━━━━━━━━━━━",
            f"{buy_emoji} Buy (Entry) : ₹{_fmt(tl.buy)}",
            f"🎯 Target 1  : ₹{_fmt(tl.target1)}   (1:1 R:R)",
            f"🎯 Target 2  : ₹{_fmt(tl.target2)}   (1:2 R:R)",
            f"🛑 Stop Loss : ₹{_fmt(tl.stop_loss)}",
            "",
            f"Risk         : ₹{_fmt(tl.risk)} per lot",
            f"Reward T1    : ₹{_fmt(tl.reward1)}  |  R:R = 1:{tl.rr1}",
            f"Reward T2    : ₹{_fmt(tl.reward2)}  |  R:R = 1:{tl.rr2}",
            "",
            tl.note,
            "",
            "<i>⚠️ Calculated levels only. Not financial advice.</i>",
            "<i>Always verify before placing any trade.</i>",
        ]

    lines.append(f"\n<i>Updated: {datetime.now().strftime('%H:%M:%S')}</i>")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Chain + status messages
# ─────────────────────────────────────────────────────────────────────────────

def _build_chain_message(
    rows, spot, expiry, support, resistance, atm, pcr, max_pain
) -> str:
    bias = "neutral"
    if pcr is not None:
        bias = "bearish" if pcr < 0.9 else ("bullish" if pcr > 1.1 else "neutral")
    lines = [
        "<b>NIFTY 50 — Option Chain</b>",
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
              f"<i>Updated: {datetime.now().strftime('%H:%M:%S')}</i>"]
    return "\n".join(lines)

def _build_status_message(spot, expiry, support, resistance, atm, pcr, max_pain, call_top, put_top) -> str:
    bias = "neutral"
    if pcr is not None:
        bias = "bearish pressure" if pcr < 0.9 else ("bullish pressure" if pcr > 1.1 else "neutral")
    lines = [
        f"<b>NIFTY Weekly Watch</b> | Expiry {expiry}",
        f"Spot: {_fmt(spot)} | ATM: {_fmt(atm, 0)} | PCR: {_fmt(pcr)}",
        f"Bias: {bias}",
        f"Support: {_fmt(support, 0)} | Resistance: {_fmt(resistance, 0)} | Max Pain: {_fmt(max_pain, 0)}",
    ]
    if call_top:
        lines.append("Top Call OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in call_top))
    if put_top:
        lines.append("Top Put OI:  " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in put_top))
    lines.append(f"<i>Updated: {datetime.now().strftime('%H:%M:%S')}</i>")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Help
# ─────────────────────────────────────────────────────────────────────────────

HELP_TEXT = """<b>NIFTY Intraday Bot Commands</b>

<b>Strike Analysis (intraday)</b>
  CE23700  — Call 23700: VWAP, high/low, trade levels
  PE23700  — Put  23700: VWAP, high/low, trade levels

<b>Market Overview</b>
  /chain   — ±5 strike option chain table
  /status  — Spot, PCR, bias, support, resistance
  /expiry  — Current weekly expiry
  /help    — This help message

Type CE or PE followed by the strike (e.g. CE24500)."""

STRIKE_RE = re.compile(r"^(CE|PE)\s*(\d{4,6})$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────

class MarketWatchAgent:
    def __init__(self, cfg: Config):
        self.cfg    = cfg
        self.api    = DhanApiClient(cfg)
        self.bot    = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        self.expiry: Optional[str] = None

    def _pick_expiry(self) -> str:
        expiries = self.api.expiry_list()
        today    = datetime.now().date()
        future   = sorted(d for x in expiries if (d := _parse_date(x)) and d >= today)
        if not future:
            raise RuntimeError("No valid future expiry from Dhan.")
        return future[0].isoformat()

    def _ensure_expiry(self) -> str:
        if self.expiry is None:
            self.expiry = self._pick_expiry()
        return self.expiry

    def _fetch_chain(self):
        expiry   = self._ensure_expiry()
        snapshot = self.api.option_chain(expiry)
        data     = snapshot.get("data", {})
        spot     = _num(data.get("last_price"))
        oc: Dict[str, Any] = data.get("oc") or {}
        if not oc:
            raise RuntimeError("Empty option chain — market may be closed.")
        return expiry, spot, oc

    def _fetch_intraday(self) -> Optional[IntradayLevels]:
        """Fetch 5-min candles for NIFTY index and compute intraday levels."""
        try:
            candles = self.api.intraday_candles(
                security_id=self.cfg.underlying_security_id,
                exchange_segment=self.cfg.underlying_segment,
                instrument="INDEX",
                interval=5,
            )
            return _analyse_intraday(candles)
        except Exception as e:
            print(f"Intraday fetch failed: {e}")
            return None

    # ── handlers ──────────────────────────────────────────────────────────────

    def _handle_strike(self, side: str, strike: float) -> None:
        expiry, spot, oc = self._fetch_chain()

        all_strikes         = sorted(float(k) for k in oc.keys())
        atm                 = _nearest_strike(all_strikes, spot)
        oi_support, oi_res  = _support_resistance_oi(oc)
        pcr_val             = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val        = _max_pain(oc)

        row         = _get_row(oc, strike)
        option_data = row.get(side.lower(), {})

        if not row:
            self.bot.send(
                f"⚠️ Strike <b>{strike:,.0f}</b> not found in the option chain.\n"
                f"Range: {all_strikes[0]:,.0f} – {all_strikes[-1]:,.0f}"
            )
            return

        # Fetch intraday levels for NIFTY underlying
        self.bot.send("⏳ Fetching intraday candles…")
        il = self._fetch_intraday()

        msg = _build_strike_message(
            side=side, strike=strike, option_data=option_data,
            spot=spot, expiry=expiry,
            oi_support=oi_support, oi_resistance=oi_res,
            atm=atm, pcr=pcr_val, max_pain=max_pain_val,
            il=il,
        )
        self.bot.send(msg)

    def _handle_chain(self) -> None:
        expiry, spot, oc    = self._fetch_chain()
        all_strikes         = sorted(float(k) for k in oc.keys())
        atm                 = _nearest_strike(all_strikes, spot)
        rows                = _strikes_around_atm(oc, spot, self.cfg.strikes_window)
        support, resistance = _support_resistance_oi(oc)
        pcr_val             = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val        = _max_pain(oc)
        self.bot.send(_build_chain_message(rows, spot, expiry, support, resistance, atm, pcr_val, max_pain_val))

    def _handle_status(self) -> None:
        expiry, spot, oc    = self._fetch_chain()
        all_strikes         = sorted(float(k) for k in oc.keys())
        atm                 = _nearest_strike(all_strikes, spot)
        support, resistance = _support_resistance_oi(oc)
        pcr_val             = _pcr(oc, spot, self.cfg.strikes_window)
        max_pain_val        = _max_pain(oc)
        call_top            = _top_oi(oc, "ce", 3)
        put_top             = _top_oi(oc, "pe", 3)
        self.bot.send(_build_status_message(spot, expiry, support, resistance, atm, pcr_val, max_pain_val, call_top, put_top))

    # ── dispatcher ────────────────────────────────────────────────────────────

    def _dispatch(self, raw: str) -> None:
        text = raw.strip()
        cmd  = text.split("@")[0].lower()

        m = STRIKE_RE.match(text.replace(" ", ""))
        if m:
            side   = m.group(1).upper()
            strike = float(m.group(2))
            print(f"  Strike query: {side} {strike}")
            self._handle_strike(side, strike)
            return

        if cmd in ("/chain", "chain"):       self._handle_chain()
        elif cmd in ("/status", "status"):   self._handle_status()
        elif cmd in ("/expiry", "expiry"):
            self.bot.send(f"Current weekly expiry: <b>{self._ensure_expiry()}</b>")
        elif cmd in ("/help", "help", "/start", "start"):
            self.bot.send(HELP_TEXT)
        else:
            self.bot.send(f"Unknown command: <code>{text}</code>\n\n{HELP_TEXT}")

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        print(f"Market Watch Agent started | {self.cfg.underlying_name_hint}")
        self.bot.send(f"<b>NIFTY Intraday Bot is online!</b>\n\n{HELP_TEXT}")

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
                time.sleep(self.cfg.tg_poll_interval)

            except KeyboardInterrupt:
                self.bot.send("NIFTY Intraday Bot stopped.")
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