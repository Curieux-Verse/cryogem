# API deviations

Operating rule 8: **when an API's real behaviour contradicts the spec, trust the
API and record the difference here.**

Each entry: what the document said, what the API actually did, what the code now
does, and the date observed.

---

## Format

### YYYY-MM-DD — `<endpoint>`
- **Document said:**
- **Observed:**
- **Code now does:**
- **Impact:**

---

## 2026-09-09 — HTTP mocking library
- **Document said:** use `responses` for HTTP mocking in tests.
- **Observed:** `responses` mocks `requests`; this project uses `httpx`.
- **Code now does:** uses `respx`, the httpx-native equivalent.
- **Impact:** none beyond the dependency name.

## Open items to verify at first live run

These are asserted by the spec but not yet confirmed against the live APIs.
Confirm each on the first real collection and record the answer above.

- [ ] Binance funding interval is **not** uniformly 8h — at least one symbol
      should show 4.0 or 1.0. If every symbol reads 8.0, the derivation from
      consecutive `fundingTime` deltas is wrong (spec 6.1.1).
- [ ] `/fapi/v1/fundingInfo` exists and publishes the interval directly (R7).
      If it does, prefer it over deriving from deltas.
- [ ] `futures/data/openInterestHist` really retains only ~30 days.
- [ ] At least one perp has no Binance spot pair (orphan perp -> ratio `inf`).
- [ ] CoinGecko free tier page size / rate limit as actually enforced.
- [ ] CryptoPanic plan availability (public docs mark the free Developer plan
      discontinued — check the account page, not the docs) (R2).
- [ ] LunarCrush free-tier request limits (R3).

---

## 2026-09-09 — Binance funding interval is predominantly 4h, not 8h

- **Document said:** settlement "is no longer always 8h; it varies per symbol
  (4h, sometimes 1h)", and the Phase 1 acceptance bar was "verify at least one
  symbol shows 4.0 or 1.0".
- **Observed** (live `/fapi/v1/exchangeInfo` + `/fapi/v1/fundingRate`, 653 USDT
  perpetuals):

  | interval | symbols | share |
  |---|---:|---:|
  | 4h  | 499 | 76.4% |
  | 8h  | 143 | 21.9% |
  | 1h  |   8 |  1.2% |
  | 2h  |   2 |  0.3% |
  | unknown | 1 | 0.2% |

- **Code now does:** derives the interval per symbol from consecutive
  `fundingTime` deltas (modal gap, must hold a 60% majority), stores it on
  `universe_snapshot` and copies it onto every `derivatives_snapshot` row.
  `funding_apr = rate * (8760 / interval)`. When the interval cannot be
  established the APR is NULL — never a fallback of 8h.
- **Impact:** large, and larger than the spec anticipated. The naive
  `rate * 3 * 365` formula is correct for only 22% of the universe and
  understates the annualised rate by 2x for the 76% that settle 4-hourly.
  Using it would mis-rank essentially the whole cross-section.
- **Also note:** a 2h interval exists, which the spec did not mention. It is in
  the accepted set (1, 2, 4, 8).

## 2026-09-09 — Binance lists perpetuals with non-ASCII (CJK) symbols

- **Document said:** nothing; symbols were assumed to be `[A-Z0-9]`.
- **Observed:** 5 live USDT perps carry Chinese-character base assets
  (rendered escaped in logs, e.g. `\u9f99\u867eUSDT`).
- **Code now does:** accepts any symbol free of whitespace. These assets enter
  `universe_snapshot` and are disqualified honestly by `L1_NO_MCAP`, because no
  market-cap source resolves such a ticker.
- **Impact:** the earlier A-Z-only rule dropped them from the universe
  entirely, which understated the funnel denominator and hid them from the
  rejection wall. Admitting-then-failing is the correct behaviour for a
  disqualification-first system.
- **Secondary:** logging a CJK symbol to a cp1252 Windows console raised
  `UnicodeEncodeError` and could kill a collector mid-run over a log line. The
  console stream is now reconfigured to UTF-8 with `backslashreplace`.

## 2026-09-09 — Binance announcement CMS: shape changed and dates removed

- **Document said:** poll
  `.../cms/article/catalog/list/query?catalogId=48...` and read articles with a
  `releaseDate`.
