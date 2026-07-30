"""Shadow pipeline: features → setup detection → paper entry → grading.

Runs on every chain-snapshot cycle. A setup that fires is recorded even when
the fill engine rejects it — the UNFILLABLE rate is itself a key result
(spec §5.3).

One position per (strategy, symbol, day): these are day-trading hypotheses
with one clean observation per day each, not scaling machines.
"""

import json
import logging

from . import db, market_calendar as cal
from .config import Config
from .features import compute_features
from .fill_engine import PaperFillEngine
from .grader import update_open_trades
from .strategies import ALL_STRATEGIES

log = logging.getLogger("shadow")


def _already_traded_today(conn, strategy, symbol, ts) -> bool:
    day = cal.trading_day_of(ts)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_trades WHERE strategy=? AND symbol=? "
        "AND substr(entry_ts, 1, 10) = ? AND outcome IS NOT 'UNFILLABLE'",
        (strategy, symbol, f"{day}",)).fetchone()
    return row["n"] > 0


def _record_setup(conn, ts, cand) -> int:
    cur = conn.execute(
        "INSERT INTO setups (ts, symbol, strategy, version, direction, "
        "rationale, features_json, confidence) VALUES (?,?,?,?,?,?,?,?)",
        (ts, cand.symbol, cand.strategy, cand.version, cand.direction,
         cand.rationale, cand.features_json, cand.confidence))
    return cur.lastrowid


def _try_enter(conn, ts, cand, setup_id, engine: PaperFillEngine):
    lq = [(leg, db.quote_for_leg(conn, cand.symbol, leg.expiry, leg.strike,
                                 leg.right, ts)) for leg in cand.legs]
    fill = engine.fill_structure(lq, closing=False)
    legs_json = json.dumps({
        "legs": [vars(l) for l in cand.legs],
        "exit_rules": vars(cand.exit_rules),
        "units": 1})
    if not fill.ok:
        conn.execute(
            "INSERT INTO paper_trades (setup_id, strategy, version, symbol, "
            "structure, legs_json, entry_ts, slippage_model, outcome) "
            "VALUES (?,?,?,?,?,?,?,?, 'UNFILLABLE')",
            (setup_id, cand.strategy, cand.version, cand.symbol,
             cand.structure, legs_json, ts, fill.reason))
        log.info("UNFILLABLE %s %s @ %s: %s", cand.strategy, cand.symbol, ts,
                 fill.reason)
        return
    conn.execute(
        "INSERT INTO paper_trades (setup_id, strategy, version, symbol, "
        "structure, legs_json, entry_ts, entry_fill, entry_mid, "
        "entry_spread_pct, slippage_model, commissions, max_adverse, "
        "max_favorable) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
        (setup_id, cand.strategy, cand.version, cand.symbol, cand.structure,
         legs_json, ts, fill.net_fill, fill.net_mid, fill.spread_pct,
         engine.slippage_model(), fill.commissions))
    log.info("ENTER %s %s @ %s: fill %.2f (mid %.2f, spread %.1f%%)",
             cand.strategy, cand.symbol, ts, fill.net_fill, fill.net_mid,
             fill.spread_pct * 100)


def run_cycle(conn, ts: str, cfg: Config | None = None):
    """One shadow cycle at decision time `ts` (data ≤ ts only)."""
    cfg = cfg or Config()
    engine = PaperFillEngine(cfg)
    strategies = [S() for S in ALL_STRATEGIES]
    for symbol in cfg.symbols:
        feats = compute_features(conn, symbol, ts)
        if not feats:
            continue
        chain = db.chain_at(conn, symbol, ts)
        if not chain:
            continue
        for strat in strategies:
            if _already_traded_today(conn, strat.name, symbol, ts):
                continue
            try:
                cand = strat.detect(conn, symbol, ts, feats, chain)
            except Exception:
                log.exception("detector %s crashed at %s", strat.name, ts)
                continue
            if cand is None:
                continue
            setup_id = _record_setup(conn, ts, cand)
            _try_enter(conn, ts, cand, setup_id, engine)
    update_open_trades(conn, ts, cfg)
    conn.commit()
