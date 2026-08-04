"""Historical 1-minute bar backfill — the free-tier data path.

The aggregates endpoint returns a whole date RANGE per request, so a strict
per-minute rate limit costs almost nothing here: roughly one call per month of
history. Two years of SPY is ~24 calls.

This exists because H3-P1 stage 1 needs only underlying bars. It is the one
component of the research programme that can reach statistical significance
immediately, without options entitlements and without waiting months for
forward collection.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone

from . import market_calendar as cal
from .sources.polygon import PolygonError

log = logging.getLogger("backfill")

MAX_RESULTS = 50_000          # provider cap per aggregates response


def _month_ranges(start: date, end: date):
    """Monthly chunks: ~8,200 one-minute bars each, well under the cap."""
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12),
                   1 if cur.month == 12 else cur.month + 1, 1)
        yield max(cur, start), min(nxt - timedelta(days=1), end)
        cur = nxt


class RateLimiter:
    """Spaces requests to respect a measured per-minute cap."""

    def __init__(self, per_minute: int = 5):
        self.interval = 60.0 / max(per_minute, 1)
        self._last = 0.0

    def wait(self):
        gap = time.monotonic() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.monotonic()


def fetch_range(source, symbol: str, start: date, end: date,
                limiter: RateLimiter) -> list:
    """One aggregates call for [start, end]. Returns normalized bar dicts."""
    limiter.wait()
    j = source._get(
        f"/v2/aggs/ticker/{symbol}/range/1/minute/"
        f"{start.isoformat()}/{end.isoformat()}",
        {"adjusted": "true", "sort": "asc", "limit": MAX_RESULTS})
    results = j.get("results") or []
    if len(results) >= MAX_RESULTS:
        log.warning("%s %s..%s hit the %d-result cap; range may be truncated",
                    symbol, start, end, MAX_RESULTS)
    out = []
    for r in results:
        ts = r.get("t")
        if ts is None:
            continue
        out.append({
            "event_ts": datetime.fromtimestamp(
                ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": r.get("o"), "high": r.get("h"), "low": r.get("l"),
            "close": r.get("c"), "volume": r.get("v"), "vwap": r.get("vw"),
            "n_trades": r.get("n"),
        })
    return out


def store(conn, symbol: str, bars: list, source_name: str) -> int:
    fetch_ts = cal.utcnow_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO underlying_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(b["event_ts"], symbol, b["open"], b["high"], b["low"], b["close"],
          b["volume"], b["vwap"], b["n_trades"], fetch_ts, source_name)
         for b in bars])
    conn.commit()
    return len(bars)


def discover_lookback(source, symbol: str, limiter: RateLimiter,
                      max_years: int = 6) -> date | None:
    """Find how far back this plan actually serves, by probing backwards.

    The entitlement is measured, not read off a pricing page.
    """
    today = date.today()
    earliest = None
    for years in range(1, max_years + 1):
        probe_start = today - timedelta(days=365 * years)
        probe_end = probe_start + timedelta(days=3)
        try:
            bars = fetch_range(source, symbol, probe_start, probe_end, limiter)
        except PolygonError as e:
            log.info("lookback probe stopped at ~%dy: HTTP %d",
                     years, e.status_code)
            break
        if not bars:
            log.info("lookback probe: no bars at ~%dy back", years)
            break
        earliest = probe_start
        log.info("lookback probe: %d bars available ~%dy back (%s)",
                 len(bars), years, probe_start)
    return earliest


def run(conn, source, symbols, start: date, end: date,
        per_minute: int = 5) -> dict:
    """Backfill [start, end] for each symbol. Returns per-symbol bar counts."""
    limiter = RateLimiter(per_minute)
    totals = {}
    for symbol in symbols:
        n = 0
        for chunk_start, chunk_end in _month_ranges(start, end):
            try:
                bars = fetch_range(source, symbol, chunk_start, chunk_end,
                                   limiter)
            except PolygonError as e:
                log.warning("%s %s..%s failed: HTTP %d %s", symbol,
                            chunk_start, chunk_end, e.status_code,
                            e.body[:120])
                continue
            n += store(conn, symbol, bars, source.name)
            log.info("%s %s..%s: %d bars (running total %d)", symbol,
                     chunk_start, chunk_end, len(bars), n)
        totals[symbol] = n
    return totals


def coverage(conn, symbol: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(event_ts) AS lo, MAX(event_ts) AS hi, "
        "COUNT(DISTINCT substr(event_ts,1,10)) AS days "
        "FROM underlying_bars WHERE symbol=?", (symbol,)).fetchone()
    return {"symbol": symbol, "bars": row["n"], "first": row["lo"],
            "last": row["hi"], "days": row["days"]}
