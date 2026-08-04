"""Measure how rich index implied volatility actually is.

This is the H1-P1 equivalent of the H3-P1 stage-1 test: establish the SIZE of
the claimed effect on cheap data, before paying for the data needed to trade
it. The friction arithmetic in PHASE1.md says a 0DTE credit spread only
survives if index options are around 20% rich; at 8-13% it loses at every
friction level. So the size is the whole question.

Method — deliberately model-free and daily, so it needs no intraday data and
no quotes:

    priced_move(D)   = ATM straddle close on day D for the expiry at D+1,
                       divided by spot(D)         ← what the market charged
    realized_move(D) = |spot(D+1) - spot(D)| / spot(D)   ← what happened
    vrp(D)           = priced_move(D) - realized_move(D)
    richness         = mean(vrp) / mean(priced_move)

**Hard boundary.** These are CLOSE prices from trade aggregates, not quotes.
They measure a market-wide premium; they may never touch the fill engine.
Using them to simulate entries and exits would be mid-price backtesting —
precisely the error the spec identifies as why retail options strategies work
in testing and lose in production.
"""

import math
import statistics

from .. import market_calendar as cal
from .intraday_momentum import bootstrap_ci


def _spot_closes(conn, symbol: str) -> dict:
    """{trading_day: regular-session close} from backfilled underlying bars."""
    rows = conn.execute(
        "SELECT event_ts, close FROM underlying_bars WHERE symbol=? "
        "ORDER BY event_ts ASC", (symbol,)).fetchall()
    out = {}
    for r in rows:
        if r["close"] is None:
            continue
        mins = cal.minutes_since_open(r["event_ts"])
        if mins is None or not (0 <= mins < 390):
            continue                       # regular session only
        out[cal.trading_day_of(r["event_ts"])] = r["close"]
    return out


def straddle_closes(conn, symbol: str) -> dict:
    """{(trade_day, expiry): straddle_close} from stored option bars."""
    rows = conn.execute(
        "SELECT event_ts, expiry, strike, right, close FROM option_bars "
        "WHERE symbol=? AND close IS NOT NULL", (symbol,)).fetchall()
    legs: dict = {}
    for r in rows:
        day = cal.trading_day_of(r["event_ts"])
        legs.setdefault((day, r["expiry"], r["strike"]), {})[r["right"]] = \
            r["close"]
    out = {}
    for (day, expiry, strike), sides in legs.items():
        if "C" in sides and "P" in sides:
            out[(day, expiry, strike)] = sides["C"] + sides["P"]
    return out


def observations(conn, symbol: str) -> list:
    """One (day, priced_move, realized_move, vrp) per usable day."""
    spots = _spot_closes(conn, symbol)
    straddles = straddle_closes(conn, symbol)
    days = sorted(spots)
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}

    out = []
    for (day, expiry, strike), straddle in sorted(straddles.items()):
        spot = spots.get(day)
        follow = nxt.get(day)
        if spot is None or follow is None or expiry != follow:
            continue                       # only the D -> D+1 expiry
        spot_next = spots.get(follow)
        if spot_next is None or spot <= 0 or straddle <= 0:
            continue
        # only use strikes genuinely near the money
        if abs(strike - spot) / spot > 0.005:
            continue
        priced = straddle / spot
        realized = abs(spot_next - spot) / spot
        out.append({"day": day, "expiry": expiry, "strike": strike,
                    "spot": spot, "straddle": straddle,
                    "priced_move": priced, "realized_move": realized,
                    "vrp": priced - realized})
    return out


