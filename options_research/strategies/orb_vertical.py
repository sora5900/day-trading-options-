"""Strategy 1 — Opening Range Breakout as a DEBIT VERTICAL (spec §6.1).

Mechanical rationale: overnight information gets absorbed in the first
15-30 minutes; a decisive break of that range with volume has documented
continuation. Verticals, not naked longs — they cut the theta and vega that
otherwise eat an intraday long option alive.

Kill criterion: test-set net P&L <= 0 after spread costs → discard.
"""

from .. import db, market_calendar as cal
from ..fill_engine import Leg
from .base import Strategy, Candidate, ExitRules, nearest_strike, nearest_expiry


class OrbVertical(Strategy):
    name = "orb_vertical"
    version = "v1"

    RANGE_MIN = 15            # 9:30-9:45 (a 9:30-10:00 variant = version bump)
    LATEST_ENTRY_MIN = 120    # no chasing breaks after 11:30 ET
    BUFFER_PCT = 0.0005       # break must clear range by 0.05% of spot
    VOL_MULT = 1.3            # interval volume vs trailing 15-min average
    DTE_MAX = 2

    def detect(self, conn, symbol, ts, features, chain):
        mins = cal.minutes_since_open(ts)
        if mins is None or mins <= self.RANGE_MIN or mins > self.LATEST_ENTRY_MIN:
            return None
        if not features.get("orb_broken") or features.get("orb_high") is None:
            return None
        u = db.underlying_at(conn, symbol, ts)
        if not u or not u["last"]:
            return None
        spot = u["last"]
        buffer = spot * self.BUFFER_PCT
        direction = features["orb_broken"]
        if direction == "UP" and spot < features["orb_high"] + buffer:
            return None
        if direction == "DOWN" and spot > features["orb_low"] - buffer:
            return None
        if not self._volume_confirms(conn, symbol, ts):
            return None

        today = cal.trading_day_of(ts)
        expiry = nearest_expiry(chain, self.DTE_MAX, today)
        if not expiry:
            return None
        exp_chain = [c for c in chain if c["expiry"] == expiry]
        right = "C" if direction == "UP" else "P"
        k_long = nearest_strike(exp_chain, spot, right, offset=0)
        k_short = nearest_strike(exp_chain, spot, right, offset=2)
        if k_long is None or k_short is None or k_long == k_short:
            return None
        return Candidate(
            symbol=symbol, strategy=self.name, version=self.version,
            direction="LONG" if direction == "UP" else "SHORT",
            structure=("call_debit_vertical" if right == "C"
                       else "put_debit_vertical"),
            legs=[Leg(expiry, k_long, right, +1), Leg(expiry, k_short, right, -1)],
            exit_rules=ExitRules(target_pct=0.50, stop_pct=0.40,
                                 time_stop_et="15:45"),
            rationale=(f"ORB break {direction} of [{features['orb_low']:.2f}, "
                       f"{features['orb_high']:.2f}] at {spot:.2f} with volume "
                       f"confirmation, {mins:.0f}m after open"),
            confidence=0.5,
            features_json=self.pack_features(features))

    def _volume_confirms(self, conn, symbol, ts) -> bool:
        """Last-interval volume > VOL_MULT × trailing 15-min per-interval avg.

        underlying_snap.volume is cumulative day volume, so differentiate."""
        day = cal.trading_day_of(ts)
        rows = db.underlying_between(conn, symbol, f"{day}T00:00:00Z", ts)
        vols = [r["volume"] for r in rows if r["volume"]]
        if len(vols) < 8:
            return False
        deltas = [b - a for a, b in zip(vols, vols[1:]) if b >= a]
        if len(deltas) < 6:
            return False
        recent = deltas[-1]
        trailing = deltas[-61:-1] if len(deltas) > 61 else deltas[:-1]
        avg = sum(trailing) / len(trailing)
        return avg > 0 and recent > self.VOL_MULT * avg
