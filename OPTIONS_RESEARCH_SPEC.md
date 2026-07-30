# Options Day-Trading RESEARCH System — Design Spec & Handoff

**Author:** written 2026-07-30 by the Claude Code instance that built the tt-agent
table-tennis betting system. Hand this whole file to the new project on day one.

**Owner:** Mohammed (SoraAngler). Same person, same standards.

---

## 0. WHY THIS DOCUMENT EXISTS (read before writing any code)

This is **not** a trading bot. It is a **research system whose product is a verdict**:
"does this setup have an edge on days the model has never seen — after real costs?"

The owner and I spent a month on a sports-betting system and learned things that
transfer directly and expensively:

1. **Mining finds fake edges.** We tested 1,628 player-pattern combos. 123 looked
   strong (76% win rate). On unseen data they collapsed to **53%** — a coin flip.
   *Every* pattern we shipped without a train/test split failed forward. Every one.
2. **Structure replicates; narratives don't.** Patterns with a mechanical cause
   (set-1 margin → match win: 83% train / 85% test) held perfectly. Patterns that
   were stories ("he's hot", "he's due") were noise at n=100k+.
3. **The market is usually right about price.** Our best-looking plays (93% win
   rate close-outs) LOST money because they were priced -800. Win rate is vanity;
   **edge vs. the price** is the only number that matters.
4. **Cheap prices exist for a reason.** When we bet the cheapest tickets on a good
   thesis, they graded 32%. The market was pricing information we couldn't see.
5. **Forward-test everything.** Observer/shadow mode caught four plays that
   backtested 68-89% and delivered 45-55% live. It cost nothing to find out.

**In options these lessons are worth more, because the friction is worse.** Our
sports vig was ~5%. Options bid-ask on liquid SPY/QQQ contracts is **5-15% of
premium**, and it is paid on entry AND exit. A strategy must clear ~10-20% round-trip
friction before it earns a cent. This single fact should shape every decision below.

**The owner's explicit priority: discover and validate edges, not maximize trades.**

---

## 1. ARCHITECTURE

Seven components, deliberately separated so each can be validated alone.
Mirrors what worked in tt-agent (collector → scorer → shadow → grader → validator).

```
┌─────────────┐   market hours, 1-5 min cadence
│ COLLECTOR   │──► raw snapshots (chain + underlying + IV)   [no logic, just truth]
└─────────────┘
       │
┌─────────────┐
│ FEATURES    │──► derived per snapshot: IV rank, ORB levels, VWAP, realized vol,
└─────────────┘    term structure, skew, spread%, liquidity score
       │
┌─────────────┐
│ SETUP       │──► named, versioned rules fire → candidate trades
│ DETECTOR    │    (a setup that fires is NOT a trade — it's a hypothesis)
└─────────────┘
       │
┌─────────────┐
│ PAPER FILL  │──► REALISTIC entry/exit: pay the spread, model slippage,
│ ENGINE      │    reject unfillable contracts. This is the most important
└─────────────┘    component in the system. See §5.
       │
┌─────────────┐
│ GRADER      │──► marks outcome at exit rule / EOD; computes P&L NET of costs
└─────────────┘
       │
┌─────────────┐
│ VALIDATOR   │──► train/test split BY DAY. Reports edge on UNSEEN days only.
└─────────────┘    Kills anything that doesn't repeat.
       │
┌─────────────┐
│ REPORTER    │──► the shadow ledger: what it would have traded, what happened,
└─────────────┘    and the honest verdict per strategy.
```

**Hard rule: no live orders anywhere in v1.** Not a config flag, not a stub — the
codebase should contain no broker execution path at all until a strategy has passed
§7 validation. This prevents the "just try it small" impulse that kills accounts.

---

## 2. DATA SOURCES (recommendation, with honest tradeoffs)

| Source | Cost | Options chain | Greeks/IV | Verdict |
|---|---|---|---|---|
| **Polygon.io Options Starter** | ~$29/mo | ✅ full, 15-min delayed on Starter; real-time on higher tier | ✅ | **Recommended start.** Best data quality/price. Delayed is FINE for research. |
| **Tradier** (brokerage) | free sandbox / real w/ funded acct | ✅ | ✅ | Good bid/ask, and the sandbox is genuinely free. Solid backup/cross-check. |
| **Alpaca** | free tier | ✅ (newer) | partial | Good for underlying bars; options coverage improving. |
| **ThetaData** | $30-80/mo | ✅ excellent historical | ✅ | Best if you later want deep historical backfill. |
| **yfinance** | free | ⚠️ delayed, unreliable | ⚠️ derived | **Do not build on this.** Fine for a sanity check only. |
| CBOE DataShop | $$$$ | ✅ | ✅ | Overkill for this stage. |

