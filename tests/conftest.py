import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from options_research import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


def quote(bid=1.00, ask=1.10, volume=500, oi=1000, **kw):
    """A chain_snap-shaped quote dict for fill-engine tests."""
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    q = {"bid": bid, "ask": ask, "volume": volume, "open_interest": oi,
         "last": mid, "iv": 0.2, "delta": 0.5, "gamma": 0.01,
         "theta": -0.05, "vega": 0.1}
    q.update(kw)
    return q


# ── schema v2 row builders (event_ts is the authoritative event time) ────────

def insert_chain(conn, event_ts, symbol="SPY", expiry="2026-07-31",
                 strike=630.0, right="C", bid=1.00, ask=1.10, volume=500,
                 oi=1000, iv=0.2, delta=0.5, underlying=630.0,
                 fetch_ts=None, source="polygon", delay_class="delayed_15m"):
    conn.execute(
        "INSERT OR REPLACE INTO chain_snap VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_ts, symbol, expiry, strike, right, bid, ask,
         (bid + ask) / 2 if bid and ask else None, volume, oi, iv, delta,
         0.01, -0.05, 0.1, underlying, fetch_ts or event_ts, source,
         delay_class))
    conn.commit()


def insert_underlying(conn, event_ts, symbol="SPY", last=630.0, volume=1000,
                      vwap=None, day_open=630.0, day_high=631.0,
                      day_low=629.0, prev_close=629.5, fetch_ts=None,
                      source="polygon", delay_class="delayed_15m"):
    conn.execute(
        "INSERT OR REPLACE INTO underlying_snap VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_ts, symbol, last, last - 0.01, last + 0.01, volume,
         vwap if vwap is not None else last, day_open, day_high, day_low,
         prev_close, fetch_ts or event_ts, source, delay_class))
    conn.commit()
