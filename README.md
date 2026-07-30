# Options Day-Trading Research System

**This is not a trading bot.** It is a research system whose product is a
verdict: *"does this setup have an edge on days the model has never seen —
after real costs?"*

Full design rationale and the lessons it encodes are in
[`OPTIONS_RESEARCH_SPEC.md`](OPTIONS_RESEARCH_SPEC.md). The short version,
learned expensively on a prior betting-research project:

- Mining finds fake edges — every pattern shipped without a train/test split
  failed forward.
- Structure replicates; narratives don't.
- Win rate is vanity; **edge vs the price, net of friction, is the only number
  that matters** — and options friction is 10–20% round trip.
- The default verdict is DISCARD. A strategy earns continuation.

**Hard rule: there is no broker execution path in this codebase**, and none may
be added until a strategy passes the §7 validation protocol.

## Architecture

```
COLLECTOR → FEATURES → SETUP DETECTOR → PAPER FILL ENGINE → GRADER → VALIDATOR → REPORTER
raw truth    derived     hypotheses       pays the spread     net P&L   unseen days   honest verdicts
```

- **Collector** (`collector.py`) — SPY/QQQ snapshots, strikes ±7% of spot,
  0–7 DTE, 5-min chain cadence, NYSE-calendar gated, data-quality checks on
  every snapshot, two-source cross-check.
- **Features** (`features.py`) — IV rank, VRP, opening range, VWAP deviation,
  realized vol, skew, term slope, liquidity score, regime. Recomputable;
  raw tables are never edited.
- **Strategies** (`strategies/`) — versioned hypotheses with mechanical
  rationales: ORB debit vertical, VRP-fade credit spread, gap fade (plus its
  naive baseline, which the conditioned version must beat).
- **Paper fill engine** (`fill_engine.py`) — the component that decides whether
  this is real. Buys pay the ask, sells receive the bid, plus slippage and
  commissions; wide/illiquid contracts are rejected as UNFILLABLE. Never fills
  at mid.
- **Grader** (`grader.py`) — exit rules (target/stop/time), MAE/MFE, P&L net
  of all costs.
- **Validator** (`validator.py`) — train/test split **by day**, frozen on first
  use; verdicts only from unseen days; minimum 100 test trades over 30 days
  before any verdict other than UNPROVEN.
- **Reporter** (`reporter.py`) — the shadow ledger and per-strategy verdicts.

## Setup

```bash
pip install -r requirements.txt
export POLYGON_API_KEY=...     # primary (Starter tier is fine — delayed is fine)
export TRADIER_TOKEN=...       # optional but recommended cross-check (sandbox is free)

python -m options_research init
python -m options_research run          # market-hours research loop (paper only)
```

Other commands:

```bash
python -m options_research collect --once     # single snapshot cycle
python -m options_research crosscheck         # source-divergence check
python -m options_research freeze-split 2026-09-15   # first TEST day — frozen forever
python -m options_research validate           # assign splits + write verdicts
python -m options_research report --trades    # shadow ledger
```

## Discipline (non-negotiable)

1. **No look-ahead.** Decisions at time T read only data ≤ T, enforced by the
   query layer and asserted in `tests/test_no_lookahead.py`.
2. **The split is frozen.** Changing the cutoff after the fact raises an error.
   Tuning after peeking at test burns that test set: bump the strategy
   `version` and treat the old test days as train.
3. **Fills are pessimistic.** If a strategy's edge is smaller than its spread
   cost, the report shows it immediately (`avg_spread_paid_pct`).
4. **Expect "no edge".** That is a successful outcome for a research system;
   a system that always finds something is broken.

## Tests

```bash
python -m pytest tests/ -q
```

The fill engine is tested against hand-computed examples, including one that
quantifies how much a mid-price backtest would lie (3× P&L overstatement on a
realistic vertical round trip).
