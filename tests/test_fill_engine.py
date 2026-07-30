"""Hand-computed fill examples (spec §9 week 2: unit-test the fill engine
against hand-computed examples — the component most worth testing)."""

import pytest

from options_research.config import Config
from options_research.fill_engine import PaperFillEngine, Leg
from conftest import quote


@pytest.fixture
def engine():
    return PaperFillEngine(Config())


def test_buy_pays_ask_plus_slippage(engine):
    # bid 1.00 / ask 1.10, mid 1.05, spread 9.52% (fillable), 1 tick slippage
    res = engine.fill_leg(quote(1.00, 1.10), buying=True)
    assert res.fill == 1.11          # ask + 0.01, never the mid
    assert res.mid == 1.05
    assert abs(res.spread_pct - 0.0952) < 0.001


def test_sell_receives_bid_minus_slippage(engine):
    res = engine.fill_leg(quote(1.00, 1.10), buying=False)
    assert res.fill == 0.99          # bid - 0.01


def test_wide_spread_rejected(engine):
    # bid 1.00 / ask 1.15 → spread 13.95% > 10% max → UNFILLABLE
    res = engine.fill_leg(quote(1.00, 1.15), buying=True)
    assert isinstance(res, str) and res.startswith("spread_")


def test_zero_volume_rejected(engine):
    assert engine.fill_leg(quote(volume=0), buying=True) == "zero_volume"


def test_low_open_interest_rejected(engine):
    res = engine.fill_leg(quote(oi=50), buying=True)
    assert isinstance(res, str) and res.startswith("oi_")


def test_crossed_book_rejected(engine):
    assert engine.fill_leg(quote(bid=1.10, ask=1.00), buying=True) == "crossed_book"


def test_missing_quote_rejected(engine):
    assert engine.fill_leg(quote(bid=None, ask=None), buying=True) == "no_quote"


def test_force_fill_bypasses_liquidity_gates_but_not_bad_quotes(engine):
    # exits may cross a horrible spread…
    res = engine.fill_leg(quote(1.00, 1.50, volume=0, oi=0), buying=False,
                          force=True)
    assert res.fill == 0.99
    # …but a crossed book is still not a fill
    assert engine.fill_leg(quote(bid=1.10, ask=1.00), buying=False,
                           force=True) == "crossed_book"


def test_debit_vertical_hand_computed(engine):
    # Buy ATM call at 2.00/2.10 (pay 2.11), sell OTM call at 1.00/1.06
    # (receive 0.99). Net debit = 2.11 - 0.99 = 1.12. Mid = 2.05 - 1.03 = 1.02.
    # Commissions: 2 contracts × $0.65 = $1.30 this side.
    legs = [(Leg("2026-07-31", 100.0, "C", +1), quote(2.00, 2.10)),
            (Leg("2026-07-31", 102.0, "C", -1), quote(1.00, 1.06))]
    fill = engine.fill_structure(legs)
    assert fill.ok
    assert fill.net_fill == pytest.approx(1.12)
    assert fill.net_mid == pytest.approx(1.02)
    assert fill.commissions == pytest.approx(1.30)
    # spread cost paid on entry = net_fill - net_mid = 0.10 per share


def test_credit_spread_hand_computed(engine):
    # Sell 15-delta put at 0.50/0.54 (receive 0.49), buy wing at 0.30/0.33
    # (pay 0.34). Net credit = 0.49 - 0.34 = 0.15 → net_fill = -0.15.
    legs = [(Leg("2026-07-31", 95.0, "P", -1), quote(0.50, 0.54)),
            (Leg("2026-07-31", 93.0, "P", +1), quote(0.30, 0.33))]
    fill = engine.fill_structure(legs)
    assert fill.ok
    assert fill.net_fill == pytest.approx(-0.15)
    assert fill.net_mid == pytest.approx(-0.205)


def test_structure_all_or_none(engine):
    # one unfillable leg rejects the whole structure
    legs = [(Leg("2026-07-31", 100.0, "C", +1), quote(2.00, 2.10)),
            (Leg("2026-07-31", 102.0, "C", -1), quote(volume=0))]
    fill = engine.fill_structure(legs)
    assert not fill.ok and fill.reason == "zero_volume"


def test_closing_flips_legs(engine):
    # closing a long call sells it: receive bid - slip
    legs = [(Leg("2026-07-31", 100.0, "C", +1), quote(2.00, 2.10))]
    fill = engine.fill_structure(legs, closing=True)
    assert fill.ok
    assert fill.net_fill == pytest.approx(-1.99)   # cash in


def test_round_trip_pnl_net_of_costs(engine):
    """Full hand-computed round trip of a debit vertical.

    Entry: net debit 1.12 (above). Exit at better prices:
    long call 2.50/2.60 → sell at 2.49; short call 1.20/1.26 → buy at 1.27.
    Exit net = -(2.49) + 1.27 = -1.22 (cash in 1.22).
    Gross P&L = -(1.12 + (-1.22)) × 100 = $10.00
    Commissions both ways = 4 × 0.65 = $2.60 → net = $7.40.
    """
    entry = engine.fill_structure(
        [(Leg("2026-07-31", 100.0, "C", +1), quote(2.00, 2.10)),
         (Leg("2026-07-31", 102.0, "C", -1), quote(1.00, 1.06))])
    exit_ = engine.fill_structure(
        [(Leg("2026-07-31", 100.0, "C", +1), quote(2.50, 2.60)),
         (Leg("2026-07-31", 102.0, "C", -1), quote(1.20, 1.26))],
        closing=True)
    assert entry.ok and exit_.ok
    pnl_gross = -(entry.net_fill + exit_.net_fill) * 100
    assert pnl_gross == pytest.approx(10.00)
    pnl_net = pnl_gross - entry.commissions - exit_.commissions
    assert pnl_net == pytest.approx(7.40)


def test_mid_fill_would_have_lied(engine):
    """The whole reason this engine exists: the same round trip filled at mid
    claims $30.00 gross vs the honest $10.00 — a 3x overstatement.
    Entry mid 1.02, exit mid −(2.55) + 1.23 = −1.32 → −(1.02 − 1.32)·100 = 30."""
    entry = engine.fill_structure(
        [(Leg("2026-07-31", 100.0, "C", +1), quote(2.00, 2.10)),
         (Leg("2026-07-31", 102.0, "C", -1), quote(1.00, 1.06))])
    exit_ = engine.fill_structure(
        [(Leg("2026-07-31", 100.0, "C", +1), quote(2.50, 2.60)),
         (Leg("2026-07-31", 102.0, "C", -1), quote(1.20, 1.26))],
        closing=True)
    mid_pnl = -(entry.net_mid + exit_.net_mid) * 100
    real_pnl = -(entry.net_fill + exit_.net_fill) * 100
    assert mid_pnl == pytest.approx(30.00)
    assert real_pnl < mid_pnl
