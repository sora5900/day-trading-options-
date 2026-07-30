"""Split discipline and verdict thresholds (spec §7)."""

import pytest

from options_research.validator import (assign_splits, evaluate,
                                        SplitFrozenError, MIN_TEST_TRADES)


def _trade(conn, strategy, day, pnl_net, outcome="WIN", split=None,
           spread=0.05):
    conn.execute(
        "INSERT INTO paper_trades (strategy, version, symbol, structure, "
        "legs_json, entry_ts, entry_fill, entry_spread_pct, pnl_net, outcome, "
        "split) VALUES (?, 'v1', 'SPY', 'call_debit_vertical', '{}', ?, 1.0, "
        "?, ?, ?, ?)",
        (strategy, f"{day}T15:00:00Z", spread, pnl_net, outcome, split))


def test_split_assigned_by_day_and_frozen(conn):
    _trade(conn, "s1", "2026-07-01", 10.0)
    _trade(conn, "s1", "2026-07-20", -5.0, outcome="LOSS")
    res = assign_splits(conn, "2026-07-15")
    assert res == {"cutoff": "2026-07-15", "assigned_train": 1,
                   "assigned_test": 1}
    rows = {r["entry_ts"][:10]: r["split"] for r in
            conn.execute("SELECT entry_ts, split FROM paper_trades")}
    assert rows["2026-07-01"] == "train" and rows["2026-07-20"] == "test"
    # changing the cutoff later is refused — the test set is untouchable
    with pytest.raises(SplitFrozenError):
        assign_splits(conn, "2026-07-10")
    # re-running with the frozen cutoff (or none) is fine and idempotent
    assert assign_splits(conn)["assigned_train"] == 0


def test_existing_split_rows_never_reassigned(conn):
    _trade(conn, "s1", "2026-07-01", 10.0, split="test")  # pre-assigned
    assign_splits(conn, "2026-07-15")
    row = conn.execute("SELECT split FROM paper_trades").fetchone()
    assert row["split"] == "test"     # day is before cutoff, but row was frozen


def test_verdict_unproven_below_minimum_sample(conn):
    for i in range(5):
        _trade(conn, "s1", f"2026-07-{i + 1:02d}", 10.0, split="test")
    r = evaluate(conn, "s1", "v1")
    assert r["verdict"] == "UNPROVEN"


def test_verdict_discard_on_negative_test_pnl(conn):
    # 120 test trades over 40 days, seductive 75% win rate, NEGATIVE net P&L —
    # the Close-Out 2-0 trap. Must be DISCARD, not "promising".
    for i in range(120):
        day = f"2026-{7 + (i // 30):02d}-{(i % 30) + 1:02d}"
        if i % 4 == 0:
            _trade(conn, "s2", day, -40.0, outcome="LOSS", split="test")
        else:
            _trade(conn, "s2", day, +10.0, outcome="WIN", split="test")
    r = evaluate(conn, "s2", "v1")
    assert r["test"]["n"] >= MIN_TEST_TRADES
    assert r["test"]["winrate"] == pytest.approx(0.75)
    assert r["test"]["pnl"] < 0
    assert r["verdict"] == "DISCARD"


def test_verdict_continue_on_positive_test_pnl(conn):
    for i in range(120):
        day = f"2026-{7 + (i // 30):02d}-{(i % 30) + 1:02d}"
        pnl = -20.0 if i % 3 == 0 else +15.0
        _trade(conn, "s3", day, pnl,
               outcome="LOSS" if pnl < 0 else "WIN", split="test")
    r = evaluate(conn, "s3", "v1")
    assert r["verdict"] == "CONTINUE"
    # verdict is persisted so the record can't be quietly rewritten
    row = conn.execute("SELECT verdict FROM strategy_verdicts "
                       "WHERE strategy='s3'").fetchone()
    assert row["verdict"] == "CONTINUE"


def test_unfillable_excluded_from_metrics(conn):
    _trade(conn, "s4", "2026-07-01", None, outcome="UNFILLABLE", split="test")
    r = evaluate(conn, "s4", "v1")
    assert r["test"]["n"] == 0
