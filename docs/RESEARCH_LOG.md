# Research log

Answers to the open research questions (spec 17), plus any measurement the
system makes about itself that informs a design choice.

Record the answer AND the date, because several of these change over time.

---

## Open questions

| # | Question | Status | Answer |
|---|---|---|---|
| R1 | Free API for top-10 holder concentration across ETH/BSC/Base/Solana | **answered** | GoPlus token_security, keyless, 43 EVM chains + Solana. See below, D-035, D-036. |
| R2 | CryptoPanic plan availability and free-tier limits | open | |
| R3 | LunarCrush free-tier request limits | open | |
| R4 | Free source exposing unlock `recipient_type` | **answered** | DefiLlama datasets host, `category` per allocation. See below, D-037. |
| R5 | Addresses to exclude from holder concentration (bridges, staking, CEX cold) | **answered (mechanism)** | automatic rules + `config/excluded_addresses.yaml`; exchange wallets still to curate. See below, D-036. |
| R6 | Free historical daily OHLCV beyond 30 days | **answered** | Binance klines, back to 2023-09-06. D-016. |
| R7 | Does Binance publish funding interval explicitly, or must it be derived? | open | |
| R8 | Turso free-tier storage / database count / row-read allowance | **answered** | see R8 below |
| R9 | Does Turso's Python client support every SQLite feature in schema.sql? | **answered** | see R9 below |
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

## R1 — top-10 holder concentration  ·  ANSWERED 2026-09-13

**GoPlus Security `token_security`**, free and keyless. Probed live on four chains
(AAVE on Ethereum, CAKE on BSC, AERO on Base, JUP on Solana), ~1 s per call.

| Finding | Consequence |
|---|---|
| 43 EVM chains in `/supported_chains`, plus a separate Solana endpoint | readable chains are derived from this list, not hard-coded (D-035) |
| One contract per call; a comma list returned one result | ~25 calls/min, bounded batches |
| `percent` is a fraction of supply, `1` = 100% | stored as-is |
| `tag` empty on **40 of 40** holders pulled | exclusion cannot rely on labels |
| `is_locked = 1` on the CAKE burn address; NOT on AERO's VotingEscrow or AAVE's Staked AAVE | lock flag is necessary, not sufficient |
| Solana returns `token_account`, not owners, and null DEX fields | Solana readings flagged `token_accounts` |
| No rate-limit headers; limiting arrives as HTTP 200 with `code: 4029` (136 of 315 reads lost at 25/min) | 20/min, and code 4029 retried with a doubling wait |

Contract names come from Blockscout `/api/v2/addresses/{addr}`, whose
`implementations[].name` sees through proxies. Implementation: `src/collectors/contracts.py`,
`src/collectors/holders.py`.

Coverage projection over 455 screened assets with a CoinGecko id: 324 measurable,
112 native coins (check not applicable), 19 on chains no source covers (Sui, TON, ...).

## R4 — unlock `recipient_type`  ·  ANSWERED 2026-09-13

**DefiLlama's datasets host**, keyless. The emissions-adapters repository proposed as
a self-computed backstop returns 404 and its only recent copy has no license, so it is
not an option. `api.llama.fi/emission/{slug}` is 402. `defillama-datasets.llama.fi` is
200 for `/emissionsProtocolsList` (372 protocols) and `/emissions/{slug}`.

Categories observed across a 9-protocol sample (allocation counts): `Uncategorized`
1617 (one protocol's weekly emissions), `noncirculating` 251, `insiders` 55,
`privateSale` 48, `staking` 48, `publicSale` 8, `airdrop` 5, `liquidity` 3,
`farming` 2, `ecosystem` 1. `unlockType` values: `cliff`, `linear_start`,
`linear_rate_change`. Mapping to recipient_type: `settings.unlocks.category_map`, D-037.

## R5 — holder exclusions  ·  ANSWERED (mechanism) 2026-09-13

Automatic: burn addresses, GoPlus `is_locked`, the token's own DEX pairs, and contracts
whose Blockscout implementation name matches `settings.holders.exclude_contract_name_patterns`.
Curated: `config/excluded_addresses.yaml`, seeded only with the two universal burn
addresses. Exchange wallets are left to curate with a checked source -- an unverified
exclusion hides real concentration. Blockscout hosts live on 2026-09-13: eth, base,
arbitrum, polygon, zksync (200); optimism, scroll, gnosis (redirect); linea and bsc (404).

## R6 — free daily OHLCV beyond 30 days  ·  ANSWERED 2026-09-10

Binance klines serve years of daily bars; the klines collector backfilled 356,358 bars
back to 2023-09-06. See D-016.

## GoPlus access token — does it buy speed?  ·  ASSESSED 2026-09-14

Read from GoPlus's own documentation (Support, get token, account status,
available packages):

| Question | Answer |
|---|---|
| Free limit | "GoPlus Security API is free, and the rate limit is 30 calls/minute." |
| How to get a token | "If you require a higher limit than the available plans, please contact us to apply for an access token" -- by email to service@gopluslabs.io. Not self-serve. |
| Token flow | `POST /api/v1/token` with `app_key`, `time` (within +-1000 s) and `sign = sha1(app_key + time + app_secret)`; returns `access_token`, `expires_in`. The header for using it on data calls is not stated on that page. |
| Paid route | CU (compute unit) packages, e.g. "Chicken": 100,000 CU, price 100, 1 month. Accounts carry `cu_per_minute` and `cu_per_day`. Package endpoints authenticate with a wallet signature (`Authorization: PersonalSign {sign}.{timestamp}` plus `X-Address`), and can be bought over x402. |
| Not documented | CU cost of a `token_security` call; the per-minute limit each package grants. |

**Measured against our own run.** 315 holder reads took ~32 minutes, about 10 a
minute. The ceiling at the documented free limit is ~10.5 minutes (30/min).
The gap is not the limit itself: 43 reads drew code 4029 and waited 15-120 s
each, and those waits make up most of the runtime. A token or package would
shorten the run; it is not needed for freshness, since a daily run finishing
in ~32 minutes already keeps every reading under a day old (D-042).
