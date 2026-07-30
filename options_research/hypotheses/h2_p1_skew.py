"""H2-P1 — Price-space conditional VRP (skew-spike trigger).

Economic claim: the SAME risk premium as H1-P1, timed better. Put skew
reflects hedging demand, and hedging demand spikes at local fear extremes —
which is when insurance is most overbid.

This is explicitly NOT an independent edge. It is H1-P1 with a different
trigger, and the only question it must answer is whether the conditioning adds
anything over H1-P1 on the same days (see PHASE1.md §3). Fewer trades of the
same thing is not an improvement.
"""

from dataclasses import dataclass

from .. import db, market_calendar as cal, signals
from ..capabilities import PHASE1
from ..fill_engine import Leg
from .base import Candidate, DecisionContext, ExitRules, Hypothesis, Params, register


@dataclass(frozen=True)
class SkewParams(Params):
    window_start_et: str = "10:30"
    window_end_et: str = "14:30"
    skew_percentile: float = 0.85
    min_history: int = 60
    moneyness: float = 0.01
    straddle_k: float = 1.25
    width_dollars: float = 2.00
    min_credit_frac_of_width: float = 0.15
    target_pct: float = 0.50
    stop_pct: float = 2.00
    time_stop_et: str = "15:45"


@register
class H2P1Skew(Hypothesis):
    """H2-P1 — conditional VRP, triggered by a hedging-demand (skew) spike.

    Same mechanism and same structure as H1-P1, entered only when the
    equidistant put is unusually rich relative to the call for this time of
    day. Not an independent edge: its sole job is to beat H1-P1 on shared
    days, and it is discarded if the paired difference spans zero.
    """
    id = "h2_p1_skew"
    version = "v1"
    family = "variance_risk_premium"
    profile = PHASE1
    params = SkewParams()

    def detect(self, ctx: DecisionContext) -> Candidate | None:
        p = self.params
        if not ctx.expiry or not self._in_window(ctx.ts):
            return None

        skew = ctx.features.get("skew_px")
        vrp = ctx.features.get("vrp_px")
        if skew is None or vrp is None or vrp <= 0:
            return None

        hist = db.features_history(ctx.conn, ctx.symbol, ctx.ts, limit=20000)
        pctile, n_obs = signals.tod_percentile(hist, skew, ctx.ts, "skew_px")
        if pctile is None or n_obs < p.min_history \
                or pctile < p.skew_percentile:
            return None

        straddle = signals.straddle_mid(ctx.chain, ctx.spot, ctx.expiry)
        if not straddle:
            return None
        k_short = signals.strike_by_straddle_multiple(
            ctx.chain, ctx.spot, ctx.expiry, "P", p.straddle_k, straddle)
        if k_short is None:
            return None
        k_wing = signals.strike_offset(ctx.chain, ctx.expiry, "P", k_short,
                                       p.width_dollars)
        if k_wing is None:
            return None

        return Candidate(
            symbol=ctx.symbol, hypothesis_id=self.id, version=self.version,
            direction="NEUTRAL", structure="put_credit_spread",
            legs=[Leg(ctx.expiry, k_short, "P", -1),
                  Leg(ctx.expiry, k_wing, "P", +1)],
            exit_rules=ExitRules(p.target_pct, p.stop_pct, p.time_stop_et),
            rationale=(
                f"skew_px {skew:.4f} at {pctile:.0%} of {n_obs} same-time-of-day "
                f"observations (hedging-demand spike), vrp_px {vrp:.5f} > 0"),
            confidence=0.45,
            signals={"skew_px": skew, "skew_pctile": pctile, "n_obs": n_obs,
                     "vrp_px": vrp, "straddle_mid": straddle})

    def _in_window(self, ts: str) -> bool:
        et = cal.to_et(ts)
        now = et.hour * 60 + et.minute
        h1, m1 = map(int, self.params.window_start_et.split(":"))
        h2, m2 = map(int, self.params.window_end_et.split(":"))
        return h1 * 60 + m1 <= now <= h2 * 60 + m2
