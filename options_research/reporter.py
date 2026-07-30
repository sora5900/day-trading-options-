"""Reporter: the shadow ledger, grouped by hypothesis family.

Ordering is deliberate. Expected value per trade leads; win rate appears near
the bottom labelled as a vanity metric. Every interval carries its minimum
detectable edge, and any interval containing zero is annotated so a null can
never be read as a finding.
"""

from collections import defaultdict

from . import market_calendar as cal, stats
from .hypotheses import enabled
from .validator import measured_friction, unfillable_rate


def _f(v, spec="{:.2f}", none="—"):
    return spec.format(v) if v is not None else none


def _hypothesis_block(conn, hid, version, m_train, m_test, params_hash, era):
    out = []
    ur = unfillable_rate(conn, hid, version)
    out.append(f"  params_hash={params_hash}  data_era={era}  "
               f"unfillable={_f(ur, '{:.1%}')}")
    for label, m in (("train", m_train), ("TEST (unseen)", m_test)):
        if m["n"] == 0:
            continue
        out.append(f"    {label}")
        out.append(f"      EV/trade      ${_f(m['ev_per_trade'])}   "
                   f"95% CI [{_f(m['ci_lo'])}, {_f(m['ci_hi'])}]")
        if m["ci_lo"] is not None and m["ci_hi"] is not None \
                and m["ci_lo"] <= 0 <= m["ci_hi"]:
            out.append(f"                    ^ interval contains zero; "
                       f"minimum detectable edge ${_f(m['mde'])}/trade. "
                       f"A null here is NOT evidence of no edge.")
        out.append(f"      net P&L       ${_f(m['pnl'])}  over n={m['n']} "
                   f"trades / {m['n_days']} days")
        out.append(f"      profit factor {_f(m['profit_factor'])}   "
                   f"sharpe {_f(m['sharpe'])}")
        out.append(f"      avg win ${_f(m['avg_win'])} vs avg loss "
                   f"${_f(m['avg_loss'])}")
        out.append(f"      max DD ${_f(m['max_drawdown'])}   worst day "
                   f"${_f(m['worst_day'])}   largest loss "
                   f"${_f(m['largest_loss'])}")
        out.append(f"      expected shortfall (5%) ${_f(m['expected_shortfall_5'])}"
                   f"   max consec losses {m['max_consecutive_losses']}")
        out.append(f"      capital required ${_f(m['capital_required'])}   "
                   f"return on capital {_f(m['return_on_capital'], '{:.2%}')}")
        out.append(f"      avg spread paid {_f(m['avg_spread_pct'], '{:.1%}')} "
                   f"of premium")
        out.append(f"      win rate {_f(m['win_rate'], '{:.0%}')}  "
                   f"← vanity metric, listed last on purpose")
    return out


