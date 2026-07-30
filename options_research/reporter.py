"""Reporter: the shadow ledger — what the system would have traded, what
happened, and the honest verdict per strategy (spec §1).

Win rate is deliberately printed AFTER net P&L. Win rate is vanity; edge vs
the price is the only number that matters.
"""

from collections import defaultdict

from . import market_calendar as cal
from .validator import _metrics, unfillable_rate


def _fmt(v, spec="{:.2f}", none="—"):
    return spec.format(v) if v is not None else none


def build_report(conn) -> str:
    lines = ["=" * 74,
             "OPTIONS RESEARCH — SHADOW LEDGER REPORT",
             f"as of {cal.utcnow_iso()}",
             "=" * 74, ""]

    # data footprint
    days = conn.execute(
        "SELECT COUNT(DISTINCT substr(event_ts,1,10)) AS d FROM underlying_snap"
    ).fetchone()["d"]
    n_chain = conn.execute("SELECT COUNT(*) AS n FROM chain_snap").fetchone()["n"]
    lines.append(f"Data: {days} trading days collected, {n_chain:,} chain rows.")
    dq = conn.execute(
        "SELECT COUNT(*) AS n FROM data_quality WHERE note != '' "
        "AND ts >= date('now', '-7 day')").fetchone()["n"]
    lines.append(f"Data-quality notes in the last 7 days: {dq}.")
    frozen = conn.execute(
        "SELECT cutoff_date, frozen_at FROM split_config WHERE id=1").fetchone()
    if frozen:
        lines.append(f"Split frozen: train < {frozen['cutoff_date']} <= test "
                     f"(frozen {frozen['frozen_at']}).")
    else:
        lines.append("Split not yet frozen — everything is effectively train. "
                     "Freeze a cutoff before drawing ANY conclusion.")
    lines.append("")

    pairs = conn.execute(
        "SELECT DISTINCT strategy, version FROM paper_trades "
        "ORDER BY strategy, version").fetchall()
    if not pairs:
        lines.append("No paper trades yet. That is fine — data collected today "
                     "is what answers questions you haven't thought of yet.")
        return "\n".join(lines)

    for p in pairs:
        s, v = p["strategy"], p["version"]
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE strategy=? AND version=?",
            (s, v)).fetchall()
        train = _metrics([r for r in rows if r["split"] == "train"])
        test = _metrics([r for r in rows if r["split"] == "test"])
        unsplit = _metrics([r for r in rows if r["split"] is None])
        n_open = sum(1 for r in rows if r["outcome"] is None
                     and r["exit_ts"] is None)
        ur = unfillable_rate(conn, s, v)

        lines.append(f"── {s} {v} " + "─" * max(0, 60 - len(s) - len(v)))
        for label, m in (("train", train), ("TEST (unseen)", test),
                         ("unsplit", unsplit)):
            if m["n"] == 0:
                continue
            lines.append(
                f"  {label:14s} net P&L ${_fmt(m['pnl'])}  "
                f"(n={m['n']}, days={m['days']}, "
                f"sharpe={_fmt(m['sharpe'])}, worst day ${_fmt(m['worst_day'])}, "
                f"maxDD ${_fmt(m['max_drawdown'])}, "
                f"winrate {_fmt(m['winrate'], '{:.0%}')} ← vanity metric, listed last)")
            lines.append(
                f"  {'':14s} avg spread paid "
                f"{_fmt(m['avg_spread_pct'], '{:.1%}')} of premium")
        lines.append(f"  open trades: {n_open}   unfillable rate: "
                     f"{_fmt(ur, '{:.1%}')}")
        verdict = conn.execute(
            "SELECT verdict, notes, as_of FROM strategy_verdicts "
            "WHERE strategy=? AND version=? ORDER BY as_of DESC LIMIT 1",
            (s, v)).fetchone()
        if verdict:
            lines.append(f"  VERDICT [{verdict['as_of'][:10]}]: "
                         f"{verdict['verdict']} — {verdict['notes']}")
        else:
            lines.append("  VERDICT: none yet (run validate once the sample "
                         "thresholds are met)")
        lines.append("")

    lines.append("Reminder: the default verdict is DISCARD. Expect 'no edge' "
                 "for most strategies — that is a successful outcome for a "
                 "research system.")
    return "\n".join(lines)


def recent_trades(conn, limit: int = 20) -> str:
    rows = conn.execute(
        "SELECT id, strategy, symbol, structure, entry_ts, entry_fill, "
        "entry_spread_pct, exit_ts, exit_reason, pnl_net, outcome, split "
        "FROM paper_trades ORDER BY entry_ts DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "no paper trades yet"
    out = [f"{'id':>4} {'strategy':16} {'sym':4} {'entry':20} {'net':>8} "
           f"{'outcome':10} {'split':6} exit_reason"]
    for r in rows:
        out.append(f"{r['id']:>4} {r['strategy']:16} {r['symbol']:4} "
                   f"{r['entry_ts'] or '':20} "
                   f"{_fmt(r['pnl_net'], '{:>8.2f}', '       —')} "
                   f"{r['outcome'] or 'OPEN':10} {r['split'] or '—':6} "
                   f"{r['exit_reason'] or ''}")
    return "\n".join(out)
