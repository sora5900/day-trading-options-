"""Spec §5.6: a decision at time T may only use data timestamped <= T.
This test asserts it — the bounded query layer must never leak the future."""

from options_research import db


def _chain_row(conn, ts, bid, ask):
    conn.execute(
        "INSERT OR REPLACE INTO chain_snap VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, "SPY", "2026-07-31", 630.0, "C", bid, ask, (bid + ask) / 2,
         500, 1000, 0.2, 0.5, 0.01, -0.05, 0.1, 630.0))


def _under_row(conn, ts, last):
    conn.execute(
        "INSERT OR REPLACE INTO underlying_snap VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, "SPY", last, last - 0.01, last + 0.01, 1000, last, last, last + 1,
         last - 1, last))


def test_chain_at_ignores_future(conn):
    _chain_row(conn, "2026-07-30T14:00:00Z", 1.00, 1.10)
    _chain_row(conn, "2026-07-30T14:05:00Z", 2.00, 2.10)   # the future
    rows = db.chain_at(conn, "SPY", "2026-07-30T14:02:00Z")
    assert len(rows) == 1
    assert rows[0]["bid"] == 1.00          # not the future 2.00


def test_chain_at_boundary_inclusive(conn):
    _chain_row(conn, "2026-07-30T14:00:00Z", 1.00, 1.10)
    rows = db.chain_at(conn, "SPY", "2026-07-30T14:00:00Z")
    assert len(rows) == 1


def test_quote_for_leg_ignores_future(conn):
    _chain_row(conn, "2026-07-30T14:00:00Z", 1.00, 1.10)
    _chain_row(conn, "2026-07-30T14:05:00Z", 9.00, 9.10)
    q = db.quote_for_leg(conn, "SPY", "2026-07-31", 630.0, "C",
                         "2026-07-30T14:03:00Z")
    assert q["bid"] == 1.00


def test_underlying_at_ignores_future(conn):
    _under_row(conn, "2026-07-30T14:00:00Z", 630.0)
    _under_row(conn, "2026-07-30T14:05:00Z", 640.0)
    u = db.underlying_at(conn, "SPY", "2026-07-30T14:01:00Z")
    assert u["last"] == 630.0


def test_underlying_between_bounded_above(conn):
    for i, last in enumerate([630.0, 631.0, 639.0]):
        _under_row(conn, f"2026-07-30T14:0{i * 2}:00Z", last)
    rows = db.underlying_between(conn, "SPY", "2026-07-30T00:00:00Z",
                                 "2026-07-30T14:03:00Z")
    assert [r["last"] for r in rows] == [630.0, 631.0]


def test_features_history_strictly_before(conn):
    for ts, iv in [("2026-07-30T14:00:00Z", 0.15),
                   ("2026-07-30T14:05:00Z", 0.99)]:
        conn.execute(
            "INSERT OR REPLACE INTO features (ts, symbol, iv30) VALUES (?,?,?)",
            (ts, "SPY", iv))
    hist = db.features_history(conn, "SPY", "2026-07-30T14:05:00Z")
    assert [r["iv30"] for r in hist] == [0.15]   # 14:05 row itself excluded


def test_features_compute_uses_only_past(conn):
    """End-to-end: a future price spike must not leak into features at T."""
    from options_research.features import compute_features
    # session 2026-07-30 opens 13:30Z; build a flat morning then a future spike
    for i in range(20):
        _under_row(conn, f"2026-07-30T13:{30 + i:02d}:00Z", 630.0 + i * 0.01)
    _chain_row(conn, "2026-07-30T13:50:00Z", 1.00, 1.10)
    _under_row(conn, "2026-07-30T14:10:00Z", 700.0)        # future spike
    row = compute_features(conn, "SPY", "2026-07-30T13:50:00Z")
    assert row is not None
    # ORB high must come from the first 15 minutes, not the future spike
    assert row["orb_high"] is not None and row["orb_high"] < 700.0