def run(conn, symbol: str = "SPY") -> dict:
    obs = observations(conn, symbol)
    if len(obs) < 30:
        return {"symbol": symbol, "n_days": len(obs),
                "error": "need >= 30 usable days; run `vrp-backfill` first"}

    priced = [o["priced_move"] for o in obs]
    realized = [o["realized_move"] for o in obs]
    vrp = [o["vrp"] for o in obs]
    vrp_bps = [v * 10_000 for v in vrp]

    lo, hi = bootstrap_ci(vrp_bps, seed="vrp")
    mean_priced = statistics.mean(priced)
    mean_vrp = statistics.mean(vrp)
    richness = mean_vrp / mean_priced if mean_priced else None

    sd = statistics.pstdev(vrp_bps)
    mde = (1.96 + 0.8416) * sd / math.sqrt(len(vrp_bps))
    win = sum(1 for o in obs if o["vrp"] > 0) / len(obs)

    return {
        "symbol": symbol, "n_days": len(obs),
        "first_day": obs[0]["day"], "last_day": obs[-1]["day"],
        "mean_priced_move_pct": round(mean_priced * 100, 4),
        "mean_realized_move_pct": round(statistics.mean(realized) * 100, 4),
        "mean_vrp_bps": round(mean_vrp * 10_000, 2),
        "vrp_ci_bps": (round(lo, 2) if lo is not None else None,
                       round(hi, 2) if hi is not None else None),
        "richness": round(richness, 4) if richness is not None else None,
        "days_premium_positive": round(win, 4),
        "mde_bps": round(mde, 2),
        "significant": bool(lo is not None and (lo > 0 or hi < 0)),
    }


VIABILITY_THRESHOLD = 0.20      # PHASE1.md: only survives if ~20% rich


def render(res: dict) -> str:
    if "error" in res:
        return (f"VRP MAGNITUDE ({res['symbol']}): {res['error']} "
                f"(have {res.get('n_days')} days)")
    w = 74
    r = res["richness"]
    out = ["=" * w,
           f"VRP MAGNITUDE — {res['symbol']}, 1-day ATM straddle "
           f"(close prices, NOT quotes)",
           "=" * w,
           f"  days          {res['n_days']}  ({res['first_day']} .. "
           f"{res['last_day']})",
           "",
           f"  market charged  {res['mean_priced_move_pct']:.3f}% "
           f"average expected move",
           f"  market realized {res['mean_realized_move_pct']:.3f}% "
           f"average actual move",
           f"  premium         {res['mean_vrp_bps']:+.2f} bps  "
           f"95% CI [{res['vrp_ci_bps'][0]:+.2f}, {res['vrp_ci_bps'][1]:+.2f}]",
           f"  minimum detectable at this n: {res['mde_bps']:.2f} bps",
           "",
           f"  RICHNESS        {r:.1%} of the premium was excess"
           if r is not None else "  RICHNESS        —",
           f"  days premium positive: {res['days_premium_positive']:.1%}  "
           f"← vanity metric",
           ""]
    if r is None:
        out.append("  VERDICT: not computable.")
    elif not res["significant"]:
        out.append("  VERDICT: premium NOT distinguishable from zero at this "
                   "sample size.")
        out.append("           A null here is not proof of absence — compare "
                   "the minimum")
        out.append("           detectable edge above against what you "
                   "expected to find.")
    elif r >= VIABILITY_THRESHOLD:
        out.append(f"  VERDICT: {r:.1%} rich, at or above the ~{VIABILITY_THRESHOLD:.0%} "
                   f"the friction arithmetic requires.")
        out.append("           H1-P1 is worth pursuing WITH real quote data. "
                   "This measurement")
        out.append("           cannot model fills and does not substitute for "
                   "that test.")
    else:
        out.append(f"  VERDICT: only {r:.1%} rich, below the ~{VIABILITY_THRESHOLD:.0%} "
                   f"the friction arithmetic")
        out.append("           requires. On these numbers a 0DTE credit "
                   "spread loses to costs")
        out.append("           at every friction level in PHASE1.md — before "
                   "any tail risk.")
    out += ["",
            "  Close prices measure a market-wide premium. They may NEVER",
            "  reach the fill engine: that would be mid-price backtesting.",
            "=" * w]
    return "\n".join(out)
