"""Price-space signal layer — the premium-tier replacements (PHASE1.md §1).

Hand-computed throughout: these estimators replace vendor IV and vendor
Greeks, so an error here is invisible in a way an API failure would not be.
"""

import pytest

from options_research import signals
from conftest import insert_underlying


def _c(strike, right, bid, ask, expiry="2026-07-30", oi=1000, volume=500):
    return {"expiry": expiry, "strike": float(strike), "right": right,
            "bid": bid, "ask": ask, "open_interest": oi, "volume": volume}


@pytest.fixture
def chain():
    """Spot 630. Symmetric-ish book; ATM call 3.00 mid, ATM put 2.00 mid."""
    return [
        _c(624, "P", 0.40, 0.44), _c(626, "P", 0.70, 0.76),
        _c(630, "P", 1.95, 2.05), _c(634, "P", 4.90, 5.10),
        _c(626, "C", 5.90, 6.10), _c(630, "C", 2.95, 3.05),
        _c(634, "C", 0.90, 0.96), _c(636, "C", 0.40, 0.44),
    ]


def test_straddle_mid_is_sum_of_atm_mids(chain):
    # ATM call mid 3.00 + ATM put mid 2.00 = 5.00
    assert signals.straddle_mid(chain, 630.0, "2026-07-30") == pytest.approx(5.00)


def test_priced_move_is_straddle_over_spot(chain):
    pm = signals.priced_move(chain, 630.0, "2026-07-30")
    assert pm == pytest.approx(5.00 / 630.0)
    assert pm == pytest.approx(0.007937, abs=1e-6)   # ~0.79% expected move


def test_straddle_none_without_two_sided_quotes():
    one_sided = [_c(630, "C", 2.95, 3.05), _c(630, "P", None, None)]
    assert signals.straddle_mid(one_sided, 630.0, "2026-07-30") is None


def test_crossed_book_is_rejected_not_averaged():
    crossed = [_c(630, "C", 3.10, 3.00), _c(630, "P", 1.95, 2.05)]
    assert signals.straddle_mid(crossed, 630.0, "2026-07-30") is None


def test_realized_move_is_absolute_fractional(conn):
    day = "2026-07-30"
    insert_underlying(conn, f"{day}T14:00:00Z", last=630.0)
    insert_underlying(conn, f"{day}T15:00:00Z", last=633.15)
    rm = signals.realized_move(conn, "SPY", f"{day}T15:00:00Z", 30)
    assert rm == pytest.approx(3.15 / 630.0)         # 0.5%


def test_vrp_px_is_priced_minus_realized(conn, chain):
    day = "2026-07-30"
    insert_underlying(conn, f"{day}T14:00:00Z", last=630.0)
    insert_underlying(conn, f"{day}T15:00:00Z", last=631.26)   # +0.2%
    v = signals.vrp_px(chain, conn, "SPY", 630.0, "2026-07-30",
                       f"{day}T15:00:00Z")
    assert v == pytest.approx(5.00 / 630.0 - 1.26 / 630.0)
    assert v > 0                                     # charged more than realized


def test_skew_px_positive_when_put_richer(chain):
    # at 1% moneyness: put 623.7 -> strike 624 (mid 0.42);
    #                  call 636.3 -> strike 636 (mid 0.42) => ~0 skew
    s = signals.skew_px(chain, 630.0, "2026-07-30", moneyness=0.01)
    assert s == pytest.approx(0.0, abs=1e-6)

    richer_put = [c for c in chain if not (c["strike"] == 624
                                           and c["right"] == "P")]
    richer_put.append(_c(624, "P", 0.90, 0.94))      # mid 0.92 vs call 0.42
    s2 = signals.skew_px(richer_put, 630.0, "2026-07-30", moneyness=0.01)
    assert s2 == pytest.approx((0.92 - 0.42) / 5.00)


