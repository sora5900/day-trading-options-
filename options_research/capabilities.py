"""Provider-agnostic capability model.

The system must never design around a feature the subscription does not
actually include, and must never silently substitute or locally estimate a
missing field. This module defines what the research plan REQUIRES; each
provider implements `probe()` and reports what it can actually deliver.

If any blocking requirement fails, the collector refuses to start.
"""

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"      # works, but degraded (e.g. sparse population)
    SKIP = "SKIP"      # not attempted (dependency failed)


@dataclass(frozen=True)
class Requirement:
    id: str
    description: str
    blocking: bool          # True → collection refuses without it
    why: str                # what breaks in the research plan without it


# ── What the registered hypotheses actually need ────────────────────────────
REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "auth", "API key authenticates", True,
        "nothing works"),
    Requirement(
        "underlying_snapshot",
        "Underlying quote/aggregate for SPY/QQQ (last, bid, ask, volume, "
        "VWAP, day OHLC, prev close)", True,
        "VWAP gate, realized vol, gap and ORB features, H3 stage 1"),
    Requirement(
        "chain_snapshot",
        "Option chain snapshot filtered by strike band and expiry", True,
        "every hypothesis; no chain means no research"),
    Requirement(
        "chain_bid_ask",
        "Populated bid AND ask on chain contracts", True,
        "the paper fill engine is the core of the system; mid-only data "
        "makes every verdict a lie"),
    Requirement(
        "chain_greeks",
        "Populated delta (and gamma/theta/vega) on chain contracts", True,
        "H1/H2 select the short leg BY DELTA (15Δ); H2 needs 25Δ skew. "
        "Locally estimating Greeks is explicitly forbidden"),
    Requirement(
        "chain_iv",
        "Populated implied volatility on chain contracts", True,
        "VRP trigger (H1) and skew trigger (H2) are both IV-derived"),
    Requirement(
        "chain_open_interest",
        "Populated open interest on chain contracts", True,
        "liquidity gate (OI >= 500) in every hypothesis"),
    Requirement(
        "chain_volume",
        "Populated contract volume", True,
        "liquidity gate; UNFILLABLE classification"),
    Requirement(
        "exchange_timestamp",
        "Per-quote exchange/SIP timestamp, distinct from fetch time", True,
        "platform upgrade #1: authoritative event time. Without it every "
        "time-of-day result is smeared by variable API delay, which "
        "invalidates H1's 12:00 entry and H3 entirely"),
    Requirement(
        "delay_class",
        "Delay class determinable (realtime vs delayed)", True,
        "platform upgrade #3: delayed-era and realtime-era rows must never "
        "be pooled in one validation sample"),
    Requirement(
        "zero_dte",
        "Same-day (0DTE) expiries present in the chain", True,
        "H1, H2 and H3 stage 3 are all 0DTE"),
    Requirement(
        "xsp_options",
        "XSP option chain available", False,
        "H1 arm B (cash-settled European comparator). Without it the "
        "settlement A/B cannot run and H1 is SPY-only"),
    Requirement(
        "xsp_underlying",
        "XSP/SPX underlying index value available", False,
        "needed to compute XSP moneyness, VWAP gate and realized vol. "
        "May require a separate Indices subscription"),
    Requirement(
        "historical_quotes",
        "Historical NBBO quotes (backfill)", False,
        "informational only — we are forward-collect-only by decision"),
    Requirement(
        "historical_aggs_1m",
        "Historical 1-minute aggregates", False,
        "would accelerate H3 stage 1 (underlying-only test) by months"),
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
    results: list                    # list[CheckResult]

    def by_id(self, rid: str) -> CheckResult | None:
        return next((r for r in self.results if r.requirement_id == rid), None)

    @property
    def blocking_failures(self) -> list:
        return [r for r in self.results
                if r.status is Status.FAIL and r.requirement.blocking]

    @property
    def can_collect(self) -> bool:
        return not self.blocking_failures

    def render(self) -> str:
        w = 78
        out = ["=" * w,
               f"CAPABILITY PROBE — provider: {self.provider}",
               "=" * w, ""]

        icon = {Status.PASS: "✓", Status.FAIL: "✗",
                Status.WARN: "!", Status.SKIP: "-"}
        for r in self.results:
            out.append(f" {icon[r.status]} {r.status.value:5s} "
                       f"{r.requirement_id:22s} {r.detail}")
            if r.raw_error:
                out.append(f"           provider said: {r.raw_error[:200]}")
        out.append("")

        if self.can_collect:
            out.append("VERDICT: all blocking requirements met — collection "
                       "may start.")
        else:
            out.append("VERDICT: COLLECTION REFUSED. Blocking requirements "
                       "unmet:")
            for r in self.blocking_failures:
                out.append(f"  • {r.requirement_id}: {r.requirement.description}")
                out.append(f"    breaks: {r.requirement.why}")
            out.append("")
            out.append("No field will be substituted or locally estimated. "
                       "Either simplify the experiment or upgrade the plan.")

        degraded = [r for r in self.results
                    if r.status in (Status.FAIL, Status.WARN)
                    and not r.requirement.blocking]
        if degraded:
            out.append("")
            out.append("NON-BLOCKING GAPS (research proceeds, reduced scope):")
            for r in degraded:
                out.append(f"  • {r.requirement_id}: {r.requirement.why}")
        out.append("=" * w)
        return "\n".join(out)
