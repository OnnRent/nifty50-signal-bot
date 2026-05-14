from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import os
import statistics
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
import requests


DHAN_BASE = "https://api.dhan.co/v2"
TELEGRAM_BASE = "https://api.telegram.org"
INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"


@dataclasses.dataclass
class Config:
    dhan_client_id: str
    dhan_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    underlying_security_id: int = 13
    underlying_segment: str = "IDX_I"
    underlying_name_hint: str = "NIFTY 50"
    poll_seconds: float = 3.5
    intraday_refresh_seconds: int = 60
    strikes_window: int = 10
    http_timeout: int = 15

    @staticmethod
    def from_env() -> "Config":
        missing = []
        required = {
            "DHAN_CLIENT_ID": os.getenv("DHAN_CLIENT_ID", "").strip(),
            "DHAN_ACCESS_TOKEN": os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        }
        for k, v in required.items():
            if not v:
                missing.append(k)
        if missing:
            raise SystemExit(
                "Missing required env vars: " + ", ".join(missing)
            )

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name, str(default)).strip()
            try:
                return int(raw)
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name, str(default)).strip()
            try:
                return float(raw)
            except ValueError:
                return default

        return Config(
            dhan_client_id=required["DHAN_CLIENT_ID"],
            dhan_access_token=required["DHAN_ACCESS_TOKEN"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            underlying_security_id=_int("DHAN_UNDERLYING_SECURITY_ID", 13),
            underlying_segment=os.getenv("DHAN_UNDERLYING_SEGMENT", "IDX_I").strip(),
            underlying_name_hint=os.getenv("UNDERLYING_NAME_HINT", "NIFTY 50").strip(),
            poll_seconds=_float("POLL_SECONDS", 3.5),
            intraday_refresh_seconds=_int("INTRADAY_REFRESH_SECONDS", 60),
            strikes_window=_int("STRIKES_WINDOW", 10),
            http_timeout=_int("HTTP_TIMEOUT", 15),
        )


class DhanApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "access-token": cfg.dhan_access_token,
                "client-id": cfg.dhan_client_id,
            }
        )

    def get_instrument_master(self) -> List[Dict[str, str]]:
        r = requests.get(INSTRUMENT_MASTER_URL, timeout=self.cfg.http_timeout)
        r.raise_for_status()
        reader = csv.DictReader(r.text.splitlines())
        return list(reader)

    def resolve_underlying(self) -> Tuple[int, str, str]:
        """Try to find the underlying security id and exchange segment.

        If resolution fails, the configured fallback values are returned.
        """
        hint = self.cfg.underlying_name_hint.lower()
        try:
            rows = self.get_instrument_master()
        except Exception:
            return (
                self.cfg.underlying_security_id,
                self.cfg.underlying_segment,
                self.cfg.underlying_name_hint,
            )

        candidate = None
        for row in rows:
            blob = " | ".join((str(v) for v in row.values() if v is not None)).lower()
            if hint in blob:
                candidate = row
                break

        if not candidate:
            return (
                self.cfg.underlying_security_id,
                self.cfg.underlying_segment,
                self.cfg.underlying_name_hint,
            )

        # The detailed master often has these columns.
        sec_id = candidate.get("SEM_SMST_SECURITY_ID") or candidate.get("SecurityId") or candidate.get("SECURITY_ID")
        symbol = candidate.get("SM_SYMBOL_NAME") or candidate.get("SYMBOL_NAME") or self.cfg.underlying_name_hint
        seg = candidate.get("SEM_SEGMENT") or candidate.get("SEGMENT") or self.cfg.underlying_segment

        try:
            sec_int = int(str(sec_id).strip())
        except Exception:
            sec_int = self.cfg.underlying_security_id

        # For index options Dhan docs use IDX_I.
        # If the master says something else, we keep the user-configured value.
        if not seg:
            seg = self.cfg.underlying_segment

        return sec_int, seg, symbol

    def expiry_list(self, underlying_scrip: int, underlying_seg: str) -> List[str]:
        payload = {
            "UnderlyingScrip": underlying_scrip,
            "UnderlyingSeg": underlying_seg,
        }
        r = self.session.post(
            f"{DHAN_BASE}/optionchain/expirylist",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return [str(x) for x in data]

    def option_chain(self, underlying_scrip: int, underlying_seg: str, expiry: str) -> Dict[str, Any]:
        payload = {
            "UnderlyingScrip": underlying_scrip,
            "UnderlyingSeg": underlying_seg,
            "Expiry": expiry,
        }
        r = self.session.post(
            f"{DHAN_BASE}/optionchain",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        return r.json()

    def intraday_ohlc(
        self,
        security_id: int,
        exchange_segment: str,
        instrument: str,
        start: str,
        end: str,
        interval: int = 5,
        oi: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval),
            "oi": bool(oi),
            "fromDate": start,
            "toDate": end,
        }
        r = self.session.post(
            f"{DHAN_BASE}/charts/intraday",
            data=json.dumps(payload),
            timeout=self.cfg.http_timeout,
        )
        r.raise_for_status()
        return r.json()


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
            "disable_web_page_preview": True,
        }
        r = self.session.post(url, data=payload, timeout=self.timeout)
        r.raise_for_status()


