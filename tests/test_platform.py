"""The extensibility contract: adding a hypothesis family must be cheap, and
pre-registration must be mechanically enforced rather than trusted."""

from dataclasses import dataclass

import pytest

from options_research import journal
from options_research.capabilities import PHASE1, FULL
from options_research.config import Config
from options_research.fill_engine import Leg
from options_research.hypotheses import (Candidate, DecisionContext, ExitRules,
                                         Hypothesis, Params, REGISTRY, enabled,
                                         get, manifest_all, params_hash,
                                         register)
from options_research.shadow import ensure_manifest, PreRegistrationError
from conftest import insert_chain, insert_underlying


# ── registry ────────────────────────────────────────────────────────────────

def test_all_registered_hypotheses_load():
    keys = set(REGISTRY)
    assert {"h1_p1_vrp@v1", "h2_p1_skew@v1", "h3_p1_momentum@v1",
            "c1_orb_control@v1", "c2_random_cost@v1"} <= keys


def test_every_hypothesis_declares_family_and_docstring():
    for h in enabled(PHASE1):
        assert h.family != "unclassified", f"{h.key} needs a family"
        assert (h.__doc__ or "").strip(), f"{h.key} needs a rationale docstring"


def test_duplicate_registration_is_refused():
    """Two rule sets under one identity would silently pool their trades."""
    with pytest.raises(ValueError, match="duplicate"):
        @register
        class Dup(Hypothesis):
            id, version = "h1_p1_vrp", "v1"

            def detect(self, ctx):
                return None


def test_full_profile_hypotheses_excluded_from_phase1():
    @register
    class NeedsGreeks(Hypothesis):
        """Requires vendor Greeks."""
        id, version, family, profile = "needs_greeks", "v1", "test", FULL

        def detect(self, ctx):
            return None

    try:
        assert "needs_greeks" not in {h.id for h in enabled(PHASE1)}
        assert "needs_greeks" in {h.id for h in enabled(FULL)}
    finally:
        REGISTRY.pop("needs_greeks@v1", None)


def test_controls_can_be_excluded():
    ids = {h.id for h in enabled(PHASE1, include_controls=False)}
    assert "c1_orb_control" not in ids and "c2_random_cost" not in ids


def test_adding_a_family_requires_only_a_class():
    """The whole extensibility claim, exercised: subclass, register, done —
    manifest, hashing and profile filtering all come free."""
    @dataclass(frozen=True)
    class MyParams(Params):
        threshold: float = 0.42

    @register
    class MyThing(Hypothesis):
        """A new family added with no platform changes."""
        id, version, family = "my_thing", "v1", "mean_reversion"
        params = MyParams()

        def detect(self, ctx):
            return None

    try:
        h = get("my_thing@v1")
        assert h is not None and h.family == "mean_reversion"
        assert h.params_hash and len(h.params_hash) == 16
        m = h.manifest()
        assert m["params"]["threshold"] == 0.42
        assert m["docstring"].startswith("A new family")
        assert "my_thing" in {x["id"] for x in manifest_all(PHASE1)}
    finally:
        REGISTRY.pop("my_thing@v1", None)


# ── pre-registration enforcement ────────────────────────────────────────────

def test_params_hash_changes_with_any_parameter():
    @dataclass(frozen=True)
    class P(Params):
        a: float = 1.0

    @dataclass(frozen=True)
    class Q(Params):
        a: float = 1.01

    assert params_hash(P()) != params_hash(Q())
    assert params_hash(P()) == params_hash(P())      # stable across calls


def test_manifest_recorded_on_first_sight(conn):
    h = get("h1_p1_vrp@v1")
    ensure_manifest(conn, h)
    row = conn.execute(
        "SELECT * FROM hypothesis_manifest WHERE hypothesis_id=?",
        ("h1_p1_vrp",)).fetchone()
    assert row["params_hash"] == h.params_hash
    assert row["family"] == "variance_risk_premium"


