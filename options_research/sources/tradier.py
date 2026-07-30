"""Tradier source — cross-check provider (spec §2).

Kept deliberately at parity with the Polygon source's normalized shapes so the
cross-check compares data, not adapter quirks. Tradier's sandbox is free, which
makes it the cheapest available second opinion on quote quality.
"""

import time
from datetime import date, datetime, timezone

import requests

from ..capabilities import CheckResult, Status
from .base import QuoteSource, epoch_to_iso, classify_delay


class TradierError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:300]}")


class TradierSource(QuoteSource):
    name = "tradier"

    def __init__(self, token: str, base: str = "https://sandbox.tradier.com/v1",
                 timeout: float = 20.0):
        if not token:
            raise ValueError("TRADIER_TOKEN is not set")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}", "Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            try:
                r = self.session.get(self.base + path, params=params,
                                     timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise TradierError(r.status_code, r.text)
                return r.json() or {}
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        return {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get_underlying(self, symbol: str) -> dict | None:
        fetch_ts = self._now_iso()
        j = self._get("/markets/quotes", {"symbols": symbol})
        q = (j.get("quotes") or {}).get("quote")
        if not q:
            return None
        if isinstance(q, list):
            q = q[0]
        event_ts = epoch_to_iso(q.get("trade_date")) or epoch_to_iso(
            q.get("bid_date"))
        return {
            "last": q.get("last"), "bid": q.get("bid"), "ask": q.get("ask"),
            "volume": q.get("volume"), "vwap": None,
            "day_open": q.get("open"), "day_high": q.get("high"),
            "day_low": q.get("low"), "prev_close": q.get("prevclose"),
            "event_ts": event_ts, "fetch_ts": fetch_ts,
            "delay_class": classify_delay(event_ts, fetch_ts),
        }

    def _expirations(self, symbol: str) -> list[str]:
        j = self._get("/markets/options/expirations",
                      {"symbol": symbol, "includeAllRoots": "true"})
        exp = (j.get("expirations") or {}).get("date") or []
        return exp if isinstance(exp, list) else [exp]

    def get_chain(self, symbol, spot, strike_band_pct, dte_min, dte_max):
        fetch_ts = self._now_iso()
        today = date.today()
        lo, hi = spot * (1 - strike_band_pct), spot * (1 + strike_band_pct)
        out = []
        for exp in self._expirations(symbol):
            try:
                dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            except ValueError:
                continue
            if not (dte_min <= dte <= dte_max):
                continue
            j = self._get("/markets/options/chains",
                          {"symbol": symbol, "expiration": exp,
                           "greeks": "true"})
            opts = (j.get("options") or {}).get("option") or []
            if isinstance(opts, dict):
                opts = [opts]
            for o in opts:
                if not (lo <= (o.get("strike") or 0) <= hi):
                    continue
                g = o.get("greeks") or {}
                event_ts = (epoch_to_iso(o.get("bid_date"))
                            or epoch_to_iso(o.get("trade_date")))
                out.append({
                    "expiry": exp, "strike": o.get("strike"),
                    "right": "C" if o.get("option_type") == "call" else "P",
                    "bid": o.get("bid"), "ask": o.get("ask"),
                    "last": o.get("last"), "volume": o.get("volume"),
                    "open_interest": o.get("open_interest"),
                    "iv": g.get("mid_iv") or g.get("smv_vol"),
                    "delta": g.get("delta"), "gamma": g.get("gamma"),
                    "theta": g.get("theta"), "vega": g.get("vega"),
                    "event_ts": event_ts, "fetch_ts": fetch_ts,
                    "delay_class": classify_delay(event_ts, fetch_ts),
                })
        return out

    def probe(self) -> list:
        out = []
        try:
            self._get("/markets/quotes", {"symbols": "SPY"})
            out.append(CheckResult("auth", Status.PASS, "token authenticates"))
        except TradierError as e:
            out.append(CheckResult("auth", Status.FAIL,
                                   f"HTTP {e.status_code}", e.body[:300]))
            return out
        except Exception as e:                            # noqa: BLE001
            out.append(CheckResult("auth", Status.FAIL, "request failed",
                                   str(e)[:300]))
            return out

        u = self.get_underlying("SPY")
        out.append(CheckResult(
            "underlying_snapshot",
            Status.PASS if u and u.get("last") else Status.FAIL,
            f"SPY last={u.get('last') if u else None} (vwap not provided)"))

        if not (u and u.get("last")):
            return out
        try:
            chain = self.get_chain("SPY", u["last"], 0.07, 0, 7)
        except TradierError as e:
            out.append(CheckResult("chain_snapshot", Status.FAIL,
                                   f"HTTP {e.status_code}", e.body[:300]))
            return out
        out.append(CheckResult("chain_snapshot",
                               Status.PASS if chain else Status.FAIL,
                               f"{len(chain)} contracts"))
        if chain:
            n = len(chain)
            for rid, key in (("chain_bid_ask", "bid"),
                             ("chain_greeks", "delta"),
                             ("chain_iv", "iv"),
                             ("chain_open_interest", "open_interest"),
                             ("chain_volume", "volume")):
                f = sum(1 for c in chain if c.get(key) is not None) / n
                out.append(CheckResult(
                    rid,
                    Status.PASS if f >= 0.8 else
                    (Status.WARN if f > 0 else Status.FAIL),
                    f"{key} populated on {f:.0%}"))
            with_ts = sum(1 for c in chain if c.get("event_ts"))
            out.append(CheckResult(
                "exchange_timestamp",
                Status.PASS if with_ts else Status.FAIL,
                f"present on {with_ts}/{n} contracts"))
        return out
