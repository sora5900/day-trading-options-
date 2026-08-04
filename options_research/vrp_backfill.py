"""Backfill daily close prices for historical ATM straddles.

For each backfilled trading day D: spot = regular-session close, expiry = the
next trading day (SPY/QQQ list an expiration every session), strike = nearest
listed dollar strike. Fetch the daily aggregate bar for that call and put on
day D and store both closes.

Cost: 2 requests per day per symbol (~1,000 for a year of SPY) — trivial now
that the rate limit is gone. Aggregates only; no quotes involved anywhere.
"""

import logging
from datetime import date

from . import market_calendar as cal
from .analysis.vrp_magnitude import _spot_closes
from .sources.polygon import PolygonError

log = logging.getLogger("vrp_backfill")


def run(conn, source, symbol: str = "SPY") -> dict:
    spots = _spot_closes(conn, symbol)
    days = sorted(spots)
    if not days:
        return {"error": "no underlying bars stored; run `backfill` first"}
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}

    have = {r["event_ts"][:10] for r in conn.execute(
        "SELECT DISTINCT event_ts FROM option_bars WHERE symbol=?",
        (symbol,))}

    stored = skipped = failed = 0
    for day in days[:-1]:
        if day in have:
            skipped += 1
            continue
        expiry = nxt[day]
        strike = float(round(spots[day]))
        got = 0
        for right in ("C", "P"):
            try:
                occ = source._occ_ticker(symbol, expiry, strike, right)
                j = source._get(
                    f"/v2/aggs/ticker/{occ}/range/1/day/{day}/{day}",
                    {"adjusted": "true", "limit": 5})
            except PolygonError as e:
                if e.status_code == 429:
                    log.warning("throttled at %s; re-run to resume", day)
                    return {"stored": stored, "skipped": skipped,
                            "failed": failed, "resumable": True}
                failed += 1
                continue
            for r in j.get("results") or []:
                iso = _iso(r.get("t"))
                if not iso:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO option_bars VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iso, symbol, expiry, strike, right, r.get("o"),
                     r.get("h"), r.get("l"), r.get("c"), r.get("v"),
                     r.get("vw"), r.get("n"), cal.utcnow_iso(), source.name))
                got += 1
        if got:
            stored += 1
            if stored % 50 == 0:
                conn.commit()
                log.info("%s: %d days stored (through %s)", symbol, stored,
                         day)
    conn.commit()
    return {"symbol": symbol, "stored": stored, "already_had": skipped,
            "failed_contracts": failed}


def _iso(ms) -> str | None:
    from datetime import datetime, timezone
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None
