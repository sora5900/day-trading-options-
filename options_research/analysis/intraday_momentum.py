"""H3-P1 STAGE 1 — reproduce market intraday momentum on the underlying.

Claim under test (Gao, Han, Li & Zhou, JFE 2018): the first half-hour return
predicts the last half-hour return in index ETFs.

Stage 1 uses ONLY underlying bars. No options, no entitlements beyond
historical aggregates, and no forward collection. It is therefore the one
Phase 1 component that can reach statistical significance immediately.

Stage 2 (the friction hurdle) follows from the numbers this produces, and the
options test set is never opened unless stage 2 clears.

Statistics are deliberately plain: a univariate OLS with a day-bootstrapped
interval. Each observation is one trading day, so days are already the
independent unit — no clustering correction needed.
"""

import hashlib
import math
import statistics
from collections import defaultdict

from .. import market_calendar as cal

# One-way friction on SPY shares: ~1c spread on a ~$630 underlying.
SHARE_FRICTION_BPS_ROUNDTRIP = 0.32     # ≈ 2 x (0.01 / 630) in bps


def _sessions(conn, symbol: str) -> dict:
    """{trading_day: [(minutes_since_open, open, close), ...]} from bars."""
    rows = conn.execute(
        "SELECT event_ts, open, close FROM underlying_bars "
        "WHERE symbol=? ORDER BY event_ts ASC", (symbol,)).fetchall()
    by_day = defaultdict(list)
    for r in rows:
        if r["open"] is None or r["close"] is None:
            continue
        mins = cal.minutes_since_open(r["event_ts"])
        if mins is None:
            continue                      # not a session (holiday/weekend)
        by_day[cal.trading_day_of(r["event_ts"])].append(
            (mins, r["open"], r["close"]))
    return by_day


def session_length_minutes(day: str) -> float | None:
    """Regular-session length for `day`, from the exchange calendar."""
    bounds = cal.session_bounds(day)
    if bounds is None:
        return None
    return (bounds[1] - bounds[0]).total_seconds() / 60.0


def daily_observations(conn, symbol: str, first_window: int = 30,
                       last_window: int = 30,
                       full_session_minutes: int = 390) -> list:
    """One (day, r1, r_last) per FULL trading day.

    r1     = first `first_window` minutes, open-to-close
    r_last = final `last_window` minutes, open-to-close

    Two things this must get right, both learned from real data:

    1. Vendors include EXTENDED HOURS (04:00-20:00 ET). Session length is
       therefore taken from the exchange calendar, never inferred from the
       span of available bars — otherwise a half day looks full and its
       "last 30 minutes" gets read out of after-hours trading.
    2. Only regular-session minutes are used. Bars outside [0, session) have
       negative or overshooting offsets and are excluded by construction.
    """
    out = []
    for day, bars in sorted(_sessions(conn, symbol).items()):
        length = session_length_minutes(day)
        if length is None or length < full_session_minutes - 5:
            continue                      # half day / early close — different object
        bars.sort(key=lambda b: b[0])
        first = [b for b in bars if 0 <= b[0] < first_window]
        last = [b for b in bars if length - last_window <= b[0] < length]
        if len(first) < first_window // 2 or len(last) < last_window // 2:
            continue                      # too gappy to trust
        r1 = (first[-1][2] - first[0][1]) / first[0][1]
        r_last = (last[-1][2] - last[0][1]) / last[0][1]
        out.append({"day": day, "r1": r1, "r_last": r_last})
    return out


# ── plain univariate OLS ────────────────────────────────────────────────────

def ols(xs, ys) -> dict:
    n = len(xs)
    if n < 3:
        return {"n": n}
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return {"n": n}
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    alpha = my - beta * mx
    resid = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((y - my) ** 2 for y in ys)
    se = math.sqrt((ss_res / (n - 2)) / sxx) if n > 2 else None
    return {"n": n, "beta": beta, "alpha": alpha,
            "t_stat": (beta / se) if se else None,
            "se": se,
            "r_squared": (1 - ss_res / ss_tot) if ss_tot > 0 else None}


