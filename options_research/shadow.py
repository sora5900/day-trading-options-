"""Shadow pipeline: features → registered hypotheses → paper entry → grading.

Registry-driven: every hypothesis enabled for the active profile is evaluated
each cycle, independently. Adding a family means registering a class — this
file does not change.

Discipline enforced here:
  * One position per (hypothesis, symbol, day). These are day-scale
    hypotheses with one clean observation each, not scaling machines.
  * Every decision is journaled, including NO_FIRE, so a hypothesis that never
    fires is distinguishable from one that was never evaluated.
  * The pre-registration manifest is written on first sight of each
    hypothesis, and a parameter-hash change is refused rather than pooled.
"""

import json
import logging

from . import db, journal, market_calendar as cal, signals
from .config import Config
from .features import compute_features, nearest_expiry
from .fill_engine import PaperFillEngine, CONTRACT_MULT
from .grader import sweep_stranded, update_open_trades
from .hypotheses import DecisionContext, enabled

log = logging.getLogger("shadow")


class PreRegistrationError(RuntimeError):
    """Raised when a hypothesis's parameters changed without a version bump."""


def ensure_manifest(conn, hypothesis) -> None:
    """Record the pre-registration, and refuse silent parameter drift.

    Editing a threshold without bumping `version` changes the params hash. If
    trades already exist under the old hash, pooling them with new ones would
    quietly mix two different rule sets into one sample — so it raises.
    """
    prior = conn.execute(
        "SELECT params_hash FROM hypothesis_manifest WHERE hypothesis_id=? "
        "AND version=?", (hypothesis.id, hypothesis.version)).fetchall()
    hashes = {r["params_hash"] for r in prior}
    if hashes and hypothesis.params_hash not in hashes:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM paper_trades WHERE strategy=? "
            "AND version=?", (hypothesis.id, hypothesis.version)).fetchone()["n"]
        if n:
            raise PreRegistrationError(
                f"{hypothesis.key}: parameters changed (hash "
                f"{sorted(hashes)} -> {hypothesis.params_hash}) but "
                f"{n} trades already exist under this version. Bump `version` "
                f"and start a fresh untouched test period; do not re-tune a "
                f"live registration.")
    conn.execute(
        "INSERT OR IGNORE INTO hypothesis_manifest VALUES (?,?,?,?,?,?,?,?)",
        (hypothesis.id, hypothesis.version, hypothesis.params_hash,
         cal.utcnow_iso(), hypothesis.family, hypothesis.profile,
         int(hypothesis.is_control), json.dumps(hypothesis.manifest(),
                                                default=str)))
    conn.commit()


def _already_traded_today(conn, hypothesis_id, symbol, ts) -> bool:
    day = cal.trading_day_of(ts)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_trades WHERE strategy=? AND symbol=? "
        "AND substr(entry_ts,1,10)=? AND (outcome IS NULL OR outcome!='UNFILLABLE')",
        (hypothesis_id, symbol, day)).fetchone()
    return row["n"] > 0


def _capital_at_risk(cand) -> float | None:
    """Width x 100 for a vertical — the fixed, defined risk."""
    strikes = sorted({l.strike for l in cand.legs})
    if len(strikes) != 2:
        return None
    return round((strikes[1] - strikes[0]) * CONTRACT_MULT, 2)


