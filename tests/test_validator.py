"""Split discipline, stratum separation, verdict thresholds, and the control
circuit-breakers (PHASE1.md §5-§6)."""

import pytest

from options_research.validator import (assign_splits, evaluate, evaluate_all,
                                        check_controls, measured_friction,
                                        paired_family_comparison,
                                        PipelineHalt, SplitFrozenError,
                                        MIN_TEST_TRADES)


def _trade(conn, strategy, day, pnl_net, outcome="WIN", split=None,
           spread=0.05, params_hash="h1", era="delayed_15m", version="v1"):
    conn.execute(
        "INSERT INTO paper_trades (strategy, version, params_hash, gate_mode, "
        "capital_at_risk, symbol, structure, legs_json, entry_ts, entry_fill, "
        "entry_spread_pct, pnl_net, outcome, split, data_era) "
        "VALUES (?,?,?, 'oi_volume', 200, 'SPY', 'put_credit_spread', '{}', "
        "?, 1.0, ?, ?, ?, ?, ?)",
        (strategy, version, params_hash, f"{day}T15:00:00Z", spread, pnl_net,
         outcome, split, era))
    conn.commit()


def _many(conn, strategy, n=120, pnl_win=15.0, pnl_loss=-20.0, every=3,
          split="test", **kw):
    for i in range(n):
        day = f"2026-{7 + (i // 28):02d}-{(i % 28) + 1:02d}"
        pnl = pnl_loss if i % every == 0 else pnl_win
        _trade(conn, strategy, day, pnl,
               outcome="LOSS" if pnl < 0 else "WIN", split=split, **kw)


# ── split discipline ────────────────────────────────────────────────────────

def test_split_assigned_by_day_and_frozen(conn):
    _trade(conn, "s1", "2026-07-01", 10.0)
    _trade(conn, "s1", "2026-07-20", -5.0, outcome="LOSS")
    res = assign_splits(conn, "2026-07-15")
    assert res == {"cutoff": "2026-07-15", "assigned_train": 1,
                   "assigned_test": 1}
    with pytest.raises(SplitFrozenError):
        assign_splits(conn, "2026-07-10")
    assert assign_splits(conn)["assigned_train"] == 0


def test_existing_split_rows_never_reassigned(conn):
    _trade(conn, "s1", "2026-07-01", 10.0, split="test")
    assign_splits(conn, "2026-07-15")
    assert conn.execute("SELECT split FROM paper_trades").fetchone()["split"] \
        == "test"


# ── stratum separation ──────────────────────────────────────────────────────

def test_data_eras_are_never_pooled(conn):
    """Upgrade #3: a delayed-era and a realtime-era sample are different
    experiments and must produce separate verdicts."""
    _many(conn, "s_era", n=40, era="delayed_15m")
    _many(conn, "s_era", n=40, era="realtime")
    results = evaluate(conn, "s_era", "v1")
    eras = {r["data_era"] for r in results}
    assert eras == {"delayed_15m", "realtime"}
    assert all(r["test"]["n"] == 40 for r in results)   # not 80


def test_parameter_drift_is_never_pooled(conn):
    """Trades made under different frozen parameters are different
    hypotheses, even under one version string."""
    _many(conn, "s_drift", n=30, params_hash="aaa")
    _many(conn, "s_drift", n=30, params_hash="bbb")
    results = evaluate(conn, "s_drift", "v1")
    assert {r["params_hash"] for r in results} == {"aaa", "bbb"}
    assert all(r["test"]["n"] == 30 for r in results)


# ── verdicts ────────────────────────────────────────────────────────────────

def test_verdict_unproven_below_minimum_sample(conn):
    _many(conn, "s2", n=5)
    r = evaluate(conn, "s2", "v1")[0]
    assert r["verdict"] == "UNPROVEN"
    assert "NOT evidence of no edge" in r["notes"]


def test_seductive_win_rate_with_negative_pnl_is_discarded(conn):
    """The Close-Out 2-0 trap: 75% win rate, clearly negative EV → DISCARD.
    Win rate must never rescue a strategy that loses money."""
    _many(conn, "s3", n=140, pnl_win=10.0, pnl_loss=-100.0, every=4)
    r = evaluate(conn, "s3", "v1")[0]
    assert r["test"]["n"] >= MIN_TEST_TRADES
    assert r["test"]["win_rate"] == pytest.approx(0.75)
    assert r["test"]["ev_per_trade"] < 0
    assert r["test"]["ci_hi"] < 0
    assert r["verdict"] == "DISCARD"