- **Observed** (live, 200 OK, `total: 2253`):
  1. Articles are at `data.articles`, **not** `data.catalogs[0].articles`.
  2. **There is no date field at all.** `releaseDate` and `publishDate` are both
     absent/None on every article. Available keys: `id`, `code`, `title`,
     `imageLink`, `shortLink`, `body`, `type`, `catalogId`, `catalogName`,
     `publishDate` (null), `footer`.
- **Code now does:**
  - Accepts both payload shapes, so an upstream revert does not silently empty
    the calendar.
  - Resolves the date in order: real date field, then a `YYYY-MM-DD` parsed out
    of the title (Binance embeds it in many), then **undated**.
  - An undated announcement is stored with `confidence='expected'` and our
    discovery time, and the run warns with a count. It is **never** stamped
    with today's date dressed up as a publication time.
- **Impact:**
  - The news-lag distribution cannot be measured from this endpoint any more,
    because there is no publication timestamp to subtract. Lag measurement now
    depends on the Tier-3 RSS/CryptoPanic feeds, which do carry `published_at`.
  - `L1_AGE` is unaffected: it reads `onboardDate` from `exchangeInfo`, which is
    authoritative, not from announcements. That is why the check was wired to
    exchangeInfo in the first place.
- **Secondary finding:** "Binance Will **Add** X on Earn, Buy Crypto, Convert,
  VIP Loan and Margin" is not a listing -- the asset is already trading and is
  merely reaching another product. A bare `will add` pattern produced false
  listing events for long-listed assets (AERO). The classifier now excludes
  product-addition titles while exempting genuine
  "USD-Margined ... Perpetual Contract" launches, which the word "margin" would
  otherwise catch.

---

## 2026-09-10 — libsql-client discards the server's error message

**Observed:** any statement Turso rejects surfaces in Python as a bare
`KeyError('result')`. No SQL, no server message, no constraint name.

**Cause:** `libsql_client/http.py:64` reads `response["result"]`
unconditionally. When the server answers with an `error` key instead, the
lookup raises `KeyError` and the error payload is thrown away.

**Why it matters here:** the whole point of the JSON logs is that an unattended
3am failure is diagnosable afterwards. `KeyError: 'result'` in a collector log
identifies neither the table nor the constraint, and this is the production
backend.

**What the code does now:** `src/db/connection.py` wraps every libSQL call and
converts that specific `KeyError` into `TursoStatementError`, which carries the
failing SQL text. Parameters are deliberately excluded — they are market data
here, but "never log the values" is an easier rule to keep than one with
exceptions.

**Also corrected:** `src/ops/migrate.py --verify` no longer infers enforcement
from the error message, because there is no message to read. It compares row
counts before and after.


---

## 2026-09-13 — GoPlus `token_security` (R1)

- **Document said** (the R1 source brief): exclude burn, CEX and locked holders by
  their `tag`; remove LP holdings using `lp_holders`; GoPlus is 30 calls/min.
- **Observed** (live, AAVE/CAKE/AERO/JUP):
  1. `tag` was an empty string on all 40 top holders pulled, including the
     CAKE burn address holding 92.6% of supply.
  2. `lp_holders` are holders of the LP token, not the pool addresses that
     appear among the token's holders. The pools are in `dex[].pair`.
  3. Uniswap V4 entries in `dex[]` carry a 32-byte pool id as `pair`, not an
     address; V4 liquidity sits in the singleton PoolManager.
  4. `contract_addresses=a,b` returned a result for one address only.
  5. Solana returns `token_account` (not owner) and `dex[]` entries with every
     field null.
  6. No rate-limit headers on any response. Rate limiting arrives as **HTTP 200
     with `code: 4029`** in the body, so an HTTP-status retry never sees it. The
     first live run, paced at 25/min, lost 136 of 315 reads this way.
  7. The keyless ceiling is well under the documented 30/min, and even spacing
     does not remove it (2026-09-14, evenly spaced probes, no backoff):

     | Spaced rate | Calls | Code 4029 | Successful reads/min |
     |---|---:|---:|---:|
     | 30/min | 90 | 40 | 16.9 |
     | 25/min | 75 | 29 | 15.5 |
     | 20/min | 60 | 17 | 14.6 |
     | 13/min | 72 | 6 | 12.1 |
     | 12/min | 72 | 4 | 11.5 |

     A refused call costs only its slot: faster sending still lands more reads.
     The 20/min burst run before this (27 min 43 s, 11.4 reads/min) lost most of
     its time to 48 backoff waits of 15-60 s, arriving every ~12 calls, ~60 s apart.
     A live run spaced at 25/min with a 3 s doubling wait took 26 min 31 s (402
     calls, 87 refused, 0 lost): refusals cluster, so pacing alone gains ~4%.
