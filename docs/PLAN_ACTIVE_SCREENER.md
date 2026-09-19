# Plan: from a daily snapshot to an active screener

**Status:** draft for decision · **Written:** 2026-09-19 · **Owner:** repo owner
**Scope:** everything needed so the screen visibly responds to the market within the hour, without giving up the rules that make its output trustworthy.

Already shipped on branch `fix/supply-metrics-and-mappings` (D-071, D-072, D-073), so they're not repeated as work items below:
- verified DefiLlama map: 157 rows, where 4 of 28 worked before;
- net issuance in the supply block;
- sectors for the unclassified top-ranked coins.

---

## 1. Why it looks dead today

Measured on the 2026-09-19 screen (158 ranked survivors):

| Block (weight) | Coins measured | Cause |
|---|---|---|
| Attention (15) | 0 | LunarCrush answers `402 Payment Required` every day |
| Fundamental (35) | 9 | 13 of 28 map rows matched nothing (**fixed, D-071**) |
| Events (10) | 46 | 43 of those 46 share one value |
| Sector (15) | 89 | top-ranked coins unmapped (**fixed, D-073**) |
| Supply (25) | 158 | really only float ratio; 36 coins tie at 84.49 (**fixed, D-072**) |
| Drawdown (10) | 158 | distance below the all-time high |

Two structural problems stay even after the fixes:

1. **Every input is slow.** Float, issuance, drawdown, fundamentals and sector are 7-to-90-day quantities, and the screen runs once a day. Nothing in the score *can* move much overnight. The ranking was 0.945 rank-correlated between 09-16 and 09-19.
2. **Missing data is rewarded.** A block with no data drops out and its weight goes to the rest (`layer2_score.py:365-372`). So a coin measured on two strong blocks outranks one measured on five: 10 of today's top 15 were scored on supply and drawdown alone.

The plan fixes (2) first, because every block added on top of it inherits the bias. Then it adds a second, fast clock for (1).

---

## 2. Target shape: two clocks, measured separately

```
              daily 03:10                               hourly :03
┌──────────────────────────────────┐   ┌────────────────────────────────────────┐
│ GEM SCORE   horizon 7–90 days    │   │ PULSE     horizon 4–72 hours           │
│ L1 kill → L2 blocks → L3 advice  │   │ L1 survivors only (never overrides L1) │
│ + new MOMENTUM/FLOW block (7d)   │   │ flow · volume · OI · 1H/4H structure    │
│ journal: 1d/7d/30d/90d           │   │ pulse journal: 4h/24h/72h              │
└──────────────┬───────────────────┘   └───────────────────┬────────────────────┘
               └────────── "ALIGNED" = top-25 Gem AND Pulse ≥ 70 ──┘ → alert
```

**Why two clocks instead of one bigger score.** A 4-hour signal inside a 90-day score does two bad things:
- It churns the ranking every hour for reasons unrelated to what the ranking claims.
- The journal can no longer attribute a return to either horizon, which destroys the thing that makes this project different from a signal channel.

With separate clocks, each signal is scored, published and journalled at the horizon where it has evidence.

The Gem score still gets faster: a 7-day momentum and flow block (section 5.3) adds a medium-horizon input that genuinely changes day to day.

---

## 3. The decision this forces: D-002

D-002 says *"derivatives are a risk check, never a buy trigger"* and the README calls it a non-negotiable. Putting OI into a score changes that rule, so it needs your explicit decision, recorded as a new D-number. Here is what the evidence supports:

| Signal | Evidence it predicts direction | Evidence it predicts fragility |
|---|---|---|
| **Order flow** (buyer- minus seller-initiated volume) | **Yes.** Cross-section of 82 coins, 2018–22: predictive, with a *permanent* effect (Anastasopoulos et al., *Order Flow and Cryptocurrency Returns*, J. Financial Markets 2026) | — |
| **Price + volume trend** across horizons | **Yes.** CTREND over 3,000+ coins survives costs and holds in large, liquid coins (Fieberg et al., JFQA 2025). Momentum is one of three crypto factors (Liu, Tsyvinski & Wu, JF 2022) | — |
| **Intraday returns** | Both momentum *and* reversal, depending on jumps and liquidity (Wen et al., 2022) | — |
| **Open interest change, alone** | **No study found** showing standalone OI change predicts direction cross-sectionally | **Yes.** OI build-ups precede liquidation cascades (Oct 10–11 2025: ~$19B liquidated); TRB, the D-002 case |
| **Funding** | Weak for direction; stronger at extremes, as a crowding measure | Yes (TRB) |

