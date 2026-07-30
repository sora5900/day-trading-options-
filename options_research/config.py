"""Central configuration. Defaults follow OPTIONS_RESEARCH_SPEC.md."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Universe (spec §4)
    symbols: tuple = ("SPY", "QQQ")
    strike_band_pct: float = 0.07        # strikes within ±7% of spot
    dte_min: int = 0
    dte_max: int = 7                     # 0-7 DTE: day-trading research

    # Cadence (spec §4) — start at 5-min chain cadence
    underlying_cadence_secs: int = 15
    chain_cadence_secs: int = 300
    wide_snapshot_times_et: tuple = ("09:35", "12:45", "15:55")

    # Paper fill engine (spec §5) — non-negotiable realism
    spread_max_pct: float = 0.10         # reject if (ask-bid)/mid > 10%
    min_open_interest: int = 100
    slippage_ticks: int = 1
    tick_size: float = 0.01
    commission_per_contract: float = 0.65  # each way

    # Data quality thresholds
    stale_max_secs: float = 120.0
    wide_spread_alert_frac: float = 0.5

    # Storage
    db_path: str = field(
        default_factory=lambda: os.environ.get("OPTIONS_DB", "data/research.db"))

    # Data source credentials (research can run without them; collector will
    # refuse to start and say which key is missing)
    polygon_api_key: str = field(
        default_factory=lambda: os.environ.get("POLYGON_API_KEY", ""))
    tradier_token: str = field(
        default_factory=lambda: os.environ.get("TRADIER_TOKEN", ""))
    tradier_base: str = field(
        default_factory=lambda: os.environ.get(
            "TRADIER_BASE", "https://sandbox.tradier.com/v1"))


DEFAULT = Config()
