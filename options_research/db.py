"""SQLite schema and the no-look-ahead query layer.

SCHEMA v2 — platform upgrades #1-#3:
  * `event_ts` (the EXCHANGE timestamp) is the authoritative event time on all
    raw tables and is what every downstream read is bounded by.
  * `fetch_ts`, `source`, and `delay_class` are stored separately and are
    data-quality metadata only. `fetch_ts` must never be used as an event time
    — a variable API delay would smear every time-of-day result.
  * `delay_class` travels all the way through to `paper_trades.data_era`, so
    the validator can refuse to pool delayed-era and realtime-era rows in one
    validation sample.

Raw tables are never edited and never derived. Features are recomputable.
"""

import os
import sqlite3

SCHEMA_VERSION = 2

SCHEMA = """
-- ── RAW TRUTH (never edited, never derived) ──────────────────────────────
CREATE TABLE IF NOT EXISTS underlying_snap (
  event_ts TEXT NOT NULL,            -- EXCHANGE timestamp: authoritative
  symbol TEXT NOT NULL,
  last REAL, bid REAL, ask REAL, volume INTEGER,
  vwap REAL, day_open REAL, day_high REAL, day_low REAL, prev_close REAL,
  fetch_ts TEXT NOT NULL,            -- when WE called (metadata only)
  source TEXT NOT NULL,
  delay_class TEXT NOT NULL,         -- 'realtime' | 'delayed_15m' | 'unknown'
  PRIMARY KEY (event_ts, symbol, source));
CREATE INDEX IF NOT EXISTS idx_under_lookup
  ON underlying_snap(symbol, event_ts);

CREATE TABLE IF NOT EXISTS chain_snap (
  event_ts TEXT NOT NULL,
  symbol TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL,
  right TEXT NOT NULL,
  bid REAL, ask REAL, last REAL, volume INTEGER, open_interest INTEGER,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  underlying REAL,
  fetch_ts TEXT NOT NULL,
  source TEXT NOT NULL,
  delay_class TEXT NOT NULL,
  PRIMARY KEY (event_ts, symbol, expiry, strike, right, source));
CREATE INDEX IF NOT EXISTS idx_chain_lookup
  ON chain_snap(symbol, expiry, event_ts);
CREATE INDEX IF NOT EXISTS idx_chain_leg
  ON chain_snap(symbol, expiry, strike, right, event_ts);

-- ── DERIVED FEATURES (recomputable; safe to drop and rebuild) ────────────
CREATE TABLE IF NOT EXISTS features (
  ts TEXT, symbol TEXT,              -- ts is a decision time on the event clock
  iv30 REAL, iv_rank REAL, iv_percentile REAL,
  realized_vol_5m REAL, realized_vol_30m REAL,
  vrp REAL,
  orb_high REAL, orb_low REAL, orb_broken TEXT,
  vwap_dev REAL, atr_pct REAL,
  skew_25d REAL, term_slope REAL,
  liquidity_score REAL,
  regime TEXT,
  delay_class TEXT,
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
  split TEXT,
  data_era TEXT,                     -- delay_class at entry; never pool eras
  fill_scenario TEXT);               -- 'optimistic' | 'base' | 'pessimistic'

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

-- ── SPLIT FREEZE ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS split_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cutoff_date TEXT NOT NULL,
  frozen_at TEXT NOT NULL);

-- ── CAPABILITY PROBE LOG ────────────────────────────────────────────────
-- Every probe run is retained: an entitlement that changes mid-study is a
-- data-regime change and must be visible in the record.
CREATE TABLE IF NOT EXISTS capability_probe (
  ts TEXT, provider TEXT, requirement_id TEXT, status TEXT,
  detail TEXT, raw_error TEXT, evidence_json TEXT);

CREATE TABLE IF NOT EXISTS schema_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);
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
    conn.execute("INSERT OR IGNORE INTO schema_meta (id, version) VALUES (1, ?)",
                 (SCHEMA_VERSION,))
    conn.commit()
    return conn


# ── No-look-ahead query layer ────────────────────────────────────────────────
# A decision at time T may only use data with event_ts <= T (spec §5.6).
# Bounded by the EXCHANGE clock, never the fetch clock.

def chain_at(conn, symbol: str, as_of: str, source: str | None = None):
    """Latest chain snapshot at or before `as_of`. Never returns future rows."""
    q = ("SELECT MAX(event_ts) AS t FROM chain_snap "
         "WHERE symbol=? AND event_ts<=?")
    args = [symbol, as_of]
    if source:
        q += " AND source=?"
        args.append(source)
    row = conn.execute(q, args).fetchone()
    if not row or row["t"] is None:
        return []
    q2 = "SELECT * FROM chain_snap WHERE symbol=? AND event_ts=?"
    args2 = [symbol, row["t"]]
    if source:
        q2 += " AND source=?"
        args2.append(source)
    return conn.execute(q2, args2).fetchall()


def underlying_at(conn, symbol: str, as_of: str):
    return conn.execute(
        "SELECT * FROM underlying_snap WHERE symbol=? AND event_ts<=? "
        "ORDER BY event_ts DESC LIMIT 1", (symbol, as_of)).fetchone()


def underlying_between(conn, symbol: str, start: str, as_of: str):
    return conn.execute(
        "SELECT * FROM underlying_snap WHERE symbol=? AND event_ts>=? "
        "AND event_ts<=? ORDER BY event_ts ASC",
        (symbol, start, as_of)).fetchall()


def features_history(conn, symbol: str, as_of: str, limit: int = 5000):
    """Feature rows strictly before `as_of`, newest first."""
    return conn.execute(
        "SELECT * FROM features WHERE symbol=? AND ts<? ORDER BY ts DESC "
        "LIMIT ?", (symbol, as_of, limit)).fetchall()


def quote_for_leg(conn, symbol: str, expiry: str, strike: float, right: str,
                  as_of: str):
    return conn.execute(
        "SELECT * FROM chain_snap WHERE symbol=? AND expiry=? AND strike=? "
        "AND right=? AND event_ts<=? ORDER BY event_ts DESC LIMIT 1",
        (symbol, expiry, strike, right, as_of)).fetchone()


def record_probe(conn, provider: str, ts: str, results) -> None:
    import json
    conn.executemany(
        "INSERT INTO capability_probe VALUES (?,?,?,?,?,?,?)",
        [(ts, provider, r.requirement_id, r.status.value, r.detail,
          r.raw_error, json.dumps(r.evidence, default=str))
         for r in results])
    conn.commit()