**Recommendation (proposed as D-074):** OI may raise a score only as a *conditioner* of price and flow, never by itself. Funding stays risk-only.

| Price | OI | Taker flow | Reading | Effect on Pulse |
|---|---|---|---|---|
| ↑ | ↑ | buy-dominant | new longs with conviction | positive (confirmation) |
| ↑ | ↓ | any | short covering: move without new money | neutral |
| ↓ | ↑ | sell-dominant | new shorts | negative |
| ↓ | ↓ | any | long liquidation / capitulation | negative now, flagged as possible exhaustion |
| flat | ↑↑ (top 5%) | any | leverage building with no move | **penalty** (the TRB shape) |

This keeps the TRB lesson as a penalty. It also avoids the trap where "negative funding = bullish" raises a score.

---

## 4. Phase 0: scoring hygiene (prerequisite, ~1 day)

| # | Change | Why | Done when |
|---|---|---|---|
| 0.1 | **Coverage-aware scoring.** A missing block scores a neutral 50 instead of dropping out; publish `coverage` (weight measured ÷ weight total) per asset. Apply the same rule *inside* blocks. | Stops missing data being rewarded. Simulated on 09-19 data: the top 15 becomes COMP, RUNE, FLOW, AR, RPL, EIGEN, ICP, ETHFI, POL… and no longer only 2-block coins. | A test shows a 2-block asset cannot outrank an otherwise identical 5-block asset on missing weight alone |
| 0.2 | **`score_version`** on `layer2_result` and `journal_entry` (additive column) | Methodology changes (D-071/072/073, 0.1, 5.3) must split journal cohorts, never blend them | `journal --report` groups by version |
| 0.3 | **Attention source.** Choose one: (a) pay LunarCrush; (b) substitute a free proxy; (c) set weight to 0 until a source exists | 15 points of weight are currently distributed as noise | Block measured for >60% of survivors, or weight 0 with a D-note |
| 0.4 | **CryptoPanic `404`** on `/api/developer/v2/posts/` every hour | News labels are empty; path or plan tier mismatch | One successful hourly fetch |
| 0.5 | **Holder-concentration audit**: 13 assets fail at 99–100% (SFP 99.8%, AZTEC 100%) | The kept/(1 − excluded) formula pushes toward 100% when most supply is excluded; unlabelled exchange wallets on BSC/Solana count as whales | Top 20 failures checked on an explorer; extra CEX wallets added to `excluded_addresses.yaml` with sources |
| 0.6 | `active_addresses_24h` has no writer | Another permanent None in the fundamental block | Removed, or sourced |

---

## 5. Phase 1–2: the micro-sentiment layer

### 5.1 Data (verified reachable via `www.binance.com` on 2026-09-19; D-070 host rule)

| Endpoint | What it gives | Scope | Request cost |
|---|---|---|---|
| `/fapi/v1/klines?interval=1h` | OHLC, quote volume, **taker-buy quote volume**, trades | all TRADING perps (526) | weight 1 at `limit` < 100 |
| `/futures/data/openInterestHist?period=1h` | OI in contracts and USD, hourly; **30 days kept, so backfillable** | L1 survivors (~160) | 500 rows per call; separate IP limit (verify) |
| `/fapi/v1/fundingRate` | settled funding | survivors | already collected hourly |
| `/futures/data/takerlongshortRatio`, `topLongShortPositionRatio` | positioning ratios | optional, later | — |

**This revises D-015.** Kline taker-buy volume is aggressor-flagged by Binance itself and aggregated per bar. That is exactly the input D-015 said the project did not have. Bar-level flow (a CVD built from 1h bars) becomes honest to compute. The objection stands only for tick-level CVD.

**Storage:** two tables, `bar_1h` (ts, base_asset, o/h/l/c, quote_vol, taker_buy_quote, trades) and `oi_1h` (ts, base_asset, oi_contracts, oi_usd).
- 4H bars are always **resampled** from 1H, never stored separately, so the two can't disagree.
- Features are computed in-process from the fetched window, so hourly DB reads stay near zero.

### 5.2 Features (all on **closed** bars only; a feature stamped *t* uses bars closing before *t*)

