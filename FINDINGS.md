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

## Open questions

| Question | Status | Blocked by |
|---|---|---|
| H1-P1 price-space VRP | Not started | Options entitlement (~$29/mo) |
| H2-P1 conditional VRP | Not started | Options entitlement |
| C2 measured friction | Not started | Options entitlement |
| H3-P1 QQQ cross-check | **Done — consistent null** | — |

## Closed

| Hypothesis | Verdict | Date | Basis |
|---|---|---|---|
| H3-P1 (options branch) | **DISCARD — permanently closed** | 2026-08-04 | Fails stage-2 friction hurdle by 26-68× even at the optimistic bound |
| H3-P1 (shares branch) | **DISCARD — no evidence** | 2026-08-04 | Negative point estimate on both SPY and QQQ |
