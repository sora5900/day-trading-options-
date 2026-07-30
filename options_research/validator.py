"""Validator: frozen day-split, independent per-hypothesis verdicts, and the
control circuit-breakers.

Enforced invariants:
  1. Split by trading DAY, cutoff frozen on first use; never reassigned.
  2. Delayed-era and realtime-era trades are NEVER pooled (upgrade #3).
  3. Trades with different parameter hashes are NEVER pooled — silent
     parameter drift cannot contaminate a sample.
  4. Confidence intervals bootstrap by DAY (upgrade #4), and every interval is
     reported with its minimum detectable edge.
  5. C1 (leakage control) reading positive HALTS the pipeline.
  6. C2 (cost control) reading >= 0 invalidates every other verdict.
  7. The default verdict is DISCARD; UNPROVEN when the sample cannot resolve.
"""

from collections import defaultdict

from . import market_calendar as cal, stats
from .hypotheses import enabled, get

MIN_TEST_TRADES = 100
MIN_TEST_DAYS = 30


class SplitFrozenError(RuntimeError):
    pass


class PipelineHalt(RuntimeError):
    """A control read anomalously. Results are not trustworthy."""


def assign_splits(conn, cutoff_date: str | None = None) -> dict:
    frozen = conn.execute(
        "SELECT cutoff_date FROM split_config WHERE id=1").fetchone()
    if frozen:
        if cutoff_date and cutoff_date != frozen["cutoff_date"]:
            raise SplitFrozenError(
                f"split cutoff is frozen at {frozen['cutoff_date']}; refusing "
                f"to reassign to {cutoff_date}. A new split is a new "
                f"hypothesis VERSION, not a new cutoff.")
        cutoff_date = frozen["cutoff_date"]
    else:
        if not cutoff_date:
            raise ValueError("no split cutoff frozen yet — pass one to freeze it")
        conn.execute("INSERT INTO split_config (id, cutoff_date, frozen_at) "
                     "VALUES (1, ?, ?)", (cutoff_date, cal.utcnow_iso()))
    n_train = conn.execute(
        "UPDATE paper_trades SET split='train' WHERE split IS NULL "
        "AND substr(entry_ts,1,10) < ?", (cutoff_date,)).rowcount
    n_test = conn.execute(
        "UPDATE paper_trades SET split='test' WHERE split IS NULL "
        "AND substr(entry_ts,1,10) >= ?", (cutoff_date,)).rowcount
    conn.commit()
    return {"cutoff": cutoff_date, "assigned_train": n_train,
            "assigned_test": n_test}


def _strata(conn, hypothesis_id: str, version: str) -> list:
    """Distinct (params_hash, data_era) strata — never pooled across."""
    rows = conn.execute(
        "SELECT DISTINCT params_hash, data_era FROM paper_trades "
        "WHERE strategy=? AND version=?", (hypothesis_id, version)).fetchall()
    return [(r["params_hash"], r["data_era"]) for r in rows]


def _trades(conn, hypothesis_id, version, params_hash, era, split=None):
    q = ("SELECT * FROM paper_trades WHERE strategy=? AND version=? "
         "AND params_hash IS ? AND data_era IS ?")
    args = [hypothesis_id, version, params_hash, era]
    if split:
        q += " AND split=?"
        args.append(split)
    return conn.execute(q, args).fetchall()


def unfillable_rate(conn, hypothesis_id, version) -> float | None:
    row = conn.execute(
        "SELECT SUM(CASE WHEN outcome='UNFILLABLE' THEN 1 ELSE 0 END) AS u, "
        "COUNT(*) AS n FROM paper_trades WHERE strategy=? AND version=?",
        (hypothesis_id, version)).fetchone()
    return (row["u"] or 0) / row["n"] if row["n"] else None


def evaluate(conn, hypothesis_id: str, version: str,
             as_of: str | None = None) -> list:
    """Evaluate one hypothesis. Returns one verdict per (hash, era) stratum."""
    as_of = as_of or cal.utcnow_iso()
    out = []
    for params_hash, era in _strata(conn, hypothesis_id, version):
        train = stats.metrics(_trades(conn, hypothesis_id, version,
                                      params_hash, era, "train"))
        test = stats.metrics(_trades(conn, hypothesis_id, version,
                                     params_hash, era, "test"))
        verdict, notes = _verdict(test)
        ur = unfillable_rate(conn, hypothesis_id, version)
        if ur is not None:
            notes += f" unfillable_rate={ur:.1%}."
        gate_modes = {r["gate_mode"] for r in
                      _trades(conn, hypothesis_id, version, params_hash, era)}
        notes += f" gate_modes={sorted(m for m in gate_modes if m)}."
        conn.execute(
            "INSERT OR REPLACE INTO strategy_verdicts VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hypothesis_id, version, as_of, train["n"], train["win_rate"],
             train["pnl"], test["n"], test["win_rate"], test["pnl"],
             test["sharpe"], test["avg_spread_pct"], verdict, notes))
        out.append({"hypothesis": hypothesis_id, "version": version,
                    "params_hash": params_hash, "data_era": era,
                    "train": train, "test": test, "verdict": verdict,
                    "notes": notes})
    conn.commit()
    return out


