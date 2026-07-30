"""Options day-trading RESEARCH system.

This is not a trading bot. It is a research system whose product is a verdict:
"does this setup have an edge on days the model has never seen — after real costs?"

HARD RULE: no live orders anywhere. There is no broker execution path in this
codebase, and none may be added until a strategy has passed the validation
protocol in OPTIONS_RESEARCH_SPEC.md §7.
"""

__version__ = "0.1.0"
