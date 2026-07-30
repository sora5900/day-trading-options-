"""Feature engine: derived per snapshot, recomputable, no look-ahead.

Every input read goes through the db.*_at helpers, which are bounded by the
decision timestamp. iv30 here is a PROXY: the vega-weighted ATM IV of the
nearest collected expiries (we collect 0-7 DTE). It is consistent over time,
which is what ranking and VRP need — but it is not a true 30-day IV. Noted in
the schema docs and in the reporter.
"""

import math
import statistics
from datetime import timedelta

from . import db, market_calendar as cal

ANNUALIZE_MIN = math.sqrt(252 * 390)   # per-minute log-returns → annual vol


def _realized_vol(rows, window_min: float, as_of: str) -> float | None:
    """Annualized realized vol from underlying `last` over trailing window."""
    cutoff = cal.parse_ts(as_of) - timedelta(minutes=window_min)
    pts = [(cal.parse_ts(r["ts"]), r["last"]) for r in rows
           if r["last"] and cal.parse_ts(r["ts"]) >= cutoff]
    if len(pts) < 5:
        return None
    rets, prev_t, prev_p = [], None, None
    for t, p in pts:
        if prev_p and prev_p > 0 and p > 0:
            dt_min = (t - prev_t).total_seconds() / 60.0
            if dt_min > 0:
                # per-minute-normalized log return
                rets.append(math.log(p / prev_p) / math.sqrt(dt_min))
        prev_t, prev_p = t, p
    if len(rets) < 4:
        return None
    return statistics.pstdev(rets) * ANNUALIZE_MIN


def _atm_iv(chain, spot: float, expiry: str) -> float | None:
    """Vega-weighted IV of contracts within 1% of spot for one expiry."""
    num = den = 0.0
    for c in chain:
        if c["expiry"] != expiry or not c["iv"] or c["iv"] <= 0:
            continue
        if abs(c["strike"] - spot) / spot > 0.01:
            continue
        w = c["vega"] if c["vega"] and c["vega"] > 0 else 1.0
        num += c["iv"] * w
        den += w
    return num / den if den else None


def _skew_25d(chain, expiry: str) -> float | None:
    """IV(25-delta put) - IV(25-delta call), nearest expiry."""
    best_p = best_c = None
    for c in chain:
        if c["expiry"] != expiry or not c["iv"] or c["delta"] is None:
            continue
        if c["right"] == "P":
            d = abs(abs(c["delta"]) - 0.25)
            if best_p is None or d < best_p[0]:
                best_p = (d, c["iv"])
        else:
            d = abs(c["delta"] - 0.25)
            if best_c is None or d < best_c[0]:
                best_c = (d, c["iv"])
    if best_p and best_c and best_p[0] < 0.10 and best_c[0] < 0.10:
        return best_p[1] - best_c[1]
    return None


def _liquidity_score(chain, spot: float) -> float | None:
    """Median spread% across near-ATM contracts (within 1% of spot).
    Lower is better; this is the number a strategy's edge must beat."""
    spreads = []
    for c in chain:
        if abs(c["strike"] - spot) / spot > 0.01:
            continue
        b, a = c["bid"], c["ask"]
        if b and a and a >= b and (a + b) > 0:
            spreads.append((a - b) / ((a + b) / 2))
    return statistics.median(spreads) if spreads else None


def _atr_pct(conn, symbol: str, as_of: str, n_days: int = 14) -> float | None:
    """ATR% from prior days' EOD-ish snapshot rows (true range vs prev close)."""
    day = cal.trading_day_of(as_of)
    rows = conn.execute(
        "SELECT substr(ts,1,10) AS d, MAX(day_high) AS h, MIN(day_low) AS l, "
        "MAX(prev_close) AS pc, MAX(last) AS c FROM underlying_snap "
        "WHERE symbol=? AND ts<? AND substr(ts,1,10) < ? "
        "GROUP BY substr(ts,1,10) ORDER BY d DESC LIMIT ?",
        (symbol, as_of, day, n_days)).fetchall()
    trs = []
    for r in rows:
        if not (r["h"] and r["l"] and r["c"]):
            continue
        pc = r["pc"] or r["c"]
        tr = max(r["h"] - r["l"], abs(r["h"] - pc), abs(r["l"] - pc))
        trs.append(tr / r["c"])
    return statistics.mean(trs) if trs else None


