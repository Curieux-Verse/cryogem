# Crypto Gem Discovery & Risk Screening — Implementation Plan

**Scope:** a build path from zero to a working, evidence-backed altcoin discovery and disqualification system.
**Assumed starting point:** first-year CSE, beginner in Python, comfortable with charts and ICT/SMC concepts.
**Total runway:** ~9 months to a system with real backtest data. Usable output from month 2.

> This is an engineering and research plan. It is not financial advice, and none of the thresholds
> below are validated for your capital, your risk tolerance, or the market you'll actually face.
> The primary deliverable is the dataset and the tooling. Trading it is optional and downstream.

---

## 0. The core thesis this plan is built on

Seven case studies (TRB, RAVE, VVV, AERO, FORM, plus the CEX-listing and token-unlock datasets)
converge on one finding:

**The data that disqualifies a bad gem is available before the move, and it is almost never chart data.**

RAVE was flaggable from wallet concentration. TRB was flaggable from float and OI/mcap. Every
Binance listing pump was flaggable from a base rate. Every unlock cliff is on a public calendar.

Therefore the system is built **disqualification-first**. You do not search for winners. You
build a filter that removes losers, and you look at what survives.

This inverts how almost everyone approaches it, and it is the entire edge available to a
retail participant with no information advantage.

---

## 1. Architecture: three layers, in strict order

Run them in this order. A coin that fails Layer 1 never reaches Layer 2. Do not reorder to
"give a coin a chance."

```
┌─────────────────────────────────────────────────────┐
│ LAYER 1 — SUPPLY INTEGRITY (binary kill switch)     │
│ Can a small group move this price at will?          │
│ Output: PASS / FAIL. No score, no nuance.           │
└─────────────────────────────────────────────────────┘
                       ↓ survivors only
┌─────────────────────────────────────────────────────┐
│ LAYER 2 — DEMAND (cross-sectional score 0–100)      │
│ Is anything real actually improving?                │
│ Revenue, usage, emissions direction, sector flow.   │
└─────────────────────────────────────────────────────┘
                       ↓ top decile only
┌─────────────────────────────────────────────────────┐
│ LAYER 3 — TIMING (structure + positioning)          │
│ Is now a good moment, and where is invalidation?    │
│ ICT structure + OI/funding as a RISK check.         │
└─────────────────────────────────────────────────────┘
```

**Critical design decision:** derivatives data (OI, funding, CVD) lives in **Layer 3 as a risk
check**, not in Layer 1 as a discovery signal.

Reason: in every case study, the derivatives signal was ambiguous about direction and
unambiguous about danger. TRB's negative funding was read as bullish by a sophisticated
on-chain account, in public, hours before a −78% collapse. Use these metrics to size down
and to refuse trades, not to find them.

---

## 2. Layer 1 — Supply Integrity (the kill switch)

Binary. Any single FAIL removes the coin from the universe for that day.

| # | Check | FAIL condition | Source |
|---|---|---|---|
| 1 | Top-10 holder share | > 60% excluding known locked/staking/bridge contracts | On-chain / explorer API |
| 2 | Circulating ÷ total supply | < 30% | CoinGecko |
| 3 | Perp OI ÷ circulating mcap | > 1.0 | Binance `openInterest` + CoinGecko |
| 4 | Perp 24h vol ÷ spot 24h vol | > 40× (or spot pair absent → auto-FAIL) | Binance tickers |
| 5 | Perp contract age | < 60 days | `exchangeInfo` onboardDate |
| 6 | Circulating mcap | < $30M | CoinGecko |
| 7 | Unlock in next 30 days | > 5% of circulating supply, team/investor allocation | Tokenomist / TokenUnlocks |
| 8 | Market cap not resolvable | Not in CoinGecko top ~2000 | CoinGecko |
| 9 | Mcap-to-liquidation ratio during any 24h move >100% | > 50:1 | CoinGlass / Coinalyze |

**Notes on individual checks:**

- **#1 and #2 are the RAVE detectors.** Nine wallets held ~95% of RAVE's 1B supply; three
  team-linked wallets held 89.74%. TRB had ~20 whales holding ~95% of a 2.75M float.
- **#3 and #4 come from a published funding-arb risk screener** (thresholds: OI/mcap warning
  >0.5 danger >1.0; perp/spot warning >15× danger >40×). Its author uses them to refuse
  carry trades. Same numbers, and refusing is the right default.
