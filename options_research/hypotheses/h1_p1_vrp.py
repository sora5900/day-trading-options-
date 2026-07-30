"""H1-P1 — Price-space variance risk premium, strike-selected.

Economic claim: implied volatility systematically exceeds subsequently
realized volatility in index options because it is a RISK PREMIUM paid to
insurance sellers, not a pricing error. It survives arbitrage because real
risk is transferred. Timing: intraday realized vol is U-shaped, so the midday
trough is where the price charged most overstates what is subsequently
realized.

Measured entirely in price space (PHASE1.md §1) so it needs no premium tier.
This is a NEW pre-registration, not an edit of the delta-based H1 — strike
selection by straddle multiple is a different rule and gets its own untouched
test period.
"""

from dataclasses import dataclass

from .. import market_calendar as cal, signals
from ..capabilities import PHASE1
from ..fill_engine import Leg
from .base import Candidate, DecisionContext, ExitRules, Hypothesis, Params, register


@dataclass(frozen=True)
class VrpParams(Params):
    entry_et: str = "12:00"
    entry_window_min: int = 15        # accept the first snapshot in [12:00, 12:15]
    vrp_percentile: float = 0.80
    min_history: int = 60             # trailing observations at this time of day
    vwap_gate_pct: float = 0.004
    straddle_k: float = 1.25          # ~1 SD ~ 15-16 delta equivalent
    width_dollars: float = 2.00
    min_credit_frac_of_width: float = 0.15
    target_pct: float = 0.50
    stop_pct: float = 2.00
    time_stop_et: str = "15:45"


@register
class H1P1Vrp(Hypothesis):
    """H1-P1 — price-space variance risk premium, 0DTE put credit spread.

    Sells index insurance at midday, when the intraday realized-vol U-shape
    puts the trough between the open and close, on days when the price charged
    for the move (ATM straddle) is high relative to what has recently been
    realized. The premium is compensation for bearing crash risk, which is why
    it is not arbitraged away.

    Expect a high win rate and treat it as irrelevant: the edge is positive EV
    after tail losses and every cost, or it is nothing.
    """
    id = "h1_p1_vrp"
    version = "v1"
    family = "variance_risk_premium"
    profile = PHASE1
    params = VrpParams()

    def detect(self, ctx: DecisionContext) -> Candidate | None:
        p = self.params
        mins = cal.minutes_since_open(ctx.ts)
        if mins is None or not ctx.expiry:
            return None
        if not self._in_entry_window(ctx.ts):
            return None

        vrp = ctx.features.get("vrp_px")
        if vrp is None or vrp <= 0:
            return None

        # rank against the SAME time of day only — 0DTE priced move rises
        # mechanically toward expiry, so an all-day ranking fires on the clock
        from .. import db
        hist = db.features_history(ctx.conn, ctx.symbol, ctx.ts, limit=20000)
        pctile, n_obs = signals.tod_percentile(hist, vrp, ctx.ts, "vrp_px")
        if pctile is None or n_obs < p.min_history or pctile < p.vrp_percentile:
            return None

        vwap_dev = ctx.features.get("vwap_dev")
        if vwap_dev is None or abs(vwap_dev) >= p.vwap_gate_pct:
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
                f"vrp_px {vrp:.5f} at {pctile:.0%} of {n_obs} same-time-of-day "
                f"observations; straddle {straddle:.2f}, short {k_short}P / "
                f"wing {k_wing}P {ctx.expiry}"),
            confidence=0.5,
            signals={"vrp_px": vrp, "vrp_pctile": pctile, "n_obs": n_obs,
                     "straddle_mid": straddle, "vwap_dev": vwap_dev,
                     "priced_move": ctx.features.get("priced_move"),
                     "realized_move_30m": ctx.features.get("realized_move_30m")})

    def _in_entry_window(self, ts: str) -> bool:
        et = cal.to_et(ts)
        h, m = map(int, self.params.entry_et.split(":"))
        now_min = et.hour * 60 + et.minute
        start = h * 60 + m
        return start <= now_min <= start + self.params.entry_window_min