@dataclasses.dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class SignalEngine:
    def __init__(self, strikes_window: int = 10):
        self.strikes_window = strikes_window
        self.prev_chain: Optional[Dict[str, Any]] = None
        self.intraday_cache: List[Candle] = []

    @staticmethod
    def _num(v: Any, default: float = 0.0) -> float:
        try:
            if v is None:
                return default
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _safe_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _nearest_strike(strikes: Iterable[float], spot: float) -> float:
        strikes = list(strikes)
        return min(strikes, key=lambda s: abs(s - spot))

    @staticmethod
    def _max_pain(oc: Dict[str, Any]) -> Optional[float]:
        """Approximate max pain from a single snapshot.

        We compute the strike where total intrinsic loss to option holders is minimized.
        This is an inference from the chain, not a value provided by Dhan.
        """
        try:
            strikes = sorted(float(k) for k in oc.keys())
        except Exception:
            return None
        if not strikes:
            return None

        oi_map = {}
        for s in strikes:
            row = oc.get(f"{s:.6f}") or oc.get(str(s)) or {}
            ce = row.get("ce", {})
            pe = row.get("pe", {})
            oi_map[s] = (SignalEngine._num(ce.get("oi")), SignalEngine._num(pe.get("oi")))

        best = None
        best_pain = None
        for settlement in strikes:
            pain = 0.0
            for s in strikes:
                ce_oi, pe_oi = oi_map[s]
                pain += max(0.0, settlement - s) * ce_oi
                pain += max(0.0, s - settlement) * pe_oi
            if best_pain is None or pain < best_pain:
                best_pain = pain
                best = settlement
        return best

    @staticmethod
    def _pcr(oc: Dict[str, Any], center: float, window: int = 10) -> Optional[float]:
        strikes = sorted(float(k) for k in oc.keys())
        if not strikes:
            return None
        # Use a local band around ATM to make PCR more sensitive for same-day weekly trading.
        ordered = sorted(strikes, key=lambda s: abs(s - center))[: max(2, window * 2)]
        call_oi = 0.0
        put_oi = 0.0
        for s in ordered:
            row = oc.get(f"{s:.6f}") or oc.get(str(s)) or {}
            ce = row.get("ce", {})
            pe = row.get("pe", {})
            call_oi += SignalEngine._num(ce.get("oi"))
            put_oi += SignalEngine._num(pe.get("oi"))
        if call_oi <= 0:
            return None
        return put_oi / call_oi

    @staticmethod
    def _top_oi_strikes(oc: Dict[str, Any], side: str, top_n: int = 3) -> List[Tuple[float, float]]:
        items = []
        for strike_s, row in oc.items():
            try:
                strike = float(strike_s)
            except Exception:
                continue
            side_data = row.get(side, {})
            oi = SignalEngine._num(side_data.get("oi"))
            items.append((strike, oi))
        items.sort(key=lambda x: x[1], reverse=True)
        return items[:top_n]

    @staticmethod
    def _best_support_resistance(oc: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        puts = SignalEngine._top_oi_strikes(oc, "pe", 1)
        calls = SignalEngine._top_oi_strikes(oc, "ce", 1)
        support = puts[0][0] if puts else None
        resistance = calls[0][0] if calls else None
        return support, resistance

    @staticmethod
    def _candlestick_patterns(candles: List[Candle]) -> List[str]:
        if len(candles) < 3:
            return []
        a, b, c = candles[-3], candles[-2], candles[-1]
        out: List[str] = []

        # Inside bar
        if b.high <= a.high and b.low >= a.low:
            out.append("inside bar")

        # Bullish engulfing
        if a.close < a.open and b.close > b.open and b.open < a.close and b.close > a.open:
            out.append("bullish engulfing")

        # Bearish engulfing
        if a.close > a.open and b.close < b.open and b.open > a.close and b.close < a.open:
            out.append("bearish engulfing")

        # Higher high / higher low
        if a.high < b.high < c.high and a.low < b.low < c.low:
            out.append("higher-high higher-low")

        # Lower high / lower low
        if a.high > b.high > c.high and a.low > b.low > c.low:
            out.append("lower-high lower-low")

        return out

    @staticmethod
    def _volume_spike(candles: List[Candle]) -> Optional[str]:
        if len(candles) < 6:
            return None
        current = candles[-1].volume
        base = [c.volume for c in candles[-6:-1]]
        avg = sum(base) / len(base)
        if avg <= 0:
            return None
        ratio = current / avg
        if ratio >= 2.0:
            return f"volume spike x{ratio:.1f}"
        if ratio >= 1.5:
            return f"volume above average x{ratio:.1f}"
        return None

    @staticmethod
    def _breakout_signals(candles: List[Candle]) -> List[str]:
        if len(candles) < 10:
            return []
        last = candles[-1]
        prev = candles[:-1]
        prev_high = max(c.high for c in prev[-8:])
        prev_low = min(c.low for c in prev[-8:])
        out = []
        if last.close > prev_high:
            out.append(f"breakout above {prev_high:.2f}")
        if last.close < prev_low:
            out.append(f"breakdown below {prev_low:.2f}")
        return out

    @staticmethod
    def _format_money(v: Optional[float]) -> str:
        return "-" if v is None else f"{v:.2f}"

    def update_intraday_from_api(self, raw: Dict[str, Any]) -> None:
        """Normalize intraday response into Candle objects.

        Dhan returns arrays: open, high, low, close, volume, timestamp.
        """
        open_ = raw.get("open", [])
        high = raw.get("high", [])
        low = raw.get("low", [])
        close = raw.get("close", [])
        vol = raw.get("volume", [])
        ts = raw.get("timestamp", [])
        candles: List[Candle] = []
        for i in range(min(len(open_), len(high), len(low), len(close), len(vol), len(ts))):
            try:
                candles.append(
                    Candle(
                        ts=datetime.fromtimestamp(float(ts[i]), tz=timezone.utc).astimezone(),
                        open=float(open_[i]),
                        high=float(high[i]),
                        low=float(low[i]),
                        close=float(close[i]),
                        volume=float(vol[i]),
                    )
                )
            except Exception:
                continue
        self.intraday_cache = candles

    def _strike_rows_around_atm(self, oc: Dict[str, Any], spot: float) -> List[Tuple[float, Dict[str, Any]]]:
        strikes = sorted(float(k) for k in oc.keys())
        if not strikes:
            return []
        atm = self._nearest_strike(strikes, spot)
        strikes = sorted(strikes, key=lambda s: abs(s - atm))[: self.strikes_window * 2 + 1]
        rows = []
        for s in sorted(strikes):
            row = oc.get(f"{s:.6f}") or oc.get(str(s)) or {}
            rows.append((s, row))
        return rows

    def analyze(self, chain_snapshot: Dict[str, Any], expiry: str) -> Dict[str, Any]:
        data = chain_snapshot.get("data", {}) if isinstance(chain_snapshot, dict) else {}
        spot = self._num(data.get("last_price"))
        oc = self._safe_dict(data.get("oc"))

        rows = self._strike_rows_around_atm(oc, spot)
        atm = self._nearest_strike((s for s, _ in rows), spot) if rows else None
        pcr = self._pcr(oc, center=spot, window=self.strikes_window)
        max_pain = self._max_pain(oc)
        support, resistance = self._best_support_resistance(oc)

        call_oi_change = 0.0
        put_oi_change = 0.0
        call_volume_jump = 0.0
        put_volume_jump = 0.0
        ce_impulse = []
        pe_impulse = []

        for strike, row in rows:
            ce = self._safe_dict(row.get("ce"))
            pe = self._safe_dict(row.get("pe"))

            ce_oi = self._num(ce.get("oi"))
            ce_poi = self._num(ce.get("previous_oi"))
            pe_oi = self._num(pe.get("oi"))
            pe_poi = self._num(pe.get("previous_oi"))

            ce_prev_vol = max(self._num(ce.get("previous_volume")), 1.0)
            pe_prev_vol = max(self._num(pe.get("previous_volume")), 1.0)
            ce_vol = self._num(ce.get("volume"))
            pe_vol = self._num(pe.get("volume"))

            call_oi_change += ce_oi - ce_poi
            put_oi_change += pe_oi - pe_poi
            call_volume_jump += ce_vol / ce_prev_vol
            put_volume_jump += pe_vol / pe_prev_vol

            # Impulse scores use the richer fields Dhan exposes in the option chain response.
            ce_score = 0
            if ce_oi > ce_poi:
                ce_score += 1
            if ce_vol > ce_prev_vol * 1.5:
                ce_score += 1
            if self._num(ce.get("last_price")) > self._num(ce.get("previous_close_price")):
                ce_score += 1
            if self._num(ce.get("implied_volatility")) > 0:
                ce_score += 1
            if ce_score >= 3:
                ce_impulse.append(f"CE {strike:g}")

            pe_score = 0
            if pe_oi > pe_poi:
                pe_score += 1
            if pe_vol > pe_prev_vol * 1.5:
                pe_score += 1
            if self._num(pe.get("last_price")) > self._num(pe.get("previous_close_price")):
                pe_score += 1
            if self._num(pe.get("implied_volatility")) > 0:
                pe_score += 1
            if pe_score >= 3:
                pe_impulse.append(f"PE {strike:g}")

        candles = self.intraday_cache
        patterns = self._candlestick_patterns(candles)
        vol_spike = self._volume_spike(candles)
        breakout = self._breakout_signals(candles)

        # Make the message short but information-dense.
        direction_hint = "neutral"
        if pcr is not None:
            if pcr < 0.9:
                direction_hint = "bearish pressure"
            elif pcr > 1.1:
                direction_hint = "bullish pressure"

        call_top = self._top_oi_strikes(oc, "ce", 2)
        put_top = self._top_oi_strikes(oc, "pe", 2)

        signals = []
        if patterns:
            signals.append("patterns: " + ", ".join(patterns[:3]))
        if vol_spike:
            signals.append(vol_spike)
        if breakout:
            signals.append("; ".join(breakout[:2]))
        if ce_impulse:
            signals.append("call-side impulse: " + ", ".join(ce_impulse[:4]))
        if pe_impulse:
            signals.append("put-side impulse: " + ", ".join(pe_impulse[:4]))

        payload = {
            "expiry": expiry,
            "spot": spot,
            "atm": atm,
            "pcr": pcr,
            "max_pain": max_pain,
            "support": support,
            "resistance": resistance,
            "direction_hint": direction_hint,
            "call_oi_change": call_oi_change,
            "put_oi_change": put_oi_change,
            "call_volume_jump": call_volume_jump,
            "put_volume_jump": put_volume_jump,
            "call_top": call_top,
            "put_top": put_top,
            "signals": signals,
        }
        return payload

    def render_message(self, analysis: Dict[str, Any]) -> str:
        spot = analysis.get("spot")
        atm = analysis.get("atm")
        expiry = analysis.get("expiry")
        pcr = analysis.get("pcr")
        max_pain = analysis.get("max_pain")
        support = analysis.get("support")
        resistance = analysis.get("resistance")
        direction_hint = analysis.get("direction_hint")
        signals = analysis.get("signals", [])
        call_top = analysis.get("call_top", [])
        put_top = analysis.get("put_top", [])

        lines = [
            f"NIFTY Weekly Watch | Expiry {expiry}",
            f"Spot: {self._format_money(spot)} | ATM: {self._format_money(atm)} | PCR: {self._format_money(pcr)}",
            f"Bias: {direction_hint}",
            f"Support: {self._format_money(support)} | Resistance: {self._format_money(resistance)} | Max pain: {self._format_money(max_pain)}",
        ]

        if call_top:
            lines.append("Top Call OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in call_top))
        if put_top:
            lines.append("Top Put OI: " + ", ".join(f"{s:g}({oi:.0f})" for s, oi in put_top))
        if signals:
            lines.append("Signals: " + " | ".join(signals))
        else:
            lines.append("Signals: no fresh change")

        return "\n".join(lines)

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MarketWatchAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = DhanApiClient(cfg)
        self.telegram = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.http_timeout)
        self.engine = SignalEngine(cfg.strikes_window)
        self.underlying_scrip = 13
        self.underlying_seg = "IDX_I"
        self.underlying_name = "NIFTY 50"
        self.expiry: Optional[str] = None
        self.last_message_hash: Optional[str] = None
        self.last_intraday_refresh: float = 0.0
        self.last_snapshot: Optional[Dict[str, Any]] = None

    def choose_current_weekly_expiry(self, expiries: List[str]) -> str:
        today = datetime.now().date()
        future = []
        for x in expiries:
            try:
                d = datetime.strptime(x, "%Y-%m-%d").date()
                if d >= today:
                    future.append(d)
            except Exception:
                continue
        if not future:
            raise RuntimeError("No valid future expiry dates returned by Dhan.")
        return min(future).isoformat()

    def refresh_intraday_if_needed(self) -> None:
        now = time.time()
        if now - self.last_intraday_refresh < self.cfg.intraday_refresh_seconds:
            return

        # We try to fetch 5-minute candles for the NIFTY underlying.
        # Dhan docs show intraday candle request fields securityId, exchangeSegment, instrument, interval, oi, fromDate, toDate.
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=2)
        payload = self.api.intraday_ohlc(
            security_id=self.underlying_scrip,
            exchange_segment=self.underlying_seg,
            instrument="INDEX",  # If your Dhan account expects a different label, change here.
            start=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            interval=5,
            oi=False,
        )
        self.engine.update_intraday_from_api(payload)
        self.last_intraday_refresh = now

    def run(self) -> None:
        print(
            f"Resolved underlying: {self.underlying_name} | securityId={self.underlying_scrip} | segment={self.underlying_seg}"
        )
        self.telegram.send(
            f"NIFTY Market Watch Agent started\nUnderlying: {self.underlying_name}\nsecurityId={self.underlying_scrip} segment={self.underlying_seg}"
        )

        while True:
            try:
                if self.expiry is None:
                    expiries = self.api.expiry_list(self.underlying_scrip, self.underlying_seg)
                    self.expiry = self.choose_current_weekly_expiry(expiries)
                    self.telegram.send(f"Current weekly expiry selected: {self.expiry}")

                # self.refresh_intraday_if_needed()
                snapshot = self.api.option_chain(self.underlying_scrip, self.underlying_seg, self.expiry)
                self.last_snapshot = snapshot
                analysis = self.engine.analyze(snapshot, self.expiry)
                message = self.engine.render_message(analysis)
                msg_hash = self.engine.digest(message)

                if msg_hash != self.last_message_hash:
                    self.telegram.send(message)
                    self.last_message_hash = msg_hash
                    print("Alert sent:\n" + message + "\n")
                else:
                    print(f"No change | Spot={analysis.get('spot')} | PCR={analysis.get('pcr')}")

                time.sleep(self.cfg.poll_seconds)

            except KeyboardInterrupt:
                self.telegram.send("NIFTY Market Watch Agent stopped.")
                print("Stopped.")
                return

            except requests.HTTPError as e:
                # Dhan / Telegram HTTP errors come here.
                print(f"HTTP error: {e}")
                time.sleep(max(5, int(self.cfg.poll_seconds)))

            except Exception as e:
                print(f"Error: {e}")
                time.sleep(max(5, int(self.cfg.poll_seconds)))


def main() -> None:
    cfg = Config.from_env()
    agent = MarketWatchAgent(cfg)
    agent.run()


if __name__ == "__main__":
    main()