| ID | Feature | Definition | TF | Direction |
|---|---|---|---|---|
| F1 | **Taker flow** | (2·taker_buy − volume) / volume, summed over the window | 4H, 24H | higher = better |
| F2 | **Up-volume thrust** | log(volume of the last closed bar / median of the prior 30 bars) × sign(bar return) | 1H, 4H | higher = better |
| F3 | **Vol-adjusted momentum** | return / realised volatility | 24H, 7D | higher = better |
| F4 | **OI × price quadrant** | OI change in *contracts* (never notional) against price change; table in section 3 | 4H, 24H | conditioner |
| F5 | **4H structure** | EMA20 vs EMA50 stack and slope; **trendline break** = the L3 descending-line detector run on 4H closes, plus its mirror (ascending support broken on a close = bearish). State ∈ {bull_break, bull_trend, neutral, bear_trend, bear_break}, with bars since the event | 4H | state score |
| F6 | **1H structure** | the same at 1H, confirmation only | 1H | low weight |
| R1 | **Crowding** (risk) | funding ≥ 95th pct of own 90 days AND OI 24h ≥ 95th pct | — | penalty ×0.7 |
| R2 | **Leverage build without move** (risk) | OI 24h ≥ 95th pct and \|return 24h\| < 1σ | — | penalty ×0.8 |
| R3 | **Thin book** (risk) | 24h quote volume below a floor (TUNE_ME), where flow is noise | — | excluded from Pulse |

Normalisation is **cross-sectional percentiles within the hour's survivor set**, the Layer 2 cardinal rule. F1 and F3 also carry a time-series z-score against the asset's own 30 days, so "unusual for this coin" and "strong against the universe" are both visible.

### 5.3 Scoring

**Pulse (0–100, hourly, survivors only):**

| Component | Weight | TUNE_ME note |
|---|---|---|
| F1 taker flow 24H (4H at half weight) | 25 | strongest evidence |
| F5 4H structure | 20 | the "trendline" signal you asked for |
| F3 vol-adjusted momentum 24H/7D | 20 | factor evidence |
| F2 up-volume thrust 4H | 15 | the volume-spike signal |
| F4 OI confirmation | 10 | conditioner only (section 3) |
| F6 1H structure | 10 | confirmation |
| R1 / R2 penalties | multiplier | the TRB guard |

**Gem score gets a MOMENTUM/FLOW block** (7-day flow, 7-day vol-adjusted momentum, 3D/1W structure).

Proposed weights (sum 110, normalised to 100 as today):

| Block | Weight |
|---|---|
| Fundamental | 30 |
| Supply | 20 |
| Momentum/flow | 20 |
| Sector | 10 |
| Events | 10 |
| Attention | 10, or 0 per 0.3 |
| Drawdown | 10 |

Momentum and drawdown (reversal) deliberately pull in opposite directions. The literature supports both, at different horizons. After 30 days the existing `block_correlation_report` and the journal settle it with data, not opinion.

**ALIGNED** = Gem rank ≤ 25 AND Pulse ≥ 70 AND F5 ∈ {bull_break, bull_trend} AND no R-flag. It is the only thing that triggers an alert.

---

## 6. Phase 3: pipeline, dashboard, alerts

- **No new schedule.** The hourly work runs inside `collect-hourly`, which already fires 24 times a day. The Actions terms forbid near-continuous "serverless" use (`scripts/run_collectors.py` header). Adding triggers is the risk; adding steps to an existing hourly run is not. Suggested: move the cron-job.org hourly slot from :25 to **:03**, so bars are 3 minutes old instead of 25.
- **Steps:** collect 1H bars + OI → compute Pulse → `pulse_result` (ts, base_asset, score, components, state, flags) → bake `pulse.json` → **deploy to Pages without a git commit**.
  - Why no commit: 24 data commits a day would bloat a permanent history. The daily data commit stays the canonical record.
  - How: `build-site` gains a path that takes `pulse.json` as a workflow artifact.
- **Dashboard:** a **Pulse** tab showing:
  - a "last updated hh:mm UTC" stamp at the top (this alone fixes the "nothing changes" impression);
  - movers: the top 20 by Pulse, and the biggest risers since the previous hour;
  - per asset: 1H/4H sparklines with flow, OI and structure chips;
  - the ALIGNED list pinned first.
- **Telegram:** only *state changes*, capped per day and deduplicated:
  - a new ALIGNED name;
  - a bear_break on a name in the Gem top 15.

  Never "still bullish" repeats.

---

## 7. Phase 4: measurement (non-negotiable)