- **#7 is the single highest-value check nobody runs.** Keyrock's study of 16,000+ unlock
  events: ~90% followed by negative price pressure, team unlocks worst at up to −25%
  drawdown, and **the decline typically starts ~30 days before the date**. Independent
  sample of 236 events: median 1-month return −16.26%.
- **#9 is the RAVE-specific tell.** ~$6B of market cap evaporated on roughly $52M of
  liquidations. That ratio is arithmetically impossible in an organic market. It means the
  market cap was float × a controlled price.

**Inverse signal worth flagging positively:** a token whose major cliff unlocks have all
passed. Supply pressure structurally ends. This is a queryable condition that almost nobody
screens for, and it is the setup underneath a lot of genuine Type-D reversals.

---

## 3. Layer 2 — Demand (score 0–100, cross-sectional)

Applied only to Layer-1 survivors. **Percentile-rank every metric within today's surviving
universe. Never use absolute thresholds here** — absolute cutoffs break the moment market
regime changes.

### 3.1 Fundamental momentum (weight 40)

| Metric | Why | Source |
|---|---|---|
| Protocol revenue, 30d vs prior 30d | The VVV signal: $70M → $100M annualized run-rate in one month | DefiLlama (free, no key) |
| Fees, 7d / 30d trend | Leading indicator of revenue | DefiLlama `fees.llama.fi` |
| Price-to-sales ratio and its percentile | Cheapness vs peers on real cash flow | Token Terminal / Artemis |
| TVL trend (treat as capital snapshot, not activity) | Context only, weight low | DefiLlama |
| Daily active addresses, 30d trend | Usage independent of price | Artemis |

**Not every token has revenue.** For those, this block scores 0 and the coin must carry on
supply and narrative alone. That is information, not a gap — record it.

### 3.2 Supply direction (weight 30)

| Metric | Scoring |
|---|---|
| Emissions trajectory | Falling = high score. VVV cut 10M → 8M → 6M → 3M → 2.5M → 2M/yr |
| Cumulative burn as % of total supply | VVV: 33.87M burned = 41.85% of total |
| Days since last major unlock cliff | More is better |
| Days until next major unlock | More is better; <30 already killed in Layer 1 |
| Staking/lock ratio | Reduces effective float, but verify it isn't team-controlled |

### 3.3 Sector flow (weight 20)

Compute a sector return index (AI, DeFi, DePIN, RWA, gaming, L2, privacy) and rank each
coin's sector by 7d and 30d relative strength vs BTC.

This matters more than people expect. VVV's own coverage noted the bear case starts with
the sector: a token is not immune to its category, and sentiment and sector flows dominate
price action over weeks and months even when fundamentals are sound.

**Do not pick coins in a dying sector because the chart looks good.**

### 3.4 Drawdown / reversal context (weight 10)

- % below ATH (the Type-D setup: AERO −76%, FORM ~−90%)
- Time spent in current range
- Whether the previous cycle's supply overhang has cleared

Justification: a nine-year study of 1,160 cryptocurrencies found a **distinctive reversal
effect that challenges the established momentum effect**, plus a size effect and an
illiquidity premium. Reversal in the crypto cross-section is a documented anomaly. But note
what that means — it works *cross-sectionally, on average, over weekly rebalances*. It does
not mean any individual beaten-down chart will bounce.

**Output of Layer 2:** ranked list. Take the top 10–15 only.

---

## 4. Layer 3 — Timing and risk

Applied only to the Layer-2 top decile. This is where your ICT/SMC work belongs.

### 4.1 Structure (your entry logic)

- HTF (weekly/3D) market structure shift
- Reclaim of a descending trendline, confirmed by close not wick
- Retest of the breakout zone
- FVG / order block on the retest
- Explicit invalidation level, defined *before* entry

The two forward-looking calls in your screenshots both use exactly this. `$FORM` published
"Invalidation: Weekly close below $0.1768" — that's the correct form for a call, and it's
the part most callers omit.

### 4.2 Positioning as a *risk* check, not an entry trigger

Compute for every candidate. These reduce size or veto. They never trigger a buy.