def _verdict(test: dict) -> tuple:
    """Default DISCARD. UNPROVEN when the sample cannot resolve anything."""
    if test["n"] == 0:
        return "UNPROVEN", "no graded test trades."
    if test["n"] < MIN_TEST_TRADES or test["n_days"] < MIN_TEST_DAYS:
        return "UNPROVEN", (
            f"insufficient test sample: {test['n']} trades over "
            f"{test['n_days']} days (need >={MIN_TEST_TRADES} over "
            f">={MIN_TEST_DAYS}). Minimum detectable edge at this size is "
            f"${test['mde']}/trade — a null here is NOT evidence of no edge.")
    lo, hi = test["ci_lo"], test["ci_hi"]
    if lo is not None and hi is not None:
        if hi < 0:
            return "DISCARD", (
                f"95% day-bootstrapped CI on EV/trade is [{lo}, {hi}] — "
                f"entirely negative after costs.")
        if lo > 0:
            return "CONTINUE", (
                f"95% CI [{lo}, {hi}] excludes zero on the positive side; "
                f"EV/trade ${test['ev_per_trade']}. Continuation earned, not "
                f"proven — forward paper period still required.")
        return "UNPROVEN", (
            f"95% CI [{lo}, {hi}] contains zero; minimum detectable edge "
            f"${test['mde']}/trade. Keep collecting — a null at this "
            f"resolution is uninformative, not negative.")
    if test["pnl"] <= 0:
        return "DISCARD", f"test net P&L {test['pnl']} <= 0 after costs."
    return "UNPROVEN", "interval unavailable (too few distinct test days)."


# ── control circuit-breakers ────────────────────────────────────────────────

def check_controls(conn) -> dict:
    """Inspect the negative controls. Raises PipelineHalt on C1 anomaly.

    C1 positive  → look-ahead leakage or fill optimism; results are void.
    C2 >= 0      → the cost model understates friction; ALL verdicts void.
    """
    report = {"c1": None, "c2": None, "halt": False, "messages": []}

    for h in enabled(include_controls=True):
        if not h.is_control:
            continue
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE strategy=? AND version=? "
            "AND split='test'", (h.id, h.version)).fetchall()
        m = stats.metrics(rows)
        entry = {"n": m["n"], "ev_per_trade": m["ev_per_trade"],
                 "ci": (m["ci_lo"], m["ci_hi"]), "mde": m["mde"]}

        if h.measures_friction:
            report["c2"] = entry
            if m["n"] >= 20 and m["ci_lo"] is not None and m["ci_lo"] >= 0:
                report["halt"] = True
                report["messages"].append(
                    f"C2 cost control EV/trade CI is [{m['ci_lo']}, "
                    f"{m['ci_hi']}] — an untriggered strategy should lose "
                    f"approximately the round-trip friction. Reading >= 0 "
                    f"means the fill engine understates costs, and EVERY "
                    f"other verdict in the system is invalid.")
        else:
            report["c1"] = entry
            if m["n"] >= 20 and m["ci_lo"] is not None and m["ci_lo"] > 0:
                report["halt"] = True
                report["messages"].append(
                    f"C1 leakage control EV/trade CI is [{m['ci_lo']}, "
                    f"{m['ci_hi']}] — excludes zero on the POSITIVE side. "
                    f"ORB has no mechanical basis, so this is evidence of "
                    f"look-ahead leakage or fill optimism, not of an edge.")

    if report["halt"]:
        raise PipelineHalt(" ".join(report["messages"]))
    return report


def measured_friction(conn) -> dict | None:
    """Round-trip friction measured by C2 — the input to H3-P1's stage-2 gate.

    Uses measured cost rather than an assumed figure, and converges far faster
    than any edge estimate (PHASE1.md §0).
    """
    h = get("c2_random_cost@v1")
    if h is None:
        return None
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE strategy=? AND version=?",
        (h.id, h.version)).fetchall()
    m = stats.metrics(rows)
    if m["n"] < 20:
        return {"n": m["n"], "friction_per_trade": None,
                "note": f"only {m['n']} control trades; need >=20"}
    return {"n": m["n"],
            "friction_per_trade": (-m["ev_per_trade"]
                                   if m["ev_per_trade"] is not None else None),
            "ci": (m["ci_lo"], m["ci_hi"]),
            "avg_spread_pct": m["avg_spread_pct"]}


def evaluate_all(conn, as_of: str | None = None) -> list:
    pairs = conn.execute(
        "SELECT DISTINCT strategy, version FROM paper_trades").fetchall()
    out = []
    for p in pairs:
        out.extend(evaluate(conn, p["strategy"], p["version"], as_of))
    return out


def paired_family_comparison(conn, a_id: str, b_id: str, version: str = "v1"
                             ) -> dict:
    """Compare two hypotheses on the SAME days (PHASE1.md §3).

    Answers whether a conditioning adds anything over its parent, rather than
    just producing fewer trades of the same thing.
    """
    def by_day(hid):
        rows = conn.execute(
            "SELECT entry_ts, pnl_net FROM paper_trades WHERE strategy=? "
            "AND version=? AND pnl_net IS NOT NULL", (hid, version)).fetchall()
        d = defaultdict(list)
        for r in rows:
            d[r["entry_ts"][:10]].append(r["pnl_net"])
        return d

    a, b = by_day(a_id), by_day(b_id)
    shared = sorted(set(a) & set(b))
    if len(shared) < 3:
        return {"n_shared_days": len(shared),
                "note": "too few shared days to compare"}
    diffs = [sum(b[d]) / len(b[d]) - sum(a[d]) / len(a[d]) for d in shared]
    fake = [{"entry_ts": f"{d}T00:00:00Z", "pnl_net": v}
            for d, v in zip(shared, diffs)]
    point, lo, hi, n = stats.day_bootstrap_ci(fake)
    return {"a": a_id, "b": b_id, "n_shared_days": len(shared),
            "mean_diff_per_day": round(point, 2) if point is not None else None,
            "ci": (lo, hi),
            "conclusion": (
                "conditioning adds nothing (CI contains zero)"
                if (lo is None or hi is None or (lo <= 0 <= hi))
                else ("conditioning helps" if lo > 0 else "conditioning hurts"))}