def _regime(vwap_dev, rv30, rv_hist) -> str:
    """Coarse classifier: trend / chop / high-vol. Deliberately dumb in v1."""
    if rv30 and rv_hist and len(rv_hist) >= 20:
        srt = sorted(rv_hist)
        if rv30 > srt[int(len(srt) * 0.8)]:
            return "high-vol"
    if vwap_dev is not None and abs(vwap_dev) > 0.004:
        return "trend"
    return "chop"


def compute_features(conn, symbol: str, ts: str) -> dict | None:
    """Compute and persist the feature row for (symbol, ts)."""
    u = db.underlying_at(conn, symbol, ts)
    chain = db.chain_at(conn, symbol, ts)
    if not u or not u["last"]:
        return None
    spot = u["last"]
    day = cal.trading_day_of(ts)
    day_rows = db.underlying_between(conn, symbol, f"{day}T00:00:00Z", ts)

    rv5 = _realized_vol(day_rows, 5, ts)
    rv30 = _realized_vol(day_rows, 30, ts)

    expiries = sorted({c["expiry"] for c in chain})
    iv_near = _atm_iv(chain, spot, expiries[0]) if expiries else None
    iv_next = _atm_iv(chain, spot, expiries[1]) if len(expiries) > 1 else None
    iv30 = iv_near  # proxy — see module docstring
    term_slope = (iv_next - iv_near) if (iv_near and iv_next) else None

    # IV rank/percentile vs own trailing history (prior feature rows)
    hist = db.features_history(conn, symbol, ts, limit=20 * 80)
    iv_hist = [r["iv30"] for r in hist if r["iv30"]]
    rv_hist = [r["realized_vol_30m"] for r in hist if r["realized_vol_30m"]]
    iv_rank = iv_pctile = None
    if iv30 and len(iv_hist) >= 20:
        lo, hi = min(iv_hist), max(iv_hist)
        iv_rank = (iv30 - lo) / (hi - lo) if hi > lo else 0.5
        iv_pctile = sum(1 for x in iv_hist if x < iv30) / len(iv_hist)

    vrp = (iv30 - rv30) if (iv30 and rv30) else None

    # Opening range: first 15 minutes of the session
    orb_high = orb_low = None
    orb_broken = None
    mins = cal.minutes_since_open(ts)
    if mins is not None and mins >= 0:
        orb_rows = [r for r in day_rows
                    if (m := cal.minutes_since_open(r["ts"])) is not None
                    and 0 <= m <= 15 and r["last"]]
        if orb_rows:
            orb_high = max(r["last"] for r in orb_rows)
            orb_low = min(r["last"] for r in orb_rows)
            if mins > 15:
                if spot > orb_high:
                    orb_broken = "UP"
                elif spot < orb_low:
                    orb_broken = "DOWN"

    vwap_dev = ((spot - u["vwap"]) / u["vwap"]) if u["vwap"] else None
    atr_pct = _atr_pct(conn, symbol, ts)

    row = {
        "ts": ts, "symbol": symbol,
        "iv30": iv30, "iv_rank": iv_rank, "iv_percentile": iv_pctile,
        "realized_vol_5m": rv5, "realized_vol_30m": rv30, "vrp": vrp,
        "orb_high": orb_high, "orb_low": orb_low, "orb_broken": orb_broken,
        "vwap_dev": vwap_dev, "atr_pct": atr_pct,
        "skew_25d": _skew_25d(chain, expiries[0]) if expiries else None,
        "term_slope": term_slope,
        "liquidity_score": _liquidity_score(chain, spot),
        "regime": _regime(vwap_dev, rv30, rv_hist),
    }
    conn.execute(
        "INSERT OR REPLACE INTO features VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["ts"], row["symbol"], row["iv30"], row["iv_rank"],
         row["iv_percentile"], row["realized_vol_5m"], row["realized_vol_30m"],
         row["vrp"], row["orb_high"], row["orb_low"], row["orb_broken"],
         row["vwap_dev"], row["atr_pct"], row["skew_25d"], row["term_slope"],
         row["liquidity_score"], row["regime"]))
    conn.commit()
    return row
