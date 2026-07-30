"""Decision journal and deterministic replay (platform upgrades #6, #7).

If a hypothesis fails, we need to know WHY — which means recording exactly
what the detector saw at decision time, not reconstructing it later from
data that has since been rewritten by a schema change or a feature rebuild.

Every decision records: the hypothesis identity and parameter hash, the full
feature vector, the signals that fired, the exact chain rows read for the
legs, the fill inputs and their result, the effective config, and the code
version (git SHA). `replay(conn, trade_id)` reconstructs the decision from
the journal alone and re-runs the fill arithmetic, so a replay that disagrees
with the stored trade is proof the engine changed underneath the record.
"""

import json
import subprocess
from functools import lru_cache

from . import market_calendar as cal


@lru_cache(maxsize=1)
def code_version() -> str:
    """Git SHA of the running code, with a dirty flag."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=__file__.rsplit("/", 2)[0]).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
            cwd=__file__.rsplit("/", 2)[0]).stdout.strip()
        if not sha:
            return "unknown"
        return f"{sha}{'-dirty' if dirty else ''}"
    except Exception:                                    # noqa: BLE001
        return "unknown"


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row is not None else None


def record(conn, *, hypothesis, ctx, decision: str, candidate=None,
           leg_quotes=None, fill=None, trade_id=None, reason: str = "") -> int:
    """Write one journal entry. `decision` is FIRE / UNFILLABLE / NO_FIRE."""
    payload = {
        "features": ctx.features,
        "signals": (candidate.signals if candidate else {}),
        "legs": ([vars(l) for l in candidate.legs] if candidate else []),
        "leg_quotes": [_row_to_dict(q) for _, q in (leg_quotes or [])],
        "fill": ({"ok": fill.ok, "reason": fill.reason,
                  "net_fill": fill.net_fill, "net_mid": fill.net_mid,
                  "spread_pct": fill.spread_pct,
                  "commissions": fill.commissions} if fill else None),
        "config": {
            "spread_max_pct": ctx.cfg.spread_max_pct,
            "min_open_interest": ctx.cfg.min_open_interest,
            "slippage_ticks": ctx.cfg.slippage_ticks,
            "tick_size": ctx.cfg.tick_size,
            "commission_per_contract": ctx.cfg.commission_per_contract,
            "profile": ctx.cfg.profile,
        },
        "gate_mode": ctx.gate_mode,
        "spot": ctx.spot,
        "expiry": ctx.expiry,
        "chain_rows_considered": len(ctx.chain),
        "reason": reason,
    }
    cur = conn.execute(
        "INSERT INTO decision_journal (ts, hypothesis_id, version, "
        "params_hash, symbol, decision, payload_json, code_version, trade_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ctx.ts, hypothesis.id, hypothesis.version, hypothesis.params_hash,
         ctx.symbol, decision, json.dumps(payload, default=str),
         code_version(), trade_id))
    conn.commit()
    return cur.lastrowid


def entries_for_trade(conn, trade_id: int) -> list:
    return conn.execute(
        "SELECT * FROM decision_journal WHERE trade_id=? ORDER BY ts",
        (trade_id,)).fetchall()


def replay(conn, trade_id: int) -> dict:
    """Reconstruct a trade's decision from the journal and re-run its fill.

    Returns a report including `fill_matches`: False means the fill engine's
    behaviour changed since the trade was recorded, which invalidates any
    comparison between old and new trades in the same sample.
    """
    trade = conn.execute("SELECT * FROM paper_trades WHERE id=?",
                         (trade_id,)).fetchone()
    if trade is None:
        return {"error": f"no trade {trade_id}"}
    entries = entries_for_trade(conn, trade_id)
    if not entries:
        return {"error": f"no journal entry for trade {trade_id} — "
                         "recorded before journaling, not replayable"}

    entry = entries[0]
    payload = json.loads(entry["payload_json"])
    report = {
        "trade_id": trade_id,
        "hypothesis": f"{entry['hypothesis_id']}@{entry['version']}",
        "params_hash": entry["params_hash"],
        "code_version_at_decision": entry["code_version"],
        "code_version_now": code_version(),
        "decision_ts": entry["ts"],
        "symbol": entry["symbol"],
        "spot": payload.get("spot"),
        "expiry": payload.get("expiry"),
        "gate_mode": payload.get("gate_mode"),
        "signals": payload.get("signals"),
        "features": payload.get("features"),
        "legs": payload.get("legs"),
        "leg_quotes": payload.get("leg_quotes"),
        "recorded_fill": payload.get("fill"),
        "stored_entry_fill": trade["entry_fill"],
        "stored_exit_fill": trade["exit_fill"],
        "stored_pnl_net": trade["pnl_net"],
        "exit_reason": trade["exit_reason"],
        "outcome": trade["outcome"],
    }

    # Re-run the fill arithmetic against the quotes as recorded
    recorded = payload.get("fill")
    if recorded and recorded.get("ok") and payload.get("leg_quotes"):
        from .config import Config
        from .fill_engine import Leg, PaperFillEngine
        cfg = Config()
        for k, v in (payload.get("config") or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        engine = PaperFillEngine(cfg)
        lq = [(Leg(**leg), q) for leg, q in
              zip(payload["legs"], payload["leg_quotes"]) if q]
        if len(lq) == len(payload["legs"]):
            refill = engine.fill_structure(lq, closing=False)
            report["replayed_fill"] = {
                "ok": refill.ok, "reason": refill.reason,
                "net_fill": refill.net_fill, "net_mid": refill.net_mid,
                "commissions": refill.commissions}
            report["fill_matches"] = (
                refill.ok == recorded["ok"]
                and abs((refill.net_fill or 0)
                        - (recorded["net_fill"] or 0)) < 1e-6)
        else:
            report["replayed_fill"] = None
            report["fill_matches"] = None
    return report


def render_replay(report: dict) -> str:
    if "error" in report:
        return f"REPLAY FAILED: {report['error']}"
    out = ["=" * 74,
           f"REPLAY — trade {report['trade_id']}  ({report['hypothesis']})",
           "=" * 74,
           f"  decision at   {report['decision_ts']}  {report['symbol']} "
           f"spot={report['spot']}",
           f"  params_hash   {report['params_hash']}",
           f"  code version  {report['code_version_at_decision']} at decision"
           f"  /  {report['code_version_now']} now",
           f"  expiry        {report['expiry']}   gate={report['gate_mode']}",
           "",
           "  WHY IT ENTERED (signals at decision time):"]
    for k, v in (report.get("signals") or {}).items():
        out.append(f"    {k:22s} {v}")
    out.append("")
    out.append("  WHAT IT SAW (leg quotes as recorded):")
    for leg, q in zip(report.get("legs") or [],
                      report.get("leg_quotes") or []):
        if q:
            out.append(f"    {leg['expiry']} {leg['strike']}{leg['right']} "
                       f"qty={leg['qty']:+d}  bid={q.get('bid')} "
                       f"ask={q.get('ask')} oi={q.get('open_interest')} "
                       f"vol={q.get('volume')} event_ts={q.get('event_ts')}")
    out.append("")
    rec, rep = report.get("recorded_fill"), report.get("replayed_fill")
    out.append(f"  FILL   recorded net={rec.get('net_fill') if rec else None} "
               f"mid={rec.get('net_mid') if rec else None}")
    if rep:
        out.append(f"         replayed net={rep['net_fill']} "
                   f"mid={rep['net_mid']}")
        match = report.get("fill_matches")
        out.append(f"         MATCH: {match}"
                   + ("" if match else
                      "   <-- fill engine changed since this trade; "
                      "old and new trades are NOT comparable"))
    out.append("")
    out.append(f"  OUTCOME  {report['outcome']} via {report['exit_reason']}   "
               f"net P&L ${report['stored_pnl_net']}")
    out.append("=" * 74)
    return "\n".join(out)