def _rng(seed: str):
    state = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16)

    def nxt(n: int) -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % (2 ** 64)
        return (state >> 17) % n
    return nxt


def bootstrap_ci(values, n_boot: int = 5000, seed: str = "h3p1",
                 alpha: float = 0.05):
    """Day-level bootstrap of the mean. Each value is already one day."""
    n = len(values)
    if n < 3:
        return None, None
    nxt = _rng(seed)
    draws = []
    for _ in range(n_boot):
        draws.append(sum(values[nxt(n)] for _ in range(n)) / n)
    draws.sort()
    return (draws[int((alpha / 2) * n_boot)],
            draws[min(n_boot - 1, int((1 - alpha / 2) * n_boot))])


def run(conn, symbol: str, min_r1_bps: float = 0.0) -> dict:
    """Stage 1. `min_r1_bps` optionally ignores noise-sized morning moves."""
    obs = daily_observations(conn, symbol)
    if min_r1_bps:
        obs = [o for o in obs if abs(o["r1"]) * 10_000 >= min_r1_bps]
    if len(obs) < 30:
        return {"symbol": symbol, "n_days": len(obs),
                "error": "need >= 30 clean sessions"}

    xs = [o["r1"] for o in obs]
    ys = [o["r_last"] for o in obs]
    reg = ols(xs, ys)

    # The economically meaningful form: take position sign(r1) into the close.
    rule = [(1 if o["r1"] > 0 else -1) * o["r_last"] for o in obs]
    rule_bps = [r * 10_000 for r in rule]
    mean_bps = statistics.mean(rule_bps)
    lo, hi = bootstrap_ci(rule_bps)
    hit = sum(1 for o in obs
              if (o["r1"] > 0) == (o["r_last"] > 0)) / len(obs)

    sd = statistics.pstdev(rule_bps)
    mde_bps = (1.96 + 0.8416) * sd / math.sqrt(len(rule_bps))

    return {
        "symbol": symbol,
        "n_days": len(obs),
        "first_day": obs[0]["day"], "last_day": obs[-1]["day"],
        "regression": reg,
        "rule_mean_bps": round(mean_bps, 3),
        "rule_ci_bps": (round(lo, 3) if lo is not None else None,
                        round(hi, 3) if hi is not None else None),
        "rule_sd_bps": round(sd, 2),
        "hit_rate": round(hit, 4),
        "mde_bps": round(mde_bps, 3),
        "significant": bool(lo is not None and (lo > 0 or hi < 0)),
    }


def stage2_hurdle(stage1: dict, option_friction_pct: float,
                  delta_leverage: float = 20.0) -> dict:
    """STAGE 2 — does the measured effect clear options friction?

    `option_friction_pct` must come from C2's MEASURED round-trip friction,
    not an assumption. Underlying-share friction is compared alongside,
    because 'valid effect, wrong instrument' is a real and useful verdict.
    """
    edge_bps = stage1.get("rule_mean_bps")
    if edge_bps is None:
        return {"error": "stage 1 produced no edge estimate"}

    shares_net_bps = edge_bps - SHARE_FRICTION_BPS_ROUNDTRIP
    option_move_pct = (edge_bps / 100.0) * delta_leverage   # bps -> % , levered
    option_friction_bps = option_friction_pct * 100 * 100

    return {
        "underlying_edge_bps": round(edge_bps, 3),
        "share_friction_bps": SHARE_FRICTION_BPS_ROUNDTRIP,
        "shares_net_bps": round(shares_net_bps, 3),
        "shares_viable": shares_net_bps > 0 and stage1.get("significant"),
        "delta_leverage": delta_leverage,
        "option_move_pct_of_premium": round(option_move_pct, 3),
        "option_friction_pct_of_premium": round(option_friction_pct * 100, 2),
        "options_clear_hurdle": option_move_pct > option_friction_pct * 100,
        "verdict": _stage2_verdict(stage1, shares_net_bps, option_move_pct,
                                   option_friction_pct * 100),
    }


