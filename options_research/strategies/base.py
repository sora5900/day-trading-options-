"""Strategy interface.

A setup FIRING is a hypothesis, not a trade (spec §1). Strategies only emit
candidates; the fill engine decides whether the market would actually let us
in, and the grader decides what happened. Strategies never see the future:
they receive a decision timestamp and read through the bounded query layer.

Versioning matters: the validation protocol burns a test set per version
(spec §7.3), so any rule change requires a version bump.
"""

import json
from dataclasses import dataclass, field

from ..fill_engine import Leg


@dataclass
class ExitRules:
    target_pct: float | None      # close when unrealized P&L >= +target_pct of risk basis
    stop_pct: float | None        # close when unrealized P&L <= -stop_pct of risk basis
    time_stop_et: str             # "HH:MM" ET hard exit
    # For credit structures the basis is the credit received; for debit
    # structures it is the debit paid.


@dataclass
class Candidate:
    symbol: str
    strategy: str
    version: str
    direction: str                # 'LONG' / 'SHORT' / 'NEUTRAL'
    structure: str                # 'call_debit_vertical', 'put_credit_spread', ...
    legs: list                    # list[Leg]
    exit_rules: ExitRules
    rationale: str
    confidence: float             # the model's own honest number, 0-1
    features_json: str = ""


def nearest_strike(chain, spot: float, right: str, offset: int = 0):
    """Strike ladder helper: ATM strike for `right`, offset in strikes
    (positive = further OTM for calls up / puts down)."""
    strikes = sorted({c["strike"] for c in chain if c["right"] == right})
    if not strikes:
        return None
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    j = atm_i + offset if right == "C" else atm_i - offset
    if 0 <= j < len(strikes):
        return strikes[j]
    return None


def nearest_expiry(chain, dte_max: int, today: str):
    """Soonest expiry in the chain with DTE <= dte_max (dates as YYYY-MM-DD)."""
    from datetime import date
    t = date.fromisoformat(today)
    cands = sorted({c["expiry"] for c in chain if c["expiry"]})
    for e in cands:
        try:
            dte = (date.fromisoformat(e) - t).days
        except ValueError:
            continue
        if 0 <= dte <= dte_max:
            return e
    return None


def strike_by_delta(chain, expiry: str, right: str, target_abs_delta: float):
    """Strike whose |delta| is closest to target, same expiry/right."""
    best = None
    for c in chain:
        if c["expiry"] != expiry or c["right"] != right or c["delta"] is None:
            continue
        d = abs(abs(c["delta"]) - target_abs_delta)
        if best is None or d < best[0]:
            best = (d, c["strike"])
    if best and best[0] <= 0.10:
        return best[1]
    return None


class Strategy:
    name = "base"
    version = "v1"

    def detect(self, conn, symbol: str, ts: str, features: dict,
               chain) -> Candidate | None:
        raise NotImplementedError

    @staticmethod
    def pack_features(features: dict) -> str:
        return json.dumps({k: v for k, v in features.items()
                           if k not in ("ts", "symbol")}, default=str)
