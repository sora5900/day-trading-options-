"""Strategy 2 — Intraday Volatility-Premium Fade, defined-risk CREDIT SPREAD
(spec §6.2).

Mechanical rationale: implied vol systematically exceeds subsequent realized
vol (the volatility risk premium). When intraday IV spikes without a matching
realized move, that gap is the tradable object.

WARNING baked into the kill criterion: this will show a seductive 70-80% win
rate even when it loses money (the Close-Out 2-0 trap from tt-agent: 93% win
rate, still -EV). Grade it on net P&L and worst-day drawdown, never win rate.
"""

from .. import db, market_calendar as cal
from ..fill_engine import Leg
from .base import (Strategy, Candidate, ExitRules, nearest_expiry,
                   strike_by_delta, nearest_strike)


class VrpFade(Strategy):
    name = "vrp_fade"
    version = "v1"

    VRP_PCTILE = 0.80         # vrp above its own trailing 80th percentile
    MIN_HISTORY = 60          # need enough trailing vrp observations to rank
    SHORT_DELTA = 0.18        # 15-20 delta short leg
    WING_STRIKES = 2          # long wing this many strikes further out
    DTE_MAX = 2
    EARLIEST_MIN = 45         # let the open settle before selling premium
    LATEST_ENTRY_MIN = 300

    def detect(self, conn, symbol, ts, features, chain):
        mins = cal.minutes_since_open(ts)
        if mins is None or not (self.EARLIEST_MIN <= mins <= self.LATEST_ENTRY_MIN):
            return None
        vrp = features.get("vrp")
        if vrp is None or vrp <= 0:
            return None
        # no trend break in progress: price inside the opening range
        if features.get("orb_broken"):
            return None
        hist = db.features_history(conn, symbol, ts, limit=5000)
        vrp_hist = [r["vrp"] for r in hist if r["vrp"] is not None]
        if len(vrp_hist) < self.MIN_HISTORY:
            return None
        pctile = sum(1 for x in vrp_hist if x < vrp) / len(vrp_hist)
        if pctile < self.VRP_PCTILE:
            return None

        u = db.underlying_at(conn, symbol, ts)
        if not u or not u["last"]:
            return None
        spot = u["last"]
        today = cal.trading_day_of(ts)
        expiry = nearest_expiry(chain, self.DTE_MAX, today)
        if not expiry:
            return None
        exp_chain = [c for c in chain if c["expiry"] == expiry]

        # v1 mechanical side choice: sell the put spread when price sits in the
        # upper half of the day's range, the call spread in the lower half —
        # fade the wing price is moving away from. Versioned choice; if the
        # data disagrees, v2 tests the alternative on a fresh split.
        hi, lo = u["day_high"], u["day_low"]
        upper_half = not (hi and lo and hi > lo) or \
            (spot - lo) / (hi - lo) >= 0.5
        right = "P" if upper_half else "C"
        k_short = strike_by_delta(exp_chain, expiry, right, self.SHORT_DELTA)
        if k_short is None:
            return None
        k_wing = nearest_strike(
            [c for c in exp_chain if c["right"] == right], k_short, right,
            offset=self.WING_STRIKES)
        if k_wing is None or k_wing == k_short:
            return None
        return Candidate(
            symbol=symbol, strategy=self.name, version=self.version,
            direction="NEUTRAL",
            structure=("put_credit_spread" if right == "P"
                       else "call_credit_spread"),
            legs=[Leg(expiry, k_short, right, -1), Leg(expiry, k_wing, right, +1)],
            exit_rules=ExitRules(target_pct=0.50, stop_pct=2.0,
                                 time_stop_et="15:45"),
            rationale=(f"VRP {vrp:.3f} at {pctile:.0%} of own trailing history "
                       f"({len(vrp_hist)} obs), price inside opening range; "
                       f"selling {right} spread {k_short}/{k_wing} {expiry}"),
            confidence=0.5,
            features_json=self.pack_features(features))