def build_report(conn) -> str:
    w = 78
    lines = ["=" * w, "OPTIONS RESEARCH — SHADOW LEDGER", f"as of {cal.utcnow_iso()}",
             "=" * w, ""]

    days = conn.execute(
        "SELECT COUNT(DISTINCT substr(event_ts,1,10)) AS d FROM underlying_snap"
    ).fetchone()["d"]
    n_chain = conn.execute("SELECT COUNT(*) AS n FROM chain_snap").fetchone()["n"]
    lines.append(f"Data: {days} trading days, {n_chain:,} chain rows.")
    eras = [r["delay_class"] for r in conn.execute(
        "SELECT DISTINCT delay_class FROM chain_snap")]
    lines.append(f"Data eras present: {sorted(e for e in eras if e)} "
                 f"(never pooled in one verdict).")
    frozen = conn.execute(
        "SELECT cutoff_date, frozen_at FROM split_config WHERE id=1").fetchone()
    lines.append(
        f"Split frozen: train < {frozen['cutoff_date']} <= test "
        f"(frozen {frozen['frozen_at']})." if frozen else
        "Split NOT frozen — everything is effectively train. Freeze a cutoff "
        "before drawing any conclusion.")
    n_open = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_trades WHERE exit_ts IS NULL "
        "AND outcome IS NULL").fetchone()["n"]
    n_ungraded = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_trades WHERE outcome='UNGRADED'"
    ).fetchone()["n"]
    if n_open or n_ungraded:
        lines.append(f"SAMPLE LOSS: {n_open} still-open, {n_ungraded} UNGRADED "
                     f"(no quotes after entry day). Ungraded trades are "
                     f"EXCLUDED from every metric below — if this number is "
                     f"not near zero, the sample is biased by collection gaps.")
    lines.append("")

    # ── control health first: nothing below is meaningful without it ──────
    lines.append("── CONTROLS " + "─" * (w - 12))
    fr = measured_friction(conn)
    if fr and fr.get("friction_per_trade") is not None:
        lines.append(f"  C2 measured round-trip friction: "
                     f"${fr['friction_per_trade']:.2f}/trade "
                     f"(n={fr['n']}, avg spread "
                     f"{_f(fr.get('avg_spread_pct'), '{:.1%}')})")
        lines.append("     ^ this is the hurdle every hypothesis must clear, "
                     "measured rather than assumed")
    else:
        lines.append(f"  C2 friction not yet measurable "
                     f"({fr['n'] if fr else 0} control trades; need >=20)")
    lines.append("")

    rows = conn.execute(
        "SELECT DISTINCT strategy, version, params_hash, data_era "
        "FROM paper_trades ORDER BY strategy, version").fetchall()
    if not rows:
        lines.append("No paper trades yet. Data collected today is what "
                     "answers questions you have not thought of yet.")
        return "\n".join(lines)

    families = {h.id: h.family for h in enabled(include_controls=True)}
    by_family = defaultdict(list)
    for r in rows:
        by_family[families.get(r["strategy"], "unregistered")].append(r)

    for family in sorted(by_family):
        lines.append(f"── FAMILY: {family} " + "─" * max(0, w - 12 - len(family)))
        for r in by_family[family]:
            hid, ver = r["strategy"], r["version"]
            def sel(split):
                return conn.execute(
                    "SELECT * FROM paper_trades WHERE strategy=? AND version=? "
                    "AND params_hash IS ? AND data_era IS ? AND split=?",
                    (hid, ver, r["params_hash"], r["data_era"], split)
                ).fetchall()
            m_train, m_test = stats.metrics(sel("train")), stats.metrics(sel("test"))
            lines.append(f"  {hid}@{ver}")
            lines.extend(_hypothesis_block(conn, hid, ver, m_train, m_test,
                                           r["params_hash"], r["data_era"]))
            v = conn.execute(
                "SELECT verdict, notes, as_of FROM strategy_verdicts "
                "WHERE strategy=? AND version=? ORDER BY as_of DESC LIMIT 1",
                (hid, ver)).fetchone()
            lines.append(f"    VERDICT [{v['as_of'][:10]}]: {v['verdict']} — "
                         f"{v['notes']}" if v else
                         "    VERDICT: none yet (run validate)")
            lines.append("")

    lines.append("The default verdict is DISCARD. Expect 'no edge' for most "
                 "hypotheses — that is a successful outcome for a research "
                 "system. The failure mode is a system that always finds "
                 "something.")
    lines.append("=" * w)
    return "\n".join(lines)


def recent_trades(conn, limit: int = 20) -> str:
    rows = conn.execute(
        "SELECT id, strategy, symbol, entry_ts, gate_mode, pnl_net, outcome, "
        "split, exit_reason FROM paper_trades ORDER BY entry_ts DESC LIMIT ?",
        (limit,)).fetchall()
    if not rows:
        return "no paper trades yet"
    out = [f"{'id':>4} {'hypothesis':20} {'sym':4} {'entry':20} {'net':>8} "
           f"{'outcome':10} {'gate':12} exit"]
    for r in rows:
        out.append(f"{r['id']:>4} {r['strategy']:20} {r['symbol']:4} "
                   f"{r['entry_ts'] or '':20} "
                   f"{_f(r['pnl_net'], '{:>8.2f}', '       —')} "
                   f"{r['outcome'] or 'OPEN':10} {r['gate_mode'] or '—':12} "
                   f"{r['exit_reason'] or ''}")
    return "\n".join(out)


def manifest_report(conn) -> str:
    """The pre-registration record — what was frozen, and when."""
    rows = conn.execute(
        "SELECT * FROM hypothesis_manifest ORDER BY family, hypothesis_id"
    ).fetchall()
    if not rows:
        return "no hypotheses registered yet"
    out = ["PRE-REGISTRATION MANIFEST", "=" * 78]
    for r in rows:
        out.append(f"{r['hypothesis_id']}@{r['version']}  "
                   f"[{r['family']}]{'  (CONTROL)' if r['is_control'] else ''}")
        out.append(f"  params_hash {r['params_hash']}   registered "
                   f"{r['registered_at']}")
    return "\n".join(out)