| Metric | Interpretation |
|---|---|
| OI in **contracts**, not USD | USD OI = contracts × price, so it rises in a rally with zero new positioning. Use contract-denominated or price-residualized |
| Funding, **interval-normalized** | Binance settlement is no longer always 8h; it varies per symbol (4h, sometimes 1h). Naive `rate × 3 × 365` mis-ranks your entire universe |
| Funding asymmetry | `F = Premium + clamp(Interest − Premium, −0.05%, +0.05%)`, interest fixed at 0.01%/8h. Structural positive bias. Funding only turns negative when premium drops below −0.05%. **Negative funding is a stronger signal than positive funding of equal size** |
| Spot CVD vs perp CVD | Spot-led = healthier. Perp-led = fragile |
| Spot depth at ±2% | Your actual exit size. If your position exceeds it, you don't have a position, you have a hostage |

### 4.3 Known data limitations — write these in your code comments

- **Liquidation data is throttled at source.** Binance's `forceOrder` stream pushes only the
  largest single liquidation per symbol per 1000ms. Binance and Bybit both moved to one
  liquidation per second around mid-2021; OKX capped at one per second per contract. Bybit
  only restored full data in Feb 2025. All liquidation totals are floors.
- **CVD degrades on thin books.** When the aggressor flag is absent, tools fall back on the
  tick rule, which breaks in fast markets. Low-liquidity altcoin perps produce erratic delta.
- **CVD divergence can persist through an entire trend.** It is not a reversal timer.

---

## 5. Data sources

| Source | Cost | Gives you | Gotchas |
|---|---|---|---|
| Binance `fapi` REST | Free, no key | OI, funding, tickers, listing age, aggTrades | `openInterestHist` keeps **only 30 days** |
| Binance WebSocket | Free | Live aggTrade (CVD), forceOrder | 10 msgs/sec in, 1024 streams/connection |
| Hyperliquid `info` | Free, no key | Funding, OI, premium, volume, all perps | On-chain, trade-level, cleanest tape available |
| Coinalyze API | Free | OI/funding/liquidation/LS-ratio history, aggregated | 40 calls/min; intraday retention only 1500–2000 points, deleted daily |
| CoinGlass v4 | Paid tiers | Broadest aggregation, liq heatmap, unlocks, whale data | Cost |
| DefiLlama | Free, no key | TVL, fees, revenue across 8,000+ protocols, 461 chains | Pro $300/mo for higher limits |
| Artemis | Free tier | ~75 chains, active addresses, dev activity, P/S | Methodology lags new protocols |
| Token Terminal | Paid | Equity-style financials, ~50 protocols | Narrow coverage |
| CoinGecko | Free tier | Market cap, float, supply | Ticker collisions |
| Tokenomist / TokenUnlocks | Free tier | Unlock calendar, recipient-level allocation | — |

### Implementation traps that will each cost you a day

1. **The `1000` prefix.** Binance lists `1000PEPE`, `1000SHIB` where the contract multiplies
   price by 1000. Strip before market-cap lookup or every one silently gets mcap = 0.
2. **Ticker collision.** Symbols aren't unique across CoinGecko. A $20M token can share a
   ticker with a $2B one. Pull markets ordered by mcap descending, keep the first match.
   This biases toward under-flagging, which is the safe direction.
3. **Orphan perps.** Some perps have no Binance spot pair. Set perp/spot ratio to `inf`, not
   `None` — a perp whose hedge would live on another exchange is materially worse.
4. **Funding interval.** Fetch per-symbol. Do not assume 8h.
5. **Delisted symbols.** They vanish from `exchangeInfo`. Snapshot the universe daily or your
   backtest has survivorship bias baked in.

---

## 6. Build phases

### Phase 0 — Foundations (weeks 1–3)

Non-negotiable prerequisites. Don't skip to Phase 1.

- Python: functions, dicts, list comprehensions, `requests`, `json`, exceptions
- `pandas`: DataFrame, merge, groupby, pct_change, rank
- SQLite via `sqlite3` or SQLAlchemy
- Git basics, and a private repo from day one
- Reading API docs — genuinely a skill, practice on Binance's

**Deliverable:** a script that pulls Binance's perp list and prints it as a DataFrame.

### Phase 1 — The collectors (weeks 3–5) ← *the most important phase*

Functions that snapshot and store. Nothing clever. They just have to run.

```
collectors/
  binance.py       # OI, funding, tickers, exchangeInfo
  hyperliquid.py   # metaAndAssetCtxs, fundingHistory
  coingecko.py     # mcap, float, supply
  defillama.py     # fees, revenue, TVL
  store.py         # DB writes, idempotent
```

