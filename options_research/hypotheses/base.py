"""Hypothesis contract and registry — the extension point.

Adding a hypothesis family is meant to be cheap. Everything below comes free
once a class is registered: pre-registration hashing, split assignment,
journaling, deterministic replay, day-bootstrapped metrics, negative-control
comparison, and per-hypothesis verdicts.

To add one:

    @register
    class MyThing(Hypothesis):
        id, version, family, profile = "my_thing", "v1", "mean_reversion", PHASE1
        params = MyParams()                 # frozen dataclass
        def detect(self, ctx): ...          # -> Candidate | None

**Pre-registration enforcement.** Every hypothesis hashes its frozen
parameters, and the hash is stored on every trade it produces. Editing a
threshold without bumping `version` changes the hash, and the validator
refuses to pool trades across differing hashes. Silent parameter drift — the
easiest way to fool yourself on a multi-hypothesis platform — becomes a
loud error rather than a quiet contamination of the sample.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields, is_dataclass

from ..capabilities import PHASE1, FULL
from ..fill_engine import Leg


# ── what a hypothesis receives (stable signature) ───────────────────────────

@dataclass
class DecisionContext:
    """Everything a detector may look at, bundled so the `detect` signature
    stays stable as the platform grows. A hypothesis must read nothing outside
    this object — that is what keeps no-look-ahead enforceable."""
    conn: object
    symbol: str
    ts: str                      # decision time, on the exchange clock
    spot: float
    features: dict
    chain: list                  # rows with event_ts <= ts
    expiry: str | None           # nearest tradeable expiry
    gate_mode: str
    cfg: object


@dataclass
class ExitRules:
    target_pct: float | None
    stop_pct: float | None
    time_stop_et: str


@dataclass
class Candidate:
    symbol: str
    hypothesis_id: str
    version: str
    direction: str
    structure: str
    legs: list                   # list[Leg]
    exit_rules: ExitRules
    rationale: str
    confidence: float
    signals: dict = field(default_factory=dict)   # what fired, for the journal


# ── parameters ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Params:
    """Base for frozen hypothesis parameters.

    Frozen so a detector cannot mutate its own thresholds mid-run, and hashed
    so any edit is detectable after the fact.
    """

    def as_dict(self) -> dict:
        if not is_dataclass(self):
            return {}
        return {f.name: getattr(self, f.name) for f in fields(self)}


def params_hash(params: Params) -> str:
    payload = json.dumps(params.as_dict(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ── the contract ────────────────────────────────────────────────────────────

class Hypothesis(ABC):
    id: str = "base"
    version: str = "v1"
    family: str = "unclassified"
    profile: str = PHASE1
    params: Params = Params()

    #: True for negative controls — never promotable, and monitored for
    #: anomalous strength (a strong control means the pipeline is broken).
    is_control: bool = False

    #: Controls whose EV should be approximately -friction rather than 0.
    measures_friction: bool = False

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def params_hash(self) -> str:
        return params_hash(self.params)

    @abstractmethod
    def detect(self, ctx: DecisionContext) -> Candidate | None:
        """Return a candidate or None. Must read only from `ctx`."""

    def manifest(self) -> dict:
        """The pre-registration record, written before any test observation."""
        return {
            "id": self.id, "version": self.version, "family": self.family,
            "profile": self.profile, "is_control": self.is_control,
            "measures_friction": self.measures_friction,
            "params": self.params.as_dict(),
            "params_hash": self.params_hash,
            "docstring": (self.__doc__ or "").strip(),
        }


# ── registry ────────────────────────────────────────────────────────────────

REGISTRY: dict[str, type] = {}


def register(cls):
    """Register a hypothesis class. Duplicate id@version is an error — two
    different rule sets under one identity would silently pool their trades."""
    key = f"{cls.id}@{cls.version}"
    if key in REGISTRY:
        raise ValueError(
            f"duplicate hypothesis {key}: bump `version` instead of editing "
            f"an existing registration in place")
    REGISTRY[key] = cls
    return cls


def enabled(profile: str = PHASE1, include_controls: bool = True) -> list:
    """Instantiate every hypothesis runnable under `profile`.

    A `full`-profile hypothesis is skipped in phase1 (it needs vendor Greeks);
    a `phase1` hypothesis runs under either, since price-space signals are
    available regardless of tier.
    """
    out = []
    for cls in REGISTRY.values():
        if cls.profile == FULL and profile == PHASE1:
            continue
        if cls.is_control and not include_controls:
            continue
        out.append(cls())
    return sorted(out, key=lambda h: (h.family, h.id))


def get(key: str):
    cls = REGISTRY.get(key)
    return cls() if cls else None


def manifest_all(profile: str = PHASE1) -> list:
    return [h.manifest() for h in enabled(profile)]
