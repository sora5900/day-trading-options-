"""Price-space signal layer (PHASE1.md §1).

Every estimator here is derived from OBSERVED prices — no vendor Greeks, no
vendor implied volatility, no local option-pricing model. This is what removes
the premium-tier dependency, and it has an independent virtue: a straddle mid
is observed, while vendor IV is computed under someone else's model
assumptions.

Shared by all hypothesis families. Adding a new family should mean writing a
trigger on top of these, not re-deriving them.
"""

import statistics

from . import db, market_calendar as cal


# ── the priced move (replaces implied volatility) ───────────────────────────

def atm_legs(chain, spot: float, expiry: str) -> dict:
    """Nearest-to-spot call and put with two-sided quotes, same expiry."""
    legs = {}
    for right in ("C", "P"):
        cands = [c for c in chain
                 if c["expiry"] == expiry and c["right"] == right
                 and c["bid"] and c["ask"] and c["ask"] >= c["bid"]]
        if cands:
            legs[right] = min(cands, key=lambda c: abs(c["strike"] - spot))
    return legs


def mid(contract) -> float | None:
    if not contract or not contract["bid"] or not contract["ask"]:
        return None
    if contract["ask"] < contract["bid"]:
        return None
    return (contract["bid"] + contract["ask"]) / 2


def straddle_mid(chain, spot: float, expiry: str) -> float | None:
    """ATM straddle mid — the market's model-free breakeven move for `expiry`."""
    legs = atm_legs(chain, spot, expiry)
    if len(legs) < 2:
        return None
    c, p = mid(legs["C"]), mid(legs["P"])
    if c is None or p is None:
        return None
    return c + p


def priced_move(chain, spot: float, expiry: str) -> float | None:
    """Straddle mid as a fraction of spot. The IV replacement."""
    s = straddle_mid(chain, spot, expiry)
    return (s / spot) if (s and spot) else None


def realized_move(conn, symbol: str, as_of: str, window_min: int = 30
                  ) -> float | None:
    """|move| over the trailing window, as a fraction. Bounded by `as_of`."""
    day = cal.trading_day_of(as_of)
    rows = db.underlying_between(conn, symbol, f"{day}T00:00:00Z", as_of)
    if len(rows) < 2:
        return None
    end = rows[-1]["last"]
    cutoff = cal.parse_ts(as_of).timestamp() - window_min * 60
    prior = [r for r in rows
             if cal.parse_ts(r["event_ts"]).timestamp() <= cutoff and r["last"]]
    start = (prior[-1]["last"] if prior else rows[0]["last"])
    if not (start and end and start > 0):
        return None
    return abs(end - start) / start


def vrp_px(chain, conn, symbol: str, spot: float, expiry: str, as_of: str,
           window_min: int = 30) -> float | None:
    """Price-space variance risk premium: what was charged minus what happened."""
    pm = priced_move(chain, spot, expiry)
    rm = realized_move(conn, symbol, as_of, window_min)
    if pm is None or rm is None:
        return None
    return pm - rm


# ── skew (replaces 25-delta skew) ───────────────────────────────────────────

def skew_px(chain, spot: float, expiry: str, moneyness: float = 0.01
            ) -> float | None:
    """Risk-reversal price at equidistant moneyness, normalized by straddle.

    Positive → the equidistant put is richer than the call: hedging-demand
    premium, measured in dollars rather than vol points.
    """
    target_p, target_c = spot * (1 - moneyness), spot * (1 + moneyness)
    puts = [c for c in chain if c["expiry"] == expiry and c["right"] == "P"
            and mid(c) is not None]
    calls = [c for c in chain if c["expiry"] == expiry and c["right"] == "C"
             and mid(c) is not None]
    if not puts or not calls:
        return None
    p = min(puts, key=lambda c: abs(c["strike"] - target_p))
    c = min(calls, key=lambda x: abs(x["strike"] - target_c))
    # require both to be reasonably close to the intended moneyness
    if abs(p["strike"] - target_p) / spot > moneyness * 0.5:
        return None
    if abs(c["strike"] - target_c) / spot > moneyness * 0.5:
        return None
    s = straddle_mid(chain, spot, expiry)
    if not s or s <= 0:
        return None
    return (mid(p) - mid(c)) / s


