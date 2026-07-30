"""The capability gate: the collector must refuse to run rather than collect
data that cannot answer the registered hypotheses.

No field may be silently substituted or locally estimated — a missing
entitlement has to fail loudly on day one, not produce weeks of null Greeks.
"""

import pytest

from options_research.capabilities import (CheckResult, ProbeReport, Status,
                                           REQUIREMENTS, BY_ID)
from options_research.collector import Collector, CapabilityError
from options_research.config import Config
from options_research.sources.base import (epoch_to_iso, classify_delay,
                                           DELAY_REALTIME, DELAY_15M,
                                           DELAY_UNKNOWN)


def _all_pass(exclude=()):
    return [CheckResult(r.id, Status.PASS, "ok") for r in REQUIREMENTS
            if r.id not in exclude]


class FakeSource:
    name = "fake"

    def __init__(self, results):
        self._results = results

    def probe(self):
        return self._results

    def get_underlying(self, symbol):
        return None

    def get_chain(self, *a, **kw):
        return []


def test_report_passes_when_all_blocking_requirements_met():
    r = ProbeReport("fake", _all_pass())
    assert r.can_collect and not r.blocking_failures


def test_non_blocking_failure_does_not_stop_collection():
    results = _all_pass(exclude=("xsp_options", "xsp_underlying"))
    results += [CheckResult("xsp_options", Status.FAIL, "not covered"),
                CheckResult("xsp_underlying", Status.FAIL, "needs Indices")]
    r = ProbeReport("fake", results)
    assert r.can_collect                      # H1 degrades to SPY-only
    assert "NON-BLOCKING GAPS" in r.render()


def test_blocking_failure_stops_collection():
    results = _all_pass(exclude=("chain_greeks",))
    results.append(CheckResult("chain_greeks", Status.FAIL, "delta NULL",
                               "not entitled"))
    r = ProbeReport("fake", results)
    assert not r.can_collect
    out = r.render()
    assert "COLLECTION REFUSED" in out
    assert "chain_greeks" in out
    assert "not entitled" in out              # provider's verbatim error shown


def test_collector_raises_rather_than_collecting_degraded(conn):
    results = _all_pass(exclude=("chain_iv",))
    results.append(CheckResult("chain_iv", Status.FAIL, "iv NULL on 100%"))
    c = Collector(conn, FakeSource(results), Config())
    with pytest.raises(CapabilityError) as e:
        c.require_capability()
    assert "chain_iv" in str(e.value)


def test_probe_results_are_recorded(conn):
    c = Collector(conn, FakeSource(_all_pass()), Config())
    c.probe()
    n = conn.execute("SELECT COUNT(*) AS n FROM capability_probe").fetchone()
    assert n["n"] == len(REQUIREMENTS)


def test_every_requirement_documents_what_it_breaks():
    for r in REQUIREMENTS:
        assert r.why, f"{r.id} must say what breaks without it"


def test_greeks_and_iv_are_blocking():
    """Delta selects the short leg (15Δ) and IV drives both VRP and skew
    triggers. Estimating them locally is explicitly forbidden, so their
    absence must block collection."""
    assert BY_ID["chain_greeks"].blocking
    assert BY_ID["chain_iv"].blocking
    assert BY_ID["exchange_timestamp"].blocking


# ── timestamp normalization: providers are inconsistent about units ─────────

@pytest.mark.parametrize("value", [
    1785000000,             # seconds
    1785000000_000,         # milliseconds
    1785000000_000000,      # microseconds
    1785000000_000000000,   # nanoseconds
])
def test_epoch_units_all_normalize_to_same_instant(value):
    assert epoch_to_iso(value) == "2026-07-25T17:20:00Z"


@pytest.mark.parametrize("bad", [None, 0, -5, "", "abc"])
def test_bad_epochs_return_none_rather_than_guessing(bad):
    assert epoch_to_iso(bad) is None


def test_delay_classification():
    assert classify_delay("2026-07-30T14:00:00Z",
                          "2026-07-30T14:00:30Z") == DELAY_REALTIME
    assert classify_delay("2026-07-30T14:00:00Z",
                          "2026-07-30T14:15:10Z") == DELAY_15M
    assert classify_delay("2026-07-30T14:00:00Z",
                          "2026-07-30T20:00:00Z") == DELAY_UNKNOWN
    assert classify_delay(None, "2026-07-30T14:00:00Z") == DELAY_UNKNOWN
