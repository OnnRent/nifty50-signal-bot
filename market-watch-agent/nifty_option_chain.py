from __future__ import annotations

import dataclasses
import json
import os
import time
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DHAN_BASE = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"


# ── Config ────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    underlying_security_id: int = 13
    underlying_segment: str = "IDX_I"
    strikes_window: int = 5
    poll_seconds: float = 3.5
    http_timeout: int = 15

    @staticmethod
    def from_env() -> "Config":
        missing = [k for k in (
            "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
        ) if not os.getenv(k, "").strip()]
        if missing:
            raise SystemExit("Missing env vars: " + ", ".join(missing))
        return Config(
            dhan_client_id=os.getenv("DHAN_CLIENT_ID", "").strip(),
            dhan_access_token=os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            underlying_security_id=int(os.getenv("DHAN_UNDERLYING_SECURITY_ID", "13")),
            underlying_segment=os.getenv("DHAN_UNDERLYING_SEGMENT", "IDX_I").strip(),
            strikes_window=int(os.getenv("STRIKES_WINDOW", "5")),
            poll_seconds=float(os.getenv("POLL_SECONDS", "3.5")),
            http_timeout=int(os.getenv("HTTP_TIMEOUT", "15")),
        )


# ── Dhan API ──────────────────────────────────────────────────────────────────

class DhanApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "access-token": cfg.dhan_access_token,
            "client-id": cfg.dhan_client_id,
        })

    def expiry_list(self) -> List[str]:
        payload = {
            "UnderlyingScrip": self.cfg.underlying_security_id,
            "UnderlyingSeg": self.cfg.underlying_segment,
        }
        r = self.session.post(
            f"{DHAN_BASE}/optionchain/expirylist",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        return [str(x) for x in r.json().get("data", [])]

    def option_chain(self, expiry: str) -> Dict[str, Any]:
        payload = {
            "UnderlyingScrip": self.cfg.underlying_security_id,
            "UnderlyingSeg": self.cfg.underlying_segment,
            "Expiry": expiry,
        }
        r = self.session.post(
            f"{DHAN_BASE}/optionchain",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        return r.json()


# ── Telegram ──────────────────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.session = requests.Session()

    def send(self, text: str) -> None:
        url = f"{TELEGRAM_BASE}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = self.session.post(url, data=payload, timeout=self.timeout)
        r.raise_for_status()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def _nearest_strike(strikes: List[float], spot: float) -> float:
    return min(strikes, key=lambda s: abs(s - spot))


def _parse_date(s: str):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _strikes_around_atm(
    oc: Dict[str, Any], spot: float, window: int
) -> List[Tuple[float, Dict[str, Any]]]:
    all_strikes = sorted(float(k) for k in oc.keys())
    if not all_strikes:
        return []
    atm = _nearest_strike(all_strikes, spot)
    nearby = sorted(all_strikes, key=lambda s: abs(s - atm))[: window * 2 + 1]
    return [(s, oc.get(f"{s:.6f}") or oc.get(str(s)) or {}) for s in sorted(nearby)]


def _support_resistance(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    best_pe, support = -1.0, None
    best_ce, resistance = -1.0, None
    for k, row in oc.items():
        try:
            strike = float(k)
        except Exception:
            continue
        pe_oi = _num(row.get("pe", {}).get("oi"))
        ce_oi = _num(row.get("ce", {}).get("oi"))
        if pe_oi > best_pe:
            best_pe, support = pe_oi, strike
        if ce_oi > best_ce:
            best_ce, resistance = ce_oi, strike
    return support, resistance


def _pcr(oc: Dict[str, Any]) -> Optional[float]:
    call_oi = sum(_num(v.get("ce", {}).get("oi")) for v in oc.values())
    put_oi  = sum(_num(v.get("pe", {}).get("oi")) for v in oc.values())
    return (put_oi / call_oi) if call_oi > 0 else None


def _max_pain(oc: Dict[str, Any]) -> Optional[float]:
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        return None
    oi_map = {
        s: (_num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("ce", {}).get("oi")),
            _num((oc.get(f"{s:.6f}") or oc.get(str(s)) or {}).get("pe", {}).get("oi")))
        for s in strikes
    }
    best, best_pain = None, None
    for settlement in strikes:
        pain = sum(
            max(0.0, settlement - s) * ce + max(0.0, s - settlement) * pe
            for s, (ce, pe) in oi_map.items()
        )
        if best_pain is None or pain < best_pain:
            best_pain, best = pain, settlement
    return best


def _oi_bar(value: float, max_val: float, width: int = 10) -> str:
    if max_val <= 0:
        return "░" * width
    filled = int(round(value / max_val * width))
    return "█" * filled + "░" * (width - filled)


def _fmt(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.{decimals}f}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── Terminal display ──────────────────────────────────────────────────────────

def print_chain(
    rows: List[Tuple[float, Dict[str, Any]]],
    spot: float,
    expiry: str,
    support: Optional[float],
    resistance: Optional[float],
    atm: float,
    pcr: Optional[float],
    max_pain: Optional[float],
) -> None:
    max_ce_oi = max((_num(r.get("ce", {}).get("oi")) for _, r in rows), default=1.0) or 1.0
    max_pe_oi = max((_num(r.get("pe", {}).get("oi")) for _, r in rows), default=1.0) or 1.0

    W = 120
    print()
    print("=" * W)
    print(f"  NIFTY 50 Option Chain  |  Expiry: {expiry}  |  Spot: {_fmt(spot)}  |  "
          f"ATM: {atm:,.0f}  |  PCR: {_fmt(pcr)}  |  Max Pain: {_fmt(max_pain, 0)}")
    print(f"  Support (highest PE OI): {_fmt(support, 0)}   |   Resistance (highest CE OI): {_fmt(resistance, 0)}")
    print("=" * W)

    CE_HDR = f"{'OI Bar':>10}  {'OI':>10}  {'Chg OI':>9}  {'Vol':>8}  {'IV%':>5}  {'LTP':>7}"
    PE_HDR = f"{'LTP':>7}  {'IV%':>5}  {'Vol':>8}  {'Chg OI':>9}  {'OI':>10}  {'OI Bar':>10}"
    print(f"  {'── CALLS (CE) ──':^55}  {'STRIKE':^16}  {'── PUTS (PE) ──':^55}")
    print(f"  {CE_HDR}  {'STRIKE':^16}  {PE_HDR}")
    print("-" * W)

    for strike, row in rows:
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}

        ce_oi  = _num(ce.get("oi"));         ce_prv = _num(ce.get("previous_oi"))
        ce_vol = _num(ce.get("volume"));      ce_iv  = _num(ce.get("implied_volatility"))
        ce_ltp = _num(ce.get("last_price"))

        pe_oi  = _num(pe.get("oi"));         pe_prv = _num(pe.get("previous_oi"))
        pe_vol = _num(pe.get("volume"));      pe_iv  = _num(pe.get("implied_volatility"))
        pe_ltp = _num(pe.get("last_price"))

        tag = ""
        if strike == atm:        tag += "[ATM]"
        if strike == support:    tag += "[S]"
        if strike == resistance: tag += "[R]"

        prefix = "▶ " if strike == atm else ("S " if strike == support else ("R " if strike == resistance else "  "))

        ce_line = (f"{_oi_bar(ce_oi, max_ce_oi):>10}  {ce_oi:>10,.0f}  "
                   f"{ce_oi - ce_prv:>+9,.0f}  {ce_vol:>8,.0f}  {ce_iv:>4.1f}%  {ce_ltp:>7.2f}")
        pe_line = (f"{pe_ltp:>7.2f}  {pe_iv:>4.1f}%  {pe_vol:>8,.0f}  "
                   f"{pe_oi - pe_prv:>+9,.0f}  {pe_oi:>10,.0f}  {_oi_bar(pe_oi, max_pe_oi):>10}")

        print(f"{prefix}{ce_line}  {strike:>7,.0f} {tag:<8}  {pe_line}")

    print("-" * W)
    print("  [ATM] At-the-money  |  [S] Support — highest Put OI  |  [R] Resistance — highest Call OI")
    print("=" * W)


# ── Telegram message ──────────────────────────────────────────────────────────

def build_telegram_message(
    rows: List[Tuple[float, Dict[str, Any]]],
    spot: float,
    expiry: str,
    support: Optional[float],
    resistance: Optional[float],
    atm: float,
    pcr: Optional[float],
    max_pain: Optional[float],
) -> str:
    bias = "neutral"
    if pcr is not None:
        if pcr < 0.9:   bias = "bearish pressure"
        elif pcr > 1.1: bias = "bullish pressure"

    lines = [
        "<b>NIFTY 50 — Option Chain Alert</b>",
        f"Expiry : {expiry}",
        f"Spot   : {_fmt(spot)}  |  ATM: {_fmt(atm, 0)}",
        f"PCR    : {_fmt(pcr)}  |  Bias: {bias}",
        f"Max Pain      : {_fmt(max_pain, 0)}",
        f"Support  [S]  : {_fmt(support, 0)}  (highest PE OI)",
        f"Resistance [R]: {_fmt(resistance, 0)}  (highest CE OI)",
        "",
        "<b>±5 strikes around ATM</b>",
        "<pre>",
        f"{'Strike':<8} {'Tag':<7} {'CE OI':>9} {'CE LTP':>7} | {'PE LTP':>7} {'PE OI':>9}",
        "-" * 54,
    ]

    for strike, row in rows:
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        ce_oi  = _num(ce.get("oi"));  ce_ltp = _num(ce.get("last_price"))
        pe_oi  = _num(pe.get("oi"));  pe_ltp = _num(pe.get("last_price"))

        tag = ""
        if strike == atm:        tag = "ATM"
        if strike == support:    tag += " S"
        if strike == resistance: tag += " R"
        tag = tag.strip()

        lines.append(
            f"{strike:<8,.0f} {tag:<7} {ce_oi:>9,.0f} {ce_ltp:>7.2f} | {pe_ltp:>7.2f} {pe_oi:>9,.0f}"
        )

    lines += [
        "</pre>",
        "S = Support (high PE OI)  |  R = Resistance (high CE OI)",
    ]
    return "\n".join(lines)


# ── Agent loop ────────────────────────────────────────────────────────────────

class OptionChainAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = DhanApiClient(cfg)
        self.telegram = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        self.expiry: Optional[str] = None
        self.last_hash: Optional[str] = None

    def _pick_expiry(self) -> str:
        expiries = self.api.expiry_list()
        today = datetime.now().date()
        future = sorted(d for x in expiries if (d := _parse_date(x)) and d >= today)
        if not future:
            raise RuntimeError("No valid future expiry returned.")
        return future[0].isoformat()

    def run(self) -> None:
        self.telegram.send(
            "<b>NIFTY Option Chain Monitor started</b>\n"
            "Watching ±5 strikes | Support &amp; Resistance alerts active."
        )
        print("NIFTY Option Chain Monitor started. Press Ctrl+C to stop.\n")

        while True:
            try:
                # Pick expiry once; reset at midnight automatically
                if self.expiry is None:
                    self.expiry = self._pick_expiry()
                    self.telegram.send(f"Weekly expiry selected: <b>{self.expiry}</b>")
                    print(f"Expiry: {self.expiry}")

                snapshot = self.api.option_chain(self.expiry)
                data = snapshot.get("data", {})
                spot = _num(data.get("last_price"))
                oc: Dict[str, Any] = data.get("oc") or {}

                if not oc:
                    print("Empty option chain — retrying…")
                    time.sleep(self.cfg.poll_seconds)
                    continue

                all_strikes     = sorted(float(k) for k in oc.keys())
                atm             = _nearest_strike(all_strikes, spot)
                rows            = _strikes_around_atm(oc, spot, self.cfg.strikes_window)
                support, resistance = _support_resistance(oc)
                pcr             = _pcr(oc)
                max_pain        = _max_pain(oc)

                # Always print to terminal
                print_chain(rows, spot, self.expiry, support, resistance, atm, pcr, max_pain)

                # Send Telegram only when data changed
                tg_msg   = build_telegram_message(rows, spot, self.expiry, support, resistance, atm, pcr, max_pain)
                msg_hash = _digest(tg_msg)

                if msg_hash != self.last_hash:
                    self.telegram.send(tg_msg)
                    self.last_hash = msg_hash
                    print(">>> Telegram alert sent.\n")
                else:
                    print(f"    No change | Spot={_fmt(spot)} | PCR={_fmt(pcr)} "
                          f"| S={_fmt(support, 0)} | R={_fmt(resistance, 0)}\n")

                time.sleep(self.cfg.poll_seconds)

            except KeyboardInterrupt:
                self.telegram.send("NIFTY Option Chain Monitor stopped.")
                print("\nStopped.")
                return

            except requests.HTTPError as e:
                print(f"HTTP error: {e}")
                time.sleep(max(5, int(self.cfg.poll_seconds)))

            except Exception as e:
                print(f"Error: {e}")
                time.sleep(max(5, int(self.cfg.poll_seconds)))


# ── Entry ─────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = Config.from_env()
    agent = OptionChainAgent(cfg)
    agent.run()


if __name__ == "__main__":
    main()