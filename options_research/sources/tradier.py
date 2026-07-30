"""Tradier source (sandbox is genuinely free — spec §2 backup/cross-check)."""

import time
from datetime import date, datetime

import requests

from .base import QuoteSource


class TradierSource(QuoteSource):
    name = "tradier"

    def __init__(self, token: str, base: str = "https://sandbox.tradier.com/v1",
                 timeout: float = 15.0):
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
                r.raise_for_status()
                return r.json() or {}
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        return {}

    def get_underlying(self, symbol: str) -> dict | None:
        j = self._get("/markets/quotes", {"symbols": symbol})
        q = (j.get("quotes") or {}).get("quote")
        if not q:
            return None
        if isinstance(q, list):
            q = q[0]
        return {
            "last": q.get("last"), "bid": q.get("bid"), "ask": q.get("ask"),
            "volume": q.get("volume"), "vwap": None,
            "day_open": q.get("open"), "day_high": q.get("high"),
            "day_low": q.get("low"), "prev_close": q.get("prevclose"),
            "quote_ts": q.get("trade_date"),
        }

    def _expirations(self, symbol: str) -> list[str]:
        j = self._get("/markets/options/expirations",
                      {"symbol": symbol, "includeAllRoots": "true"})
        exp = (j.get("expirations") or {}).get("date") or []
        return exp if isinstance(exp, list) else [exp]

    def get_chain(self, symbol, spot, strike_band_pct, dte_min, dte_max):
        today = date.today()
        lo, hi = spot * (1 - strike_band_pct), spot * (1 + strike_band_pct)
        out = []
        for exp in self._expirations(symbol):
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            if not (dte_min <= dte <= dte_max):
                continue
            j = self._get("/markets/options/chains",
                          {"symbol": symbol, "expiration": exp, "greeks": "true"})
            opts = (j.get("options") or {}).get("option") or []
            if isinstance(opts, dict):
                opts = [opts]
            for o in opts:
                if not (lo <= (o.get("strike") or 0) <= hi):
                    continue
                g = o.get("greeks") or {}
                out.append({
                    "expiry": exp, "strike": o.get("strike"),
                    "right": "C" if o.get("option_type") == "call" else "P",
                    "bid": o.get("bid"), "ask": o.get("ask"),
                    "last": o.get("last"), "volume": o.get("volume"),
                    "open_interest": o.get("open_interest"),
                    "iv": g.get("mid_iv") or g.get("smv_vol"),
                    "delta": g.get("delta"), "gamma": g.get("gamma"),
                    "theta": g.get("theta"), "vega": g.get("vega"),
                    "quote_ts": o.get("trade_date"),
                })
        return out
