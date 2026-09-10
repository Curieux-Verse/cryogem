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
