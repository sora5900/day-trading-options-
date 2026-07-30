"""Central configuration. Defaults follow OPTIONS_RESEARCH_SPEC.md."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Requirement profile: 'phase1' runs on the cheapest delayed plan and
    # needs prices only; 'full' additionally requires vendor Greeks and IV.
    profile: str = "phase1"

    # Universe. XSP (H1 arm B) is enabled only if the capability probe
    # confirms both the option chain and the underlying index value — it is
    # never assumed available.
    symbols: tuple = ("SPY", "QQQ")
    arm_b_symbol: str = "XSP"
    arm_b_enabled: bool = False          # set by the probe, never by hand
    strike_band_pct: float = 0.07        # strikes within ±7% of spot
    dte_min: int = 0
    dte_max: int = 7                     # 0-7 DTE: day-trading research

    # Cadence: 1-minute chain snapshots (~10.6 GB/yr). Storage is a rounding
    # error against permanently foreclosing timing questions — you cannot
    # resample the past.
    underlying_cadence_secs: int = 15
    chain_cadence_secs: int = 60
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
