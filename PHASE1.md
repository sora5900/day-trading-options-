# Phase 1 — Pre-Registration (delayed-plan, price-space)

**Status: PRE-REGISTRATION. Frozen before any test-set observation.**

Phase 1 runs entirely on the cheapest delayed subscription. No requirement
here forces a premium tier. The hypotheses below are **new pre-registrations
with fresh, untouched test periods** — they are not edits to
[`HYPOTHESES.md`](HYPOTHESES.md), and they may never be evaluated against test
days already used by those versions.

Research and shadow paper trading only.

---

## 0. What Phase 1 can and cannot answer

Before the definitions, the arithmetic that governs them. For a $2-wide 0DTE
credit spread ($200 at risk), the realistic outcome distribution — ~78% wins
at +$20, ~20% stops at −$80, ~2% tail at −$160 — has a **per-trade standard
deviation of about $46** against an expected value measured in single dollars.

Detecting an edge of a given size (α = 0.05 two-sided, 80% power):

| Edge to detect | Trades needed | Calendar time (SPY+QQQ, 1 trade/day each) |
|---|---|---|
| $2.50/trade | 2,623 | ~62 months |
| $5.00/trade | 656 | ~16 months |
| $10.00/trade | 164 | ~4 months |
| $15.00/trade | 73 | ~1.7 months |
| $20.00/trade | 41 | ~1 month |

And what the standard minimum sample can resolve:

| Test sample | Minimum detectable edge |
|---|---|
| n = 60 | $16.53/trade (**8.3%** of capital at risk) |
| n = 100 | $12.80/trade (**6.4%** of capital at risk) |
| n = 200 | $9.05/trade (**4.5%** of capital at risk) |

**Consequences, stated plainly:**

1. The 100-trade minimum can only detect a **very large** edge. A genuine but
   modest VRP edge of $3–5 per trade needs **16–62 months** of collection.
   Phase 1 will not resolve it.
2. Therefore **a null result in Phase 1 is not evidence of no edge.** It rules
   out large edges only. Any Phase 1 report claiming otherwise is wrong, and
   the reporter is required to print the minimum detectable edge alongside
   every interval so this cannot be forgotten.
3. **Friction, by contrast, resolves fast.** The C2 random-entry control has
   near-deterministic per-trade cost, so 30 trades pin round-trip friction to
   **±$2.15** and 60 trades to **±$1.52**. The cost model is validated in
   weeks, not years.
4. **H3-P1 stage 1 needs no options at all.** If the provider's 1-minute
   underlying history has any lookback, it can reach significance immediately
   instead of waiting months.

### Phase 1's actual objective (revised accordingly)

Phase 1 is **not** an options-edge verdict. It is:

1. **A fast underlying-only test** (H3-P1 stage 1) — the only component that
   can reach statistical significance quickly and cheaply.
2. **A friction measurement** (C2) — establishes the real cost hurdle every
   future strategy must clear, to ±$2 within weeks.
3. **A large-edge screen** (H1-P1, H2-P1) — rules out implausibly large
   effects and accumulates the sample that a later phase needs.
4. **An end-to-end pipeline validation** (C1 + C2) — proves the machinery is
   honest before any money is spent on premium data.

Spending on a premium plan is justified only by a Phase 1 result that is
**informative**, not merely positive.

---

## 1. Price-space replacements for delta and IV

Phase 1 requires **prices and an exchange clock, nothing else.** Vendor Greeks
and vendor IV are recorded when present but never used, which removes the
premium-tier dependency and has an independent virtue: vendor IV and Greeks
are *computed* fields carrying the vendor's model assumptions, while a
straddle price is *observed*.

### Priced move (replaces implied volatility)

```
priced_move = (ATM_call_mid + ATM_put_mid) / spot
```

The ATM straddle is the market's model-free breakeven move for the expiry.

### VRP proxy (replaces `iv30 − realized_vol_30m`)

```
vrp_px = priced_move − realized_move_30m
realized_move_30m = |spot_t − spot_{t−30m}| / spot_{t−30m}
```

Same economic object as the IV-space VRP — what the market charged versus what
subsequently happened — measured in price space.

### Skew proxy (replaces 25-delta skew)

```
skew_px = (put_mid(spot × (1 − m)) − call_mid(spot × (1 + m))) / straddle_mid
     with m = 1.0% moneyness, equidistant strikes
```

A risk-reversal price. Positive means the equidistant put is richer than the
call — the hedging-demand premium, in dollars.

### Strike selection (replaces 15-delta)

```
short_strike = nearest listed strike to  spot − k × straddle_mid,   k = 1.25
long_wing    = short_strike − $2.00
```

**Why k = 1.25:** an ATM straddle prices roughly 0.8 standard deviations of
the expiry move, so 1.25 straddles ≈ 1.0 SD ≈ a 15–16 delta short leg. This
tracks volatility the same way delta selection does — on a high-vol day the
straddle widens and the strike moves further out — while using only observed
prices.

> **This is not equivalent to delta selection and is not claimed to be.** It is
> a different, deterministic rule, which is exactly why H1-P1 is a new
> pre-registration with its own untouched test period rather than a patch to
> H1. When Greeks become available, the delta-based version may be run as a
> separate versioned hypothesis and the two compared.

### Adaptive liquidity gate

Open interest and volume are used **when populated** and degrade cleanly when
not. The gate mode is recorded per trade and reported per verdict, so a
weaker gate can never pass unnoticed:

