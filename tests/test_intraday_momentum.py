"""H3-P1 stage 1 and the stage-2 friction gate.

Synthetic data with a KNOWN effect, so the estimator is validated against a
planted answer rather than against itself.
"""

import pytest

from options_research import market_calendar as cal
from options_research.analysis import intraday_momentum as im


def _bar(conn, symbol, event_ts, open_, close):
    conn.execute(
        "INSERT OR REPLACE INTO underlying_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (event_ts, symbol, open_, max(open_, close), min(open_, close), close,
         100000, (open_ + close) / 2, 500, event_ts, "polygon"))


def _ts_for(day, m):
    """Minute `m` of `day`'s session, from the real NYSE calendar.

    The session open is 14:30Z under EST and 13:30Z under EDT, so it is looked
    up rather than assumed — the DAYS list below straddles the 2026 DST
    transition on purpose.
    """
    from datetime import timedelta
    open_utc, _ = cal.session_bounds(day)
    return (open_utc + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session(conn, day, r1, r_last, symbol="SPY", base=630.0):
    """Build a full 390-minute session with planted first/last 30-min returns."""
    def ts(m):
        return _ts_for(day, m)

    price = base
    for m in range(390):
        if m < 30:                       # first half hour carries r1
            o = base * (1 + r1 * m / 30)
            c = base * (1 + r1 * (m + 1) / 30)
        elif m >= 360:                   # last half hour carries r_last
            mid = base * (1 + r1)
            k = m - 360
            o = mid * (1 + r_last * k / 30)
            c = mid * (1 + r_last * (k + 1) / 30)
        else:                            # flat midday
            o = c = base * (1 + r1)
        _bar(conn, symbol, ts(m), o, c)
        price = c
    conn.commit()


# Derived from the real NYSE calendar, not hardcoded: the range straddles the
# 2026 DST transition and contains Good Friday (2026-04-03), both of which a
# handwritten list gets wrong.
DAYS = cal.trading_days_between("2026-03-02", "2026-04-24")


def test_extracts_first_and_last_half_hour_returns(conn):
    _session(conn, DAYS[0], r1=0.0050, r_last=0.0020)
    obs = im.daily_observations(conn, "SPY")
    assert len(obs) == 1
    assert obs[0]["r1"] == pytest.approx(0.0050, rel=0.05)
    assert obs[0]["r_last"] == pytest.approx(0.0020, rel=0.05)


def test_detects_a_planted_momentum_effect(conn):
    """r_last = 0.4 * r1 exactly; the regression must recover beta ~ 0.4."""
    for i, day in enumerate(DAYS):
        r1 = 0.004 * (1 if i % 2 == 0 else -1) * (1 + i % 3) / 2
        _session(conn, day, r1=r1, r_last=0.4 * r1)
    res = im.run(conn, "SPY")
    assert res["n_days"] == len(DAYS)
    assert res["regression"]["beta"] == pytest.approx(0.4, rel=0.10)
    assert res["rule_mean_bps"] > 0          # sign(r1) captures the move
    assert res["significant"] is True


def test_reports_null_when_there_is_no_effect(conn):
    """Alternating r_last independent of r1 → interval must contain zero."""
    for i, day in enumerate(DAYS):
        r1 = 0.004 * (1 if i % 2 == 0 else -1)
        r_last = 0.003 * (1 if i % 3 == 0 else -1)
        _session(conn, day, r1=r1, r_last=r_last)
    res = im.run(conn, "SPY")
    assert res["significant"] is False
    assert res["rule_ci_bps"][0] < 0 < res["rule_ci_bps"][1]
    assert "CONTAINS zero" in im.render(res)
    assert "not proof of absence" in im.render(res)


def test_refuses_to_report_on_too_few_sessions(conn):
    for day in DAYS[:5]:
        _session(conn, day, r1=0.004, r_last=0.002)
    res = im.run(conn, "SPY")
    assert "error" in res and ">= 30" in res["error"]


def test_real_half_day_is_excluded(conn):
    """2025-11-28 is the day after Thanksgiving: a real 13:00 ET early close.
    Its final 30 minutes are a different object and must not be pooled."""
    assert im.session_length_minutes("2025-11-28") < 390
    _session(conn, DAYS[0], r1=0.004, r_last=0.002)
    _session(conn, "2025-11-28", r1=0.004, r_last=0.002)
    days = {o["day"] for o in im.daily_observations(conn, "SPY")}
    assert days == {DAYS[0]}


def test_extended_hours_bars_do_not_leak_into_windows(conn):
    """Vendors return 04:00-20:00 ET. Pre-market must not become 'the first
    30 minutes', and after-hours must not become 'the last 30 minutes' —
    which is exactly what inferring session length from bar span would do."""
    from datetime import timedelta
    day = DAYS[0]
    _session(conn, day, r1=0.0040, r_last=0.0020)
    open_utc, close_utc = cal.session_bounds(day)
    for k in range(1, 121):                  # pre-market, wild prices
        _bar(conn, "SPY", (open_utc - timedelta(minutes=k)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"), 500.0, 500.0)
    for k in range(0, 120):                  # after-hours, wild prices
        _bar(conn, "SPY", (close_utc + timedelta(minutes=k)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"), 900.0, 900.0)
    conn.commit()
    obs = im.daily_observations(conn, "SPY")
    assert len(obs) == 1
    assert obs[0]["r1"] == pytest.approx(0.0040, rel=0.05)
    assert obs[0]["r_last"] == pytest.approx(0.0020, rel=0.05)


# ── stage 2: the gate that keeps the options test set closed ────────────────

def test_stage2_blocks_when_effect_not_significant():
    s1 = {"rule_mean_bps": 1.2, "significant": False, "n_days": 40,
          "mde_bps": 5.0}
    s2 = im.stage2_hurdle(s1, option_friction_pct=0.15)
    assert "NOT REPRODUCED" in s2["verdict"]
    assert not s2["options_clear_hurdle"]


def test_stage2_valid_effect_wrong_instrument():
    """The expected outcome: real effect, survives share friction, but
    leverage scales move and premium together so options still lose."""
    s1 = {"rule_mean_bps": 2.0, "significant": True, "n_days": 500,
          "mde_bps": 0.5}
    s2 = im.stage2_hurdle(s1, option_friction_pct=0.15, delta_leverage=20)
    assert s2["shares_net_bps"] > 0
    assert s2["option_move_pct_of_premium"] == pytest.approx(0.4)
    assert s2["option_friction_pct_of_premium"] == pytest.approx(15.0)
    assert not s2["options_clear_hurdle"]
    assert "WRONG INSTRUMENT" in s2["verdict"]


def test_stage2_clears_only_on_a_large_effect():
    s1 = {"rule_mean_bps": 100.0, "significant": True, "n_days": 500,
          "mde_bps": 1.0}
    s2 = im.stage2_hurdle(s1, option_friction_pct=0.15, delta_leverage=20)
    assert s2["options_clear_hurdle"]
    assert "CLEARS HURDLE" in s2["verdict"]
    assert "shares comparison arm" in s2["verdict"]


def test_ols_recovers_known_coefficients():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0 * x + 1.0 for x in xs]
    r = im.ols(xs, ys)
    assert r["beta"] == pytest.approx(2.0)
    assert r["alpha"] == pytest.approx(1.0)
    assert r["r_squared"] == pytest.approx(1.0)