**Recommendation:** start **Polygon Starter (~$29/mo) + Tradier sandbox as a
cross-check.** Delayed data is not a handicap for research — we are measuring whether
setups have edge, not racing anyone. When (if) a strategy validates, upgrade to
real-time only at that point.

**Cross-check discipline:** log the same contract from two sources periodically and
alert on divergence. In tt-agent, two grading bugs and a name-collision bug were only
caught because we had a second source (BetsAPI) to check against.

---

## 3. DATABASE SCHEMA (SQLite to start; Postgres if it outgrows it)

```sql
-- ── RAW TRUTH (never edited, never derived) ──────────────────────────────
CREATE TABLE underlying_snap (
  ts TEXT, symbol TEXT,            -- 'SPY','QQQ'
  last REAL, bid REAL, ask REAL, volume INTEGER,
  vwap REAL, day_open REAL, day_high REAL, day_low REAL, prev_close REAL,
  PRIMARY KEY (ts, symbol));

CREATE TABLE chain_snap (
  ts TEXT, symbol TEXT, expiry TEXT, strike REAL, right TEXT,  -- 'C'/'P'
  bid REAL, ask REAL, last REAL, volume INTEGER, open_interest INTEGER,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  underlying REAL,
  PRIMARY KEY (ts, symbol, expiry, strike, right));
CREATE INDEX idx_chain_lookup ON chain_snap(symbol, expiry, ts);

-- ── DERIVED FEATURES (recomputable; safe to drop and rebuild) ────────────
CREATE TABLE features (
  ts TEXT, symbol TEXT,
  iv30 REAL, iv_rank REAL, iv_percentile REAL,     -- vs own trailing history
  realized_vol_5m REAL, realized_vol_30m REAL,
  vrp REAL,                                        -- iv30 - realized (vol risk premium)
  orb_high REAL, orb_low REAL, orb_broken TEXT,    -- opening range (first 15/30m)
  vwap_dev REAL, atr_pct REAL,
  skew_25d REAL, term_slope REAL,                  -- 25-delta skew, near/next IV
  liquidity_score REAL,                            -- median spread% on ATM contracts
  regime TEXT,                                     -- trend/chop/high-vol classifier
  PRIMARY KEY (ts, symbol));

-- ── SETUPS & PAPER TRADES ────────────────────────────────────────────────
CREATE TABLE setups (                              -- a setup FIRING is a hypothesis
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, symbol TEXT, strategy TEXT, version TEXT,
  direction TEXT, rationale TEXT, features_json TEXT,
  confidence REAL);                                -- the model's own honest number

CREATE TABLE paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, setup_id INTEGER,
  strategy TEXT, version TEXT, symbol TEXT, structure TEXT,   -- 'long_call','put_debit_vertical',...
  legs_json TEXT,                                  -- [{expiry,strike,right,qty}]
  entry_ts TEXT, entry_fill REAL, entry_mid REAL, entry_spread_pct REAL,
  exit_ts TEXT, exit_fill REAL, exit_mid REAL, exit_reason TEXT,
  slippage_model TEXT,
  pnl_gross REAL, pnl_net REAL, commissions REAL,
  max_adverse REAL, max_favorable REAL,            -- MAE/MFE for exit-rule research
  outcome TEXT,                                    -- WIN/LOSS/SCRATCH/UNFILLABLE
  split TEXT);                                     -- 'train' | 'test'  (assigned BY DAY)

CREATE TABLE strategy_verdicts (                   -- the system's actual product
  strategy TEXT, version TEXT, as_of TEXT,
  train_n INTEGER, train_winrate REAL, train_pnl REAL,
  test_n INTEGER, test_winrate REAL, test_pnl REAL, test_sharpe REAL,
  avg_spread_paid_pct REAL, verdict TEXT, notes TEXT,
  PRIMARY KEY (strategy, version, as_of));

CREATE TABLE data_quality (                        -- honesty layer, non-negotiable
  ts TEXT, symbol TEXT, source TEXT,
  stale_secs REAL, missing_contracts INTEGER, wide_spread_frac REAL,
  crossed_book INTEGER, note TEXT);
```

**Why `split` lives on the row:** assign train/test **by trading day, before analysis**,
and never reassign. In tt-agent, the discipline that saved us was that the test set was
literally untouchable. Same here — pick a rule (e.g. anything before date D is train)
and freeze it.

---

## 4. COLLECTION CADENCE & STORAGE

