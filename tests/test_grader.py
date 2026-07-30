"""End-to-end grading of a synthetic debit vertical: mark, MAE/MFE, exit
rules, and net-of-costs P&L, all hand-computed."""

import json

import pytest

from options_research.grader import update_open_trades


LEGS = {"legs": [
            {"expiry": "2026-07-31", "strike": 630.0, "right": "C", "qty": 1},
            {"expiry": "2026-07-31", "strike": 632.0, "right": "C", "qty": -1}],
        "exit_rules": {"target_pct": 0.50, "stop_pct": 0.40,
                       "time_stop_et": "15:45"},
        "units": 1}


def _open_trade(conn, entry_fill=1.12, commissions=1.30):
    conn.execute(
        "INSERT INTO paper_trades (strategy, version, symbol, structure, "
        "legs_json, entry_ts, entry_fill, entry_mid, entry_spread_pct, "
        "commissions, max_adverse, max_favorable) "
        "VALUES ('orb_vertical','v1','SPY','call_debit_vertical',?,"
        "'2026-07-30T14:00:00Z',?,1.02,0.05,?,0,0)",
        (json.dumps(LEGS), entry_fill, commissions))


def _quotes(conn, ts, long_bid, long_ask, short_bid, short_ask):
    for strike, bid, ask in [(630.0, long_bid, long_ask),
                             (632.0, short_bid, short_ask)]:
        conn.execute(
            "INSERT OR REPLACE INTO chain_snap VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "SPY", "2026-07-31", strike, "C", bid, ask, (bid + ask) / 2,
             500, 1000, 0.2, 0.5, 0.01, -0.05, 0.1, 630.0))


def test_target_exit_hand_computed(conn):
    _open_trade(conn)   # entry debit 1.12, basis $112, target at +$56
    # long mid 2.55, short mid 0.83 → close mark −1.72 → unrealized +$60 ≥ 56
    _quotes(conn, "2026-07-30T15:00:00Z", 2.50, 2.60, 0.80, 0.86)
    update_open_trades(conn, "2026-07-30T15:00:00Z")
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["exit_reason"] == "target"
    # exit fills: sell long at 2.49, buy short at 0.87 → net −1.62
    assert t["exit_fill"] == pytest.approx(-1.62)
    assert t["pnl_gross"] == pytest.approx(50.0)   # −(1.12 − 1.62) × 100
    assert t["commissions"] == pytest.approx(2.60)
    assert t["pnl_net"] == pytest.approx(47.40)
    assert t["outcome"] == "WIN"
    assert t["max_favorable"] == pytest.approx(60.0)


def test_stop_exit(conn):
    _open_trade(conn)   # stop at −40% of $112 = −$44.80
    # long mid 0.42, short mid 0.21 → mark −0.21 → unrealized −$91
    _quotes(conn, "2026-07-30T15:00:00Z", 0.40, 0.44, 0.20, 0.22)
    update_open_trades(conn, "2026-07-30T15:00:00Z")
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["exit_reason"] == "stop"
    assert t["outcome"] == "LOSS"
    assert t["max_adverse"] == pytest.approx(-91.0)
    assert t["pnl_net"] < -44.80


def test_forced_exit_crosses_wide_spread(conn):
    """A stop with only wide quotes available still exits — reality doesn't
    let you refuse to sell just because the spread got ugly — and the record
    says so via the _forced suffix."""
    _open_trade(conn)
    # short leg spread 33% (> 10% gate) → normal close rejected → forced
    _quotes(conn, "2026-07-30T15:00:00Z", 0.40, 0.44, 0.10, 0.14)
    update_open_trades(conn, "2026-07-30T15:00:00Z")
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["exit_reason"] == "stop_forced"
    assert t["outcome"] == "LOSS"


def test_time_stop(conn):
    _open_trade(conn)
    # flat quotes (mark −1.11 → unrealized −$1, inside target/stop band),
    # but 19:50Z = 15:50 ET ≥ 15:45 time stop
    _quotes(conn, "2026-07-30T19:50:00Z", 1.60, 1.66, 0.50, 0.54)
    update_open_trades(conn, "2026-07-30T19:50:00Z")
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["exit_reason"] == "time_stop"
    assert t["exit_ts"] == "2026-07-30T19:50:00Z"


def test_never_graded_on_own_entry_cycle(conn):
    """The snapshot that filled the trade must not also stop it out: paying
    the spread reads as an instant loss against the same quotes."""
    _open_trade(conn)
    # quotes at the entry timestamp that would look like a -100% mark
    _quotes(conn, "2026-07-30T14:00:00Z", 0.40, 0.44, 0.20, 0.22)
    update_open_trades(conn, "2026-07-30T14:00:00Z")
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["exit_ts"] is None and t["outcome"] is None


def test_no_quotes_keeps_trade_open(conn):
    _open_trade(conn)
    update_open_trades(conn, "2026-07-30T15:00:00Z")   # no chain rows at all
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["exit_ts"] is None and t["outcome"] is None
