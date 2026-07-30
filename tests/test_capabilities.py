"""The capability gate: the collector must refuse to run rather than collect
data that cannot answer the registered hypotheses.

No field may be silently substituted or locally estimated — a missing
entitlement has to fail loudly on day one, not produce weeks of null Greeks.
"""

import pytest

from options_research.capabilities import (CheckResult, ProbeReport, Status,
                                           REQUIREMENTS, BY_ID, PHASE1, FULL)
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
    results = _all_pass(exclude=("chain_bid_ask",))
    results.append(CheckResult("chain_bid_ask", Status.FAIL, "bid/ask NULL",
                               "not entitled"))
    r = ProbeReport("fake", results, profile=PHASE1)
    assert not r.can_collect
    out = r.render()
    assert "COLLECTION REFUSED" in out
    assert "chain_bid_ask" in out
    assert "not entitled" in out              # provider's verbatim error shown


def test_collector_raises_rather_than_collecting_degraded(conn):
    results = _all_pass(exclude=("atm_straddle",))
    results.append(CheckResult("atm_straddle", Status.FAIL,
                               "no two-sided ATM quotes"))
    c = Collector(conn, FakeSource(results), Config())
    with pytest.raises(CapabilityError) as e:
        c.require_capability()
    assert "atm_straddle" in str(e.value)


# ── the Phase 1 redesign: price-only requirements ───────────────────────────

def test_phase1_collects_without_greeks_or_iv():
    """The whole point of the redesign: missing vendor Greeks and IV must not
    stop Phase 1, because Phase 1 works in price space."""
    results = _all_pass(exclude=("chain_greeks", "chain_iv"))
    results += [CheckResult("chain_greeks", Status.FAIL, "delta=0%"),
                CheckResult("chain_iv", Status.FAIL, "iv=0%")]
    assert ProbeReport("fake", results, profile=PHASE1).can_collect


def test_full_profile_still_requires_greeks_and_iv():
    """The delta-based hypotheses are unchanged and still demand real Greeks —
    Phase 1 relaxing the bar must not quietly relax it for everyone."""
    results = _all_pass(exclude=("chain_greeks", "chain_iv"))
    results += [CheckResult("chain_greeks", Status.FAIL, "delta=0%"),
                CheckResult("chain_iv", Status.FAIL, "iv=0%")]
    r = ProbeReport("fake", results, profile=FULL)
    assert not r.can_collect
    assert {f.requirement_id for f in r.blocking_failures} == {
        "chain_greeks", "chain_iv"}


def test_prices_are_blocking_in_every_profile():
    """Phase 1 substitutes price-space signals for Greeks, so bid/ask becomes
    load-bearing for the SIGNAL, not just for fills."""
    for profile in (PHASE1, FULL):
        assert BY_ID["chain_bid_ask"].blocks(profile)
        assert BY_ID["exchange_timestamp"].blocks(profile)


def test_atm_straddle_blocks_phase1_only():
    """The straddle mid IS the Phase 1 priced-move estimator and the strike
    selector; the delta-based profile does not need it."""
    assert BY_ID["atm_straddle"].blocks(PHASE1)
    assert not BY_ID["atm_straddle"].blocks(FULL)


def test_liquidity_fields_never_block():
    """OI/volume degrade to a tightened spread-only gate rather than halting
    collection — but the gate mode must be recorded, per PHASE1.md §1."""
    for rid in ("chain_open_interest", "chain_volume"):
        assert not BY_ID[rid].blocks(PHASE1)
        assert not BY_ID[rid].blocks(FULL)


def test_probe_results_are_recorded(conn):
    c = Collector(conn, FakeSource(_all_pass()), Config())
    c.probe()
    n = conn.execute("SELECT COUNT(*) AS n FROM capability_probe").fetchone()
    assert n["n"] == len(REQUIREMENTS)


def test_every_requirement_documents_what_it_breaks():
    for r in REQUIREMENTS:
        assert r.why, f"{r.id} must say what breaks without it"


def test_non_blocking_gaps_are_still_surfaced():
    """A relaxed requirement must never become an invisible one."""
    results = _all_pass(exclude=("chain_greeks",))
    results.append(CheckResult("chain_greeks", Status.FAIL, "delta=0%"))
    out = ProbeReport("fake", results, profile=PHASE1).render()
    assert "NON-BLOCKING GAPS" in out and "chain_greeks" in out


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
