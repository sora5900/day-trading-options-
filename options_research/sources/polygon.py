"""Polygon.io source (recommended primary — spec §2).

Starter tier is 15-min delayed. Delayed is FINE for research: we measure
whether setups have edge, not race anyone.
"""

import time
from datetime import date, timedelta

import requests

from .base import QuoteSource

BASE = "https://api.polygon.io"


class PolygonSource(QuoteSource):
    name = "polygon"

    def __init__(self, api_key: str, timeout: float = 15.0):
        if not api_key:
            raise ValueError("POLYGON_API_KEY is not set")
        self.key = api_key
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path_or_url: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.key
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        return {}

    def get_underlying(self, symbol: str) -> dict | None:
        j = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}")
        t = j.get("ticker")
        if not t:
            return None
        day, quote, minute = t.get("day", {}), t.get("lastQuote", {}), t.get("min", {})
        last = (t.get("lastTrade") or {}).get("p") or minute.get("c") or day.get("c")
        if not last:
            return None
        return {
            "last": last,
            "bid": quote.get("p"), "ask": quote.get("P"),
            "volume": day.get("v"),
            "vwap": day.get("vw") or minute.get("vw"),
            "day_open": day.get("o"), "day_high": day.get("h"),
            "day_low": day.get("l"), "prev_close": t.get("prevDay", {}).get("c"),
            "quote_ts": quote.get("t"),
        }

    def get_chain(self, symbol, spot, strike_band_pct, dte_min, dte_max):
        today = date.today()
        params = {
            "strike_price.gte": round(spot * (1 - strike_band_pct), 2),
            "strike_price.lte": round(spot * (1 + strike_band_pct), 2),
            "expiration_date.gte": (today + timedelta(days=dte_min)).isoformat(),
            "expiration_date.lte": (today + timedelta(days=dte_max)).isoformat(),
            "limit": 250,
        }
        out, url, first = [], f"/v3/snapshot/options/{symbol}", True
        while url:
            j = self._get(url, params if first else None)
            first = False
            for c in j.get("results", []):
                det, q = c.get("details", {}), c.get("last_quote", {})
                greeks = c.get("greeks", {}) or {}
                out.append({
                    "expiry": det.get("expiration_date"),
                    "strike": det.get("strike_price"),
                    "right": "C" if det.get("contract_type") == "call" else "P",
                    "bid": q.get("bid"), "ask": q.get("ask"),
                    "last": (c.get("last_trade") or {}).get("price"),
                    "volume": (c.get("day") or {}).get("volume"),
                    "open_interest": c.get("open_interest"),
                    "iv": c.get("implied_volatility"),
                    "delta": greeks.get("delta"), "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"), "vega": greeks.get("vega"),
                    "quote_ts": q.get("last_updated"),
                })
            url = j.get("next_url")
        return out
