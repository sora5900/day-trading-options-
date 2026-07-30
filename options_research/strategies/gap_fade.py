"""Strategy 3 — Overnight Gap Behaviour, directional, defined risk (spec §6.3).

Mechanical rationale: gaps in index ETFs have measurable fill/continuation
statistics conditioned on gap size and prior-day close position.

Two variants ship together on purpose: GapFadeAll is the naive "always fade
the gap" baseline; GapFadeConditioned adds the conditioning. The spec's kill
criterion for the conditioned version is that it must BEAT the naive baseline
on the test set — otherwise the conditioning adds nothing.
"""

from .. import db, market_calendar as cal
from ..fill_engine import Leg
from .base import Strategy, Candidate, ExitRules, nearest_strike, nearest_expiry


class GapFadeAll(Strategy):
    """Naive baseline: fade every qualifying gap toward prior close."""
    name = "gap_fade_all"
    version = "v1"

    MIN_GAP_ATR = 0.30        # ignore noise gaps
    MAX_GAP_ATR = 1.50        # huge gaps tend to trend, not fill
    ENTRY_AFTER_MIN = 5       # let the opening print settle
    ENTRY_BEFORE_MIN = 30
    DTE_MAX = 2

    def _gap_atr(self, u, features):
        atr_pct = features.get("atr_pct")
        if not (u and u["day_open"] and u["prev_close"] and atr_pct):
            return None
        atr_abs = atr_pct * u["prev_close"]
        if atr_abs <= 0:
            return None
        return (u["day_open"] - u["prev_close"]) / atr_abs

    def _passes_conditioning(self, conn, symbol, ts, u) -> bool:
        return True   # the naive baseline takes every qualifying gap

    def detect(self, conn, symbol, ts, features, chain):
        mins = cal.minutes_since_open(ts)
        if mins is None or not (self.ENTRY_AFTER_MIN <= mins <= self.ENTRY_BEFORE_MIN):
            return None
        u = db.underlying_at(conn, symbol, ts)
        gap_atr = self._gap_atr(u, features)
        if gap_atr is None or not (self.MIN_GAP_ATR <= abs(gap_atr) <= self.MAX_GAP_ATR):
            return None
        if not self._passes_conditioning(conn, symbol, ts, u):
            return None
        spot = u["last"]
        today = cal.trading_day_of(ts)
        expiry = nearest_expiry(chain, self.DTE_MAX, today)
        if not expiry:
            return None
        exp_chain = [c for c in chain if c["expiry"] == expiry]
        # gap up → fade down with puts; gap down → fade up with calls
        right = "P" if gap_atr > 0 else "C"
        k_long = nearest_strike(exp_chain, spot, right, offset=0)
        k_short = nearest_strike(exp_chain, spot, right, offset=2)
        if k_long is None or k_short is None or k_long == k_short:
            return None
        return Candidate(
            symbol=symbol, strategy=self.name, version=self.version,
            direction="SHORT" if gap_atr > 0 else "LONG",
            structure=("put_debit_vertical" if right == "P"
                       else "call_debit_vertical"),
            legs=[Leg(expiry, k_long, right, +1), Leg(expiry, k_short, right, -1)],
            exit_rules=ExitRules(target_pct=0.50, stop_pct=0.40,
                                 time_stop_et="11:30"),
            rationale=(f"Gap of {gap_atr:+.2f} ATR "
                       f"(open {u['day_open']:.2f} vs prev close "
                       f"{u['prev_close']:.2f}); fading toward fill"),
            confidence=0.4,
            features_json=self.pack_features(features))


class GapFadeConditioned(GapFadeAll):
    """Conditioned variant: only fade when the prior day CLOSED in the
    opposite third of its range from the gap direction — i.e. the gap is a
    reversal of, not a continuation of, prior-day momentum. Must beat the
    naive baseline on the test set or the conditioning is discarded."""
    name = "gap_fade_cond"
    version = "v1"

    def _passes_conditioning(self, conn, symbol, ts, u) -> bool:
        day = cal.trading_day_of(ts)
        prior = conn.execute(
            "SELECT MAX(day_high) AS h, MIN(day_low) AS l, MAX(last) AS c "
            "FROM underlying_snap WHERE symbol=? AND substr(ts,1,10) < ? "
            "AND ts < ? GROUP BY substr(ts,1,10) ORDER BY substr(ts,1,10) DESC "
            "LIMIT 1", (symbol, day, ts)).fetchone()
        if not prior or not (prior["h"] and prior["l"] and prior["c"]) \
                or prior["h"] <= prior["l"]:
            return False
        pos = (prior["c"] - prior["l"]) / (prior["h"] - prior["l"])
        gapped_up = u["day_open"] > u["prev_close"]
        # fade an up-gap only if prior day closed weak; a down-gap only if strong
        return pos <= 1 / 3 if gapped_up else pos >= 2 / 3
