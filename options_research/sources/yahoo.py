"""Yahoo Finance source, via the `yfinance` package.

The original spec says "do not build on this." That judgment stands for a
high-frequency collector, and this module does not pretend otherwise. It
exists because Phase 1's actual requirement is far lighter than the spec
assumed — H1-P1 takes ONE snapshot per day at a fixed time — and because the
alternative paths cost either a brokerage account or $79/month. Whether it is
adequate is decided by the probe, not by reputation in either direction.

Known limitations, stated up front rather than discovered later:

  * **No Greeks.** Yahoo returns implied volatility but no delta/gamma/theta/
    vega. Phase 1 does not need them (it selects strikes by straddle multiple),
    but the FULL profile cannot run on this source at all.
  * **No quote timestamp.** `lastTradeDate` is when the CONTRACT last TRADED,
    which for an illiquid strike can be hours or days stale. It is not the
    time the bid/ask was published. This is the serious problem, and the probe
    reports it as such rather than papering over it — an event clock derived
    from fetch time would silently smear every time-of-day result.
  * **Unofficial.** No contract, no SLA; the endpoint can change without
    notice. Cross-checking is not optional here.
"""

from datetime import datetime, timezone

from ..capabilities import CheckResult, Status
from .base import QuoteSource, classify_delay, DELAY_UNKNOWN


class YahooError(RuntimeError):
    pass


def _require_yfinance():
    try:
        import yfinance  # noqa: F401
        return yfinance
    except ImportError as e:                             # pragma: no cover
        raise YahooError(
            "yfinance is not installed. `pip install yfinance`") from e


