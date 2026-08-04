"""Measure how fresh a source's quotes actually are.

Needed because a vendor can supply excellent bid/ask while supplying no
trustworthy timestamp for it. `lastTradeDate` measures when a CONTRACT last
TRADED, which for an illiquid strike is hours old even when its quote is
current — so it cannot answer the question, and neither can guesswork.

Two independent measurements:

1. **Do quotes move?** Sample the same contracts twice, N seconds apart, and
   count how many bid/ask values changed. Live quotes move during the
   session; a cached or end-of-day snapshot does not.

2. **What is the lag?** Compare the source's reported underlying price against
   a reference series that DOES carry reliable timestamps (1-minute
   aggregates). The timestamp of the closest-matching bar is an empirical
   estimate of the source's delay.

Neither measurement licenses substituting fetch time for an exchange
timestamp. They exist so that any decision to work with an approximate clock
is made explicitly, with numbers attached, and recorded as a limitation.
"""

import time
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


def quote_movement(source, symbol: str, spot: float, wait_secs: int = 120,
                   band: float = 0.02) -> dict:
    """Sample the chain twice and report how much of it moved."""
    def snap():
        rows = source.get_chain(symbol, spot, band, 0, 7)
        return {(c["expiry"], c["strike"], c["right"]): c
                for c in rows if c.get("bid") is not None
                and c.get("ask") is not None}

    a = snap()
    t0 = _now()
    time.sleep(wait_secs)
    b = snap()
    t1 = _now()

    shared = set(a) & set(b)
    if not shared:
        return {"error": "no overlapping contracts between samples"}
    changed = sum(1 for k in shared
                  if a[k]["bid"] != b[k]["bid"] or a[k]["ask"] != b[k]["ask"])
    moved_frac = changed / len(shared)
    return {
        "n_compared": len(shared),
        "n_changed": changed,
        "fraction_changed": round(moved_frac, 4),
        "wait_secs": wait_secs,
        "sampled_at": [t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       t1.strftime("%Y-%m-%dT%H:%M:%SZ")],
        "interpretation": (
            "quotes are updating — the feed is live, though its lag is a "
            "separate question" if moved_frac > 0.10 else
            "quotes did NOT move — likely cached, stale, or the market is "
            "closed. Re-run during market hours before concluding"),
    }


def estimate_delay_vs_reference(conn, source, symbol: str,
                                lookback_min: int = 240) -> dict:
    """Estimate the source's underlying delay against timestamped 1-min bars.

    The reference series must come from a provider whose event clock is
    trusted (here: backfilled aggregates). Finding the bar whose close best
    matches the source's reported price dates that price empirically.
    """
    u = source.get_underlying(symbol)
    if not u or not u.get("last"):
        return {"error": "source returned no underlying price"}
    price = u["last"]
    fetch_ts = u.get("fetch_ts")

    rows = conn.execute(
        "SELECT event_ts, close FROM underlying_bars WHERE symbol=? "
        "ORDER BY event_ts DESC LIMIT ?", (symbol, lookback_min)).fetchall()
    if not rows:
        return {"error": "no reference bars stored; run backfill first",
                "source_price": price}

    best = min(rows, key=lambda r: abs((r["close"] or 0) - price))
    newest = rows[0]
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        lag = (datetime.strptime(fetch_ts, fmt)
               - datetime.strptime(best["event_ts"], fmt)).total_seconds()
    except (ValueError, TypeError):
        lag = None

    return {
        "source_price": price,
        "fetch_ts": fetch_ts,
        "best_matching_bar_ts": best["event_ts"],
        "best_matching_bar_close": best["close"],
        "price_gap": round(abs((best["close"] or 0) - price), 4),
        "newest_reference_bar_ts": newest["event_ts"],
        "estimated_lag_secs": lag,
        "estimated_lag_min": round(lag / 60, 1) if lag is not None else None,
        "caveat": ("the reference series is itself delayed; this measures the "
                   "gap BETWEEN the two sources, not absolute latency"),
    }


def render(movement: dict, delay: dict) -> str:
    w = 74
    out = ["=" * w, "QUOTE FRESHNESS", "=" * w, "",
           "1. DO QUOTES MOVE?"]
    if "error" in movement:
        out.append(f"   ERROR: {movement['error']}")
    else:
        out.append(f"   {movement['n_changed']}/{movement['n_compared']} "
                   f"contracts changed bid or ask over "
                   f"{movement['wait_secs']}s "
                   f"({movement['fraction_changed']:.1%})")
        out.append(f"   sampled {movement['sampled_at'][0]} -> "
                   f"{movement['sampled_at'][1]}")
        out.append(f"   -> {movement['interpretation']}")
    out += ["", "2. ESTIMATED LAG vs TIMESTAMPED REFERENCE BARS"]
    if "error" in delay:
        out.append(f"   ERROR: {delay['error']}")
    else:
        out.append(f"   source reports {symbol_fmt(delay['source_price'])} "
                   f"at fetch {delay['fetch_ts']}")
        out.append(f"   closest reference bar: {delay['best_matching_bar_ts']} "
                   f"close={symbol_fmt(delay['best_matching_bar_close'])} "
                   f"(gap {delay['price_gap']})")
        out.append(f"   newest reference bar:  "
                   f"{delay['newest_reference_bar_ts']}")
        out.append(f"   -> estimated lag: {delay['estimated_lag_min']} minutes")
        out.append(f"   caveat: {delay['caveat']}")
    out += ["", "This does NOT authorise using fetch time as an event clock.",
            "It exists so that any decision to accept an approximate clock is",
            "made explicitly, with numbers, and recorded as a limitation.",
            "=" * w]
    return "\n".join(out)


def symbol_fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else str(v)
