"""Validator: train/test split BY DAY, frozen; honest verdicts on unseen days
only (spec §7 — copy this exactly, it's the whole point).

1. Split by day, chosen in advance, frozen in split_config. Never reassigned.
2. Develop only on train.
3. Metrics in order: net P&L after costs, Sharpe/consistency, drawdown/worst
   day, spread paid vs gross edge, trade count.
4. Minimum sample before ANY verdict: >= 100 test trades across >= 30 days.
   Under that the honest verdict is UNPROVEN, not "promising".
5. The default verdict is DISCARD. A strategy earns continuation.
"""

import statistics
from collections import defaultdict

from . import market_calendar as cal

MIN_TEST_TRADES = 100
MIN_TEST_DAYS = 30


class SplitFrozenError(RuntimeError):
    pass


def assign_splits(conn, cutoff_date: str | None = None) -> dict:
    """Assign train/test by trading day of entry: day < cutoff → train,
    day >= cutoff → test.

    The first cutoff ever passed is FROZEN. Passing a different one later
    raises — the test set is untouchable. Rows keep their split forever;
    only rows with split IS NULL are ever assigned.
    """
    frozen = conn.execute("SELECT cutoff_date FROM split_config WHERE id=1"
                          ).fetchone()
    if frozen:
        if cutoff_date and cutoff_date != frozen["cutoff_date"]:
            raise SplitFrozenError(
                f"split cutoff is frozen at {frozen['cutoff_date']}; "
                f"refusing to reassign to {cutoff_date}. If you need a new "
                f"split, that is a new strategy VERSION, not a new split.")
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


def _metrics(rows):
    graded = [r for r in rows if r["outcome"] in ("WIN", "LOSS", "SCRATCH")]
    n = len(graded)
    if n == 0:
        return {"n": 0, "winrate": None, "pnl": 0.0, "sharpe": None,
                "days": 0, "worst_day": None, "max_drawdown": None,
                "avg_spread_pct": None}
    wins = sum(1 for r in graded if r["outcome"] == "WIN")
    pnl = sum(r["pnl_net"] or 0.0 for r in graded)
    by_day = defaultdict(float)
    for r in graded:
        by_day[r["entry_ts"][:10]] += r["pnl_net"] or 0.0
    daily = list(by_day.values())
    sharpe = None
    if len(daily) >= 2 and statistics.pstdev(daily) > 0:
        # per-day Sharpe, annualized
        sharpe = (statistics.mean(daily) / statistics.pstdev(daily)) * (252 ** 0.5)
    equity = peak = dd = 0.0
    for d in sorted(by_day):
        equity += by_day[d]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    spreads = [r["entry_spread_pct"] for r in graded
               if r["entry_spread_pct"] is not None]
    return {"n": n, "winrate": wins / n, "pnl": round(pnl, 2),
            "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "days": len(by_day), "worst_day": round(min(daily), 2),
            "max_drawdown": round(dd, 2),
            "avg_spread_pct": (round(statistics.mean(spreads), 4)
                               if spreads else None)}


def unfillable_rate(conn, strategy, version):
    row = conn.execute(
        "SELECT SUM(CASE WHEN outcome='UNFILLABLE' THEN 1 ELSE 0 END) AS u, "
        "COUNT(*) AS n FROM paper_trades WHERE strategy=? AND version=?",
        (strategy, version)).fetchone()
    return (row["u"] or 0) / row["n"] if row["n"] else None


def evaluate(conn, strategy: str, version: str, as_of: str | None = None) -> dict:
    """Compute the verdict for one strategy version and record it."""
    as_of = as_of or cal.utcnow_iso()
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE strategy=? AND version=? "
        "AND split IS NOT NULL", (strategy, version)).fetchall()
    train = _metrics([r for r in rows if r["split"] == "train"])
    test = _metrics([r for r in rows if r["split"] == "test"])

    if test["n"] < MIN_TEST_TRADES or test["days"] < MIN_TEST_DAYS:
        verdict = "UNPROVEN"
        notes = (f"insufficient test sample: {test['n']} trades over "
                 f"{test['days']} days (need >= {MIN_TEST_TRADES} over "
                 f">= {MIN_TEST_DAYS})")
    elif test["pnl"] <= 0:
        verdict = "DISCARD"
        notes = f"test-set net P&L {test['pnl']} <= 0 after costs"
    else:
        verdict = "CONTINUE"
        notes = (f"test-set positive: ${test['pnl']} over {test['days']} days; "
                 f"worst day {test['worst_day']}, max DD {test['max_drawdown']}. "
                 f"Continuation earned, not proven — keep walking forward.")
    ur = unfillable_rate(conn, strategy, version)
    if ur is not None:
        notes += f" unfillable_rate={ur:.1%}"
    conn.execute(
        "INSERT OR REPLACE INTO strategy_verdicts VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (strategy, version, as_of, train["n"], train["winrate"], train["pnl"],
         test["n"], test["winrate"], test["pnl"], test["sharpe"],
         test["avg_spread_pct"], verdict, notes))
    conn.commit()
    return {"strategy": strategy, "version": version, "train": train,
            "test": test, "verdict": verdict, "notes": notes}


def evaluate_all(conn, as_of: str | None = None) -> list[dict]:
    pairs = conn.execute(
        "SELECT DISTINCT strategy, version FROM paper_trades").fetchall()
    return [evaluate(conn, p["strategy"], p["version"], as_of) for p in pairs]
