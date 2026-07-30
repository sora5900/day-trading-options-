# Options Research Platform

**Not a trading bot.** An evidence-gathering platform whose product is a
verdict: *"does this hypothesis have an edge on days it has never seen —
after real costs?"*

Design documents, in reading order:

| Doc | What it fixes |
|---|---|
| [`OPTIONS_RESEARCH_SPEC.md`](OPTIONS_RESEARCH_SPEC.md) | Original architecture and the lessons behind it |
| [`HYPOTHESES.md`](HYPOTHESES.md) | Formal pre-registration; settlement mechanics; promotion criteria |
| [`PHASE1.md`](PHASE1.md) | **Start here.** Delayed-plan redesign, price-space signals, and the power analysis that governs what Phase 1 can claim |

**Hard rule: there is no broker execution path in this codebase**, and none may
be added until a hypothesis passes the full promotion bar (`HYPOTHESES.md` §6).

## The number that governs everything

A $2-wide 0DTE credit spread has a **per-trade standard deviation of ~$46**
against an EV measured in single dollars. So:

| Test sample | Minimum detectable edge |
|---|---|
| n = 100 | **$12.80/trade — 6.4% of capital at risk** |
| n = 200 | $9.05/trade — 4.5% |

A real $3–5/trade edge needs **16–62 months** of collection. Phase 1 therefore
does *not* claim to find edges. It measures friction fast, runs the
underlying-only momentum test that *can* reach significance quickly, screens
for implausibly large effects, and validates the machinery. Every report
prints the minimum detectable edge beside each interval, and any interval
containing zero is annotated: **a null is not evidence of no edge.**

## Adding a hypothesis

The platform is registry-driven. A new family is one file:

```python
@register
class MyThing(Hypothesis):
    """Why this should work, mechanically."""
    id, version, family = "my_thing", "v1", "mean_reversion"
    params = MyParams()                    # frozen dataclass

    def detect(self, ctx: DecisionContext) -> Candidate | None:
        ...
```

Everything else is inherited: pre-registration hashing, split assignment,
journaling, deterministic replay, day-bootstrapped intervals, control
comparison, and independent per-hypothesis verdicts.

**Pre-registration is enforced, not trusted.** Each hypothesis hashes its
frozen parameters, and the hash is stored on every trade. Edit a threshold
without bumping `version` and the platform *refuses to run* rather than
pooling two rule sets into one sample.

## Registered hypotheses (Phase 1)

| ID | Family | Signal |
|---|---|---|
| `h1_p1_vrp` | variance_risk_premium | Priced move (ATM straddle) vs realized move, midday |
| `h2_p1_skew` | variance_risk_premium | Risk-reversal price spike; must beat H1 on shared days |
| `h3_p1_momentum` | intraday_momentum | Gated behind an underlying-only reproduction + friction hurdle |
| `c1_orb_control` | control | **Leakage detector.** Positive unseen result halts the pipeline |
| `c2_random_cost` | control | **Cost meter.** Untriggered entries; EV ≈ −friction or all verdicts are void |

Phase 1 needs **prices and an exchange clock only** — no vendor Greeks, no
vendor IV. Delta selection is replaced by `spot − 1.25 × straddle_mid`
(≈1 SD ≈ 15–16 delta equivalent), and IV-space signals by their price-space
equivalents.

## Usage

```bash
pip install -r requirements.txt
export POLYGON_API_KEY=...

python -m options_research probe        # capability gate; exit 2 = refused
python -m options_research init
python -m options_research run          # market-hours shadow loop, paper only
```

```bash
python -m options_research manifest             # pre-registration record
python -m options_research controls             # control health + friction
python -m options_research freeze-split 2026-09-15
python -m options_research validate             # halts if controls are sick
python -m options_research report --trades
python -m options_research replay 42            # why trade 42 did what it did
```

## Invariants (enforced by tests)

1. **No look-ahead.** Reads are bounded by `event_ts` (the exchange clock),
   never `fetch_ts`. A quote that *happened* at 14:00 but *arrived* at 14:20
   is visible to a 14:05 decision.
2. **Frozen split.** Changing the cutoff after the fact raises.
3. **Never pool** delayed-era with realtime-era trades, or trades with
   different parameter hashes.
4. **Bootstrap by day, never by trade.** Intraday trades are correlated;
   trade-level resampling manufactures precision.
5. **Slices are train-only.** A strong slice becomes a new versioned
   hypothesis with a fresh test set, never a verdict.
6. **Pessimistic fills.** Buys pay the ask, sells receive the bid, plus
   slippage and commissions. Never the mid.
7. **No silent sample loss.** Trades stranded by collection gaps are swept and
   marked `UNGRADED`, and the report states how many — an invisibly shrinking
   sample is worse than a wrong number.
8. **Controls gate verdicts.** `validate` refuses to issue any verdict if C1
   or C2 reads anomalously.

## Tests

```bash
python -m pytest tests/ -q      # 104 tests
```

Includes hand-computed fill examples, one of which quantifies how much a
mid-price backtest would lie (**3× P&L overstatement** on a realistic vertical
round trip).
