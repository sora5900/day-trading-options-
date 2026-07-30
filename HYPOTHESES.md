# Formal Hypothesis Register

**Status: PRE-REGISTRATION. Frozen before any test-set observation.**

Every parameter below is fixed prior to opening the unseen test period. Any
change to a trigger, threshold, structure, or exit policy creates a **new
version with a fresh untouched test period**. The old test days become train
days for the new version and can never be reused as test.

Research and shadow paper trading only. No broker execution path exists in
this codebase. Promotion criteria are in §6.

---

## 0. Conventions that apply to every hypothesis

| Item | Definition |
|---|---|
| Authoritative event time | The **exchange quote timestamp**, never fetch time |
| Decision rule | Data timestamped ≤ decision time only; asserted by test |
| Position size | **Fixed: 1 spread per signal.** No scaling, ever, during validation |
| Max trades | 1 per hypothesis, per product, per day |
| Products | Reported **separately, never pooled** — settlement mechanics differ |
| Data eras | Delayed-era and realtime-era rows **never pooled** in one sample |
| Percentile triggers | Time-of-day-conditional (12:00 IV ranked against prior 12:00 IVs, not against all-day IVs) |
| Trailing history | ≥ 60 trading days before any trigger may fire |
| Cost sensitivity | Every verdict reported at optimistic / base / pessimistic fills |

### Fill sensitivity ladder

| Scenario | Entry | Exit | Slippage |
|---|---|---|---|
| Optimistic | mid + 25% toward far side | mid + 25% toward far side | 0 ticks |
| **Base (primary)** | pay ask / receive bid | pay ask / receive bid | 1 tick |
| Pessimistic | pay ask / receive bid | same, spreads × 1.5 | 3 ticks |

A hypothesis that survives only under optimistic fills is **DISCARD**.

### Required metrics (all hypotheses, all products, all fill scenarios)

Expected value per trade (headline) · net P&L after all costs · profit factor ·
win rate *(reported, never described as the edge)* · average win vs average
loss · maximum drawdown · worst day · largest single loss · expected shortfall
(CVaR 5%) · maximum consecutive-loss streak · capital required (peak margin) ·
return on capital-at-risk · Sharpe (daily, annualized) · trade frequency ·
**beta to SPY daily return and alpha net of a beta-matched underlying
position** · 95% confidence intervals **bootstrapped by trading day, not by
trade**.

> **Beta control rationale:** a short put spread is structurally long equity
> beta. If P&L disappears after subtracting a beta-matched SPY position, the
> "edge" is just market exposure you could buy cheaper. This check can kill
> H1 and H2 outright and must run before any verdict.

### Slice policy

Regime (trend/range, high/low IV), time-of-day, delta bucket, DTE, and product
slices are computed on **train only**, for hypothesis generation. A slice that
looks strong on train becomes a **new versioned hypothesis with its own fresh
test period** — it never becomes a verdict on its own. Slicing the test set
across ~5 dimensions runs dozens of simultaneous tests; some will look
significant by chance.

---

## 1. Settlement mechanics — modeled separately by product

The claim "held to expiration costs only one spread" is **true for cash-settled
European products and materially false for physically-settled American ones.**
This table is the model.

| Mechanic | SPY / QQQ | XSP (and SPX) |
|---|---|---|
| Exercise style | American — assignable any time | European — no early exercise |
| Settlement | Physical: 100 shares delivered | Cash |
| Early assignment on short leg | **Real risk.** Modeled | Impossible |
| Ex-dividend early-exercise risk | Real for short calls (quarterly) | None |
| Pin risk at expiry | **Real** — ambiguous assignment, unhedged overnight share position | None |
| OCC exercise-by-exception | Auto-exercise at **$0.01 ITM** | Cash-settled at $0.01 ITM |
| Settlement timing | PM (close) | XSP dailies/weeklies PM; **standard monthlies AM-settled (SQ/SET)** — different risk, excluded from base |
| Broker forced close | **Modeled** — see below | Rare (no assignment exposure) |
| Buying power held | width × 100, entry → expiry/close | Same |
| Tax treatment | Ordinary short-term | Section 1256 60/40 *(not modeled in P&L; noted)* |
| Commission model | $0.65/contract/leg, both ways | $0.65 broker + index-option fee, **config default $0.95 — verify with broker** |

### Broker-intervention model (SPY/QQQ only)

Applied in the simulator, not as an optional flag:

