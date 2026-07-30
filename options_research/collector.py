"""Collector: raw snapshots only. No logic, just truth (spec §1).

Data-quality checks are written on EVERY chain snapshot, not bolted on later
(spec §9: build the data_quality checks first, not last).
"""

import logging
import time
from datetime import datetime, timezone

from . import db, market_calendar as cal
from .config import Config

log = logging.getLogger("collector")


class Collector:
    def __init__(self, conn, source, cfg: Config | None = None,
                 cross_source=None):
        self.conn = conn
        self.source = source
        self.cross = cross_source
        self.cfg = cfg or Config()

    # ── single-shot snapshots ───────────────────────────────────────────────

    def snap_underlying(self, symbol: str, ts: str | None = None):
        u = self.source.get_underlying(symbol)
        if not u:
            self._dq(ts or cal.utcnow_iso(), symbol, note="underlying_fetch_failed")
            return None
        ts = ts or cal.utcnow_iso()
        self.conn.execute(
            "INSERT OR REPLACE INTO underlying_snap VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, symbol, u["last"], u["bid"], u["ask"], u["volume"], u["vwap"],
             u["day_open"], u["day_high"], u["day_low"], u["prev_close"]))
        self.conn.commit()
        return u

    def snap_chain(self, symbol: str, ts: str | None = None,
                   band_pct: float | None = None):
        u = self.source.get_underlying(symbol)
        if not u or not u["last"]:
            self._dq(ts or cal.utcnow_iso(), symbol, note="no_spot_for_chain")
            return 0
        ts = ts or cal.utcnow_iso()
        band = band_pct if band_pct is not None else self.cfg.strike_band_pct
        contracts = self.source.get_chain(
            symbol, u["last"], band, self.cfg.dte_min, self.cfg.dte_max)
        n_wide = n_crossed = 0
        rows = []
        for c in contracts:
            if c["expiry"] is None or c["strike"] is None:
                continue
            bid, ask = c["bid"], c["ask"]
            if bid and ask:
                mid = (bid + ask) / 2
                if ask < bid:
                    n_crossed += 1
                elif mid > 0 and (ask - bid) / mid > self.cfg.spread_max_pct:
                    n_wide += 1
            rows.append((ts, symbol, c["expiry"], c["strike"], c["right"],
                         bid, ask, c["last"], c["volume"], c["open_interest"],
                         c["iv"], c["delta"], c["gamma"], c["theta"], c["vega"],
                         u["last"]))
        self.conn.executemany(
            "INSERT OR REPLACE INTO chain_snap VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        n = len(rows)
        self._dq(ts, symbol,
                 stale_secs=self._staleness(contracts),
                 missing_contracts=0 if n else 1,
                 wide_spread_frac=(n_wide / n) if n else None,
                 crossed_book=n_crossed,
                 note="" if n else "empty_chain")
        self.conn.commit()
        return n

    def _staleness(self, contracts) -> float | None:
        """Age of the freshest quote timestamp in the batch, in seconds."""
        newest = None
        for c in contracts:
            t = c.get("quote_ts")
            if isinstance(t, (int, float)) and t > 0:
                # polygon: ns or ms epoch; tradier: ms epoch
                secs = t / 1e9 if t > 1e14 else t / 1e3
                newest = max(newest or 0, secs)
        if newest is None:
            return None
        return round(datetime.now(timezone.utc).timestamp() - newest, 1)

    def _dq(self, ts, symbol, stale_secs=None, missing_contracts=None,
            wide_spread_frac=None, crossed_book=None, note=""):
        self.conn.execute(
            "INSERT INTO data_quality VALUES (?,?,?,?,?,?,?,?)",
            (ts, symbol, self.source.name, stale_secs, missing_contracts,
             wide_spread_frac, crossed_book, note))

    # ── cross-check discipline (spec §2) ────────────────────────────────────

    def cross_check(self, symbol: str, n_contracts: int = 5,
                    tolerance_pct: float = 0.10):
        """Compare a few ATM contracts across sources; log divergence."""
        if self.cross is None:
            return
        ts = cal.utcnow_iso()
        u = self.source.get_underlying(symbol)
        if not u:
            return
        a = self.source.get_chain(symbol, u["last"], 0.01,
                                  self.cfg.dte_min, self.cfg.dte_max)
        b = self.cross.get_chain(symbol, u["last"], 0.01,
                                 self.cfg.dte_min, self.cfg.dte_max)
        b_map = {(c["expiry"], c["strike"], c["right"]): c for c in b}
        checked = diverged = 0
        for c in a:
            if checked >= n_contracts:
                break
            other = b_map.get((c["expiry"], c["strike"], c["right"]))
            if not other or not c["bid"] or not other["bid"]:
                continue
            checked += 1
            mid_a = (c["bid"] + c["ask"]) / 2
            mid_b = (other["bid"] + other["ask"]) / 2
            if mid_a > 0 and abs(mid_a - mid_b) / mid_a > tolerance_pct:
                diverged += 1
                self._dq(ts, symbol, note=(
                    f"XCHECK divergence {c['expiry']} {c['strike']}{c['right']}: "
                    f"{self.source.name} mid {mid_a:.2f} vs "
                    f"{self.cross.name} mid {mid_b:.2f}"))
        if checked:
            log.info("cross-check %s: %d/%d diverged >%d%%",
                     symbol, diverged, checked, int(tolerance_pct * 100))
        self.conn.commit()

    # ── the loop ────────────────────────────────────────────────────────────

    def run_loop(self, on_cycle=None):
        """Market-hours loop. Underlying every 15s, chain every 5m (config),
        full-width chain snapshot at the configured wide-snapshot times.

        `on_cycle(ts)` is called after each chain snapshot — the shadow
        pipeline (features → detect → grade) hooks in there.
        """
        last_chain = 0.0
        wide_done: set = set()
        log.info("collector loop started (chain every %ds, underlying every %ds)",
                 self.cfg.chain_cadence_secs, self.cfg.underlying_cadence_secs)
        while True:
            now = datetime.now(timezone.utc)
            if not cal.is_market_open(now):
                wide_done.clear()
                time.sleep(30)
                continue
            ts = cal.utcnow_iso()
            for sym in self.cfg.symbols:
                self.snap_underlying(sym, ts)
            if time.monotonic() - last_chain >= self.cfg.chain_cadence_secs:
                last_chain = time.monotonic()
                for sym in self.cfg.symbols:
                    n = self.snap_chain(sym, ts)
                    log.info("chain %s @ %s: %d contracts", sym, ts, n)
                if on_cycle:
                    on_cycle(ts)
            et_hhmm = cal.to_et(ts).strftime("%H:%M")
            for wide_t in self.cfg.wide_snapshot_times_et:
                key = f"{cal.trading_day_of(ts)}-{wide_t}"
                if key not in wide_done and et_hhmm >= wide_t:
                    wide_done.add(key)
                    for sym in self.cfg.symbols:
                        # wide snapshot: 3x the strike band for skew/term context
                        self.snap_chain(sym, ts,
                                        band_pct=self.cfg.strike_band_pct * 3)
            time.sleep(self.cfg.underlying_cadence_secs)
