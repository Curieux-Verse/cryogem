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
