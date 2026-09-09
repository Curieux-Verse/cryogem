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

- p50: `____` · p95: `____` · n: `____` · measured on: `____`

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