class YahooSource(QuoteSource):
    name = "yahoo"

    def __init__(self, timeout: float = 20.0):
        self.yf = _require_yfinance()
        self.timeout = timeout

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── normalized reads ────────────────────────────────────────────────────

    def get_underlying(self, symbol: str) -> dict | None:
        fetch_ts = self._now_iso()
        t = self.yf.Ticker(symbol)
        try:
            hist = t.history(period="5d", interval="1m")
        except Exception as e:                            # noqa: BLE001
            raise YahooError(f"history failed: {e}") from e
        if hist is None or hist.empty:
            return None

        # group by ET session date; Yahoo returns tz-aware timestamps
        hist = hist.tz_convert("America/New_York")
        days = sorted({idx.date() for idx in hist.index})
        today_rows = hist[[idx.date() == days[-1] for idx in hist.index]]
        prev_close = None
        if len(days) > 1:
            prev_rows = hist[[idx.date() == days[-2] for idx in hist.index]]
            if not prev_rows.empty:
                prev_close = float(prev_rows["Close"].iloc[-1])
        if today_rows.empty:
            return None

        last_idx = today_rows.index[-1]
        event_ts = last_idx.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        vol = float(today_rows["Volume"].sum())
        typical = (today_rows["High"] + today_rows["Low"]
                   + today_rows["Close"]) / 3
        vwap = (float((typical * today_rows["Volume"]).sum() / vol)
                if vol else None)
        return {
            "last": float(today_rows["Close"].iloc[-1]),
            "bid": None, "ask": None,
            "volume": vol, "vwap": vwap,
            "day_open": float(today_rows["Open"].iloc[0]),
            "day_high": float(today_rows["High"].max()),
            "day_low": float(today_rows["Low"].min()),
            "prev_close": prev_close,
            "event_ts": event_ts, "fetch_ts": fetch_ts,
            "delay_class": classify_delay(event_ts, fetch_ts),
        }

    def get_chain(self, symbol, spot, strike_band_pct, dte_min, dte_max):
        from datetime import date
        fetch_ts = self._now_iso()
        t = self.yf.Ticker(symbol)
        today = date.today()
        lo, hi = spot * (1 - strike_band_pct), spot * (1 + strike_band_pct)
        out = []
        for exp in (t.options or []):
            try:
                dte = (date.fromisoformat(exp) - today).days
            except ValueError:
                continue
            if not (dte_min <= dte <= dte_max):
                continue
            try:
                ch = t.option_chain(exp)
            except Exception:                             # noqa: BLE001
                continue
            for frame, right in ((ch.calls, "C"), (ch.puts, "P")):
                if frame is None or frame.empty:
                    continue
                for _, row in frame.iterrows():
                    strike = float(row.get("strike", 0) or 0)
                    if not (lo <= strike <= hi):
                        continue
                    out.append(self._normalize(row, exp, strike, right,
                                               fetch_ts))
        return out

    @staticmethod
    def _normalize(row, expiry, strike, right, fetch_ts) -> dict:
        def num(key):
            v = row.get(key)
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return None if f != f else f          # drop NaN

        # lastTradeDate is a TRADE time, not a quote time. It is carried
        # through so the probe can measure how stale it is, but it must not
        # be treated as an authoritative quote clock.
        trade_ts = None
        ltd = row.get("lastTradeDate")
        try:
            if ltd is not None and ltd == ltd:
                trade_ts = ltd.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:                                 # noqa: BLE001
            trade_ts = None

        return {
            "expiry": expiry, "strike": strike, "right": right,
            "bid": num("bid"), "ask": num("ask"), "last": num("lastPrice"),
            "volume": num("volume"), "open_interest": num("openInterest"),
            "iv": num("impliedVolatility"),
            "delta": None, "gamma": None, "theta": None, "vega": None,
            "event_ts": trade_ts, "fetch_ts": fetch_ts,
            "delay_class": (classify_delay(trade_ts, fetch_ts) if trade_ts
                            else DELAY_UNKNOWN),
            "last_trade_ts": trade_ts,
        }

    # ── capability probe ────────────────────────────────────────────────────

    def probe(self) -> list:
        out = []
        try:
            u = self.get_underlying("SPY")
            out.append(CheckResult("auth", Status.PASS,
                                   "no key required (unofficial endpoint)"))
        except Exception as e:                            # noqa: BLE001
            out.append(CheckResult("auth", Status.FAIL, "request failed",
                                   str(e)[:300]))
            return out

        if not (u and u.get("last")):
            out.append(CheckResult("underlying_snapshot", Status.FAIL,
                                   "no SPY bars returned"))
            return out
        out.append(CheckResult(
            "underlying_snapshot", Status.PASS,
            f"SPY last={u['last']:.2f} via 1-min history "
            f"(vwap={'yes' if u.get('vwap') else 'no'})",
            evidence={k: u.get(k) for k in ("last", "day_open", "prev_close",
                                            "event_ts", "delay_class")}))

        try:
            chain = self.get_chain("SPY", u["last"], 0.07, 0, 7)
        except Exception as e:                            # noqa: BLE001
            out.append(CheckResult("chain_snapshot", Status.FAIL,
                                   "chain fetch failed", str(e)[:300]))
            return out
        out.append(CheckResult("chain_snapshot",
                               Status.PASS if chain else Status.FAIL,
                               f"{len(chain)} contracts (SPY, ±7%, 0-7 DTE)"))
        if not chain:
            return out

        n = len(chain)
        for rid, keys, thresh in (
                ("chain_bid_ask", ("bid", "ask"), 0.80),
                ("chain_iv", ("iv",), 0.80),
                ("chain_open_interest", ("open_interest",), 0.80),
                ("chain_volume", ("volume",), 0.50)):
            fracs = {k: sum(1 for c in chain if c.get(k) is not None) / n
                     for k in keys}
            worst = min(fracs.values())
            detail = ", ".join(f"{k}={v:.0%}" for k, v in fracs.items())
            out.append(CheckResult(
                rid,
                Status.FAIL if worst == 0 else
                (Status.WARN if worst < thresh else Status.PASS),
                f"({detail})", evidence={"n_contracts": n, **fracs}))

        out.append(CheckResult(
            "chain_greeks", Status.FAIL,
            "Yahoo returns no Greeks at all — the FULL profile cannot run on "
            "this source; Phase 1 does not need them"))

        # non-zero bid/ask is what actually matters for a fill engine
        two_sided = sum(1 for c in chain
                        if c.get("bid") and c.get("ask")
                        and c["bid"] > 0 and c["ask"] >= c["bid"])
        out.append(CheckResult(
            "atm_straddle",
            Status.PASS if two_sided >= n * 0.5 else Status.FAIL,
            f"{two_sided}/{n} contracts have a usable two-sided quote "
            f"(bid>0 and ask>=bid)"))

        # THE decisive weakness: how stale is the only timestamp available?
        with_ts = [c for c in chain if c.get("event_ts")]
        if not with_ts:
            out.append(CheckResult("exchange_timestamp", Status.FAIL,
                                   "no timestamp of any kind on contracts"))
            out.append(CheckResult("delay_class", Status.FAIL,
                                   "cannot classify delay"))
            return out
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        lags = []
        for c in with_ts:
            try:
                lags.append((datetime.strptime(c["fetch_ts"], fmt)
                             - datetime.strptime(c["event_ts"], fmt))
                            .total_seconds())
            except (ValueError, TypeError):
                continue
        lags.sort()
        med = lags[len(lags) // 2] if lags else None
        p90 = lags[int(len(lags) * 0.9)] if lags else None
        out.append(CheckResult(
            "exchange_timestamp", Status.FAIL,
            f"only lastTradeDate available — a TRADE time, not a quote time. "
            f"median staleness {med / 60:.0f} min, 90th pct "
            f"{p90 / 60:.0f} min. Using it as an event clock would smear "
            f"every time-of-day result"
            if med is not None else "timestamps unparseable",
            evidence={"median_lag_secs": med, "p90_lag_secs": p90,
                      "n_with_ts": len(with_ts)}))
        out.append(CheckResult(
            "delay_class", Status.FAIL,
            "cannot be established from a trade timestamp"))
        return out
