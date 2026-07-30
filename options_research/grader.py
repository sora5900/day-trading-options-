"""Grader: marks open paper trades, applies exit rules, computes P&L NET of
costs, and tracks MAE/MFE for exit-rule research (spec §1, §5).

Sign conventions (shared with fill_engine):
  entry_fill / exit_fill are signed per-share CASH OUT for one spread unit
  (debit paid > 0, credit received < 0).
  pnl_gross = -(entry_fill + exit_fill) * 100
  pnl_net   = pnl_gross - commissions(entry + exit)
"""

import json
import logging

from . import db, market_calendar as cal
from .config import Config
from .fill_engine import PaperFillEngine, Leg, CONTRACT_MULT

log = logging.getLogger("grader")


def _legs_of(trade) -> tuple[list[Leg], dict]:
    payload = json.loads(trade["legs_json"])
    legs = [Leg(**l) for l in payload["legs"]]
    return legs, payload["exit_rules"]


def _quotes_for(conn, trade, legs, as_of):
    return [(leg, db.quote_for_leg(conn, trade["symbol"], leg.expiry,
                                   leg.strike, leg.right, as_of))
            for leg in legs]


def _risk_basis(entry_fill: float) -> float:
    """Debit structures: basis = debit paid. Credit: basis = credit received."""
    return abs(entry_fill)


def open_trades(conn):
    return conn.execute(
        "SELECT * FROM paper_trades WHERE exit_ts IS NULL AND outcome IS NULL"
    ).fetchall()


def update_open_trades(conn, ts: str, cfg: Config | None = None):
    """Mark every open trade at `ts`; close the ones whose exit rules fire."""
    cfg = cfg or Config()
    engine = PaperFillEngine(cfg)
    for trade in open_trades(conn):
        if trade["entry_ts"] >= ts:
            # never mark a trade on its own entry cycle: the same snapshot
            # that filled it would instantly show the spread cost as a
            # stop-worthy loss. Exit rules apply from the next cycle on.
            continue
        legs, rules = _legs_of(trade)
        lq = _quotes_for(conn, trade, legs, ts)
        mark = engine.mark_structure(lq, closing=True)
        if mark is None:
            continue   # no usable quotes this cycle; try again next cycle
        unrealized = -(trade["entry_fill"] + mark) * CONTRACT_MULT
        mae = min(trade["max_adverse"] if trade["max_adverse"] is not None
                  else 0.0, unrealized)
        mfe = max(trade["max_favorable"] if trade["max_favorable"] is not None
                  else 0.0, unrealized)
        conn.execute(
            "UPDATE paper_trades SET max_adverse=?, max_favorable=? WHERE id=?",
            (round(mae, 2), round(mfe, 2), trade["id"]))

        basis = _risk_basis(trade["entry_fill"]) * CONTRACT_MULT
        reason = None
        if basis > 0:
            pnl_frac = unrealized / basis
            if rules.get("target_pct") is not None and pnl_frac >= rules["target_pct"]:
                reason = "target"
            elif rules.get("stop_pct") is not None and pnl_frac <= -rules["stop_pct"]:
                reason = "stop"
        if reason is None and cal.et_time_reached(ts, rules["time_stop_et"]):
            reason = "time_stop"
        if reason:
            close_trade(conn, trade, lq, ts, reason, engine)
    conn.commit()


def close_trade(conn, trade, legs_with_quotes, ts, reason, engine):
    fill = engine.fill_structure(legs_with_quotes, closing=True)
    if not fill.ok:
        # Exits always happen — cross the spread and eat the cost honestly.
        fill = engine.fill_structure(legs_with_quotes, closing=True, force=True)
        if not fill.ok:
            log.warning("trade %s unclosable at %s (%s); retrying next cycle",
                        trade["id"], ts, fill.reason)
            return
        reason = f"{reason}_forced"
    pnl_gross = -(trade["entry_fill"] + fill.net_fill) * CONTRACT_MULT
    # commissions column stores BOTH ways; the entry side was recorded at entry
    commissions = round((trade["commissions"] or 0.0) + fill.commissions, 2)
    pnl_net = round(pnl_gross - commissions, 2)
    outcome = "WIN" if pnl_net > 0 else ("LOSS" if pnl_net < 0 else "SCRATCH")
    conn.execute(
        "UPDATE paper_trades SET exit_ts=?, exit_fill=?, exit_mid=?, "
        "exit_reason=?, pnl_gross=?, pnl_net=?, commissions=?, outcome=? "
        "WHERE id=?",
        (ts, fill.net_fill, fill.net_mid, reason, round(pnl_gross, 2),
         pnl_net, commissions, outcome, trade["id"]))
    log.info("closed trade %s (%s %s): %s, net $%.2f", trade["id"],
             trade["strategy"], trade["symbol"], reason, pnl_net)