1. **Forced close at 15:30 ET on expiry day** if the short strike is ITM **or**
   within **0.3% of spot**. Position is closed at market — full spread paid.
   This is the realistic retail experience and it removes most of the
   "free expiry" benefit.
2. **Early assignment**: at any snapshot where the short leg is ITM and its
   extrinsic value < $0.05, the position is treated as assigned → modeled as a
   forced close at that snapshot's quotes.
3. **Pin risk**: if |settlement − short strike| < 0.1%, the free expiry is
   **not** granted; modeled as a forced close at 15:45 ET.
4. **Free expiry granted only** when the short strike is OTM by > 0.3% of spot
   at 15:30 ET. Only then is the exit spread genuinely avoided.

### Hard invariant (simulator + any future implementation)

```
realized_loss_per_spread ≤ (width × 100) − credit_received + commissions
```

Asserted on every closed trade. A violation halts the pipeline as a simulator
bug — it is not reported as a result.

---

## 2. H1 — Variance Risk Premium, defined-risk short premium

**Economic claim:** implied vol systematically exceeds subsequent realized vol
in index options because it is a *risk premium* — compensation for bearing
crash risk transferred from institutional hedgers. It persists because real
risk is transferred, not because of a pricing error. Timing conditioning: the
intraday realized-vol U-shape means the midday trough is where implied most
overstates what is subsequently realized.

**Arms (reported separately, never pooled):**
- **A: SPY** — American, physically settled
- **B: XSP** — European, cash-settled, size-matched to SPY
- **C: QQQ** — American; secondary

> Arm B is the point of the design: XSP has the settlement advantage but
> structurally **wider percentage spreads** than SPY. Whether the clean expiry
> beats the wider spread is an empirical question, not an assumption.

| Parameter | Frozen value |
|---|---|
| Entry time | **12:00 ET**, first snapshot at/after |
| Trigger | `vrp = atm_iv(traded expiry) − realized_vol_30m` at **≥ 80th percentile** of its own trailing 60-day, **time-of-day-conditional** distribution |
| Additional gate | \|price − VWAP\| / VWAP < **0.4%** (not extended / no trend break in progress) |
| Contract type | **Put credit spread** — defined risk, never naked |
| Expiration | **0DTE** (same-day) |
| Short strike | **15-delta**, accepted band \|Δ\| ∈ [0.12, 0.20], closest to 0.15 |
| Long wing | **$2.00 wide** (both products, for comparability) |
| Max risk | $200 − credit, per spread |
| Liquidity gate | Each leg: bid-ask ≤ **8% of mid**, OI ≥ **500**, volume > 0 |
| Economic gate | Net credit ≥ **15% of width** ($0.30 on a $2 spread) |
| Position size | **1 spread**, fixed |
| Capital required | $200 peak margin per product (no overlap — 0DTE, 1/day) |

**Exit policies — compared, not assumed.** The same entry population is graded
under all five:

| ID | Policy |
|---|---|
| E0 | Hold to expiration (full settlement model of §1) |
| E1 | Close at 50% of credit captured |
| E2 | Close at 2× credit loss (stop) |
| E3 | E1 + E2 combined |
| E4 | Time exit 15:30 ET unconditionally |

**Multiple-comparison handling:** the primary exit policy is selected on
**train** and frozen before the test look. All five are reported on test, but
only the pre-registered primary produces the verdict; the other four are
labeled exploratory.

**Exclusions:** FOMC announcement days · half/early-close days · ex-dividend
dates (both arms, for comparability) · any day where data-quality flags fire at
the decision snapshot (stale > 120s, missing chain, crossed book) · the first
60 trading days of collection (insufficient trailing distribution).

**Stress tests:** gap replay (worst historical underlying moves against open
positions) · IV × 1.5 shock at exit · post-15:00 spread widening × 2 ·
forced liquidation pulled forward to 15:00 · worst-quote-of-minute exits.

---

## 3. H2 — Conditional VRP (skew-spike trigger)

**Economic claim:** the same risk premium as H1, timed better. Put skew
reflects hedging demand; demand spikes at local fear extremes, which is when
insurance is most overbid.

**This is not an independent edge.** It is H1 with a different trigger. The
question it must answer is narrow and specific.

| Parameter | Frozen value |
|---|---|
| Entry window | **10:30–14:30 ET**, first qualifying snapshot, max 1/day |
| Trigger | `skew_25d = IV(25Δ put) − IV(25Δ call)` at **≥ 85th percentile** of trailing 60-day, time-of-day-conditional distribution |
| Additional gate | `vrp > 0` |
| Structure / strikes / width / gates / size | **Identical to H1** (put credit spread, 0DTE, 15Δ, $2 wide) |
| Exit | Same five-policy comparison; primary chosen on train |
| Exclusions | Identical to H1 |

