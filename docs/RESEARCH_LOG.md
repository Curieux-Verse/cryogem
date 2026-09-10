# Research log

Answers to the open research questions (spec 17), plus any measurement the
system makes about itself that informs a design choice.

Record the answer AND the date, because several of these change over time.

---

## Open questions

| # | Question | Status | Answer |
|---|---|---|---|
| R1 | Free API for top-10 holder concentration across ETH/BSC/Base/Solana | open | |
| R2 | CryptoPanic plan availability and free-tier limits | open | |
| R3 | LunarCrush free-tier request limits | open | |
| R4 | Free source exposing unlock `recipient_type` | open | |
| R5 | Addresses to exclude from holder concentration (bridges, staking, CEX cold) | open | seed list in `config/excluded_addresses.yaml` |
| R6 | Free historical daily OHLCV beyond 30 days | open | |
| R7 | Does Binance publish funding interval explicitly, or must it be derived? | open | |
| R8 | Turso free-tier storage / database count / row-read allowance | open | |
| R9 | Does Turso's Python client support every SQLite feature in schema.sql? | **partial** | Local sqlite3: full DDL applies, 21 tables + 24 indexes + 3 triggers. Turso untested until Phase 10 step 1. |
| R10 | Oracle Cloud Always Free signups from India, idle-reclamation policy | open | |
| R11 | Actions artifact retention default; release assets still unlimited | open | |
| R12 | Maximum fine-grained PAT expiry; is "no expiration" still offered? | open | |
| R13 | Does the Actions dispatch endpoint need Contents:write as well? | open | |
| R14 | cron-job.org free-tier job count limit (need 4) | open | |

---

## Measurements this system makes about itself

These are answers only the collected data can give. Fill them in as the
horizons elapse — they are the point of collecting.

### News lag distribution (Tier 3)
After 14 days of `news_item` rows, report the `lag_seconds` percentiles.
Expected: median well above 15 seconds, which is the empirical answer to
"can I trade news?" — from the user's own data rather than an assertion.

**First measurement, 2026-09-09** (single poll, n=74, from the CoinDesk,
Cointelegraph and The Block RSS feeds):

| statistic | seconds | human |
|---|---:|---|
| freshest item in the poll | 1,476 | ~24.6 min |
| p50 | 25,004 | ~6.9 hours |
| p95 | 101,444 | ~28.2 hours |

**Read this carefully before drawing the conclusion.** This is the *age of each
item at the moment we first saw it*. A first poll against an RSS feed returns
that feed's entire current window, which is mostly a backlog published over the
preceding day rather than items we were late to. The p50 and p95 are therefore
an upper bound on steady-state latency, not a measurement of it.

The figure that does mean something today is the **freshest item: ~25 minutes
old**. Even the newest thing in the feed was published long after the move it
describes. Nothing here arrives within the seconds a listing takes, and the
floor is bounded by the poll interval anyway. Both point the same way, which is
why news is confined to journal labelling and is never a trigger.

**The measurement that settles it** needs steady-state data: once the collector
has run continuously for a few days, recompute over items whose
`fetched_at_utc` is at least 24h after collection began, so the initial backlog
is excluded. Record it here and replace this note.

- steady-state p50: `____` · p95: `____` · n: `____` · measured on: `____`

### Trigger lag (cron-job.org intended vs workflow actual start)
p95 should stay under 3 minutes. Tens of minutes means an `on: schedule`
trigger is still live somewhere in the repo.

- p95: `____` · measured on: `____`

### L1 survivor rate
Target band is 20–50% of the universe. Outside it, thresholds need review —
not silent adjustment. Record the reasoning in DECISIONS.md.

- survivors/universe: `____` · measured on: `____`

### L2 block correlation matrix
After 30 days of scores. Any pair with |rho| > 0.8 is measuring one thing twice.

- measured on: `____` · highest pair: `____`

---

## R8 — Turso free-tier allowances  ·  ANSWERED 2026-09-10

Read from the vendor page rather than third-party summaries, which disagree:

| | Free tier |
|---|---:|
| Databases | 100 |
| Storage | 5 GB |
| Rows read / month | 500,000,000 |
| Rows written / month | 10,000,000 |

Source: turso.tech/pricing and docs.turso.tech/help/usage-and-billing.

Sizing against the real database after cutover: 362,818 rows total, dominated
by 357,121 daily price bars. Storage is a non-issue. **Row READS are the
metered resource that matters**, which is why every query pattern is indexed
and why table_stats maintains counts incrementally instead of running
`SELECT COUNT(*)` over the time-series tables.

## R9 — does libSQL honour the schema, including the append-only triggers?  ·  ANSWERED 2026-09-10

**Yes, fully.** `init-db` against Turso executed 52 statements and created all
23 tables. The append-only guarantee was then tested against the real database
rather than assumed:

    removal attempted on journal_entry: 50 rows before, 50 after.
    The row survived, so the trigger IS enforced.

**The finding that nearly caused a false negative.** The first version of this
check matched on the exception message and reported `append_only_enforced:
False` — on a database that was enforcing it correctly. libsql-client discards
the server's error text (see the API_DEVIATIONS entry), so there was no message
to match. The check now uses the row count, which is ground truth.

The general lesson: when verifying that something is *prevented*, assert on the
observable state, not on the error you expected to see. The error is the
library's account of what happened; the row count is what happened.