- **Code now does:** excludes by burn address, `is_locked`, `dex[].pair`, a
  curated file, and Blockscout implementation names (`src/collectors/holders.py`);
  one contract per call, spaced evenly at 25/min (`rate_limit_spacing`), retrying
  code 4029 after a short doubling wait (3 s, 6 retries); Solana flagged
  `token_accounts`.
- **Impact:** a tag-based filter would have excluded nothing, and AERO would have
  failed L1 at a raw 67% with half its supply in a vote escrow.

## 2026-09-13 — DefiLlama emissions (R4)

- **Document said:** `api.llama.fi/emission/{protocol}` is reachable
  unauthenticated, and `DefiLlama/emissions-adapters` is an open-source backstop.
- **Observed:** `api.llama.fi/emissions` and `/emission/aptos` return **402**.
  `github.com/DefiLlama/emissions-adapters` returns **404** (so do the obvious
  renames); the only recent copy has no license. The public host
  `defillama-datasets.llama.fi` returns **200** for `/emissionsProtocolsList` and
  `/emissions/{slug}`, with `metadata.unlockEvents[].cliffAllocations[]` and
  `linearAllocations[]` (`recipient`, `category`, `amount` or
  `previousRatePerWeek`/`newRatePerWeek`/`endTimestamp`). A category not in the
  brief exists: `Uncategorized`. Some protocols have `gecko_id: null` but a
  `metadata.token` of `<platform>:<address>`.
- **Code now does:** reads the datasets host (`src/collectors/unlocks.py`), maps
  `category` through `settings.unlocks.category_map`, and resolves missing gecko
  ids through `asset_contract.platforms_json`.
- **Impact:** R4 resolved at no cost; the proposed backstop does not exist.

## 2026-09-13 — Solana public RPC

- **Document said:** `getTokenLargestAccounts` on the public RPC is a free,
  "unlimited-ish" Solana cross-check.
- **Observed:** HTTP 429 "Too many requests for a specific RPC call" on the first
  `getTokenLargestAccounts` call; `getTokenSupply` succeeded.
- **Code now does:** does not use it. Solana concentration comes from GoPlus alone
  and is flagged approximate.

## 2026-09-13 — CoinGecko `/asset_platforms` and `/coins/list`

- **Observed:** `chain_identifier` is the EVM chain id, and null for Solana, Tron,
  Sui and TON. `native_coin_id` is `ethereum` for Base, Arbitrum, Optimism, Linea,
  zkSync, Scroll and Blast. In `/coins/list?include_platform=true` the first
  platform listed matched `/coins/{id}` `asset_platform_id` for all 48 multi-platform
  coins looked up in the first live run (2026-09-13). The ordering is still
  undocumented, so the guess stays provisional until a lookup confirms it. The
  free tier answered 85 of 136 calls in that run with 429; retries recovered all
  but 11 origin lookups.
- **Code now does:** derives GoPlus chains from `chain_identifier`; treats the
  first-listed platform as provisional only, confirmed by `asset_platform_id` in
  bounded batches (`src/collectors/contracts.py`).

## 2026-09-13 — Blockscout `/api/v2/addresses/{address}`

- **Observed:** proxies return their proxy contract as `name`
  (`InitializableImmutableAdminUpgradeabilityProxy`, `SafeProxy`) and the logic
  contract in `implementations[].name` (`ATokenWithDelegationInstance`, `Safe`).
  Hosts: eth, base, arbitrum, polygon, zksync answer 200; optimism, scroll and
  gnosis answer 301; `explorer.linea.build` and `bsc.blockscout.com` are 404.
- **Code now does:** matches the implementation name first, then the contract
  name; chains without a host are flagged `names_unavailable`.
