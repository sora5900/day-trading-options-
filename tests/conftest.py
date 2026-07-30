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
