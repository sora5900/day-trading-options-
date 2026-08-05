# Findings

Research log. **Negative results are the product.** Each entry records what was
tested, on what data, what came back, and what it closes — so a question that
has been answered is not quietly re-opened later.

---

## F-001 — Massive/Polygon free tier: what it actually provides

**Date:** 2026-08-04 · **Method:** `options_research probe`, live account

Measured, not read off a pricing page:

| Capability | Result |
|---|---|
| Auth | ✓ (`api.polygon.io`) |
| Underlying **snapshot** | ✗ 403 `NOT_AUTHORIZED` |
| Option chain snapshot | ✗ 403 |
| XSP options / SPX index | ✗ 403 |
| Rate limit | ✗ throttled after 4 rapid requests (~5/min) |
| **Historical 1-min aggregates** | **✓ PASS** |
| Lookback | **exactly 2 years** (3y → 403) |

**Consequence:** all options research (H1-P1, H2-P1) is blocked without a paid
plan. Underlying-only research is not blocked. The collector correctly refused
to start rather than accumulate unusable data.

---

## F-002 — H3-P1 stage 1: intraday momentum does NOT reproduce on SPY

**Date:** 2026-08-04 · **Data:** SPY 1-minute bars, 426,402 rows,
**495 full sessions**, 2024-08-05 → 2026-08-03 · **Cost:** $0

Testing Gao, Han, Li & Zhou (JFE 2018): does the first half-hour return predict
the last half-hour return?

### Result

| Measure | Value |
|---|---|
| Regression beta (`r_last ~ r1`) | **+0.0266** (t = **0.77**) |
| R² | 0.0012 |
| Rule `sign(r1)` into the close | **−0.079 bps/day** |
| 95% CI (day bootstrap) | **[−1.998, +1.919] bps** |
| Hit rate | 48.9% |
| Daily SD | 22.6 bps |
| Minimum detectable edge at n=495 | **2.841 bps** |

Beta is positive — the direction the literature predicts — but indistinguishable
from noise. The rule's point estimate is slightly negative.

### This is an UNDERPOWERED null, stated honestly

| Effect to detect | Sessions needed | Years |
|---|---|---|
| 0.5 bps/day | 16,036 | 63.6 |
| 1.0 bps/day | 4,009 | **15.9** |
| 2.0 bps/day | 1,003 | 4.0 |
| 2.84 bps/day | 497 | 2.0 ← the limit of this sample |

A genuine 1 bps/day effect would need ~16 years of data. The free tier provides
2. **This question cannot be resolved to the precision that would matter on
achievable data.** A null here is not proof the effect is absent.

### Why the null is nevertheless decisive

Run the **most optimistic end of the interval** (+1.919 bps — a value with no
evidence behind it) through the stage-2 friction hurdle at 20× delta leverage:

| Option friction | Levered move | Shortfall |
|---|---|---|
| 10% of premium | +0.384% | **26×** |
| 15% of premium | +0.384% | **39×** |
| 26% of premium | +0.384% | **68×** |

Even the best case fails by one to two orders of magnitude. Leverage scales the
move and the premium together; it does not rescue the trade.

Shares arm: point estimate **−0.399 bps/day (−1.01%/yr)** net of ~0.32 bps
round-trip share friction. Optimistic bound +1.599 bps/day (+4.03%/yr), with no
evidence supporting it.

### Verdict: **DISCARD**

- The **options branch of H3-P1 is permanently closed.** Stage 2 fails at every
  plausible friction level even under the most generous reading, so stage 3 is
  never run and the options test set is never opened.
- The **shares branch has no supporting evidence** (negative point estimate).
- Re-opening this requires a *different* hypothesis with its own
  pre-registration — not a re-run of this one on more data.

### What it cost

$0 and roughly 25 minutes, against the alternative of months of options
collection plus a paid plan to test a hypothesis that fails on arithmetic.
This is the stage gate doing exactly its job.

### QQQ cross-check — consistent

Same 495 sessions, same window:

| Measure | SPY | QQQ |
|---|---|---|
| Beta | +0.0266 (t=0.77) | +0.0214 (t=0.79) |
| R² | 0.0012 | 0.0013 |
| Rule mean | −0.079 bps/day | −0.395 bps/day |
| 95% CI | [−1.998, +1.919] | [−2.873, +2.104] |
| Hit rate | 48.9% | 48.7% |
| Daily SD | 22.6 bps | 28.7 bps |
| MDE | 2.841 bps | 3.610 bps |

**This is NOT two independent confirmations.** SPY and QQQ correlate ~0.9;
this is one market measured twice. It rules out a data bug or a single-series
quirk — not sampling error.

