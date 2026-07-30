"""H3-P1 — Market intraday momentum, staged.

Economic claim: the first half-hour return predicts the last half-hour return
in index ETFs (Gao, Han, Li & Zhou, JFE 2018) — informed-trader activity
clusters at the open and the close. Published, out-of-sample validated,
replicated internationally.

STAGED, and stage 1 touches no options at all. Because it needs only
underlying bars, stage 1 is the only Phase 1 component that can reach
statistical significance quickly on cheap data — see PHASE1.md §0. Stage 1
lives in `analysis/intraday_momentum.py`; the options mapping below is
STAGE 3 and must not run until the stage-2 friction hurdle clears against
C2's MEASURED friction, not an assumed figure.
"""

from dataclasses import dataclass

from .. import market_calendar as cal, signals
from ..capabilities import PHASE1
from ..fill_engine import Leg
from .base import Candidate, DecisionContext, ExitRules, Hypothesis, Params, register


@dataclass(frozen=True)
class MomentumParams(Params):
    r1_start_et: str = "09:30"
    r1_end_et: str = "10:00"
    entry_et: str = "15:30"
    entry_window_min: int = 5
    exit_et: str = "15:58"            # ATM 0DTE cannot be held to settlement
    min_r1_abs: float = 0.0010        # ignore noise-sized first-half-hour moves
    width_strikes: int = 2
    #: Stage 3 gate. Must be flipped only by a recorded stage-2 pass.
    stage2_cleared: bool = False


@register
class H3P1Momentum(Hypothesis):
    """H3-P1 stage 3 — intraday-momentum continuation as a 0DTE debit vertical.

    Gated: emits nothing until `stage2_cleared` is set by a recorded stage-2
    friction-hurdle pass against C2's MEASURED friction. Stage 1 (the
    underlying-only reproduction) runs outside this class and needs no options
    data at all, which is why it is Phase 1's highest-priority test.
    """
    id = "h3_p1_momentum"
    version = "v1"
    family = "intraday_momentum"
    profile = PHASE1
    params = MomentumParams()

    def detect(self, ctx: DecisionContext) -> Candidate | None:
        p = self.params
        # Stage 3 is gated on stage 2. Until the friction hurdle is cleared
        # against measured friction, this hypothesis deliberately emits
        # nothing and the options test set is never opened.
        if not p.stage2_cleared:
            return None
        if not ctx.expiry or not self._in_entry_window(ctx.ts):
            return None

        r1 = self._first_half_hour_return(ctx)
        if r1 is None or abs(r1) < p.min_r1_abs:
            return None

        right = "C" if r1 > 0 else "P"
        k_long = signals.strike_by_straddle_multiple(
            ctx.chain, ctx.spot, ctx.expiry, right, k=0.0)
        if k_long is None:
            return None
        strikes = sorted({c["strike"] for c in ctx.chain
                          if c["expiry"] == ctx.expiry and c["right"] == right})
        try:
            i = strikes.index(k_long)
        except ValueError:
            return None
        j = i + p.width_strikes if right == "C" else i - p.width_strikes
        if not (0 <= j < len(strikes)):
            return None
        k_short = strikes[j]

        return Candidate(
            symbol=ctx.symbol, hypothesis_id=self.id, version=self.version,
            direction="LONG" if r1 > 0 else "SHORT",
            structure=("call_debit_vertical" if right == "C"
                       else "put_debit_vertical"),
            legs=[Leg(ctx.expiry, k_long, right, +1),
                  Leg(ctx.expiry, k_short, right, -1)],
            exit_rules=ExitRules(None, None, p.exit_et),
            rationale=(f"first-half-hour return {r1:+.4f}; intraday-momentum "
                       f"continuation into the close"),
            confidence=0.35,
            signals={"r1": r1})

    def _in_entry_window(self, ts: str) -> bool:
        et = cal.to_et(ts)
        h, m = map(int, self.params.entry_et.split(":"))
        now = et.hour * 60 + et.minute
        return h * 60 + m <= now <= h * 60 + m + self.params.entry_window_min

    def _first_half_hour_return(self, ctx: DecisionContext) -> float | None:
        from .. import db
        day = cal.trading_day_of(ctx.ts)
        rows = db.underlying_between(ctx.conn, ctx.symbol,
                                     f"{day}T00:00:00Z", ctx.ts)
        opens, closes = [], []
        for r in rows:
            mins = cal.minutes_since_open(r["event_ts"])
            if mins is None or not r["last"]:
                continue
            if 0 <= mins <= 1:
                opens.append(r["last"])
            if 29 <= mins <= 31:
                closes.append(r["last"])
        if not opens or not closes:
            return None
        o, c = opens[0], closes[-1]
        return (c - o) / o if o else None