- **Pulse journal, append-only like the existing one.**
  - What gets an entry: an asset *entering* ALIGNED or the Pulse top 10.
  - Entry price: the **next** 1H open, never the signal bar.
  - Control: a random survivor at the same hour.
  - Horizons: 4h, 24h, 72h from `bar_1h`, reporting return, versus BTC, and max adverse and favourable excursion.
- **Stated in advance, before any data:**
  - After **200 events**, the Pulse top decile must beat its control on median 24h return by more than round-trip costs (10 bp taker ×2 plus slippage).
  - If it doesn't, the Gem MOMENTUM/FLOW weight goes to 0 and Pulse is labelled "observational". Writing this down now is what stops the result being argued away later.
- **Versioning:** every weight above is locked when it ships. Changes get a D-number and a new `score_version`, never an edit in place.

---

## 8. Budgets

| Resource | Today | After this plan | Limit |
|---|---|---|---|
| Turso writes / month | ~0.45M | +~0.6M (bars 526×24×30 ≈ 380k, OI 160×24×30 ≈ 115k, pulse ≈ 115k) | 10M |
| Turso reads / month | low | +~5M (supply history 3M, pulse state) | 500M |
| Binance weight / hour | ~600 | +~700 | 2,400 **per minute** |
| Actions runs / day | 24 hourly + daily chain | unchanged count; hourly job ~+3 min | unlimited minutes (public repo) |
| CoinGecko calls / month | ~2k | +~0.6k after the one-off backfill | 10k (Demo) |

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Overfitting weights to the first weeks | Weights fixed before observing; kill criteria in section 7; changes only via D-number |
| `www.binance.com` throttles or closes the proxied paths | D-070 fallback `data.binance.vision` is T+1, so Pulse would go dark rather than stale; the site shows "Pulse unavailable" |
| Thin perps make flow noise | R3 volume floor; percentiles instead of raw values |
| Look-ahead in intraday features | Closed bars only; `as_of` upper bound in every loader, as Layer 3 already does |
| Pulse overriding the kill switch | Pulse is computed only over L1 survivors, by construction |

---

## 10. Order of work

| Step | Work | Estimate | Blocked on |
|---|---|---|---|
| 1 | Phase 0.1–0.2 (coverage, score_version) | 1 day | your yes to 0.1 |
| 2 | Phase 0.3–0.6 | 1 day | your choice on attention |
| 3 | `bar_1h` + `oi_1h` collectors, 30-day OI backfill | 1–2 days | — |
| 4 | Features F1–F6, R1–R3, with look-ahead tests | 2 days | D-074 decision (section 3) |
| 5 | Pulse score, `pulse_result`, hourly steps, `pulse.json` deploy path | 1–2 days | — |
| 6 | Dashboard Pulse tab, Telegram state alerts | 1–2 days | — |
| 7 | Pulse journal + evaluation report | 1 day | — |
| 8 | Gem MOMENTUM/FLOW block + reweight (new `score_version`) | 1 day | 2 weeks of `bar_1h` |

---

## 11. Decisions needed from you

1. **D-074:** accept OI as a *conditioner* only (section 3), or keep D-002 absolute and use flow, volume and structure without OI?
2. **Phase 0.1:** approve neutral-fill coverage scoring?
3. **Attention:** pay for LunarCrush, use a free proxy, or zero the weight?
4. **Hourly slot:** move cron-job.org from :25 to :03?
5. **Kill criteria** in section 7: accept as written?

---

### Sources

- Anastasopoulos, Gradojevic, Liu, Maynard, Tsiakas: [*Order Flow and Cryptocurrency Returns*](https://www.sciencedirect.com/science/article/pii/S1386418126000029) ([working paper](https://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2025-Greece/papers/OrderFlowpaper.pdf))
- Fieberg et al.: [*A Trend Factor for the Cross Section of Cryptocurrency Returns*](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178), JFQA 2025
- Liu, Tsyvinski, Wu: [*Common Risk Factors in Cryptocurrency*](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.13119), Journal of Finance 2022
- Wen, Bouri, Xu, Zhao: [*Intraday return predictability in the cryptocurrency markets: Momentum, reversal, or both*](https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833), 2022
- [*Early-warning signals are event-heterogeneous across seven crypto-perpetual liquidation cascades*](https://arxiv.org/html/2607.27070), 2026
- [*Anatomy of the Oct 10–11, 2025 Crypto Liquidation Cascade*](https://papers.ssrn.com/sol3/Delivery.cfm/5611392.pdf?abstractid=5611392&mirid=1)
