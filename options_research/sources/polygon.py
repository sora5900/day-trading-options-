"""Polygon.io source.

Endpoint paths and entitlement behaviour below are BEST-EFFORT: this code was
written without access to the official documentation (the environment's egress
policy blocked polygon.io). The `probe()` method exists precisely because of
that — it determines empirically what the key can do rather than trusting any
assumption baked into this file. Where the correct request shape is uncertain
(notably index-option tickers), the probe tries several candidate forms and
reports which one the API accepted.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

from ..capabilities import CheckResult, Status
from .base import (QuoteSource, epoch_to_iso, classify_delay,
                   DELAY_UNKNOWN)

# Polygon rebranded to Massive. Both hosts are tried unless one is pinned via
# MASSIVE_API_BASE / POLYGON_API_BASE, and the probe reports which answered.
CANDIDATE_BASES = ("https://api.polygon.io", "https://api.massive.com")
BASE = os.environ.get("MASSIVE_API_BASE") or os.environ.get(
    "POLYGON_API_BASE") or CANDIDATE_BASES[0]


class PolygonError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:300]}")


class PolygonSource(QuoteSource):
    name = "polygon"

    def __init__(self, api_key: str, timeout: float = 20.0,
                 base: str | None = None):
        if not api_key:
            raise ValueError("POLYGON_API_KEY is not set")
        self.key = api_key
        self.timeout = timeout
        self.base = base or BASE
        self.session = requests.Session()

    def resolve_base(self) -> str:
        """Find the host that actually answers this key.

        Polygon rebranded to Massive; depending on account vintage either host
        may serve, so this is detected rather than assumed. Pin it with
        MASSIVE_API_BASE to skip the probing.
        """
        if os.environ.get("MASSIVE_API_BASE") or os.environ.get(
                "POLYGON_API_BASE"):
            return self.base
        for candidate in CANDIDATE_BASES:
            try:
                r = self.session.get(
                    candidate + "/v3/reference/tickers",
                    params={"ticker": "SPY", "limit": 1, "apiKey": self.key},
                    timeout=self.timeout)
                if r.status_code < 500:
                    self.base = candidate
                    return candidate
            except requests.RequestException:
                continue
        return self.base

    # ── transport ───────────────────────────────────────────────────────────

    def _get(self, path_or_url: str, params: dict | None = None,
             retries: int = 3) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.key
        url = (path_or_url if path_or_url.startswith("http")
               else self.base + path_or_url)
        last = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    last = PolygonError(429, r.text)
                    continue
                if r.status_code >= 400:
                    # entitlement errors are 401/403 and must NOT be retried —
                    # the message is the answer we want
                    raise PolygonError(r.status_code, r.text)
                return r.json()
            except requests.RequestException as e:
                last = e
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        if last:
            raise last
        return {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── normalized reads ────────────────────────────────────────────────────

    def get_underlying(self, symbol: str) -> dict | None:
        fetch_ts = self._now_iso()
        j = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}")
        t = j.get("ticker")
        if not t:
            return None
        day = t.get("day") or {}
        quote = t.get("lastQuote") or {}
        minute = t.get("min") or {}
        trade = t.get("lastTrade") or {}
        last = trade.get("p") or minute.get("c") or day.get("c")
        if not last:
            return None
        event_ts = (epoch_to_iso(quote.get("t"))
                    or epoch_to_iso(trade.get("t"))
                    or epoch_to_iso(minute.get("t")))
        return {
            "last": last,
            "bid": quote.get("p"), "ask": quote.get("P"),
            "volume": day.get("v"),
            "vwap": day.get("vw") or minute.get("vw"),
            "day_open": day.get("o"), "day_high": day.get("h"),
            "day_low": day.get("l"),
            "prev_close": (t.get("prevDay") or {}).get("c"),
            "event_ts": event_ts,
            "fetch_ts": fetch_ts,
            "delay_class": classify_delay(event_ts, fetch_ts),
        }

    def get_chain(self, symbol, spot, strike_band_pct, dte_min, dte_max):
        fetch_ts = self._now_iso()
        today = date.today()
        params = {
            "strike_price.gte": round(spot * (1 - strike_band_pct), 2),
            "strike_price.lte": round(spot * (1 + strike_band_pct), 2),
            "expiration_date.gte": (today + timedelta(days=dte_min)).isoformat(),
            "expiration_date.lte": (today + timedelta(days=dte_max)).isoformat(),
            "limit": 250,
        }
        out, url, first = [], f"/v3/snapshot/options/{symbol}", True
        while url:
            j = self._get(url, params if first else None)
            first = False
            for c in j.get("results", []):
                out.append(self._normalize_contract(c, fetch_ts))
            url = j.get("next_url")
        return out

    @staticmethod
    def _normalize_contract(c: dict, fetch_ts: str) -> dict:
        det = c.get("details") or {}
        q = c.get("last_quote") or {}
        greeks = c.get("greeks") or {}
        day = c.get("day") or {}
        trade = c.get("last_trade") or {}
        event_ts = (epoch_to_iso(q.get("last_updated"))
                    or epoch_to_iso(q.get("sip_timestamp"))
                    or epoch_to_iso(trade.get("sip_timestamp")))
        # Polygon labels snapshot freshness explicitly when present
        tf = (q.get("timeframe") or c.get("timeframe") or "").upper()
        if tf == "REAL-TIME":
            delay = "realtime"
        elif tf == "DELAYED":
            delay = "delayed_15m"
        else:
            delay = classify_delay(event_ts, fetch_ts)
        return {
            "expiry": det.get("expiration_date"),
            "strike": det.get("strike_price"),
            "right": ("C" if det.get("contract_type") == "call"
                      else "P" if det.get("contract_type") == "put" else None),
            "bid": q.get("bid"), "ask": q.get("ask"),
            "last": trade.get("price"),
            "volume": day.get("volume"),
            "open_interest": c.get("open_interest"),
            "iv": c.get("implied_volatility"),
            "delta": greeks.get("delta"), "gamma": greeks.get("gamma"),
            "theta": greeks.get("theta"), "vega": greeks.get("vega"),
            "event_ts": event_ts, "fetch_ts": fetch_ts,
            "delay_class": delay,
        }

    # ── capability probe ────────────────────────────────────────────────────

    def probe(self) -> list:
        """Determine empirically what this key/plan actually delivers."""
        results: list[CheckResult] = []
        add = results.append

        # 1. auth (also settles which host serves this account)
        base = self.resolve_base()
        try:
            self._get("/v3/reference/tickers", {"ticker": "SPY", "limit": 1})
            add(CheckResult("auth", Status.PASS,
                            f"key authenticates against {base}",
                            evidence={"api_base": base}))
        except PolygonError as e:
            add(CheckResult("auth", Status.FAIL,
                            f"HTTP {e.status_code}", e.body[:300]))
            for r in ("underlying_snapshot", "chain_snapshot", "chain_bid_ask",
                      "chain_greeks", "chain_iv", "chain_open_interest",
                      "chain_volume", "exchange_timestamp", "delay_class",
                      "zero_dte", "xsp_options", "xsp_underlying",
                      "historical_quotes", "historical_aggs_1m"):
                add(CheckResult(r, Status.SKIP, "auth failed"))
            return results
        except Exception as e:                          # noqa: BLE001
            add(CheckResult("auth", Status.FAIL, "request failed", str(e)[:300]))
            return results

        # 2. underlying snapshot
        spot = None
        try:
            u = self.get_underlying("SPY")
            if not u:
                add(CheckResult("underlying_snapshot", Status.FAIL,
                                "empty snapshot payload"))
            else:
                spot = u["last"]
                missing = [k for k in ("last", "volume", "day_open",
                                       "day_high", "day_low", "prev_close")
                           if u.get(k) is None]
                vwap_note = "" if u.get("vwap") is not None else " (vwap NULL)"
                add(CheckResult(
                    "underlying_snapshot",
                    Status.PASS if not missing else Status.WARN,
                    f"SPY last={spot}{vwap_note}"
                    + (f" missing={missing}" if missing else ""),
                    evidence={k: u.get(k) for k in
                              ("last", "bid", "ask", "vwap", "day_open",
                               "prev_close", "event_ts", "delay_class")}))
        except PolygonError as e:
            add(CheckResult("underlying_snapshot", Status.FAIL,
                            f"HTTP {e.status_code}", e.body[:300]))

        # 3-9. chain snapshot and field population
        chain = []
        if spot:
            try:
                chain = self.get_chain("SPY", spot, 0.07, 0, 7)
                add(CheckResult("chain_snapshot",
                                Status.PASS if chain else Status.FAIL,
                                f"{len(chain)} contracts returned "
                                f"(SPY, ±7%, 0-7 DTE)"))
            except PolygonError as e:
                add(CheckResult("chain_snapshot", Status.FAIL,
                                f"HTTP {e.status_code}", e.body[:300]))
        else:
            add(CheckResult("chain_snapshot", Status.SKIP, "no spot price"))

        if chain:
            results.extend(self._field_population_checks(chain))
            results.extend(self._zero_dte_check(chain))
            results.append(self._atm_straddle_check(chain, spot))
        else:
            for r in ("chain_bid_ask", "chain_greeks", "chain_iv",
                      "chain_open_interest", "chain_volume", "atm_straddle",
                      "exchange_timestamp", "delay_class", "zero_dte"):
                add(CheckResult(r, Status.SKIP, "no chain returned"))

        # 10-11. XSP — try candidate ticker forms, report which the API took
        results.extend(self._xsp_checks())

        # 12. historical NBBO quotes (informational; forward-collect-only)
        results.append(self._historical_quotes_check(chain))

        # 13. historical 1-minute aggregates
        results.append(self._historical_aggs_check())

        return results

    def _field_population_checks(self, chain: list) -> list:
        """A field that exists but is NULL on most contracts is not usable.
        Population fraction is measured, not assumed."""
        n = len(chain)
        out = []

        def frac(key) -> float:
            return sum(1 for c in chain if c.get(key) is not None) / n

        for rid, keys, thresh in (
                ("chain_bid_ask", ("bid", "ask"), 0.80),
                ("chain_greeks", ("delta", "gamma", "theta", "vega"), 0.80),
                ("chain_iv", ("iv",), 0.80),
                ("chain_open_interest", ("open_interest",), 0.80),
                ("chain_volume", ("volume",), 0.50)):
            fracs = {k: frac(k) for k in keys}
            worst = min(fracs.values())
            detail = ", ".join(f"{k}={v:.0%}" for k, v in fracs.items())
            if worst == 0:
                status = Status.FAIL
                detail = f"NOT POPULATED ({detail})"
            elif worst < thresh:
                status = Status.WARN
                detail = f"sparsely populated ({detail})"
            else:
                status = Status.PASS
                detail = f"populated ({detail})"
            out.append(CheckResult(rid, status, detail,
                                   evidence={"n_contracts": n, **fracs}))

        # exchange timestamp: must exist AND differ from fetch time
        with_ts = [c for c in chain if c.get("event_ts")]
        if not with_ts:
            out.append(CheckResult(
                "exchange_timestamp", Status.FAIL,
                "NO per-quote exchange timestamp on any contract"))
            out.append(CheckResult(
                "delay_class", Status.FAIL,
                "cannot classify delay without an exchange timestamp"))
            return out

        sample = with_ts[0]
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
            f"present on {len(with_ts)}/{n} contracts; median lag behind "
            f"fetch = {med:.0f}s" if med is not None else "present",
            evidence={"sample_event_ts": sample.get("event_ts"),
                      "sample_fetch_ts": sample.get("fetch_ts"),
                      "median_lag_secs": med}))

        classes = {c.get("delay_class") for c in with_ts}
        unknown = DELAY_UNKNOWN in classes
        out.append(CheckResult(
            "delay_class",
            Status.WARN if unknown else Status.PASS,
            f"observed classes: {sorted(x for x in classes if x)}"
            + (" — lag did not match a known profile; rows will be tagged "
               "'unknown' and MUST NOT be pooled with other eras" if unknown
               else ""),
            evidence={"classes": sorted(x for x in classes if x),
                      "median_lag_secs": med}))
        return out

    def _zero_dte_check(self, chain: list) -> list:
        today = date.today().isoformat()
        n0 = sum(1 for c in chain if c.get("expiry") == today)
        if n0:
            return [CheckResult("zero_dte", Status.PASS,
                                f"{n0} same-day contracts present")]
        expiries = sorted({c.get("expiry") for c in chain if c.get("expiry")})
        return [CheckResult(
            "zero_dte", Status.WARN,
            f"no 0DTE contracts right now (today={today}). Expiries seen: "
            f"{expiries[:4]}. Expected on a trading day; benign on a "
            "weekend/holiday — re-run during market hours to confirm")]

    def _atm_straddle_check(self, chain: list, spot: float) -> CheckResult:
        """Phase 1's entire signal rests on the ATM straddle mid, so verify
        the nearest expiry actually has two-sided quotes on BOTH legs."""
        expiries = sorted({c["expiry"] for c in chain if c.get("expiry")})
        if not expiries:
            return CheckResult("atm_straddle", Status.FAIL, "no expiries")
        exp = expiries[0]
        legs = {}
        for right in ("C", "P"):
            cands = [c for c in chain
                     if c["expiry"] == exp and c["right"] == right
                     and c.get("bid") and c.get("ask")]
            if cands:
                legs[right] = min(cands, key=lambda c: abs(c["strike"] - spot))
        if len(legs) < 2:
            return CheckResult(
                "atm_straddle", Status.FAIL,
                f"expiry {exp}: two-sided ATM quotes missing for "
                f"{sorted({'C', 'P'} - set(legs))}")
        straddle = sum((legs[r]["bid"] + legs[r]["ask"]) / 2 for r in ("C", "P"))
        return CheckResult(
            "atm_straddle", Status.PASS,
            f"expiry {exp}: ATM straddle mid = {straddle:.2f} "
            f"({straddle / spot * 100:.2f}% of spot = priced move)",
            evidence={"expiry": exp, "straddle_mid": round(straddle, 4),
                      "priced_move_pct": round(straddle / spot, 6),
                      "call_strike": legs["C"]["strike"],
                      "put_strike": legs["P"]["strike"]})

    def _xsp_checks(self) -> list:
        """XSP ticker conventions differ for index options; try candidates."""
        out = []
        chain_ok, chain_form, chain_err = None, None, ""
        for form in ("XSP", "I:XSP"):
            try:
                j = self._get(f"/v3/snapshot/options/{form}", {"limit": 5})
                if j.get("results"):
                    chain_ok, chain_form = True, form
                    break
                chain_ok, chain_err = False, f"{form}: empty results"
            except PolygonError as e:
                chain_ok = False
                chain_err = f"{form}: HTTP {e.status_code} {e.body[:160]}"
        out.append(CheckResult(
            "xsp_options",
            Status.PASS if chain_ok else Status.FAIL,
            f"accepted ticker form '{chain_form}'" if chain_ok
            else "no XSP option chain returned",
            "" if chain_ok else chain_err))

        idx_ok, idx_form, idx_err = None, None, ""
        for form in ("I:XSP", "I:SPX"):
            try:
                j = self._get("/v3/snapshot/indices", {"ticker": form})
                if j.get("results"):
                    idx_ok, idx_form = True, form
                    break
                idx_ok, idx_err = False, f"{form}: empty results"
            except PolygonError as e:
                idx_ok = False
                idx_err = (f"{form}: HTTP {e.status_code} {e.body[:160]}")
        out.append(CheckResult(
            "xsp_underlying",
            Status.PASS if idx_ok else Status.FAIL,
            f"index value available via '{idx_form}'" if idx_ok
            else "index value NOT available — likely requires a separate "
                 "Indices subscription",
            "" if idx_ok else idx_err))
        return out

    def _historical_quotes_check(self, chain: list) -> CheckResult:
        if not chain:
            return CheckResult("historical_quotes", Status.SKIP,
                               "no contract available to test")
        c = chain[0]
        try:
            occ = self._occ_ticker("SPY", c["expiry"], c["strike"], c["right"])
        except Exception:                                # noqa: BLE001
            return CheckResult("historical_quotes", Status.SKIP,
                               "could not build an OCC ticker")
        try:
            j = self._get(f"/v3/quotes/{occ}", {"limit": 1})
            n = len(j.get("results") or [])
            return CheckResult(
                "historical_quotes",
                Status.PASS if n else Status.WARN,
                f"entitled ({n} row sample) for {occ}" if n
                else f"endpoint reachable but returned no rows for {occ}")
        except PolygonError as e:
            return CheckResult(
                "historical_quotes", Status.FAIL,
                f"HTTP {e.status_code} — backfill unavailable "
                "(forward-collect-only, as planned)", e.body[:300])

    def _historical_aggs_check(self) -> CheckResult:
        end = date.today()
        start = end - timedelta(days=7)
        try:
            j = self._get(
                f"/v2/aggs/ticker/SPY/range/1/minute/"
                f"{start.isoformat()}/{end.isoformat()}",
                {"limit": 5, "adjusted": "true"})
            n = len(j.get("results") or [])
            return CheckResult(
                "historical_aggs_1m",
                Status.PASS if n else Status.WARN,
                f"{n} sample 1-min bars returned for SPY" if n
                else "endpoint reachable but returned no bars")
        except PolygonError as e:
            return CheckResult("historical_aggs_1m", Status.FAIL,
                               f"HTTP {e.status_code}", e.body[:300])

    @staticmethod
    def _occ_ticker(underlying: str, expiry: str, strike: float,
                    right: str) -> str:
        """OCC-21 option symbol, e.g. O:SPY260731C00630000."""
        d = datetime.strptime(expiry, "%Y-%m-%d").strftime("%y%m%d")
        return f"O:{underlying}{d}{right}{int(round(strike * 1000)):08d}"
