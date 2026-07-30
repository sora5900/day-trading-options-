"""Source interface. Every source returns the same normalized dict shapes so
the collector, cross-checker, and tests don't care which vendor produced them.

Underlying dict keys:
  last, bid, ask, volume, vwap, day_open, day_high, day_low, prev_close, quote_ts

Contract dict keys:
  expiry ('YYYY-MM-DD'), strike, right ('C'/'P'), bid, ask, last, volume,
  open_interest, iv, delta, gamma, theta, vega, quote_ts
"""

from abc import ABC, abstractmethod


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
