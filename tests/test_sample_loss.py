"""Ungraded trades are invisible sample loss, and invisible sample loss is
worse than a wrong number — a systematic collection gap (say, every volatile
afternoon) would silently delete the worst trades and inflate every result."""

import json

import pytest

from options_research.grader import (stranded_trades, sweep_stranded,
                                     update_open_trades)
from conftest import insert_chain

LEGS = {"legs": [{"expiry": "2026-07-30", "strike": 626.0, "right": "P",
                  "qty": -1},
                 {"expiry": "2026-07-30", "strike": 624.0, "right": "P",
                  "qty": 1}],
        "exit_rules": {"target_pct": 0.5, "stop_pct": 2.0,
                       "time_stop_et": "15:45"},
        "units": 1}


def _open_trade(conn, entry_ts="2026-07-30T16:00:00Z", entry_fill=-0.50):
    conn.execute(
        "INSERT INTO paper_trades (strategy, version, symbol, structure, "
        "legs_json, entry_ts, entry_fill, entry_mid, commissions, "
        "max_adverse, max_favorable) VALUES ('h1_p1_vrp','v1','SPY',"
        "'put_credit_spread',?,?,?,-0.52,1.30,0,0)",
        (json.dumps(LEGS), entry_ts, entry_fill))
    conn.commit()


def _quotes(conn, ts, short_bid=0.20, short_ask=0.22, long_bid=0.08,
            long_ask=0.10):
    insert_chain(conn, ts, expiry="2026-07-30", strike=626.0, right="P",
                 bid=short_bid, ask=short_ask, oi=5000, volume=900)
    insert_chain(conn, ts, expiry="2026-07-30", strike=624.0, right="P",
                 bid=long_bid, ask=long_ask, oi=5000, volume=900)


def test_trade_open_past_its_session_is_stranded(conn):
    _open_trade(conn)
    assert stranded_trades(conn, "2026-07-30T20:00:00Z") == []   # same day
    assert len(stranded_trades(conn, "2026-07-31T14:00:00Z")) == 1


def test_sweep_closes_stranded_trade_at_last_available_quote(conn):
    _open_trade(conn)
    _quotes(conn, "2026-07-30T19:30:00Z")          # last quote of that session
    res = sweep_stranded(conn, "2026-07-31T14:00:00Z")
    assert res == {"closed": 1, "ungraded": 0}
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["exit_reason"].startswith("stranded_eod_sweep")
    assert t["outcome"] in ("WIN", "LOSS", "SCRATCH")
    assert t["pnl_net"] is not None


def test_unquotable_stranded_trade_becomes_ungraded_not_invisible(conn):
    """With no quotes at all it cannot be priced — but it must still appear in
    the ledger rather than sitting open forever."""
    _open_trade(conn)
    res = sweep_stranded(conn, "2026-07-31T14:00:00Z")
    assert res == {"closed": 0, "ungraded": 1}
    t = conn.execute("SELECT * FROM paper_trades").fetchone()
    assert t["outcome"] == "UNGRADED"
    assert t["exit_reason"] == "no_quotes_after_entry_day"


def test_ungraded_trades_excluded_from_metrics_but_counted(conn):
    from options_research import stats
    _open_trade(conn)
    sweep_stranded(conn, "2026-07-31T14:00:00Z")
    rows = conn.execute("SELECT * FROM paper_trades").fetchall()
    assert stats.metrics(rows)["n"] == 0          # excluded from the numbers
    n_ungraded = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_trades WHERE outcome='UNGRADED'"
    ).fetchone()["n"]
    assert n_ungraded == 1                        # but visible in the ledger


def test_report_surfaces_sample_loss(conn):
    from options_research.reporter import build_report
    _open_trade(conn)
    sweep_stranded(conn, "2026-07-31T14:00:00Z")
    out = build_report(conn)
    assert "SAMPLE LOSS" in out and "UNGRADED" in out


def test_sweep_does_not_touch_trades_from_today(conn):
    _open_trade(conn, entry_ts="2026-07-30T16:00:00Z")
    _quotes(conn, "2026-07-30T16:30:00Z")
    assert sweep_stranded(conn, "2026-07-30T19:00:00Z") == {"closed": 0,
                                                            "ungraded": 0}
    assert conn.execute("SELECT outcome FROM paper_trades").fetchone()[0] is None
