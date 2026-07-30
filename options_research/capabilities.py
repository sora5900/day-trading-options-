"""Provider-agnostic capability model, with per-phase requirement profiles.

The system must never design around a feature the subscription does not
actually include, and must never silently substitute or locally estimate a
missing field. Each provider implements `probe()` and reports what it can
actually deliver; if a requirement blocking the active profile fails, the
collector refuses to start.

Two profiles:
  PHASE1 — runs on the cheapest delayed plan. Requires PRICES and an exchange
           clock, nothing more. Greeks and vendor IV are recorded when present
           but never required, because Phase 1 works in price space.
  FULL   — the delta/IV-based hypotheses in HYPOTHESES.md.
"""

from dataclasses import dataclass, field
from enum import Enum

PHASE1 = "phase1"
FULL = "full"
BOTH = frozenset({PHASE1, FULL})


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"      # works, but degraded (e.g. sparse population)
    SKIP = "SKIP"      # not attempted (dependency failed)


@dataclass(frozen=True)
class Requirement:
    id: str
    description: str
    blocking_in: frozenset      # profiles this requirement blocks
    why: str

    def blocks(self, profile: str) -> bool:
        return profile in self.blocking_in


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "auth", "API key authenticates", BOTH,
        "nothing works"),
    Requirement(
        "underlying_snapshot",
        "Underlying quote/bar for SPY/QQQ (last, volume, day OHLC, prev close)",
        BOTH,
        "realized vol, VWAP gate, gap features, and H3-P1 stage 1"),
    Requirement(
        "chain_snapshot",
        "Option chain snapshot filtered by strike band and expiry", BOTH,
        "every options hypothesis"),
    Requirement(
        "chain_bid_ask",
        "Populated bid AND ask on chain contracts", BOTH,
        "the paper fill engine, and in Phase 1 the ENTIRE signal: priced "
        "move and skew are both derived from prices"),
    Requirement(
        "atm_straddle",
        "Two-sided quotes on both ATM call and ATM put of the traded expiry",
        frozenset({PHASE1}),
        "the Phase 1 priced-move estimator IS the ATM straddle mid. Without "
        "it there is no VRP trigger and no strike-selection rule"),
    Requirement(
        "exchange_timestamp",
        "Per-quote exchange/SIP timestamp, distinct from fetch time", BOTH,
        "authoritative event time. Without it every time-of-day result is "
        "smeared by variable API delay"),
    Requirement(
        "delay_class",
        "Delay class determinable (realtime vs delayed)", BOTH,
        "delayed-era and realtime-era rows must never be pooled"),
    Requirement(
        "zero_dte",
        "Same-day (0DTE) expiries present in the chain", BOTH,
        "H1-P1 and H2-P1 are 0DTE"),
    # ── recorded when present, never required in Phase 1 ──────────────────
    Requirement(
        "chain_greeks",
        "Populated delta/gamma/theta/vega", frozenset({FULL}),
        "FULL profile selects strikes by delta. Phase 1 selects by straddle "
        "multiple instead, so this is recorded for later study only"),
    Requirement(
        "chain_iv",
        "Populated implied volatility", frozenset({FULL}),
        "FULL profile's VRP and skew triggers are IV-derived. Phase 1 uses "
        "price-space equivalents, so this is recorded but not required"),
    Requirement(
        "chain_open_interest", "Populated open interest", frozenset(),
        "liquidity gate uses OI when available; degrades to a spread-only "
        "gate and records which mode was used"),
    Requirement(
        "chain_volume", "Populated contract volume", frozenset(),
        "liquidity gate; UNFILLABLE classification"),
    Requirement(
        "xsp_options", "XSP option chain available", frozenset(),
        "H1 arm B (cash-settled European comparator). Without it H1 is "
        "SPY/QQQ-only and the settlement A/B is deferred"),
    Requirement(
        "xsp_underlying", "XSP/SPX underlying index value", frozenset(),
        "XSP moneyness and realized vol; may need a separate Indices plan"),
    Requirement(
        "historical_quotes", "Historical NBBO quotes (backfill)", frozenset(),
        "informational only — forward-collect-only by decision"),
    Requirement(
        "historical_aggs_1m",
        "Historical 1-minute underlying aggregates", frozenset(),
        "HIGH VALUE: H3-P1 stage 1 needs only underlying bars, so lookback "
        "here converts a months-long wait into an immediate answer"),
)

BY_ID = {r.id: r for r in REQUIREMENTS}


@dataclass
class CheckResult:
    requirement_id: str
    status: Status
    detail: str = ""
    raw_error: str = ""              # provider's verbatim error, never paraphrased
    evidence: dict = field(default_factory=dict)

    @property
    def requirement(self) -> Requirement:
        return BY_ID[self.requirement_id]


@dataclass
class ProbeReport:
    provider: str
    results: list
    profile: str = PHASE1

    def by_id(self, rid: str) -> CheckResult | None:
        return next((r for r in self.results if r.requirement_id == rid), None)

    @property
    def blocking_failures(self) -> list:
        return [r for r in self.results
                if r.status is Status.FAIL
                and r.requirement.blocks(self.profile)]

    @property
    def can_collect(self) -> bool:
        return not self.blocking_failures

    def render(self) -> str:
        w = 78
        out = ["=" * w,
               f"CAPABILITY PROBE — provider: {self.provider}  "
               f"profile: {self.profile}",
               "=" * w, ""]
        icon = {Status.PASS: "✓", Status.FAIL: "✗",
                Status.WARN: "!", Status.SKIP: "-"}
        for r in self.results:
            mark = "*" if r.requirement.blocks(self.profile) else " "
            out.append(f" {icon[r.status]}{mark}{r.status.value:5s} "
                       f"{r.requirement_id:22s} {r.detail}")
            if r.raw_error:
                out.append(f"           provider said: {r.raw_error[:200]}")
        out.append("")
        out.append(" (* = blocking for this profile)")
        out.append("")

        if self.can_collect:
            out.append(f"VERDICT: all {self.profile} requirements met — "
                       "collection may start.")
        else:
            out.append("VERDICT: COLLECTION REFUSED. Blocking requirements "
                       "unmet:")
            for r in self.blocking_failures:
                out.append(f"  • {r.requirement_id}: {r.requirement.description}")
                out.append(f"    breaks: {r.requirement.why}")
            out.append("")
            out.append("No field will be substituted or locally estimated.")

        degraded = [r for r in self.results
                    if r.status in (Status.FAIL, Status.WARN)
                    and not r.requirement.blocks(self.profile)]
        if degraded:
            out.append("")
            out.append("NON-BLOCKING GAPS (research proceeds, reduced scope):")
            for r in degraded:
                out.append(f"  • {r.requirement_id}: {r.requirement.why}")
        out.append("=" * w)
        return "\n".join(out)
