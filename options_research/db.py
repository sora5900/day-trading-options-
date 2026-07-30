"""SQLite schema and access helpers.

Schema follows OPTIONS_RESEARCH_SPEC.md §3 exactly, plus one table
(`split_config`) that freezes the train/test split rule so it cannot be
quietly changed after analysis has begun.

Raw tables (underlying_snap, chain_snap) are never edited and never derived.
Features are recomputable — safe to drop and rebuild.
"""

import os
import sqlite3

SCHEMA = """
-- ── RAW TRUTH (never edited, never derived) ──────────────────────────────
CREATE TABLE IF NOT EXISTS underlying_snap (
  ts TEXT, symbol TEXT,
  last REAL, bid REAL, ask REAL, volume INTEGER,
  vwap REAL, day_open REAL, day_high REAL, day_low REAL, prev_close REAL,
  PRIMARY KEY (ts, symbol));

CREATE TABLE IF NOT EXISTS chain_snap (
  ts TEXT, symbol TEXT, expiry TEXT, strike REAL, right TEXT,
  bid REAL, ask REAL, last REAL, volume INTEGER, open_interest INTEGER,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  underlying REAL,
  PRIMARY KEY (ts, symbol, expiry, strike, right));
CREATE INDEX IF NOT EXISTS idx_chain_lookup ON chain_snap(symbol, expiry, ts);

-- ── DERIVED FEATURES (recomputable; safe to drop and rebuild) ────────────
CREATE TABLE IF NOT EXISTS features (
  ts TEXT, symbol TEXT,
  iv30 REAL, iv_rank REAL, iv_percentile REAL,
  realized_vol_5m REAL, realized_vol_30m REAL,
  vrp REAL,
  orb_high REAL, orb_low REAL, orb_broken TEXT,
  vwap_dev REAL, atr_pct REAL,
  skew_25d REAL, term_slope REAL,
  liquidity_score REAL,
  regime TEXT,
  PRIMARY KEY (ts, symbol));

-- ── SETUPS & PAPER TRADES ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS setups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, symbol TEXT, strategy TEXT, version TEXT,
  direction TEXT, rationale TEXT, features_json TEXT,
  confidence REAL);

CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, setup_id INTEGER,
  strategy TEXT, version TEXT, symbol TEXT, structure TEXT,
  legs_json TEXT,
  entry_ts TEXT, entry_fill REAL, entry_mid REAL, entry_spread_pct REAL,
  exit_ts TEXT, exit_fill REAL, exit_mid REAL, exit_reason TEXT,
  slippage_model TEXT,
  pnl_gross REAL, pnl_net REAL, commissions REAL,
  max_adverse REAL, max_favorable REAL,
  outcome TEXT,
  split TEXT);

CREATE TABLE IF NOT EXISTS strategy_verdicts (
  strategy TEXT, version TEXT, as_of TEXT,
  train_n INTEGER, train_winrate REAL, train_pnl REAL,
  test_n INTEGER, test_winrate REAL, test_pnl REAL, test_sharpe REAL,
  avg_spread_paid_pct REAL, verdict TEXT, notes TEXT,
  PRIMARY KEY (strategy, version, as_of));

CREATE TABLE IF NOT EXISTS data_quality (
  ts TEXT, symbol TEXT, source TEXT,
  stale_secs REAL, missing_contracts INTEGER, wide_spread_frac REAL,
  crossed_book INTEGER, note TEXT);

-- ── SPLIT FREEZE (not in spec schema, demanded by spec §3/§7 discipline) ─
-- One row, written once. The train/test cutoff date can never be silently
-- changed: attempting to assign splits with a different cutoff is an error.
CREATE TABLE IF NOT EXISTS split_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cutoff_date TEXT NOT NULL,
  frozen_at TEXT NOT NULL);
"""


def connect(db_path: str) -> sqlite3.Connection:
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


# ── No-look-ahead query layer ────────────────────────────────────────────────
# Every decision-time read goes through these. A decision at time T may only
# use data timestamped <= T (spec §5 rule 6). tests/test_no_lookahead.py
# asserts this behaviour.

def chain_at(conn, symbol: str, as_of: str):
    """Latest chain snapshot at or before `as_of`. Never returns future rows."""
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM chain_snap WHERE symbol=? AND ts<=?",
        (symbol, as_of)).fetchone()
    if not row or row["ts"] is None:
        return []
    return conn.execute(
        "SELECT * FROM chain_snap WHERE symbol=? AND ts=?",
        (symbol, row["ts"])).fetchall()


def underlying_at(conn, symbol: str, as_of: str):
    """Latest underlying snapshot at or before `as_of`."""
    return conn.execute(
        "SELECT * FROM underlying_snap WHERE symbol=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (symbol, as_of)).fetchone()


def underlying_between(conn, symbol: str, start: str, as_of: str):
    """Underlying snapshots in [start, as_of], ascending. Bounded above by as_of."""
    return conn.execute(
        "SELECT * FROM underlying_snap WHERE symbol=? AND ts>=? AND ts<=? "
        "ORDER BY ts ASC", (symbol, start, as_of)).fetchall()


def features_history(conn, symbol: str, as_of: str, limit: int = 5000):
    """Feature rows strictly before `as_of`, newest first."""
    return conn.execute(
        "SELECT * FROM features WHERE symbol=? AND ts<? ORDER BY ts DESC LIMIT ?",
        (symbol, as_of, limit)).fetchall()


def quote_for_leg(conn, symbol: str, expiry: str, strike: float, right: str,
                  as_of: str):
    """Latest quote for one contract at or before `as_of`."""
    return conn.execute(
        "SELECT * FROM chain_snap WHERE symbol=? AND expiry=? AND strike=? "
        "AND right=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (symbol, expiry, strike, right, as_of)).fetchone()