def test_silent_parameter_drift_is_refused(conn):
    """Editing a threshold without bumping the version must raise once trades
    exist — otherwise two rule sets quietly pool into one sample."""
    h = get("h1_p1_vrp@v1")
    ensure_manifest(conn, h)
    conn.execute(
        "INSERT INTO paper_trades (strategy, version, params_hash, entry_ts, "
        "pnl_net, outcome) VALUES ('h1_p1_vrp','v1',?, "
        "'2026-07-30T16:00:00Z', 5.0, 'WIN')", (h.params_hash,))
    conn.commit()

    class Drifted:
        id, version, family, profile = "h1_p1_vrp", "v1", "x", PHASE1
        key = "h1_p1_vrp@v1"
        is_control = False
        params_hash = "deadbeefdeadbeef"

        def manifest(self):
            return {}

    with pytest.raises(PreRegistrationError, match="parameters changed"):
        ensure_manifest(conn, Drifted())


def test_drift_allowed_before_any_trades_exist(conn):
    """Pre-registration is only binding once it has produced observations."""
    h = get("h1_p1_vrp@v1")
    ensure_manifest(conn, h)

    class Drifted:
        id, version, family, profile = "h1_p1_vrp", "v1", "x", PHASE1
        key = "h1_p1_vrp@v1"
        is_control = False
        params_hash = "0123456789abcdef"

        def manifest(self):
            return {}

    ensure_manifest(conn, Drifted())     # no trades yet → allowed


# ── controls behave as instruments, not strategies ──────────────────────────

def test_controls_are_flagged_and_c2_measures_friction():
    c1, c2 = get("c1_orb_control@v1"), get("c2_random_cost@v1")
    assert c1.is_control and not c1.measures_friction
    assert c2.is_control and c2.measures_friction


def test_random_control_is_deterministic():
    """Seeded firing must reproduce exactly, or replay is meaningless."""
    c2 = get("c2_random_cost@v1")
    ctx = DecisionContext(conn=None, symbol="SPY", ts="2026-07-30T16:00:00Z",
                          spot=630.0, features={}, chain=[], expiry=None,
                          gate_mode="oi_volume", cfg=Config())
    assert c2._fires(ctx) == c2._fires(ctx)
    ctx2 = DecisionContext(conn=None, symbol="QQQ", ts=ctx.ts, spot=560.0,
                           features={}, chain=[], expiry=None,
                           gate_mode="oi_volume", cfg=Config())
    # different symbol draws independently from the same seed
    assert isinstance(c2._fires(ctx2), bool)


def test_momentum_stage3_gated_until_stage2_clears():
    """H3-P1 must emit nothing until the friction hurdle is passed, so the
    options test set is never opened prematurely."""
    h = get("h3_p1_momentum@v1")
    assert h.params.stage2_cleared is False
    ctx = DecisionContext(conn=None, symbol="SPY", ts="2026-07-30T19:30:00Z",
                          spot=630.0, features={}, chain=[],
                          expiry="2026-07-30", gate_mode="oi_volume",
                          cfg=Config())
    assert h.detect(ctx) is None


# ── journal + replay ────────────────────────────────────────────────────────

def test_journal_records_and_replays(conn):
    from options_research.shadow import run_cycle
    day = "2026-07-30"
    for i in range(40):
        insert_underlying(conn, f"{day}T{13 + (30 + i) // 60:02d}:"
                                f"{(30 + i) % 60:02d}:00Z",
                          last=630.0, vwap=630.0)
    for strike in range(620, 641):
        for right in ("C", "P"):
            insert_chain(conn, f"{day}T16:00:00Z", expiry=day,
                         strike=float(strike), right=right,
                         bid=1.00, ask=1.05, oi=5000, volume=900)
    insert_underlying(conn, f"{day}T16:00:00Z", last=630.0, vwap=630.0)
    run_cycle(conn, f"{day}T16:00:00Z", Config(), journal_no_fire=True)

    n = conn.execute("SELECT COUNT(*) AS n FROM decision_journal").fetchone()
    assert n["n"] > 0, "every decision must be journaled, including NO_FIRE"
    decisions = {r["decision"] for r in
                 conn.execute("SELECT decision FROM decision_journal")}
    assert "NO_FIRE" in decisions


def test_replay_of_missing_trade_is_explicit(conn):
    rep = journal.replay(conn, 9999)
    assert "error" in rep
    assert journal.render_replay(rep).startswith("REPLAY FAILED")


def test_code_version_is_recorded():
    v = journal.code_version()
    assert isinstance(v, str) and v
