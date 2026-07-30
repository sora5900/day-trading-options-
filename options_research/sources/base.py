"""Source interface — the replaceability boundary.

Everything above this line (db, features, strategies, fill engine, validator)
is provider-agnostic. Swapping Polygon for Tradier, ThetaData, or a real-time
tier means implementing this interface and nothing else. No provider-specific
field name, ticker format, or entitlement rule may leak upward.

Normalized shapes returned by every source:

underlying dict:
  last, bid, ask, volume, vwap, day_open, day_high, day_low, prev_close,
  event_ts (ISO-8601 UTC, EXCHANGE timestamp — authoritative event time),
  fetch_ts (ISO-8601 UTC, when we called), delay_class

contract dict:
  expiry ('YYYY-MM-DD'), strike, right ('C'/'P'), bid, ask, last, volume,
  open_interest, iv, delta, gamma, theta, vega,
  event_ts, fetch_ts, delay_class

`event_ts` is the authoritative event time everywhere downstream. `fetch_ts`
is recorded separately for data-quality accounting only and must never be used
as an event time.

Sources MUST return None for a field the provider did not populate. Filling a
gap with a mid-price, a stale value, or a locally computed Greek is forbidden:
it converts a known entitlement gap into a silent data error.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone

DELAY_REALTIME = "realtime"
DELAY_15M = "delayed_15m"
DELAY_UNKNOWN = "unknown"


def epoch_to_iso(value) -> str | None:
    """Normalize a provider epoch (ns/us/ms/s) to ISO-8601 UTC.

    Providers are inconsistent about units; magnitude disambiguates.
    Returns None for anything unusable rather than guessing.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e17:        # nanoseconds
        secs = v / 1e9
    elif v > 1e14:      # microseconds
        secs = v / 1e6
    elif v > 1e11:      # milliseconds
        secs = v / 1e3
    else:               # seconds
        secs = v
    try:
        return datetime.fromtimestamp(secs, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def classify_delay(event_ts: str | None, fetch_ts: str | None) -> str:
    """Empirically classify delay from observed lag.

    More trustworthy than the plan description: it measures what actually
    arrived rather than what the pricing page claims.
    """
    if not event_ts or not fetch_ts:
        return DELAY_UNKNOWN
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        lag = (datetime.strptime(fetch_ts, fmt)
               - datetime.strptime(event_ts, fmt)).total_seconds()
    except ValueError:
        return DELAY_UNKNOWN
    if lag < 120:
        return DELAY_REALTIME
    if 300 <= lag <= 2400:      # ~5-40 min covers a 15-min delayed feed
        return DELAY_15M
    return DELAY_UNKNOWN


class QuoteSource(ABC):
    name: str = "base"

    @abstractmethod
    def get_underlying(self, symbol: str) -> dict | None:
        ...

    @abstractmethod
    def get_chain(self, symbol: str, spot: float, strike_band_pct: float,
                  dte_min: int, dte_max: int) -> list[dict]:
        """Filtered chain: strikes within ±band of spot, expiries in DTE range."""
        ...

    @abstractmethod
    def probe(self) -> list:
        """Return list[CheckResult] describing what this key can actually do.

        Must attempt real calls against real endpoints. Must capture the
        provider's verbatim error text for any failure — entitlement messages
        are the only reliable source of truth about a plan.
        """
        ...