def _stage2_verdict(stage1, shares_net_bps, option_move_pct,
                    option_friction_pct) -> str:
    if not stage1.get("significant"):
        return ("EFFECT NOT REPRODUCED — interval contains zero at "
                f"n={stage1.get('n_days')} days (minimum detectable "
                f"{stage1.get('mde_bps')} bps). Do not proceed to options.")
    if option_move_pct > option_friction_pct:
        return ("CLEARS HURDLE — proceed to stage 3, but only with the shares "
                "comparison arm running alongside.")
    if shares_net_bps > 0:
        return ("VALID EFFECT, WRONG INSTRUMENT — the effect reproduces and "
                "survives share friction, but leverage scales the move and "
                "the premium together, so it does not clear options friction. "
                "Trade the underlying or nothing. Do NOT open the options "
                "test set.")
    return ("EFFECT REAL BUT UNTRADEABLE — does not survive friction in any "
            "instrument tested.")


def render(stage1: dict, stage2: dict | None = None) -> str:
    if "error" in stage1:
        return f"H3-P1 STAGE 1 ({stage1['symbol']}): {stage1['error']} " \
               f"(have {stage1.get('n_days')} days)"
    r = stage1["regression"]
    w = 74
    out = ["=" * w,
           f"H3-P1 STAGE 1 — intraday momentum, {stage1['symbol']} "
           f"(underlying only)",
           "=" * w,
           f"  sessions      {stage1['n_days']}  "
           f"({stage1['first_day']} .. {stage1['last_day']})",
           "",
           "  REGRESSION  r_last ~ r1",
           f"    beta        {r.get('beta'):+.4f}" if r.get("beta") is not None
           else "    beta        —",
           f"    t-stat      {r.get('t_stat'):+.2f}" if r.get("t_stat")
           is not None else "    t-stat      —",
           f"    R^2         {r.get('r_squared'):.5f}" if r.get("r_squared")
           is not None else "    R^2         —",
           "",
           "  TRADING RULE  position = sign(r1) into the close",
           f"    mean return {stage1['rule_mean_bps']:+.3f} bps/day",
           f"    95% CI      [{stage1['rule_ci_bps'][0]:+.3f}, "
           f"{stage1['rule_ci_bps'][1]:+.3f}] bps  (day bootstrap)",
           f"    hit rate    {stage1['hit_rate']:.1%}  "
           f"← vanity metric, listed after the interval",
           f"    daily SD    {stage1['rule_sd_bps']:.1f} bps",
           f"    min. detectable edge at this n: "
           f"{stage1['mde_bps']:.3f} bps",
           ""]
    if stage1["significant"]:
        out.append("  → interval EXCLUDES zero: the effect reproduces here.")
    else:
        out.append("  → interval CONTAINS zero: not reproduced at this sample "
                   "size.")
        out.append("    A null here is not proof of absence — compare the "
                   "minimum")
        out.append("    detectable edge above against the effect you expected.")
    if stage2:
        out += ["", "  STAGE 2 — friction hurdle",
                f"    underlying edge      {stage2['underlying_edge_bps']:+.3f} bps",
                f"    share friction       {stage2['share_friction_bps']:.2f} bps"
                f"  → net {stage2['shares_net_bps']:+.3f} bps",
                f"    levered option move  "
                f"{stage2['option_move_pct_of_premium']:.2f}% of premium",
                f"    option friction      "
                f"{stage2['option_friction_pct_of_premium']:.2f}% of premium",
                "",
                f"  VERDICT: {stage2['verdict']}"]
    out.append("=" * w)
    return "\n".join(out)