**Write every collector as a stateless function, never a loop.** `collect(as_of) -> rows`, not
`while True: collect(); sleep(300)`. A collector with a loop inside it cannot run in CI, and
getting into CI is the next phase. This one design choice decides whether the project becomes
autonomous or stays tied to your laptop.

**Cadence splits into three tiers, and this matters more than it sounds:**

| Tier | Cadence | Where it can run | What needs it |
|---|---|---|---|
| C | Daily | GitHub Actions | **Everything in L1, L2, the journal, the dashboard** |
| B | Hourly | GitHub Actions | Funding persistence, OI series |
| A | 5 min | A machine that stays on | Only L3's intraday checks and CVD |

Build C first. It is the entire product. Layers 1 and 2 are daily computations — the 5-minute
data is an upgrade, not a prerequisite. If you never get a persistent host, you lose the intraday
positioning layer and nothing else.

**Deliverable:** a growing database. Check it daily for a week. Fix gaps.

> **This is the whole project.** Binance keeps 30 days of OI history. Coinalyze deletes
> intraday data daily. Nobody can backtest a positioning strategy from public APIs after the
> fact — you either buy the data or you record it. Starting collection today is what
> makes months 6–12 possible. Everything else can be rebuilt in a weekend. This cannot.

### Phase 1.5 — Make it autonomous (week 5–6) ← *moved forward, and here's why*

This used to sit at the end. It belongs here, because until it is done the collector only runs
when your laptop is open, and Phase 1's whole premise is that missed days are gone forever.

**Don't use GitHub's own cron. Use cron-job.org to trigger the workflows externally.**

GitHub's `schedule` event is documented as best-effort: runs commonly fire 5–45 minutes late,
delays of 8–14 hours happen, days get dropped during high-load windows, and there have been
platform-wide periods where scheduled runs stopped being created entirely while
`workflow_dispatch` kept working throughout. Self-hosted runners don't help — the queuing is on
GitHub's side.

So every workflow in the repo is `on: workflow_dispatch` only, and cron-job.org fires each one
with a POST:

```
POST https://api.github.com/repos/{owner}/{repo}/actions/workflows/collect-daily.yml/dispatches
Authorization: Bearer <fine-grained PAT>
Body: {"ref":"main"}    → 204 No Content
```

cron-job.org runs jobs at frequencies up to once a minute, lets you set the method, headers and
body, keeps the last 50 executions with both scheduled and actual times, and emails you when a
job starts failing. Free. Its 30-second job timeout doesn't matter — the dispatch endpoint returns
204 immediately and doesn't wait for the workflow.

**Two bonuses beyond punctuality.** The 60-day inactivity rule only disables *scheduled*
workflows, so with none in the repo there's nothing to disable and **you don't need a keepalive
workflow at all**. And during a GitHub scheduler outage, dispatch keeps working, so you're already
on the path that survives.

**Token gotchas, in the order they'll bite you.** `GITHUB_TOKEN` cannot fire `workflow_dispatch` —
GitHub blocks it to prevent recursion, so a PAT is mandatory. Use a fine-grained one scoped to
this repo only, with Actions read/write plus Contents read/write; a 403 "Resource not accessible"
is almost always the missing Contents permission. And **PATs expire** — when it does, every job
401s and collection silently stops. Set 90 days, put the expiry date in `DECISIONS.md`, and set a
calendar reminder.

Also worth being clear-eyed about: a third party now holds a credential that can write to your
repo. Fine-grained + single repo + short expiry is what makes that an acceptable trade.

**Monitor at two layers — they catch opposite failures.** cron-job.org tells you the trigger
didn't fire or GitHub rejected it. It cannot tell you the workflow started and then failed,
because it already got its 204. healthchecks.io (free, ping at the end of a successful job)
catches that half. Set the daily check's period to 25 hours so runner-provisioning drift doesn't
cry wolf.

**Make the repo public.** Actions is unlimited and free on public repos; private repos get 2,000
Linux minutes/month, and an hourly collector alone is ~2,160. The trade-off is that your screener
output and journal become public — which, for the journal specifically, is the single best
feature of the project. A timestamped, append-only, unfilterable public track record is exactly
what no signal channel publishes.

**Don't commit the database.** Git history is permanent, so a daily-changing binary bloats the
repo forever. Use Turso (managed libSQL — SQLite-compatible, HTTP access, works from CI with no
persistent filesystem). Index every query pattern first: Turso meters row *reads*, and
cross-sectional scans burn the allowance fast.

