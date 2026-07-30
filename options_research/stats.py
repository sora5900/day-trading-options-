"""Statistics: day-bootstrapped intervals and honest power reporting.

Two rules encoded here, both from PHASE1.md §0:

1. **Bootstrap by trading DAY, never by trade.** Intraday trades are heavily
   correlated; resampling trades independently manufactures precision that
   does not exist.
2. **Every interval is reported with its minimum detectable edge.** At Phase 1
   sample sizes a null result is uninformative, and an interval containing
   zero must never be read as "no edge".
"""

import hashlib
import math
import statistics
from collections import defaultdict

Z_ALPHA = 1.959963985            # two-sided 95%
Z_POWER = 0.8416212335           # 80% power


def group_by_day(trades) -> dict:
    """{trading_day: [pnl_net, ...]} keyed on the entry date."""
    by_day = defaultdict(list)
    for t in trades:
        if t["entry_ts"] and t["pnl_net"] is not None:
            by_day[t["entry_ts"][:10]].append(t["pnl_net"])
    return dict(by_day)


def _rng(seed: str):
    """Deterministic PRNG so bootstrap intervals are reproducible in replay."""
    state = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16)

    def nxt(n: int) -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % (2 ** 64)
        return (state >> 17) % n
    return nxt


def day_bootstrap_ci(trades, statistic="ev_per_trade", n_boot: int = 5000,
                     seed: str = "phase1", alpha: float = 0.05):
    """Resample whole DAYS with replacement. Returns (point, lo, hi, n_days).

    Resampling days preserves within-day correlation; resampling trades would
    not, and would report an interval far too tight.
    """
    by_day = group_by_day(trades)
    days = sorted(by_day)
    if len(days) < 3:
        return None, None, None, len(days)

    def stat(day_list) -> float | None:
        pnls = [p for d in day_list for p in by_day[d]]
        if not pnls:
            return None
        if statistic == "ev_per_trade":
            return sum(pnls) / len(pnls)
        if statistic == "mean_day_pnl":
            return sum(pnls) / len(day_list)
        raise ValueError(statistic)

    point = stat(days)
    nxt = _rng(seed)
    draws = []
    n = len(days)
    for _ in range(n_boot):
        sample = [days[nxt(n)] for _ in range(n)]
        v = stat(sample)
        if v is not None:
            draws.append(v)
    if not draws:
        return point, None, None, n
    draws.sort()
    lo = draws[int((alpha / 2) * len(draws))]
    hi = draws[min(len(draws) - 1, int((1 - alpha / 2) * len(draws)))]
    return point, lo, hi, n


def minimum_detectable_edge(trades, power_z: float = Z_POWER,
                            alpha_z: float = Z_ALPHA) -> float | None:
    """Smallest per-trade EV this sample could have detected at 80% power.

    Printed beside every interval so a null is never mistaken for a finding.
    """
    pnls = [t["pnl_net"] for t in trades if t["pnl_net"] is not None]
    if len(pnls) < 3:
        return None
    sd = statistics.pstdev(pnls)
    if sd == 0:
        return 0.0
    return (alpha_z + power_z) * sd / math.sqrt(len(pnls))


def trades_needed(sd: float, target_edge: float, power_z: float = Z_POWER,
                  alpha_z: float = Z_ALPHA) -> int:
    if target_edge <= 0:
        return 0
    return math.ceil((alpha_z + power_z) ** 2 * (sd / target_edge) ** 2)


# ── the metric suite ────────────────────────────────────────────────────────

def metrics(trades) -> dict:
    """Every metric the owner asked for, EV first and win rate demoted."""
    graded = [t for t in trades
              if t["outcome"] in ("WIN", "LOSS", "SCRATCH")
              and t["pnl_net"] is not None]
    n = len(graded)
    if n == 0:
        return {"n": 0, "n_days": 0, "ev_per_trade": None, "pnl": 0.0,
                "profit_factor": None, "win_rate": None, "avg_win": None,
                "avg_loss": None, "max_drawdown": None, "worst_day": None,
                "largest_loss": None, "expected_shortfall_5": None,
                "max_consecutive_losses": 0, "sharpe": None,
                "capital_required": None, "return_on_capital": None,
                "avg_spread_pct": None, "mde": None,
                "ci_lo": None, "ci_hi": None}

    pnls = [t["pnl_net"] for t in graded]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    by_day = group_by_day(graded)
    daily = [sum(v) for v in by_day.values()]

    equity = peak = dd = 0.0
    for d in sorted(by_day):
        equity += sum(by_day[d])
        peak = max(peak, equity)
        dd = min(dd, equity - peak)

    streak = worst_streak = 0
    for t in sorted(graded, key=lambda x: x["entry_ts"] or ""):
        if (t["pnl_net"] or 0) < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    tail_n = max(1, int(len(pnls) * 0.05))
    es5 = sum(sorted(pnls)[:tail_n]) / tail_n

    sharpe = None
    if len(daily) >= 2 and statistics.pstdev(daily) > 0:
        sharpe = (statistics.mean(daily) / statistics.pstdev(daily)) * (252 ** 0.5)

    capital = max((t["capital_at_risk"] or 0) for t in graded) \
        if "capital_at_risk" in graded[0].keys() else None

    spreads = [t["entry_spread_pct"] for t in graded
               if t["entry_spread_pct"] is not None]
    point, lo, hi, n_days = day_bootstrap_ci(graded)

    return {
        "n": n, "n_days": len(by_day),
        "ev_per_trade": round(sum(pnls) / n, 2),
        "pnl": round(sum(pnls), 2),
        "profit_factor": (round(gross_win / gross_loss, 2)
                          if gross_loss > 0 else None),
        "win_rate": len(wins) / n,
        "avg_win": round(statistics.mean(wins), 2) if wins else None,
        "avg_loss": round(statistics.mean(losses), 2) if losses else None,
        "max_drawdown": round(dd, 2),
        "worst_day": round(min(daily), 2) if daily else None,
        "largest_loss": round(min(pnls), 2),
        "expected_shortfall_5": round(es5, 2),
        "max_consecutive_losses": worst_streak,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "capital_required": capital,
        "return_on_capital": (round(sum(pnls) / capital, 4)
                              if capital else None),
        "avg_spread_pct": (round(statistics.mean(spreads), 4)
                           if spreads else None),
        "mde": round(minimum_detectable_edge(graded) or 0, 2),
        "ci_lo": round(lo, 2) if lo is not None else None,
        "ci_hi": round(hi, 2) if hi is not None else None,
    }


def interval_is_informative(m: dict) -> bool:
    """True when the interval can distinguish a plausible edge from zero.

    If the minimum detectable edge is larger than any edge we would plausibly
    find, a null tells us nothing and the honest report is 'keep collecting'.
    """
    return bool(m.get("mde") and m["mde"] <= 10.0)