**Decisive test:** does H2's EV per trade exceed H1's EV per trade **on the
same days**? Reported as a paired difference with a day-bootstrapped 95% CI.
If the CI includes zero, the conditioning adds nothing and H2 is discarded —
fewer trades of the same thing is not an improvement.

Both H1 and H2 are pre-registered before the test look, so they may share a
test period. Neither may be re-tuned using the other's test results.

---

## 4. H3 — Intraday momentum (staged, gated)

**Economic claim:** the first half-hour return predicts the last half-hour
return in index ETFs (Gao, Han, Li & Zhou, *JFE* 2018) — informed-trader
activity clusters at open and close. Published, out-of-sample validated,
replicated internationally.

**Explicitly staged. Options are not touched until the underlying reproduces.**

### Stage 1 — reproduce on the underlying (no options, no cost)
- `r1` = return 09:30 → 10:00 ET; `r_last` = return 15:30 → 16:00 ET
- Regress `r_last ~ r1`; report coefficient, t-statistic, R², day-bootstrapped CI
- Compare measured effect size against the published effect size
- Report conditional mean |r_last| — the economic, not just statistical, size

### Stage 2 — friction hurdle (a gate, not a formality)
Compute explicitly: expected underlying move × delta-leverage of the candidate
contract, versus round-trip options friction.

> Prior estimate: the effect predicts moves of **tens of basis points**. At ~20×
> delta-leverage that is ~4% on the contract, against 10–26% friction.
> **Leverage scales the move and the premium together — it does not rescue the
> trade.** If the hurdle is not cleared, classify **"valid effect, wrong
> instrument"** and **stop. Do not open the options test set.**

### Stage 3 — options mapping (only if Stage 2 clears)

| Parameter | Frozen value |
|---|---|
| Entry | **15:30 ET**, direction = sign(r1) |
| Structure | Debit vertical, ATM long / 2-strikes-OTM short |
| Expiration | 0DTE |
| Exit | **15:58 ET forced close** (ATM 0DTE cannot be held to settlement) |
| Liquidity gate | Leg spread ≤ 8% of mid, OI ≥ 500, volume > 0 |
| Size | 1 spread, fixed |
| **Mandatory comparison arm** | **Same signal traded in SPY shares, same capital at risk** |

If the shares arm beats the options arm, that is the finding and it is reported
as the headline result.

---

## 5. Negative controls — the pipeline's own instrumentation

### C1 — ORB (retained strictly as a negative control)
Definition frozen at current `orb_vertical v1`. **Never tuned.** Its rationale
("overnight information absorbed in the first 15 minutes") is chart-pattern
reasoning, not a mechanism, and it is expected to read approximately
−friction.

**Circuit breaker:** if ORB's test-set EV-per-trade confidence interval
excludes zero *on the positive side*, the pipeline **halts** and emits a
diagnostic report. A strong result here is evidence of look-ahead leakage, fill
optimism, or a data bug — not of an edge.

### C2 — Random-entry control
Same products, structures, and entry times as H1, fired on a **seeded,
reproducible pseudo-random schedule** with no trigger.

**Expected result: EV per trade ≈ −(round-trip friction).** If C2 reads ≈ 0 or
positive, the cost model is understating friction and **every other verdict in
the system is invalid.** C2 is the direct validation of the fill engine.

---

## 6. Test-period policy and promotion criteria

1. Split by **trading day**, cutoff frozen in `split_config` before any test
   observation. Changing it raises.
2. Train = first 70% of collected days; test = final 30%.
3. **One test look per version.** Any parameter change → new version, fresh
   untouched test period, old test becomes train.
4. Minimum before any verdict other than UNPROVEN: **≥ 100 test trades across
   ≥ 30 distinct test days**, per product arm.
5. Default verdict is **DISCARD**.

**Promotion to live requires all of:**
- ≥ 100 trades across ≥ 30 genuinely unseen days, per product
- Positive EV per trade with a day-bootstrapped 95% CI **excluding zero**
- Survives the **pessimistic** fill scenario, not just base
- Survives all tail-risk stress tests with max loss within defined risk
- Positive alpha after the beta-matched underlying control
- C1 and C2 both reading as expected (controls healthy)
- A **separate forward paper-trading period** completed after the test look

Until every one of these is met, the verdict is UNPROVEN or DISCARD, and no
broker execution path is written.
