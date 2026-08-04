"""Tradier source — cross-check provider (spec §2).

Kept deliberately at parity with the Polygon source's normalized shapes so the
cross-check compares data, not adapter quirks. Tradier's sandbox is free, which
makes it the cheapest available second opinion on quote quality.
"""

import time
from datetime import date, datetime, timezone

import requests

from ..capabilities import CheckResult, Status
from .base import QuoteSource, epoch_to_iso, classify_delay


class TradierError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:300]}")


class TradierSource(QuoteSource):
    name = "tradier"

    def __init__(self, token: str, base: str = "https://sandbox.tradier.com/v1",
                 timeout: float = 20.0):
        if not token:
            raise ValueError("TRADIER_TOKEN is not set")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}", "Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            try:
                r = self.session.get(self.base + path, params=params,
                                     timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise TradierError(r.status_code, r.text)
                return r.json() or {}
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        return {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get_underlying(self, symbol: str) -> dict | None:
        fetch_ts = self._now_iso()
        j = self._get("/markets/quotes", {"symbols": symbol})
        q = (j.get("quotes") or {}).get("quote")
        if not q:
            return None
        if isinstance(q, list):
            q = q[0]
        event_ts = epoch_to_iso(q.get("trade_date")) or epoch_to_iso(
            q.get("bid_date"))
        return {
            "last": q.get("last"), "bid": q.get("bid"), "ask": q.get("ask"),
            "volume": q.get("volume"), "vwap": None,
            "day_open": q.get("open"), "day_high": q.get("high"),
            "day_low": q.get("low"), "prev_close": q.get("prevclose"),
            "event_ts": event_ts, "fetch_ts": fetch_ts,
            "delay_class": classify_delay(event_ts, fetch_ts),
        }

    def _expirations(self, symbol: str) -> list[str]:
        j = self._get("/markets/options/expirations",
                      {"symbol": symbol, "includeAllRoots": "true"})
        exp = (j.get("expirations") or {}).get("date") or []
        return exp if isinstance(exp, list) else [exp]

    def get_chain(self, symbol, spot, strike_band_pct, dte_min, dte_max):
        fetch_ts = self._now_iso()
        today = date.today()
        lo, hi = spot * (1 - strike_band_pct), spot * (1 + strike_band_pct)
        out = []
        for exp in self._expirations(symbol):
            try:
                dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            except ValueError:
                continue
            if not (dte_min <= dte <= dte_max):
                continue
            j = self._get("/markets/options/chains",
                          {"symbol": symbol, "expiration": exp,
                           "greeks": "true"})
            opts = (j.get("options") or {}).get("option") or []
            if isinstance(opts, dict):
                opts = [opts]
            for o in opts:
                if not (lo <= (o.get("strike") or 0) <= hi):
                    continue
                g = o.get("greeks") or {}
                event_ts = (epoch_to_iso(o.get("bid_date"))
                            or epoch_to_iso(o.get("trade_date")))
                out.append({
                    "expiry": exp, "strike": o.get("strike"),
                    "right": "C" if o.get("option_type") == "call" else "P",
                    "bid": o.get("bid"), "ask": o.get("ask"),
                    "last": o.get("last"), "volume": o.get("volume"),
                    "open_interest": o.get("open_interest"),
                    "iv": g.get("mid_iv") or g.get("smv_vol"),
                    "delta": g.get("delta"), "gamma": g.get("gamma"),
                    "theta": g.get("theta"), "vega": g.get("vega"),
                    "event_ts": event_ts, "fetch_ts": fetch_ts,
                    "delay_class": classify_delay(event_ts, fetch_ts),
                })
        return out

    def probe(self) -> list:
        """Same requirement set as the Polygon probe, so the two are directly
        comparable and a source can be chosen on evidence."""
        import time
        from datetime import date, datetime

        out = []
        try:
            self._get("/markets/quotes", {"symbols": "SPY"})
            out.append(CheckResult("auth", Status.PASS,
                                   f"token authenticates against {self.base}"))
        except TradierError as e:
            out.append(CheckResult("auth", Status.FAIL,
                                   f"HTTP {e.status_code}", e.body[:300]))
            return out
        except Exception as e:                            # noqa: BLE001
            out.append(CheckResult("auth", Status.FAIL, "request failed",
                                   str(e)[:300]))
            return out

        u = self.get_underlying("SPY")
        if not (u and u.get("last")):
            out.append(CheckResult("underlying_snapshot", Status.FAIL,
                                   "no SPY quote returned"))
            return out
        missing = [k for k in ("last", "volume", "day_open", "day_high",
                               "day_low", "prev_close") if u.get(k) is None]
        out.append(CheckResult(
            "underlying_snapshot",
            Status.WARN if missing else Status.PASS,
            f"SPY last={u['last']} (no VWAP from this vendor; supply it from "
            f"1-min aggregates)" + (f" missing={missing}" if missing else ""),
            evidence={k: u.get(k) for k in
                      ("last", "bid", "ask", "day_open", "prev_close",
                       "event_ts", "delay_class")}))

        try:
            chain = self.get_chain("SPY", u["last"], 0.07, 0, 7)
        except TradierError as e:
            out.append(CheckResult("chain_snapshot", Status.FAIL,
                                   f"HTTP {e.status_code}", e.body[:300]))
            return out
        out.append(CheckResult("chain_snapshot",
                               Status.PASS if chain else Status.FAIL,
                               f"{len(chain)} contracts (SPY, ±7%, 0-7 DTE)"))
        if not chain:
            for r in ("chain_bid_ask", "chain_greeks", "chain_iv",
                      "chain_open_interest", "chain_volume", "atm_straddle",
                      "exchange_timestamp", "delay_class", "zero_dte"):
                out.append(CheckResult(r, Status.SKIP, "no chain returned"))
            return out

        n = len(chain)
        for rid, keys, thresh in (
                ("chain_bid_ask", ("bid", "ask"), 0.80),
                ("chain_greeks", ("delta", "gamma", "theta", "vega"), 0.80),
                ("chain_iv", ("iv",), 0.80),
                ("chain_open_interest", ("open_interest",), 0.80),
                ("chain_volume", ("volume",), 0.50)):
            fracs = {k: sum(1 for c in chain if c.get(k) is not None) / n
                     for k in keys}
            worst = min(fracs.values())
            detail = ", ".join(f"{k}={v:.0%}" for k, v in fracs.items())
            status = (Status.FAIL if worst == 0 else
                      Status.WARN if worst < thresh else Status.PASS)
            out.append(CheckResult(rid, status, f"({detail})",
                                   evidence={"n_contracts": n, **fracs}))

        # ATM straddle — the Phase 1 priced-move estimator
        expiries = sorted({c["expiry"] for c in chain if c.get("expiry")})
        if expiries:
            exp = expiries[0]
            legs = {}
            for right in ("C", "P"):
                cands = [c for c in chain if c["expiry"] == exp
                         and c["right"] == right and c.get("bid")
                         and c.get("ask")]
                if cands:
                    legs[right] = min(cands,
                                      key=lambda c: abs(c["strike"] - u["last"]))
            if len(legs) == 2:
                straddle = sum((legs[r]["bid"] + legs[r]["ask"]) / 2
                               for r in ("C", "P"))
                out.append(CheckResult(
                    "atm_straddle", Status.PASS,
                    f"expiry {exp}: ATM straddle mid = {straddle:.2f} "
                    f"({straddle / u['last'] * 100:.2f}% of spot = priced move)",
                    evidence={"expiry": exp, "straddle_mid": round(straddle, 4)}))
            else:
                out.append(CheckResult(
                    "atm_straddle", Status.FAIL,
                    f"expiry {exp}: two-sided ATM quotes missing for "
                    f"{sorted({'C', 'P'} - set(legs))}"))
            today = date.today().isoformat()
            n0 = sum(1 for c in chain if c.get("expiry") == today)
            out.append(CheckResult(
                "zero_dte", Status.PASS if n0 else Status.WARN,
                f"{n0} same-day contracts present" if n0
                else f"no 0DTE now (today={today}); expiries {expiries[:4]}"))

        with_ts = [c for c in chain if c.get("event_ts")]
        if with_ts:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            lags = []
            for c in with_ts[:200]:
                try:
                    lags.append((datetime.strptime(c["fetch_ts"], fmt)
                                 - datetime.strptime(c["event_ts"], fmt))
                                .total_seconds())
                except (ValueError, TypeError):
                    continue
            med = sorted(lags)[len(lags) // 2] if lags else None
            out.append(CheckResult(
                "exchange_timestamp", Status.PASS,
                f"present on {len(with_ts)}/{n} contracts"
                + (f"; median lag {med:.0f}s" if med is not None else ""),
                evidence={"median_lag_secs": med}))
            classes = sorted({c.get("delay_class") for c in with_ts
                              if c.get("delay_class")})
            out.append(CheckResult(
                "delay_class",
                Status.WARN if "unknown" in classes else Status.PASS,
                f"observed classes: {classes}"))
        else:
            out.append(CheckResult("exchange_timestamp", Status.FAIL,
                                   "no per-quote exchange timestamp"))
            out.append(CheckResult("delay_class", Status.FAIL,
                                   "cannot classify without a timestamp"))

        # rate limit
        ok, throttled, start = 0, None, time.monotonic()
        for i in range(8):
            try:
                self._get("/markets/quotes", {"symbols": "SPY"})
                ok += 1
            except TradierError as e:
                if e.status_code == 429:
                    throttled = i + 1
                    break
                break
        elapsed = max(time.monotonic() - start, 0.001)
        out.append(CheckResult(
            "rate_limit",
            Status.FAIL if throttled else Status.PASS,
            f"throttled after {throttled} rapid requests"
            if throttled else
            f"{ok} rapid requests, no throttling "
            f"(>= {ok / elapsed * 60:.0f}/min observed)"))
        return out
