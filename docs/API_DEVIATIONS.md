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