def _try_enter(conn, ctx, hypothesis, cand, engine: PaperFillEngine):
    lq = [(leg, db.quote_for_leg(conn, cand.symbol, leg.expiry, leg.strike,
                                 leg.right, ctx.ts)) for leg in cand.legs]

    # adaptive liquidity gate, per PHASE1.md §1 — mode recorded on the trade
    for leg, q in lq:
        if q is None or not signals.passes_gate(q, ctx.gate_mode):
            fill = engine.fill_structure(lq, closing=False)
            _record_unfillable(conn, ctx, hypothesis, cand,
                               reason=f"gate_{ctx.gate_mode}", lq=lq)
            return
    fill = engine.fill_structure(lq, closing=False)
    if not fill.ok:
        _record_unfillable(conn, ctx, hypothesis, cand, fill.reason, lq, fill)
        return

    legs_json = json.dumps({"legs": [vars(l) for l in cand.legs],
                            "exit_rules": vars(cand.exit_rules), "units": 1})
    cur = conn.execute(
        "INSERT INTO paper_trades (strategy, version, params_hash, gate_mode, "
        "capital_at_risk, hypothesis_family, symbol, structure, legs_json, "
        "entry_ts, entry_fill, entry_mid, entry_spread_pct, slippage_model, "
        "commissions, max_adverse, max_favorable, data_era) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)",
        (hypothesis.id, hypothesis.version, hypothesis.params_hash,
         ctx.gate_mode, _capital_at_risk(cand), hypothesis.family,
         cand.symbol, cand.structure, legs_json, ctx.ts, fill.net_fill,
         fill.net_mid, fill.spread_pct, engine.slippage_model(),
         fill.commissions, ctx.features.get("delay_class")))
    trade_id = cur.lastrowid
    journal.record(conn, hypothesis=hypothesis, ctx=ctx, decision="FIRE",
                   candidate=cand, leg_quotes=lq, fill=fill, trade_id=trade_id)
    conn.commit()
    log.info("ENTER %s %s @ %s fill=%.2f mid=%.2f spread=%.1f%% gate=%s",
             hypothesis.key, cand.symbol, ctx.ts, fill.net_fill, fill.net_mid,
             fill.spread_pct * 100, ctx.gate_mode)


def _record_unfillable(conn, ctx, hypothesis, cand, reason, lq, fill=None):
    legs_json = json.dumps({"legs": [vars(l) for l in cand.legs],
                            "exit_rules": vars(cand.exit_rules), "units": 1})
    cur = conn.execute(
        "INSERT INTO paper_trades (strategy, version, params_hash, gate_mode, "
        "hypothesis_family, symbol, structure, legs_json, entry_ts, "
        "slippage_model, outcome, data_era) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'UNFILLABLE', ?)",
        (hypothesis.id, hypothesis.version, hypothesis.params_hash,
         ctx.gate_mode, hypothesis.family, cand.symbol, cand.structure,
         legs_json, ctx.ts, reason, ctx.features.get("delay_class")))
    journal.record(conn, hypothesis=hypothesis, ctx=ctx,
                   decision="UNFILLABLE", candidate=cand, leg_quotes=lq,
                   fill=fill, trade_id=cur.lastrowid, reason=reason)
    conn.commit()
    log.info("UNFILLABLE %s %s @ %s: %s", hypothesis.key, cand.symbol, ctx.ts,
             reason)


def run_cycle(conn, ts: str, cfg: Config | None = None,
              journal_no_fire: bool = False):
    """One shadow cycle at decision time `ts` (reads data with event_ts <= ts)."""
    cfg = cfg or Config()
    engine = PaperFillEngine(cfg)
    hypotheses = enabled(cfg.profile)

    for h in hypotheses:
        ensure_manifest(conn, h)

    for symbol in cfg.symbols:
        feats = compute_features(conn, symbol, ts)
        if not feats:
            continue
        chain = db.chain_at(conn, symbol, ts)
        if not chain:
            continue
        u = db.underlying_at(conn, symbol, ts)
        ctx = DecisionContext(
            conn=conn, symbol=symbol, ts=ts, spot=u["last"], features=feats,
            chain=chain,
            expiry=nearest_expiry(chain, cal.trading_day_of(ts)),
            gate_mode=feats.get("gate_mode") or signals.gate_mode(chain),
            cfg=cfg)

        for h in hypotheses:
            if _already_traded_today(conn, h.id, symbol, ts):
                continue
            try:
                cand = h.detect(ctx)
            except Exception:                            # noqa: BLE001
                log.exception("detector %s crashed at %s", h.key, ts)
                continue
            if cand is None:
                if journal_no_fire:
                    journal.record(conn, hypothesis=h, ctx=ctx,
                                   decision="NO_FIRE")
                continue
            _try_enter(conn, ctx, h, cand, engine)

    update_open_trades(conn, ts, cfg)
    # A trade left open past its own session is excluded from every metric —
    # invisible sample loss. Sweep it so it lands in the ledger either way.
    swept = sweep_stranded(conn, ts, cfg)
    if swept["closed"] or swept["ungraded"]:
        log.info("stranded sweep: closed %d, ungraded %d",
                 swept["closed"], swept["ungraded"])
    conn.commit()
