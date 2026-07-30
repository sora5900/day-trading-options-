"""Feature engine: derived per snapshot, recomputable, no look-ahead.

Phase 1 features are PRICE-SPACE (see signals.py) and require no vendor IV or
Greeks. Vendor-derived fields are still computed and stored when the plan
populates them, so a later profile upgrade has history to work with — but
nothing in Phase 1 depends on them.

Every input read is bounded by the decision timestamp on the exchange clock.
"""

import math
import statistics
from datetime import timedelta

from . import db, market_calendar as cal, signals

ANNUALIZE_MIN = math.sqrt(252 * 390)


def _realized_vol(rows, window_min: float, as_of: str) -> float | None:
    cutoff = cal.parse_ts(as_of) - timedelta(minutes=window_min)
    pts = [(cal.parse_ts(r["event_ts"]), r["last"]) for r in rows
           if r["last"] and cal.parse_ts(r["event_ts"]) >= cutoff]
    if len(pts) < 5:
        return None
    rets, prev_t, prev_p = [], None, None
    for t, p in pts:
        if prev_p and prev_p > 0 and p > 0:
            dt_min = (t - prev_t).total_seconds() / 60.0
            if dt_min > 0:
                rets.append(math.log(p / prev_p) / math.sqrt(dt_min))
        prev_t, prev_p = t, p
    if len(rets) < 4:
        return None
    return statistics.pstdev(rets) * ANNUALIZE_MIN


def _atm_iv(chain, spot: float, expiry: str) -> float | None:
    num = den = 0.0
    for c in chain:
        if c["expiry"] != expiry or not c["iv"] or c["iv"] <= 0:
            continue
        if not spot or abs(c["strike"] - spot) / spot > 0.01:
            continue
        w = c["vega"] if c["vega"] and c["vega"] > 0 else 1.0
        num += c["iv"] * w
        den += w
    return num / den if den else None


def _skew_25d(chain, expiry: str) -> float | None:
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


def _atr_pct(conn, symbol: str, as_of: str, n_days: int = 14) -> float | None:
    day = cal.trading_day_of(as_of)
    rows = conn.execute(
        "SELECT substr(event_ts,1,10) AS d, MAX(day_high) AS h, "
        "MIN(day_low) AS l, MAX(prev_close) AS pc, MAX(last) AS c "
        "FROM underlying_snap WHERE symbol=? AND event_ts<? "
        "AND substr(event_ts,1,10) < ? "
        "GROUP BY substr(event_ts,1,10) ORDER BY d DESC LIMIT ?",
        (symbol, as_of, day, n_days)).fetchall()
    trs = []
    for r in rows:
        if not (r["h"] and r["l"] and r["c"]):
            continue
        pc = r["pc"] or r["c"]
        trs.append(max(r["h"] - r["l"], abs(r["h"] - pc), abs(r["l"] - pc))
                   / r["c"])
    return statistics.mean(trs) if trs else None


def _regime(vwap_dev, priced_move, pm_hist) -> str:
    """Coarse classifier. Deliberately simple; slices are train-only anyway."""
    if priced_move and pm_hist and len(pm_hist) >= 20:
        srt = sorted(pm_hist)
        if priced_move > srt[int(len(srt) * 0.8)]:
            return "high-vol"
    if vwap_dev is not None and abs(vwap_dev) > 0.004:
        return "trend"
    return "chop"


def nearest_expiry(chain, today: str, dte_max: int = 2) -> str | None:
    from datetime import date
    try:
        t = date.fromisoformat(today)
    except ValueError:
        return None
    for e in sorted({c["expiry"] for c in chain if c["expiry"]}):
        try:
            dte = (date.fromisoformat(e) - t).days
        except ValueError:
            continue
        if 0 <= dte <= dte_max:
            return e
    return None


def compute_features(conn, symbol: str, ts: str) -> dict | None:
    u = db.underlying_at(conn, symbol, ts)
    chain = db.chain_at(conn, symbol, ts)
    if not u or not u["last"]:
        return None
    spot = u["last"]
    day = cal.trading_day_of(ts)
    day_rows = db.underlying_between(conn, symbol, f"{day}T00:00:00Z", ts)
    expiry = nearest_expiry(chain, day)

    # ── price-space (Phase 1) ───────────────────────────────────────────
    straddle = signals.straddle_mid(chain, spot, expiry) if expiry else None
    pm = (straddle / spot) if (straddle and spot) else None
    rm30 = signals.realized_move(conn, symbol, ts, 30)
    vrp_price = (pm - rm30) if (pm is not None and rm30 is not None) else None
    skew_price = signals.skew_px(chain, spot, expiry) if expiry else None

    # ── vendor-derived (stored if available, never required) ────────────
    iv_near = _atm_iv(chain, spot, expiry) if expiry else None
    expiries = sorted({c["expiry"] for c in chain if c["expiry"]})
    iv_next = (_atm_iv(chain, spot, expiries[1])
               if len(expiries) > 1 else None)
    rv5 = _realized_vol(day_rows, 5, ts)
    rv30 = _realized_vol(day_rows, 30, ts)

    hist = db.features_history(conn, symbol, ts, limit=20000)
    iv_hist = [r["iv30"] for r in hist if r["iv30"]]
    pm_hist = [r["priced_move"] for r in hist if r["priced_move"]]
    iv_rank = iv_pctile = None
    if iv_near and len(iv_hist) >= 20:
        lo, hi = min(iv_hist), max(iv_hist)
        iv_rank = (iv_near - lo) / (hi - lo) if hi > lo else 0.5
        iv_pctile = sum(1 for x in iv_hist if x < iv_near) / len(iv_hist)

    # ── opening range ───────────────────────────────────────────────────
    orb_high = orb_low = orb_broken = None
    mins = cal.minutes_since_open(ts)
    if mins is not None and mins >= 0:
        orb_rows = [r for r in day_rows
                    if (m := cal.minutes_since_open(r["event_ts"])) is not None
                    and 0 <= m <= 15 and r["last"]]
        if orb_rows:
            orb_high = max(r["last"] for r in orb_rows)
            orb_low = min(r["last"] for r in orb_rows)
            if mins > 15:
                orb_broken = ("UP" if spot > orb_high
                              else "DOWN" if spot < orb_low else None)

    vwap_dev = ((spot - u["vwap"]) / u["vwap"]) if u["vwap"] else None
    gate = signals.gate_mode(chain)

    row = {
        "ts": ts, "symbol": symbol,
        "straddle_mid": straddle, "priced_move": pm,
        "realized_move_30m": rm30, "vrp_px": vrp_price, "skew_px": skew_price,
        "iv30": iv_near, "iv_rank": iv_rank, "iv_percentile": iv_pctile,
        "realized_vol_5m": rv5, "realized_vol_30m": rv30,
        "vrp": ((iv_near - rv30) if (iv_near and rv30) else None),
        "skew_25d": _skew_25d(chain, expiry) if expiry else None,
        "term_slope": ((iv_next - iv_near) if (iv_near and iv_next) else None),
        "orb_high": orb_high, "orb_low": orb_low, "orb_broken": orb_broken,
        "vwap_dev": vwap_dev, "atr_pct": _atr_pct(conn, symbol, ts),
        "liquidity_score": signals.liquidity_score(chain, spot),
        "gate_mode": gate,
        "regime": _regime(vwap_dev, pm, pm_hist),
        "delay_class": u["delay_class"],
    }
    cols = list(row.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO features ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [row[c] for c in cols])
    conn.commit()
    return row