# ── strike selection (replaces delta selection) ─────────────────────────────

def strike_by_straddle_multiple(chain, spot: float, expiry: str, right: str,
                                k: float, straddle: float | None = None
                                ) -> float | None:
    """Nearest listed strike to `spot -/+ k * straddle_mid`.

    An ATM straddle prices roughly 0.8 SD of the expiry move, so k=1.25 is
    about 1 SD (~15-16 delta equivalent). Adapts to volatility the way delta
    selection does — wider straddle pushes the strike further out — using only
    observed prices. NOT claimed equivalent to delta selection.
    """
    s = straddle if straddle is not None else straddle_mid(chain, spot, expiry)
    if not s or s <= 0:
        return None
    target = spot - k * s if right == "P" else spot + k * s
    strikes = sorted({c["strike"] for c in chain
                      if c["expiry"] == expiry and c["right"] == right})
    if not strikes:
        return None
    return min(strikes, key=lambda x: abs(x - target))


def strike_offset(chain, expiry: str, right: str, from_strike: float,
                  dollars: float) -> float | None:
    """Nearest listed strike `dollars` further OTM than `from_strike`."""
    target = from_strike - dollars if right == "P" else from_strike + dollars
    strikes = sorted({c["strike"] for c in chain
                      if c["expiry"] == expiry and c["right"] == right})
    if not strikes:
        return None
    best = min(strikes, key=lambda x: abs(x - target))
    return best if best != from_strike else None


# ── time-of-day-conditional percentile ──────────────────────────────────────

def tod_percentile(history, value: float, as_of: str, column: str,
                   tolerance_min: int = 20) -> tuple[float | None, int]:
    """Percentile of `value` against prior observations at the SAME time of day.

    0DTE priced move rises mechanically toward expiry, so ranking a noon
    reading against all-day readings would fire on a clock artifact rather
    than a signal. Returns (percentile, n_observations).
    """
    target = cal.to_et(as_of)
    target_min = target.hour * 60 + target.minute
    vals = []
    for row in history:
        v = row[column]
        if v is None:
            continue
        try:
            et = cal.to_et(row["ts"])
        except (ValueError, TypeError):
            continue
        m = et.hour * 60 + et.minute
        if abs(m - target_min) <= tolerance_min:
            vals.append(v)
    if not vals:
        return None, 0
    return sum(1 for v in vals if v < value) / len(vals), len(vals)


# ── adaptive liquidity gate (PHASE1.md §1) ──────────────────────────────────

GATE_OI_VOLUME = "oi_volume"
GATE_SPREAD_ONLY = "spread_only"


def gate_mode(chain) -> str:
    """Which liquidity gate the data actually supports.

    Degrades rather than blocking, but the mode is recorded on every trade so
    a weaker gate can never pass unnoticed.
    """
    if not chain:
        return GATE_SPREAD_ONLY
    n = len(chain)
    oi = sum(1 for c in chain if c["open_interest"] is not None) / n
    vol = sum(1 for c in chain if c["volume"] is not None) / n
    return GATE_OI_VOLUME if (oi >= 0.8 and vol >= 0.5) else GATE_SPREAD_ONLY


def passes_gate(contract, mode: str, spread_max: float = 0.08,
                spread_max_degraded: float = 0.06,
                min_oi: int = 500) -> bool:
    m = mid(contract)
    if m is None or m <= 0:
        return False
    spread = (contract["ask"] - contract["bid"]) / m
    if mode == GATE_OI_VOLUME:
        return (spread <= spread_max
                and (contract["open_interest"] or 0) >= min_oi
                and (contract["volume"] or 0) > 0)
    # degraded: tighten the spread requirement to compensate for the
    # liquidity evidence we no longer have
    return spread <= spread_max_degraded


def liquidity_score(chain, spot: float) -> float | None:
    """Median spread% across near-ATM contracts — the hurdle any edge must beat."""
    spreads = []
    for c in chain:
        if not spot or abs(c["strike"] - spot) / spot > 0.01:
            continue
        m = mid(c)
        if m and m > 0:
            spreads.append((c["ask"] - c["bid"]) / m)
    return statistics.median(spreads) if spreads else None