def test_marginally_negative_edge_is_unproven_not_discard(conn):
    """A -$2.50/trade edge is NOT resolvable at n=140 (per PHASE1.md §0), so
    the honest verdict is UNPROVEN. The validator must not overclaim in
    either direction."""
    _many(conn, "s3b", n=140, pnl_win=10.0, pnl_loss=-40.0, every=4)
    r = evaluate(conn, "s3b", "v1")[0]
    assert r["test"]["ev_per_trade"] < 0          # negative point estimate...
    assert r["test"]["ci_lo"] < 0 < r["test"]["ci_hi"]   # ...but spans zero
    assert r["verdict"] == "UNPROVEN"


def test_verdict_continue_requires_ci_excluding_zero(conn):
    _many(conn, "s4", n=140, pnl_win=15.0, pnl_loss=-5.0, every=3)
    r = evaluate(conn, "s4", "v1")[0]
    assert r["verdict"] == "CONTINUE"
    assert r["test"]["ci_lo"] > 0


def test_noisy_positive_pnl_is_unproven_not_continue(conn):
    """Positive total P&L with an interval spanning zero is UNPROVEN. Total
    P&L alone must never earn a CONTINUE."""
    for i in range(140):
        day = f"2026-{7 + (i // 28):02d}-{(i % 28) + 1:02d}"
        pnl = 300.0 if i % 20 == 0 else -12.0
        _trade(conn, "s5", day, pnl, outcome="WIN" if pnl > 0 else "LOSS",
               split="test")
    r = evaluate(conn, "s5", "v1")[0]
    assert r["test"]["pnl"] > 0
    assert r["verdict"] == "UNPROVEN"
    assert "uninformative" in r["notes"]


def test_unfillable_excluded_from_metrics(conn):
    _trade(conn, "s6", "2026-07-01", None, outcome="UNFILLABLE", split="test")
    assert evaluate(conn, "s6", "v1")[0]["test"]["n"] == 0


def test_minimum_detectable_edge_is_always_reported(conn):
    _many(conn, "s7", n=60)
    r = evaluate(conn, "s7", "v1")[0]
    assert r["test"]["mde"] is not None and r["test"]["mde"] > 0


# ── control circuit-breakers ────────────────────────────────────────────────

def test_healthy_controls_pass(conn):
    """Both controls losing roughly friction is the expected, healthy state."""
    _many(conn, "c1_orb_control", n=40, pnl_win=-8.0, pnl_loss=-12.0)
    _many(conn, "c2_random_cost", n=40, pnl_win=-9.0, pnl_loss=-11.0)
    rep = check_controls(conn)
    assert not rep["halt"]
    assert rep["c2"]["ev_per_trade"] < 0


def test_positive_leakage_control_halts_pipeline(conn):
    """A profitable ORB on unseen days means the pipeline is broken."""
    _many(conn, "c1_orb_control", n=60, pnl_win=25.0, pnl_loss=20.0)
    with pytest.raises(PipelineHalt) as e:
        check_controls(conn)
    assert "leakage" in str(e.value).lower()


def test_nonnegative_cost_control_invalidates_everything(conn):
    """An untriggered strategy that does not lose friction proves the fill
    engine understates costs."""
    _many(conn, "c2_random_cost", n=60, pnl_win=5.0, pnl_loss=4.0)
    with pytest.raises(PipelineHalt) as e:
        check_controls(conn)
    assert "invalid" in str(e.value).lower()


def test_c2_measures_friction_for_the_stage2_gate(conn):
    _many(conn, "c2_random_cost", n=40, pnl_win=-10.0, pnl_loss=-10.0)
    f = measured_friction(conn)
    assert f["friction_per_trade"] == pytest.approx(10.0)


# ── family comparison ───────────────────────────────────────────────────────

def test_paired_comparison_detects_no_added_value(conn):
    """A conditioning that just reproduces its parent must not look like an
    improvement."""
    for i in range(40):
        day = f"2026-08-{(i % 28) + 1:02d}"
        _trade(conn, "h1_p1_vrp", day, 5.0, split="test")
        _trade(conn, "h2_p1_skew", day, 5.0, split="test")
    res = paired_family_comparison(conn, "h1_p1_vrp", "h2_p1_skew")
    assert "adds nothing" in res["conclusion"]
