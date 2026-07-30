"""Hypothesis registry.

Importing a module registers its hypotheses. To add a family, drop a module
here and import it below — nothing else in the platform needs to change.
"""

from .base import (Candidate, DecisionContext, ExitRules, Hypothesis, Params,
                   REGISTRY, enabled, get, manifest_all, params_hash, register)

# Registration happens on import.
from . import h1_p1_vrp          # noqa: F401
from . import h2_p1_skew         # noqa: F401
from . import h3_p1_momentum     # noqa: F401
from . import controls           # noqa: F401

__all__ = ["Candidate", "DecisionContext", "ExitRules", "Hypothesis",
           "Params", "REGISTRY", "enabled", "get", "manifest_all",
           "params_hash", "register"]
