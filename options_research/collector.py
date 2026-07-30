"""Collector: raw snapshots only. No logic, just truth (spec §1).

Two hard gates:
  * The collector REFUSES to start unless a capability probe passes. A missing
    entitlement must fail loudly on day one, not produce six weeks of silently
    null Greeks.
  * A contract with no exchange timestamp is DROPPED, not stamped with the
    fetch time. Substituting fetch time would silently corrupt every
    time-of-day result.

Data-quality checks are written on every snapshot, not bolted on later.
"""

import logging
import time
from datetime import datetime, timezone

from . import db, market_calendar as cal
from .capabilities import PHASE1, ProbeReport, Status
from .config import Config

log = logging.getLogger("collector")


class CapabilityError(RuntimeError):
    """Raised when the provider cannot meet a blocking requirement."""


class Collector:
    def __init__(self, conn, source, cfg: Config | None = None,
                 cross_source=None):
        self.conn = conn
        self.source = source
        self.cross = cross_source
        self.cfg = cfg or Config()
        self._probe_passed = False

    # ── capability gate ─────────────────────────────────────────────────────

    def probe(self, record: bool = True,
              profile: str | None = None) -> ProbeReport:
        results = self.source.probe()
        report = ProbeReport(provider=self.source.name, results=results,
                             profile=profile or self.cfg.profile)
        if record:
            db.record_probe(self.conn, self.source.name, cal.utcnow_iso(),
                            results)
        self._probe_passed = report.can_collect
        return report

    def require_capability(self, profile: str | None = None) -> ProbeReport:
        report = self.probe(profile=profile)
        if not report.can_collect:
            raise CapabilityError(
                "collection refused — unmet blocking requirements: "
                + ", ".join(r.requirement_id
                            for r in report.blocking_failures)
                + "\n\n" + report.render())
        return report

    # ── single-shot snapshots ───────────────────────────────────────────────

    def snap_underlying(self, symbol: str):
        u = self.source.get_underlying(symbol)
        if not u:
            self._dq(cal.utcnow_iso(), symbol, note="underlying_fetch_failed")
            return None
        if not u.get("event_ts"):
            self._dq(u.get("fetch_ts") or cal.utcnow_iso(), symbol,
                     note="DROPPED underlying: no exchange timestamp")
            return None
        self.conn.execute(
            "INSERT OR REPLACE INTO underlying_snap VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (u["event_ts"], symbol, u["last"], u["bid"], u["ask"], u["volume"],
             u["vwap"], u["day_open"], u["day_high"], u["day_low"],
             u["prev_close"], u["fetch_ts"], self.source.name,
             u["delay_class"]))
        self.conn.commit()
        return u

    def snap_chain(self, symbol: str, band_pct: float | None = None):
        u = self.source.get_underlying(symbol)
        if not u or not u.get("last"):
            self._dq(cal.utcnow_iso(), symbol, note="no_spot_for_chain")
            return 0
        band = band_pct if band_pct is not None else self.cfg.strike_band_pct
        contracts = self.source.get_chain(
            symbol, u["last"], band, self.cfg.dte_min, self.cfg.dte_max)

        rows, n_wide, n_crossed, n_dropped = [], 0, 0, 0
        lags = []
        for c in contracts:
            if not c.get("event_ts"):
                n_dropped += 1
                continue
            if c["expiry"] is None or c["strike"] is None or c["right"] is None:
                n_dropped += 1
                continue
            bid, ask = c["bid"], c["ask"]
            if bid and ask:
                mid = (bid + ask) / 2
                if ask < bid:
                    n_crossed += 1
                elif mid > 0 and (ask - bid) / mid > self.cfg.spread_max_pct:
                    n_wide += 1
            lag = self._lag_secs(c["event_ts"], c["fetch_ts"])
            if lag is not None:
                lags.append(lag)
            rows.append((c["event_ts"], symbol, c["expiry"], c["strike"],
                         c["right"], bid, ask, c["last"], c["volume"],
                         c["open_interest"], c["iv"], c["delta"], c["gamma"],
                         c["theta"], c["vega"], u["last"], c["fetch_ts"],
                         self.source.name, c["delay_class"]))
        self.conn.executemany(
            "INSERT OR REPLACE INTO chain_snap VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        n = len(rows)
        note = ""
        if n_dropped:
            note = f"dropped {n_dropped} contracts (no exchange timestamp)"
        if not n:
            note = (note + "; " if note else "") + "empty_chain"
        self._dq(u.get("fetch_ts") or cal.utcnow_iso(), symbol,
                 stale_secs=(sorted(lags)[len(lags) // 2] if lags else None),
                 missing_contracts=n_dropped,
                 wide_spread_frac=(n_wide / n) if n else None,
                 crossed_book=n_crossed, note=note)
        self.conn.commit()
        return n

    @staticmethod
    def _lag_secs(event_ts: str, fetch_ts: str) -> float | None:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        try:
            return (datetime.strptime(fetch_ts, fmt)
                    - datetime.strptime(event_ts, fmt)).total_seconds()
        except (ValueError, TypeError):
            return None

    def _dq(self, ts, symbol, stale_secs=None, missing_contracts=None,
            wide_spread_frac=None, crossed_book=None, note=""):
        self.conn.execute(
            "INSERT INTO data_quality VALUES (?,?,?,?,?,?,?,?)",
            (ts, symbol, self.source.name, stale_secs, missing_contracts,
             wide_spread_frac, crossed_book, note))

    # ── cross-check discipline (spec §2) ────────────────────────────────────

    def cross_check(self, symbol: str, n_contracts: int = 5,
                    tolerance_pct: float = 0.10):
        if self.cross is None:
            return
        ts = cal.utcnow_iso()
        u = self.source.get_underlying(symbol)
        if not u or not u.get("last"):
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
            if not other or not c["bid"] or not other["bid"] \
                    or not c["ask"] or not other["ask"]:
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
            log.info("cross-check %s: %d/%d diverged >%d%%", symbol, diverged,
                     checked, int(tolerance_pct * 100))
        self.conn.commit()

    # ── the loop ────────────────────────────────────────────────────────────

    def run_loop(self, on_cycle=None):
        """Market-hours loop. Refuses to start without a passing probe."""
        if not self._probe_passed:
            self.require_capability()
        last_chain = 0.0
        wide_done: set = set()
        log.info("collector started (chain every %ds, underlying every %ds)",
                 self.cfg.chain_cadence_secs, self.cfg.underlying_cadence_secs)
        while True:
            now = datetime.now(timezone.utc)
            if not cal.is_market_open(now):
                wide_done.clear()
                time.sleep(30)
                continue
            for sym in self.cfg.symbols:
                self.snap_underlying(sym)
            if time.monotonic() - last_chain >= self.cfg.chain_cadence_secs:
                last_chain = time.monotonic()
                for sym in self.cfg.symbols:
                    n = self.snap_chain(sym)
                    log.info("chain %s: %d contracts", sym, n)
                if on_cycle:
                    on_cycle(cal.utcnow_iso())
            et_hhmm = cal.to_et(cal.utcnow_iso()).strftime("%H:%M")
            for wide_t in self.cfg.wide_snapshot_times_et:
                key = f"{cal.trading_day_of(cal.utcnow_iso())}-{wide_t}"
                if key not in wide_done and et_hhmm >= wide_t:
                    wide_done.add(key)
                    for sym in self.cfg.symbols:
                        self.snap_chain(sym,
                                        band_pct=self.cfg.strike_band_pct * 3)
            time.sleep(self.cfg.underlying_cadence_secs)
