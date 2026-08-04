"""VRP magnitude estimator, validated against planted premiums."""

import pytest

from options_research import market_calendar as cal
from options_research.analysis import vrp_magnitude as vm
from conftest import insert_underlying  # noqa: F401  (fixture file import)

DAYS = cal.trading_days_between("2026-03-02", "2026-05-15")


def _spot_bar(conn, day, close):
    from datetime import timedelta
    open_utc, _ = cal.session_bounds(day)
    ts = (open_utc + timedelta(minutes=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT OR REPLACE INTO underlying_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, "SPY", close, close, close, close, 1000, close, 10, ts,
         "polygon"))


def _straddle(conn, day, expiry, strike, call_close, put_close):
    from datetime import timedelta
    open_utc, _ = cal.session_bounds(day)
    ts = (open_utc + timedelta(minutes=380)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for right, px in (("C", call_close), ("P", put_close)):
        conn.execute(
            "INSERT OR REPLACE INTO option_bars VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "SPY", expiry, strike, right, px, px, px, px, 100, px, 10,
             ts, "polygon"))


def _build(conn, richness: float, priced_pct: float = 0.006,
           spot0: float = 630.0):
    """Plant a known richness: realized = priced * (1 - richness), with the
    move alternating direction so it is not a trend artifact."""
    spot = spot0
    for i, day in enumerate(DAYS[:-1]):
        nxt = DAYS[i + 1]
        priced = priced_pct * spot
        realized = priced * (1 - richness)
        _spot_bar(conn, day, spot)
        _straddle(conn, day, nxt, float(round(spot)), priced * 0.55,
                  priced * 0.45)
        spot = spot + realized if i % 2 == 0 else spot - realized
    _spot_bar(conn, DAYS[-1], spot)
    conn.commit()


def test_recovers_planted_richness(conn):
    _build(conn, richness=0.25)
    res = vm.run(conn, "SPY")
    assert res["n_days"] >= 30
    assert res["richness"] == pytest.approx(0.25, abs=0.03)
    assert res["significant"]
    assert "worth pursuing" in vm.render(res)


def test_thin_premium_fails_viability(conn):
    _build(conn, richness=0.10)
    res = vm.run(conn, "SPY")
    assert res["richness"] == pytest.approx(0.10, abs=0.03)
    assert "below" in vm.render(res)


def test_zero_premium_reported_as_null(conn):
    """Realized alternates above/below priced with mean ~= priced →
    premium indistinguishable from zero."""
    spot = 630.0
    for i, day in enumerate(DAYS[:-1]):
        nxt = DAYS[i + 1]
        priced = 0.006 * spot
        realized = priced * (1.6 if i % 2 == 0 else 0.4)   # mean = priced
        _spot_bar(conn, day, spot)
        _straddle(conn, day, nxt, float(round(spot)), priced * 0.5,
                  priced * 0.5)
        spot = spot + realized if i % 3 else spot - realized
    _spot_bar(conn, DAYS[-1], spot)
    conn.commit()
    res = vm.run(conn, "SPY")
    assert not res["significant"]
    assert "NOT distinguishable" in vm.render(res)


def test_far_from_money_strikes_excluded(conn):
    _build(conn, richness=0.25)
    # add a far-OTM "straddle" with an absurd price; must not contaminate
    _straddle(conn, DAYS[0], DAYS[1], 700.0, 50.0, 50.0)
    conn.commit()
    res = vm.run(conn, "SPY")
    assert res["richness"] == pytest.approx(0.25, abs=0.03)


def test_requires_minimum_sample(conn):
    _spot_bar(conn, DAYS[0], 630.0)
    _straddle(conn, DAYS[0], DAYS[1], 630.0, 2.0, 2.0)
    conn.commit()
    res = vm.run(conn, "SPY")
    assert "error" in res


def test_only_next_day_expiry_used(conn):
    """A straddle for the wrong expiry (D+3) must be ignored even if stored."""
    _build(conn, richness=0.25)
    _straddle(conn, DAYS[0], DAYS[4], float(round(630.0)), 9.0, 9.0)
    conn.commit()
    res = vm.run(conn, "SPY")
    assert res["richness"] == pytest.approx(0.25, abs=0.03)
