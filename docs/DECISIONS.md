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