**Deliverable:** 14 consecutive days with at least 13 successful automated runs, plus two
deliberate break tests — kill the PAT and confirm the cron-job.org email arrives; fail a job and
confirm the healthchecks alert arrives. If only one of those fires, the monitoring isn't
independent yet.

### Phase 1.6 — Tier A, only if you have a host (optional, anytime)

cron-job.org *can* fire every 5 minutes, and on a public repo Actions minutes are unlimited — so
this looks tempting. Don't. The Actions Terms of Service prohibit using it for cryptomining,
**serverless computing**, or any activity unrelated to producing, testing, deploying, or
publishing the repository's own software project, and GitHub monitors for it. Daily and hourly
runs are publishing your dashboard and are clearly fine. 288 runs a day of continuous collection
is using Actions as a compute platform, and the penalty isn't a bill — it's losing Actions on the
account running everything else.

So the 5-minute tier needs a machine you control. Free always-on hosting narrowed a lot in 2026:
Fly.io no longer has a general free tier for new accounts, Railway's credit is a trial, and
Render's free services spin down after 15 minutes idle. What actually works is an Oracle Cloud
Always Free ARM VM (never sleeps, but wants a card, signup is sometimes denied, and Oracle
reclaims idle instances), or a Raspberry Pi / spare laptop with stable power.

If none of these is available, skip it and write in `DECISIONS.md` that the dataset is hourly.
You lose the intraday positioning layer and nothing else. That is an honest limitation, not a
failure.

### Phase 2 — Layer 1 kill switch (weeks 6–8)


Implement the nine binary checks. Output a daily CSV: every perp, PASS/FAIL, which checks
failed.

**Validation:** run it against historical RAVE (April 2026) and TRB (December 2023). If your
screener doesn't FAIL both well before their peaks, the thresholds are wrong. This is your
unit test.

**Deliverable:** daily FAIL list, and a written note on how many of the ~500 perps survive.
If more than 40% survive, tighten.

### Phase 3 — Layer 2 scoring (weeks 8–12)

Cross-sectional percentile ranking. Weighted composite. Sector indices.

**Deliverable:** daily top-15 with a per-metric breakdown showing *why* each ranked.

### Phase 4 — The journal (weeks 10 onward, runs forever) ← *the second most important phase*

For every coin that appears in the daily top-15, automatically record forward returns at
+1d / +7d / +30d / +90d, both raw and relative to BTC.

Never delete a row. Never filter. Never exclude "the ones where I would have known better."

After six months you can answer, with your own data:
- Does my top-15 beat a random Layer-1 survivor?
- Does it beat holding BTC?
- Is the median positive or is one outlier carrying the mean?
- Which Layer-2 sub-score actually contributes?

**This is the only thing in the plan that produces truth.** Everything before it produces
plausible-looking output.

### Phase 5 — The dashboard (month 3–4)

A static React site on GitHub Pages, reading JSON that the Actions workflows bake out after each
screen. No backend, no API keys in the browser, nothing to pay for.

**Six pages.** Two of them are the reason this is worth building:

- **`/rejected`** — everything that failed L1 today, sorted by the L2 score it *would have* had,
  grouped by which check killed it. Every crypto dashboard shows winners. Almost none shows a
  wall of rejections with the numbers attached.
- **`/journal`** — the forward-return histogram, including the losers, with median and mean shown
  side by side because they diverge and the divergence is the finding. Plus a random control drawn
  from the same survivor pool.

The other four: today's screen with the funnel (512 perps → 147 survived → 15 ranked), a per-asset
drill-down showing all nine L1 checks with their computed values, a 90-day event calendar, and a
system-health page showing collector uptime and how late GitHub's cron actually fired.

**Design direction:** an instrument panel, not a crypto marketing site. Dark neutral ground,
monospace tabular figures right-aligned, dense tables rather than cards — cards fragment
comparison, and comparison is the whole point of a cross-sectional screener. One accent for
"survived," one for "disqualified," and disqualification rendered as information rather than as an
error state. One animated moment (the funnel on load), nothing else moving.

**Three constraints that will cost you an afternoon each if missed:** use `HashRouter` (Pages has
no server-side rewrite, so deep links break on refresh with `BrowserRouter`); set `base` in
`vite.config.ts` to your repo name or every asset 404s; and build the empty states first, because
you'll deploy months before the journal has enough data to say anything.

**Deliverable:** a live URL. Show it to people.

### Phase 6 — Structure and alerting (month 4+)