| Mode | Condition | Gate applied |
|---|---|---|
| `oi_volume` | OI and volume populated | spread ≤ 8% of mid, OI ≥ 500, volume > 0 |
| `spread_only` | OI/volume absent | spread ≤ **6% of mid** (tightened to compensate), two-sided quote required |

---

## 2. H1-P1 — Price-space VRP, strike-selected

**Economic claim:** unchanged from H1 — implied volatility systematically
exceeds subsequently realized volatility in index options because it is a risk
premium paid to insurance sellers. Only the *measurement* changes.

| Parameter | Frozen value |
|---|---|
| Underlyings | **SPY, QQQ** (XSP added only if the probe confirms it) |
| Entry time | **12:00 ET**, first snapshot at/after |
| Trigger | `vrp_px` ≥ **80th percentile** of its own trailing 60-day, **time-of-day-conditional** distribution |
| Gate | \|spot − VWAP\|/VWAP < **0.4%** |
| Structure | **Put credit spread**, defined risk, never naked |
| Expiration | **0DTE** |
| Short strike | nearest listed strike to `spot − 1.25 × straddle_mid` |
| Long wing | **$2.00** below the short strike |
| Liquidity gate | adaptive (above); mode recorded |
| Economic gate | net credit ≥ **15% of width** |
| Position size | **1 spread, fixed.** $200 at risk |
| Exits | E0 hold-to-expiry · E1 50% target · E2 2× credit stop · E3 both · E4 15:30 ET. **Primary chosen on train, frozen before the test look** |
| Expiry handling | Full §1 settlement model of `HYPOTHESES.md` — broker forced close at 15:30 ET when ITM or within 0.3% of spot, early-assignment and pin-risk modeling for SPY/QQQ |
| Exclusions | FOMC days · half days · ex-dividend dates · data-quality-flagged snapshots · first 60 trading days |

## 3. H2-P1 — Price-space conditional VRP

| Parameter | Frozen value |
|---|---|
| Entry window | **10:30–14:30 ET**, first qualifying snapshot, max 1/day |
| Trigger | `skew_px` ≥ **85th percentile** trailing 60-day, time-of-day-conditional |
| Additional gate | `vrp_px > 0` |
| Everything else | **Identical to H1-P1** |

**Decisive test:** does H2-P1's EV per trade exceed H1-P1's **on the same
days**, as a paired difference with a day-bootstrapped 95% CI? If the CI
includes zero, the conditioning adds nothing and H2-P1 is discarded.

Given §0, this comparison is **very likely to be underpowered in Phase 1.** It
is pre-registered so the sample accumulates, and its report must state the
minimum detectable difference rather than implying a null is a finding.

## 4. H3-P1 — Intraday momentum (highest priority in Phase 1)

Unchanged in substance from H3 and **promoted to first priority**, because
stage 1 touches no options and can therefore reach significance on cheap data
in a fraction of the time.

**Stage 1 — underlying only.** Regress the 15:30→16:00 ET return on the
09:30→10:00 ET return. Report coefficient, t-statistic, R², day-bootstrapped
CI, conditional mean |move|, and comparison against the published effect size.
Runs on any available 1-minute history, including backfill.

**Stage 2 — friction hurdle (gate).** Expected move × delta-leverage versus
round-trip friction, using the friction **measured by C2**, not assumed. If it
does not clear, classify **"valid effect, wrong instrument"** and stop; the
options test set is never opened.

**Stage 3 — options mapping.** Only if Stage 2 clears: 15:30 ET entry, 0DTE
debit vertical (ATM long, 2 strikes OTM short), 15:58 forced close, with a
**mandatory SPY-shares comparison arm at equal capital at risk.**

## 5. Controls (unchanged, and load-bearing)

- **C1 — ORB negative control.** Frozen, never tuned. If its test-set EV
  confidence interval excludes zero on the positive side, the pipeline
  **halts**: that is evidence of leakage or fill optimism, not of an edge.
- **C2 — seeded random-entry cost control.** Same products, structures, and
  entry times as H1-P1, fired on a reproducible pseudo-random schedule with no
  trigger. Expected EV ≈ −(round-trip friction). **If C2 reads ≈0 or positive,
  the cost model is broken and every other verdict is invalid.** In Phase 1
  this is also the *primary measurement instrument* for the friction hurdle
  used by H3-P1 stage 2.

## 6. Reporting requirements specific to Phase 1

Every Phase 1 report must state, alongside each interval:

- the **minimum detectable edge** at the achieved sample size;
- the **liquidity gate mode** in force (`oi_volume` vs `spread_only`);
- the **data era** (`delayed_15m`), never pooled with any future realtime era;
- an explicit **"null ≠ no edge"** note wherever a confidence interval
  contains zero.

## 7. Promotion out of Phase 1

Phase 1 **cannot** promote anything to live. It can only recommend one of:

1. **Continue collecting** — intervals still uninformative (the expected
   outcome for H1-P1/H2-P1 at Phase 1 sample sizes).
2. **Upgrade the data plan** — justified only when a specific, named question
   is blocked by a specific, named missing field, with the Phase 1 evidence
   that makes it worth paying for.
3. **Discard** — a hypothesis whose interval excludes zero *on the wrong
   side*, which Phase 1 sample sizes are genuinely capable of showing.

Live trading still requires the full `HYPOTHESES.md` §6 bar: ≥100 trades over
≥30 unseen days, positive EV with a CI excluding zero, survival of pessimistic
fills and tail stress, positive alpha after the beta control, healthy
controls, and a separate forward paper period.