def test_strike_selection_by_straddle_multiple(chain):
    # spot 630, straddle 5.00, k=1.25 -> target 630 - 6.25 = 623.75 -> 624
    k = signals.strike_by_straddle_multiple(chain, 630.0, "2026-07-30", "P",
                                            k=1.25)
    assert k == 624.0


def test_strike_selection_widens_with_volatility():
    """The point of straddle-multiple selection: a richer straddle pushes the
    strike further out, the way delta selection would."""
    calm = [_c(s, "P", 0.4, 0.44) for s in range(600, 632, 2)]
    calm += [_c(630, "C", 0.95, 1.05), _c(630, "P", 0.95, 1.05)]
    wild = [_c(s, "P", 0.4, 0.44) for s in range(600, 632, 2)]
    wild += [_c(630, "C", 4.95, 5.05), _c(630, "P", 4.95, 5.05)]
    k_calm = signals.strike_by_straddle_multiple(calm, 630.0, "2026-07-30",
                                                 "P", k=1.25)
    k_wild = signals.strike_by_straddle_multiple(wild, 630.0, "2026-07-30",
                                                 "P", k=1.25)
    assert k_wild < k_calm       # higher priced move -> further OTM


def test_strike_offset_gives_the_wing(chain):
    assert signals.strike_offset(chain, "2026-07-30", "P", 626.0, 2.0) == 624.0


# ── adaptive liquidity gate ─────────────────────────────────────────────────

def test_gate_mode_detects_missing_liquidity_fields(chain):
    assert signals.gate_mode(chain) == signals.GATE_OI_VOLUME
    stripped = [{**c, "open_interest": None, "volume": None} for c in chain]
    assert signals.gate_mode(stripped) == signals.GATE_SPREAD_ONLY


def test_degraded_gate_tightens_the_spread_requirement():
    """Losing OI/volume evidence must cost something, not be free."""
    # 7% spread: passes the 8% full gate, fails the 6% degraded gate
    c = _c(630, "P", 1.93, 2.07, oi=5000, volume=900)
    assert signals.passes_gate(c, signals.GATE_OI_VOLUME)
    assert not signals.passes_gate(c, signals.GATE_SPREAD_ONLY)


def test_full_gate_enforces_open_interest_and_volume():
    tight = _c(630, "P", 1.99, 2.01, oi=10, volume=900)
    assert not signals.passes_gate(tight, signals.GATE_OI_VOLUME)
    novol = _c(630, "P", 1.99, 2.01, oi=5000, volume=0)
    assert not signals.passes_gate(novol, signals.GATE_OI_VOLUME)
    # the degraded gate cannot check either, and says so by passing on spread
    assert signals.passes_gate(novol, signals.GATE_SPREAD_ONLY)


# ── time-of-day-conditional percentile ──────────────────────────────────────

def test_tod_percentile_only_compares_like_times():
    """0DTE priced move rises mechanically toward expiry, so an all-day
    ranking would fire on the clock rather than on a signal."""
    hist = []
    for day in range(1, 21):
        hist.append({"ts": f"2026-07-{day:02d}T16:00:00Z", "vrp_px": 0.001})
        hist.append({"ts": f"2026-07-{day:02d}T19:45:00Z", "vrp_px": 0.900})
    pct, n = signals.tod_percentile(hist, 0.002, "2026-07-30T16:00:00Z",
                                    "vrp_px")
    assert n == 20                       # only the 12:00 ET observations
    assert pct == pytest.approx(1.0)     # high vs its own time of day

    pct2, n2 = signals.tod_percentile(hist, 0.002, "2026-07-30T19:45:00Z",
                                      "vrp_px")
    assert n2 == 20 and pct2 == pytest.approx(0.0)   # low vs the late cohort


def test_tod_percentile_reports_zero_observations():
    assert signals.tod_percentile([], 1.0, "2026-07-30T16:00:00Z", "vrp_px") \
        == (None, 0)
