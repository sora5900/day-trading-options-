"""Negative controls — the pipeline's own instrumentation.

These are not strategies. They are measurement devices, and they are the
reason any positive result elsewhere can be believed.

C1 (ORB) detects LEAKAGE: it has no mechanical basis, so a strong unseen
result means look-ahead or fill optimism, not an edge.

C2 (random entry) measures COST: with no trigger at all, its expected value
IS the round-trip friction. If C2 reads about zero or positive, the fill
engine is understating costs and every other verdict in the system is void.
C2 also supplies the MEASURED friction that H3-P1's stage-2 hurdle uses
instead of an assumed figure — and per PHASE1.md §0 it converges in ~30
trades, far faster than any edge estimate.
"""

import hashlib
from dataclasses import dataclass

from .. import market_calendar as cal, signals
from ..capabilities import PHASE1
from ..fill_engine import Leg
from .base import Candidate, DecisionContext, ExitRules, Hypothesis, Params, register


@dataclass(frozen=True)
class OrbParams(Params):
    range_min: int = 15
    earliest_min: int = 16
    latest_min: int = 120
    buffer_pct: float = 0.0005
    width_strikes: int = 2
    target_pct: float = 0.50
    stop_pct: float = 0.40
    time_stop_et: str = "15:45"


@register
class C1OrbControl(Hypothesis):
    """C1 — Opening Range Breakout, retained STRICTLY as a negative control.

    Its stated rationale ("overnight information is absorbed in the first 15
    minutes") is chart-pattern reasoning, not a mechanism. Expected result is
    approximately -friction. Never tuned; a positive unseen result halts the
    pipeline as evidence of a bug.
    """
    id = "c1_orb_control"
    version = "v1"
    family = "control"
    profile = PHASE1
    params = OrbParams()
    is_control = True

    def detect(self, ctx: DecisionContext) -> Candidate | None:
        p = self.params
        mins = cal.minutes_since_open(ctx.ts)
        if mins is None or not (p.earliest_min <= mins <= p.latest_min):
            return None
        if not ctx.expiry:
            return None
        broken = ctx.features.get("orb_broken")
        hi, lo = ctx.features.get("orb_high"), ctx.features.get("orb_low")
        if not broken or hi is None or lo is None:
            return None
        buf = ctx.spot * p.buffer_pct
        if broken == "UP" and ctx.spot < hi + buf:
            return None
        if broken == "DOWN" and ctx.spot > lo - buf:
            return None

        right = "C" if broken == "UP" else "P"
        k_long = signals.strike_by_straddle_multiple(
            ctx.chain, ctx.spot, ctx.expiry, right, k=0.0)
        if k_long is None:
            return None
        k_short = signals.strike_offset(
            ctx.chain, ctx.expiry, right, k_long,
            p.width_strikes * 1.0)
        if k_short is None:
            return None
        return Candidate(
            symbol=ctx.symbol, hypothesis_id=self.id, version=self.version,
            direction="LONG" if broken == "UP" else "SHORT",
            structure=("call_debit_vertical" if right == "C"
                       else "put_debit_vertical"),
            legs=[Leg(ctx.expiry, k_long, right, +1),
                  Leg(ctx.expiry, k_short, right, -1)],
            exit_rules=ExitRules(p.target_pct, p.stop_pct, p.time_stop_et),
            rationale=f"NEGATIVE CONTROL: ORB break {broken} at {ctx.spot:.2f}",
            confidence=0.0,
            signals={"orb_broken": broken, "orb_high": hi, "orb_low": lo})


@dataclass(frozen=True)
class RandomParams(Params):
    seed: str = "phase1-c2-2026"
    fire_probability: float = 1.0     # fire on every eligible day
    entry_et: str = "12:00"
    entry_window_min: int = 15
    straddle_k: float = 1.25
    width_dollars: float = 2.00
    target_pct: float = 0.50
    stop_pct: float = 2.00
    time_stop_et: str = "15:45"


@register
class C2RandomCostControl(Hypothesis):
    """C2 — seeded random-entry cost control.

    Structurally identical to H1-P1 but with NO trigger: it fires on a
    deterministic pseudo-random schedule derived from (seed, symbol, date), so
    runs are exactly reproducible. Its expected value is the round-trip
    friction, with a negative sign.

    This is the direct validation of the fill engine. Interpretation:
      EV clearly negative, near modelled friction  → cost model healthy
      EV about zero or positive                    → costs understated;
                                                     ALL verdicts are void
    """
    id = "c2_random_cost"
    version = "v1"
    family = "control"
    profile = PHASE1
    params = RandomParams()
    is_control = True
    measures_friction = True

    def detect(self, ctx: DecisionContext) -> Candidate | None:
        p = self.params
        if not ctx.expiry or not self._in_entry_window(ctx.ts):
            return None
        if not self._fires(ctx):
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
            rationale="COST CONTROL: untriggered entry; EV should be -friction",
            confidence=0.0,
            signals={"straddle_mid": straddle})

    def _fires(self, ctx: DecisionContext) -> bool:
        """Deterministic per (seed, symbol, trading day) — reproducible across
        runs and replays, which `random` would not be."""
        day = cal.trading_day_of(ctx.ts)
        h = hashlib.sha256(
            f"{self.params.seed}|{ctx.symbol}|{day}".encode()).hexdigest()
        draw = int(h[:8], 16) / 0xFFFFFFFF
        return draw < self.params.fire_probability

    def _in_entry_window(self, ts: str) -> bool:
        et = cal.to_et(ts)
        h, m = map(int, self.params.entry_et.split(":"))
        now = et.hour * 60 + et.minute
        return h * 60 + m <= now <= h * 60 + m + self.params.entry_window_min
