"""Spec §5.6: a decision at time T may only use data timestamped <= T.

Schema v2: the bound is the EXCHANGE clock (`event_ts`), never the fetch
clock. The final test in this file is the one that matters most — it proves
a late-arriving quote is placed by when it HAPPENED, not by when we saw it.
"""

from options_research import db
from conftest import insert_chain, insert_underlying


def test_chain_at_ignores_future(conn):
    insert_chain(conn, "2026-07-30T14:00:00Z", bid=1.00, ask=1.10)
    insert_chain(conn, "2026-07-30T14:05:00Z", bid=2.00, ask=2.10)  # future
    rows = db.chain_at(conn, "SPY", "2026-07-30T14:02:00Z")
    assert len(rows) == 1
    assert rows[0]["bid"] == 1.00


def test_chain_at_boundary_inclusive(conn):
    insert_chain(conn, "2026-07-30T14:00:00Z")
    assert len(db.chain_at(conn, "SPY", "2026-07-30T14:00:00Z")) == 1


def test_quote_for_leg_ignores_future(conn):
    insert_chain(conn, "2026-07-30T14:00:00Z", bid=1.00, ask=1.10)
    insert_chain(conn, "2026-07-30T14:05:00Z", bid=9.00, ask=9.10)
    q = db.quote_for_leg(conn, "SPY", "2026-07-31", 630.0, "C",
                         "2026-07-30T14:03:00Z")
    assert q["bid"] == 1.00


def test_underlying_at_ignores_future(conn):
    insert_underlying(conn, "2026-07-30T14:00:00Z", last=630.0)
    insert_underlying(conn, "2026-07-30T14:05:00Z", last=640.0)
    assert db.underlying_at(conn, "SPY", "2026-07-30T14:01:00Z")["last"] == 630.0


def test_underlying_between_bounded_above(conn):
    for i, last in enumerate([630.0, 631.0, 639.0]):
        insert_underlying(conn, f"2026-07-30T14:0{i * 2}:00Z", last=last)
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
    assert [r["iv30"] for r in hist] == [0.15]


def test_features_compute_uses_only_past(conn):
    from options_research.features import compute_features
    for i in range(20):
        insert_underlying(conn, f"2026-07-30T13:{30 + i:02d}:00Z",
                          last=630.0 + i * 0.01)
    insert_chain(conn, "2026-07-30T13:50:00Z")
    insert_underlying(conn, "2026-07-30T14:10:00Z", last=700.0)  # future spike
    row = compute_features(conn, "SPY", "2026-07-30T13:50:00Z")
    assert row is not None
    assert row["orb_high"] is not None and row["orb_high"] < 700.0


def test_bound_is_exchange_clock_not_fetch_clock(conn):
    """A quote that HAPPENED at 14:00 but was fetched at 14:20 is visible to a
    14:05 decision. Binding on fetch_ts would hide it — and on a delayed feed
    that would silently shift every entry 15 minutes late."""
    insert_chain(conn, "2026-07-30T14:00:00Z", bid=1.00, ask=1.10,
                 fetch_ts="2026-07-30T14:20:00Z")
    rows = db.chain_at(conn, "SPY", "2026-07-30T14:05:00Z")
    assert len(rows) == 1
    assert rows[0]["bid"] == 1.00
    assert rows[0]["fetch_ts"] == "2026-07-30T14:20:00Z"


def test_sources_coexist_without_colliding(conn):
    """Two providers may hold the same contract at the same event time —
    required for cross-checking, so source is part of the key."""
    insert_chain(conn, "2026-07-30T14:00:00Z", bid=1.00, ask=1.10,
                 source="polygon")
    insert_chain(conn, "2026-07-30T14:00:00Z", bid=1.02, ask=1.12,
                 source="tradier")
    assert len(db.chain_at(conn, "SPY", "2026-07-30T14:00:00Z")) == 2
    assert len(db.chain_at(conn, "SPY", "2026-07-30T14:00:00Z",
                           source="polygon")) == 1