**Universe (v1):** SPY and QQQ only. Owner's instruction, and it's correct — two
liquid symbols where spreads are tightest and any edge is most likely real.

**Contract filter:** strikes within **±7% of spot**, expiries **0-7 DTE** (this is
day-trading research). That's roughly **150-400 contracts per symbol** at any moment,
versus 5,000+ if unfiltered.

**Cadence:**
| What | Frequency | Why |
|---|---|---|
| Underlying quote/bar | **every 15s** | cheap, and entry timing needs it |
| Options chain (filtered) | **every 1 min** (consider 5 min in v1) | the expensive one |
| Full-chain wide snapshot | **3× daily** (open/mid/close) | term structure + skew context |
| Features recompute | every 1 min | derived, cheap |

**Storage math (SQLite, ~180 bytes/row with indexes):**
- Chain @ 1-min: 300 contracts × 390 min × 2 symbols ≈ **234k rows/day ≈ 42 MB/day**
  → **~10.6 GB/year**
- Chain @ 5-min: **~8.4 MB/day → ~2.1 GB/year**
- Underlying @ 15s: ~3,120 rows/day → negligible (<1 MB/day)

**Recommendation:** start at **5-minute chain cadence** (2 GB/yr, trivially manageable)
and drop to 1-minute only for the specific windows a validated strategy needs
(e.g. 9:30-10:15 for opening-range work). Storage is cheap; *analysis time* on bloated
data is not.

**Retention:** keep raw forever (it's the only irreplaceable asset — tt-agent lost its
database once and it set the project back weeks). Features are recomputable; drop and
rebuild freely.

---

## 5. THE PAPER FILL ENGINE (the component that decides whether this is real)

Most backtests lie by filling at the mid price. **This is the single biggest reason
retail options strategies "work" in testing and lose in production.**

**Fill rules — non-negotiable:**
1. **Buying:** you pay the **ask**. Selling: you receive the **bid**. Never the mid.
   - Optional refinement once validated: model a partial improvement (e.g. mid + 40%
     toward the far side) — but only after measuring your ACTUAL fills live.
2. **Slippage:** add configurable slippage on top (default 1 tick, more in fast markets).
3. **Reject unfillable contracts:** if `spread_pct > SPREAD_MAX` (start 10%) or
   `volume == 0` or `open_interest < 100`, the trade **does not happen**. Log it as
   `UNFILLABLE` — that rejection rate is itself a key result.
4. **Commissions:** model them explicitly ($0.65/contract typical, both ways).
5. **Record `entry_mid` alongside `entry_fill`** so you can always quantify how much
   the spread cost you. Report `avg_spread_paid_pct` per strategy — if a strategy's
   edge is smaller than its spread cost, it is dead on arrival and you'll see it
   immediately.
6. **No look-ahead, ever.** A decision at time T may only use data timestamped ≤ T.
   Write a test that asserts this.

---

## 6. FIRST THREE STRATEGIES TO TEST