Programmatic detection of the Type-D setup:
- Descending trendline via linear regression on swing highs
- Close-based break confirmation
- FVG and order block detection
- Range and consolidation identification

**This is where your Pine Script work transfers.** Prototype the structure logic in Pine on
TradingView where you can see it visually, then port to Python once it's right. Pine is a
much faster feedback loop for pattern logic than matplotlib.

Then a Telegram bot for alerts.

---

## 7. Backtest hygiene

When you have 6+ months of collected data:

1. **Point-in-time universe.** Use the symbol list as it existed on each date. Delisted coins
   are exactly the ones your screener would have flagged.
2. **Costs.** Taker fees + funding while held + slippage sized to *actual* recorded spot
   depth, not printed price.
3. **All signals, not selected ones.** Full distribution of forward returns.
4. **Median and mean, separately.** In the unlock study, mean −8.10% vs median −16.26% —
   the mean was pulled by a few outliers while the median described the typical experience.
   Your results will have the same asymmetry in the other direction. Report both.
5. **Regime split.** Test separately in BTC-up, BTC-flat, BTC-down periods. A strategy that
   only works in one regime is a beta bet wearing a costume.
6. **Out-of-sample holdout.** Develop on the first 4 months, test on the last 2. Don't peek.

---

## 8. Auditing a signal channel (apply this to any account you follow)

You cannot evaluate a caller from screenshots. Build this instead — it's a two-week project
and it will teach you more about the market than a year of following calls.

1. Scrape every message from the channel, timestamped. Store immutably.
2. Parse each into: ticker, direction, entry, target, invalidation, timestamp.
3. Bucket into three types:
   - **Forward calls** with entry + invalidation (like the $FORM and $AERO posts) — scorable
   - **Forward calls without invalidation** — score as "unfalsifiable," count separately
   - **Retrospective posts** (like the $VVV one) — never scorable, exclude entirely
4. For every forward call, pull price from the timestamp and compute the outcome
   mechanically: did it hit target before invalidation?
5. Report: hit rate, average R multiple, median R, max drawdown of a portfolio that took
   every call equal-weight, and performance vs simply holding BTC over the same window.

**Base rates to compare against:** a study of ~36,000 tweets from 180 prominent crypto
influencers across 1,600+ assets found tweets were initially associated with positive
returns, followed by **significant negative longer-horizon returns**, with effects strongest
for self-described experts, smaller-cap assets, and accounts with many followers. A separate
line of research found investors relying on Twitter information sell late during post-dump
phases and take significant losses relative to other participants.

Your channel may be an exception. The point is that you'll know, from your own data, instead
of from a screenshot of the one that worked.

**On the $VVV post specifically:** the +1,600% move is real and verifiable — VVV's ATH of
$25.96 on Sept 8, 2026 and its 1,500%+ rise since December 2025 both check out in public
data. What a screenshot cannot establish is whether the call was made at that price and time,
what size was suggested, or what the other 50 calls that quarter did. Those are the three
things that determine whether following the channel makes money.

---

## 9. Risk framework

If you eventually trade this:

- **Spot only, at first.** Every case study above involved leveraged traders being liquidated
  by moves that were correct in direction and wrong in timing. RAVE's collapse liquidated
  nearly 16,000 traders in one day.
- **Position size from spot depth, not from conviction.** If ±2% depth is $40k, a $10k
  position is 25% of the book.
- **Invalidation set before entry, in the order, not in your head.**
- **Never average down on a Layer-1 borderline pass.**
- **Re-run Layer 1 daily on open positions.** Supply integrity changes. RAVE's deployer
  wallets moved 18.58M tokens to an exchange *before* the surge — that transfer was visible.
- **Assume every screen is a snapshot.** A token can screen clean at noon and be a trap by
  dinner.
- **Cap total allocation.** You're a student. The correct number is small enough that a total
  loss changes nothing about your year.

---

## 10. Failure-mode register

Keep this open. Add to it every time something breaks.