Worth recording: **beta is positive on both while the sign-following rule
loses on both.** Beta is magnitude-weighted, so a weak linear tilt carried by
a few large days can coexist with a sign rule that bleeds. Even the direction
of the relationship is not stable enough to act on.

### Caveats recorded honestly

- Two years is a short window and 2024-2026 is one regime.
- The published effect used a much longer sample; failure to reproduce here
  does not refute the paper. It refutes its *usefulness at this sample size and
  in this instrument*, which is the only question that matters here.
- Both symbols share the same 495 sessions, so their agreement is a
  consistency check, not additional statistical power.

---

## F-003 — The variance risk premium is too thin to trade at retail friction

**Date:** 2026-08-04 · **Data:** 499 days of ATM next-day straddle closes,
SPY, 2024-08-05 → 2026-07-31 (Massive Options Starter aggregates) ·
**Cost:** $29 (the Starter month), ~2 minutes of fetching

The question that H1-P1 and H2-P1 both turn on: is index implied volatility
rich enough to clear retail friction? The PHASE1.md arithmetic requires
roughly **20% richness** for a $2-wide 0DTE credit spread to survive costs.

### Measurement (model-free, daily closes)

| Measure | Value |
|---|---|
| Market charged (mean 1-day priced move) | 0.729% |
| Market realized (mean next-day move) | 0.686% |
| Premium | **+4.31 bps/day**, 95% CI **[−1.55, +10.04]** |
| **Richness** | **5.9%**, CI ≈ **[−2.1%, +13.8%]** |
| Days premium positive | 60.3% |
| Minimum detectable at n=499 | 8.31 bps |

The premium is not statistically distinguishable from zero at two years of
data — and, decisively, **even the upper bound of the interval fails the
viability threshold**:

| Reading | Richness | Gross on $40 credit | vs round trip $10.60 | vs free expiry $5.30 |
|---|---|---|---|---|
| Point estimate | 5.9% | $2.36 | **−$8.24** | **−$2.94** |
| CI upper bound | 13.8% | $5.51 | **−$5.09** | **+$0.21** |

The single non-negative cell requires simultaneously: the most optimistic
richness the data allows, a guaranteed free expiry (which the §1 settlement
model shows SPY does not reliably grant — forced close, assignment, pin
risk), and no tail events. That is not an edge; that is zero, dressed up.

### Verdict: **H1-P1 and H2-P1 DISCARDED on arithmetic**

Same logic that closed H3-P1: the optimistic bound of the measured effect
cannot clear measured/modelled costs, so the options test set never opens.
H2-P1 dies with H1-P1 — it is the same premium with a different trigger, and
a conditioning cannot rescue a premium that is not there.

### Caveats recorded honestly

- **Close-to-close, not intraday.** This measures the overnight+day straddle
  premium; H1-P1 as registered trades intraday 0DTE from noon. The intraday
  premium could differ. But it would need to be ~3× the measured all-day
  richness to reach viability, with no mechanism suggesting that.
- One regime (2024–2026, mostly low-vol grind with episodic spikes).
- The 60.3% positive-day rate shows the classic VRP shape — small steady
  premium, occasionally devoured by large moves. The shape is real; the size
  is not tradeable at retail costs.
- Richness of the ATM straddle is a proxy for richness of a 15Δ spread; skew
  could make wings relatively richer. Measuring that needs quote data, and
  nothing in this result justifies buying it.

## Open questions

| Question | Status | Blocked by |
|---|---|---|
| F-003 QQQ cross-check | Pending | Nothing — runnable now |
| C2 measured friction | Moot unless a new hypothesis needs it | — |

## Closed

| Hypothesis | Verdict | Date | Basis |
|---|---|---|---|
| H3-P1 (options branch) | **DISCARD — permanently closed** | 2026-08-04 | Fails stage-2 friction hurdle by 26-68× even at the optimistic bound |
| H3-P1 (shares branch) | **DISCARD — no evidence** | 2026-08-04 | Negative point estimate on both SPY and QQQ |
| **H1-P1 (price-space VRP)** | **DISCARD — on arithmetic** | 2026-08-04 | Measured richness 5.9% [−2.1%, +13.8%] vs ~20% required; even the optimistic bound nets ≈$0 in the best-case scenario |
| **H2-P1 (conditional VRP)** | **DISCARD — with H1-P1** | 2026-08-04 | Same premium, different trigger; conditioning cannot rescue a premium that is not there |

**All three registered hypothesis families are now closed.** Total research
spend: $29 and one evening. The system did what it was built to do: produce
well-supported "no"s before any capital was risked.