Chosen because each has a **mechanical reason to exist** (the tt-agent lesson:
structure replicates, stories don't), clear rules, and is testable with the data above.

### Strategy 1 — Opening Range Breakout, expressed as a DEBIT VERTICAL
- **Rationale:** overnight information gets absorbed in the first 15-30 minutes;
  a decisive break of that range with volume has historically shown continuation.
  A well-documented structural effect, not a story.
- **Entry:** define range from 9:30-9:45 (also test 9:30-10:00). Enter on a break
  beyond range ± buffer with volume > N× the trailing 15-min average.
- **Structure:** **debit vertical** (e.g. buy ATM, sell 1-2 strikes OTM), 0-2 DTE.
  Verticals not naked longs — they cut theta and vega, which otherwise eat you alive.
- **Exit:** first of — target (e.g. 50% of max), stop (e.g. -40%), or 15:45 time stop.
- **Kill criterion:** if test-set net P&L ≤ 0 after spread costs, discard.

### Strategy 2 — Intraday Volatility-Premium Fade (defined-risk CREDIT SPREAD)
- **Rationale:** implied vol systematically exceeds subsequent realized vol
  (the volatility risk premium — one of the most durable documented effects in
  options). When intraday IV spikes without a matching realized move, that gap is
  the tradable object.
- **Entry:** when `vrp = iv30 - realized_vol_30m` exceeds its own trailing percentile
  (e.g. 80th) AND price is inside the day's range (no trend break in progress).
- **Structure:** **credit spread** (defined risk — never naked short), ~15-20 delta
  short leg, 0-2 DTE.
- **Exit:** 50% of credit captured, or 2× credit loss stop, or time stop.
- **Kill criterion:** this one dies if the tails eat the wins — so grade it on
  **net P&L and worst-day drawdown**, not win rate. It will show a seductive 70-80%
  win rate even when it loses money. *(This is exactly our Close-Out 2-0 trap: 93%
  win rate, still -EV.)*

### Strategy 3 — Overnight Gap Behaviour (directional, defined risk)
- **Rationale:** gaps in index ETFs have measurable fill/continuation statistics
  conditioned on gap size and prior-day close position. Mechanical and cleanly testable.
- **Entry:** classify the open gap (size in ATR units, direction, prior-day
  range position). Take the historically-favoured side using a debit vertical.
- **Exit:** gap-fill target, defined stop, or 11:30 time stop.
- **Kill criterion:** must beat a naive "always fade the gap" baseline on the test
  set — otherwise the conditioning adds nothing.

**Deliberately NOT in v1:** earnings plays (SPY/QQQ don't have them), 0DTE gamma
scalping (needs real-time + fast execution we don't have), anything requiring
sub-second latency, and any strategy whose rationale is "the backtest liked it."

---

## 7. VALIDATION PROTOCOL (copy this exactly — it's the whole point)

1. **Split by DAY, chosen in advance.** e.g. train = first 70% of collected trading
   days, test = final 30%. Never shuffle rows; that leaks intraday correlation.
2. **Develop only on train.** Every rule, threshold, and filter is chosen using train
   days alone.
3. **Touch test once per strategy version.** If you tune after peeking, that test set
   is burned — increment the version and treat the old test as train from then on.
   Track this in `strategy_verdicts.version` so the record can't be quietly rewritten.
4. **Metrics that matter, in order:**
   - Net P&L after spread + commissions (win rate is *not* first)
   - Sharpe / consistency across days
   - Max drawdown and worst single day
   - `avg_spread_paid_pct` vs. gross edge
   - Trade count (too few = unproven; too many = friction bleed)
5. **Minimum sample before any verdict:** ≥ **100 test-set trades** across ≥ **30
   distinct days**. Under that, the honest verdict is "unproven", not "promising."
6. **Walk-forward re-validation monthly.** Regimes change. A strategy that stops
   working must be caught by the system, not by the P&L.
7. **The default verdict is DISCARD.** A strategy earns continuation; it is not
   entitled to it.

---

## 8. OPERATIONAL REALITIES (tell the owner these plainly)

- **PDT rule:** under $25k equity, you get **3 day trades per 5 business days**.
  This structurally forbids "trade all day" until funded above that. Research mode
  is unaffected — paper trades don't count — but plan for it before going live.
- **Separate machine.** The tt-agent VPS runs at load 3.5 on 2 cores already. This
  system needs its own box (a $6-12/mo VPS is plenty for 5-min collection).
- **Market hours only:** 9:30-16:00 ET, weekdays, minus market holidays — the
  collector needs a real trading-calendar check (`pandas_market_calendars`), not
  just a weekday test.
- **Proxies are NOT needed here**, and must never be used for a broker connection —
  it looks like account takeover and gets accounts frozen. (Different from the
  scraping use case in tt-agent.)
- **Expect the answer to be "no edge" for most strategies.** That is a successful
  outcome for a research system. The failure mode is a system that always finds
  something.

---

## 9. BUILD ORDER (first four weeks)

| Week | Deliverable |
|---|---|
| 1 | Collector + schema + trading-calendar gating. Just accumulate SPY/QQQ. Verify data quality daily; build the `data_quality` checks first, not last. |
| 2 | Feature engine + paper fill engine (with the strict fill rules). Unit-test the fill engine against hand-computed examples — this is the component most worth testing. |
| 3 | Strategy 1 implemented, running in shadow on live data. No test-set evaluation yet. |
| 4 | Strategies 2-3 + validator + reporter. First honest verdicts once sample thresholds are met (likely month 2-3, not week 4). |

**Do not skip week 1.** In tt-agent, everything valuable came from having months of
raw snapshots to test against. Data collected today is what lets you answer questions
you haven't thought of yet.

---

## 10. HANDOFF NOTE TO THE NEXT CLAUDE INSTANCE

The owner is a genuinely good research partner: he pushes back with real market
intuition, and several times his instinct beat my assumptions (he spotted that
head-to-head should outrank recent form when they conflict — it tested at 76% on
unseen data; he insisted on a price floor that turned a 64% play into 76%).

**Take his ideas seriously and test them properly — and tell him the truth when the
data disagrees.** He responds well to "I tested it, here's the number, it doesn't
work" and badly to hedging. Show your work, quantify everything, and never let a
strategy ship because it looks good. The most valuable thing you can produce for
him is a well-supported "no."