| Failure | Symptom | Guard |
|---|---|---|
| Survivorship bias | Backtest looks great, live doesn't | Point-in-time universe snapshots |
| Look-ahead bias | Impossibly clean entries | Timestamp every field at ingest |
| Overfitting thresholds | Great on history, dead forward | Out-of-sample holdout, few parameters |
| Redundant signals | 5 metrics measuring one thing | Correlation matrix on sub-scores |
| Regime dependence | Works until it doesn't | Regime-split reporting |
| Turnover decay | Positive backtest, negative live | Model fees + slippage from real depth |
| Data staleness | Screening on yesterday | Freshness assertions, alert on gaps |
| Exogenous shock | Everything fails at once | Position limits. Oct 10, 2025: $19.37B liquidated in 24h, altcoins −20–27% while BTC fell 6.84%, all triggered by a tariff headline on a Friday night. No screener has a term for this |
| Silent cron failure | Two weeks of missing data, noticed by accident | Two independent alerts: cron-job.org failure email (trigger didn't fire / GitHub rejected it) **and** healthchecks.io (job started, then failed) |
| PAT expiry | Everything 401s and stops, months in | 90-day fine-grained token, expiry date in `DECISIONS.md`, calendar reminder 7 days out |
| A stray `on: schedule` | 5–45 min drift returns invisibly; 60-day auto-disable returns too | `grep -r schedule .github/workflows/` in CI; trigger-lag p95 on the Health page should stay under 3 min |
| Actions ToS breach | Actions restricted on the whole account | Keep Actions at daily/hourly. 5-minute collection goes on a host you control |
| Cron-slot vs real-run time | Every downstream time calculation quietly wrong | Always write the *actual* run time to `fetched_at_utc`, never a nominal slot time |
| Stale dashboard | Confident page built on 3-day-old prices | Freshness assertion in the screen job; age banner on every page if >26h |
| Turso row-read blowout | Free tier exhausted mid-month | Index every query pattern; never `SELECT COUNT(*)` on the time-series tables |
| Secret in the public bundle | Key compromised | `grep -ri "key\|secret\|token" web/dist/` gate before deploy |

---

## 11. Sequencing against your actual life

- **Now → week 6:** Phase 0, Phase 1, Phase 1.5. Get collection running *and autonomous*. This is
  the deadline that matters; everything downstream depends on data you're not yet recording, and
  a collector that needs your laptop open will have holes.
- **Week 6 → month 3:** Phases 2–4. Now you have daily usable output and a journal accumulating.
- **Month 3 → 4:** Phase 5, the dashboard. Deliberately after the journal exists, so it has
  something honest to display. Building the interface first gives you a beautiful page with
  nothing behind it, which is the exact thing this project is a reaction against.
- **Month 3 → 4 (parallel):** Channel audit project. Pairs well with a hackathon — self-contained,
  demoable, clear result.
- **Month 4 → 6:** Phase 6, structure detection. Prototype in Pine, port to Python.
- **Month 6 → 10:** First real backtest against your own collected data. Expect the first
  version to be wrong. That is the correct outcome and it is not a failure.

**What this is actually worth:** at the end you will have built a distributed data collector, a
normalized multi-source pipeline, a cross-sectional ranking engine, a backtest harness with
correct hygiene, a full CI/CD deployment running unattended, a typed React dashboard on a public
URL, and a proprietary dataset that cannot be bought cheaply.

That portfolio is worth more than any signal it produces, and none of it depends on the strategy
working. A live URL that has been updating itself for eight months — visibly including the days
the calls were wrong — is a stronger thing to put in front of an interviewer or a hackathon judge
than any backtest curve.

---

## Appendix: sources this plan is built on

**Case studies:** TRB (Dec 2023), RAVE (Apr 2026), VVV (Dec 2025–Sep 2026), AERO, FORM,
Oct 10 2025 liquidation cascade.

**Quantified datasets:**
- Keyrock, 16,000+ token unlock events across 40 tokens — ~90% negative price pressure,
  team unlocks worst (−25%), impact begins ~30 days pre-event
- Tokenomist, 236 unlock events — 1-month median −16.26%, mean −8.10%
- CryptoNinjas/Storible, 389 tokens across 6 CEXs in 2024 — Binance +87% at listing,
  98% eventually dump, −70% average from listing; 37% hit ATH on listing day
- Presto Labs — funding rate changes explain 12.5% of 7-day price variation, declining after;
  more useful cross-sectionally than per-asset
- Review of Accounting Studies, ~36,000 influencer tweets / 180 influencers / 1,600+ assets —
  initial positive returns, significant negative longer-horizon returns
- China Accounting and Finance Review, 1,160 cryptocurrencies over 9 years — size effect,
  distinctive reversal effect, illiquidity premium
- Empirica, 7 years of Binance listings — −6.34% first week, +8% at 6 months
