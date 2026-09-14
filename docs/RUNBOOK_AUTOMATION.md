# Runbook — making the system autonomous

**Do this in week 4. It is the deadline that matters.** Until it is done the
collector only runs when your laptop is open, and Phase 1's whole premise is
that a missed day is gone forever. Everything after this point is an
improvement to something already running.

Order matters: cron-job.org is set up **before** the first workflow is trusted,
so the first dispatch can be tested where the response code is visible rather
than debugged from a silent 3am failure.

---

## Step 1 — Create the GitHub repo (public)

**Make it public.** Actions minutes are unlimited on public repos. A private
repo on the Free plan gets 2,000 Linux minutes/month, and the hourly collector
alone is roughly 24 x 3 min x 30 = 2,160 — over budget before the daily screen,
the journal or any site build.

What becomes public: every screener output, every journal entry, every forward
return. API keys do **not** — they live in Actions secrets, never in the repo.

The journal being public is arguably the strongest feature of the project: a
timestamped, append-only, unfilterable track record. That is precisely what no
signal channel publishes.

```bash
gh repo create gem-screener --public --source=. --remote=origin --push
```

If you insist on privacy: private data repo on a daily-only cadence
(~150 min/month) plus a separate public repo holding only the built site.
Record the choice in DECISIONS.md.

---

## Step 2 — Create the fine-grained PAT

GitHub → Settings → Developer settings → Fine-grained tokens.

| Setting | Value |
|---|---|
| Repository access | **Only select repositories** → this repo only |
| Actions | **Read and write** |
| Contents | **Read and write** |
| Metadata | Read (auto-selected) |
| Expiry | **90 days** |

- `GITHUB_TOKEN` **cannot** fire `workflow_dispatch`. GitHub blocks it to
  prevent recursive loops. A PAT is mandatory.
- A `403 "Resource not accessible by personal access token"` is almost always
  the missing **Contents** permission.
- **Write the expiry date in `docs/DECISIONS.md` and set a calendar reminder
  for 7 days before.** When a PAT expires every job 401s and collection stops
  silently. That is how this system dies in month 8.

**Say this out loud before pasting it anywhere:** cron-job.org will hold a
credential that can write to your repo. Fine-grained + single repo + 90 days is
what makes that an acceptable trade.

---

## Step 3 — Add repository secrets

Settings → Secrets and variables → Actions → New repository secret.

| Secret | Required? | Notes |
|---|---|---|
| `TURSO_DATABASE_URL` | yes | from `turso db show gem-screener --url` |
| `TURSO_AUTH_TOKEN` | yes | from `turso db tokens create gem-screener` |
| `COINGECKO_API_KEY` | no | free tier works without; 429s less with one |
| `COINALYZE_API_KEY` | no | without it, L1 check 9 records data_unavailable |
| `LUNARCRUSH_API_KEY` | no | attention block scores None without it |
| `CRYPTOPANIC_AUTH_TOKEN` | no | news is journal-labelling only |
| `HEALTHCHECK_DAILY` | recommended | UUID from healthchecks.io |
| `HEALTHCHECK_HOURLY` | recommended | separate check |
| `HEALTHCHECK_JOURNAL` | recommended | separate check |
| `HEALTHCHECK_SCREEN` | recommended | separate check |
| `HEALTHCHECK_SUPPLY` | recommended | separate check, for `collect-supply` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | no | notification only |

---

## Step 4 — Turso

```bash
turso db create gem-screener
turso db show gem-screener --url          # -> TURSO_DATABASE_URL
turso db tokens create gem-screener       # -> TURSO_AUTH_TOKEN
```

Then apply the schema once, and **confirm every index exists before the first
production run**. Turso meters ROW READS; an unindexed cross-sectional scan
over `derivatives_snapshot` burns the free allowance far faster than expected.

```bash
TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... python -m src.cli init-db
```

Open research question R9: verify the full DDL — including the three
append-only triggers — applies on libSQL. If a trigger is rejected, that is a
finding for `API_DEVIATIONS.md`, and the append-only guarantee must then be
enforced in `writes.py` instead. Do not proceed without one or the other.

---

## Step 5 — cron-job.org

Sign up (free). Create **one job per externally-triggered workflow**, all the same shape:

```
Method    POST
URL       https://api.github.com/repos/Curieux-Verse/cryogem/actions/workflows/{FILE}/dispatches
Headers   Accept: application/vnd.github+json
          Authorization: Bearer github_pat_xxxxxxxx
          X-GitHub-Api-Version: 2022-11-28
          Content-Type: application/json
Notify    ON FAILURE  <- enable for every job
```

**Only four jobs.** `screen`, `publish` and `build-site` are not triggered from
outside: `screen` chains off `collect-daily` via `workflow_run`, `publish` off
`screen`, and `build-site` off `publish`. Each link is guarded on the previous
job's success, so a failed collection never produces a screen and a failed
screen is never published.

| Job | Schedule (UTC) | Workflow file | Body |
|---|---|---|---|
| collect-daily | `10 3 * * *` | `collect-daily.yml` | `{"ref":"main","inputs":{"scheduled_for":"03:10"}}` |
| collect-hourly | `25 * * * *` | `collect-hourly.yml` | `{"ref":"main","inputs":{"scheduled_for":":25"}}` |
| journal | `10 4 * * *` | `journal.yml` | `{"ref":"main","inputs":{"scheduled_for":"04:10"}}` |
| backup | `0 5 * * 0` | `backup.yml` | `{"ref":"main"}` |

`scheduled_for` is what makes the trigger-lag panel on the Health page work,
and that panel is how a stray `on: schedule` is caught after it is merged.

**It is the cron SLOT, not a timestamp.** cron-job.org's body is a fixed string
with no templating, so a pasted ISO instant would be right on the day you paste
it and one day more wrong every day after. `"03:10"` means "the most recent
03:10 UTC"; `":25"` means "the most recent :25 past the hour". See D-031.

**Test each one with "Execute now" before trusting the schedule.**

| Code | Meaning |
|---|---|
| `204` | Success, workflow queued. This is what you want. |
| `401` | Token expired or malformed. The most likely cause of a system that stops in month 4. |
| `403` | Almost always the missing **Contents** permission. |
| `404` | Wrong owner/repo/filename — **or the workflow file is not yet on the default branch.** A `workflow_dispatch` workflow cannot be dispatched until it is merged to `main`. This catches everyone once. |
| `422` | Bad `ref`, or the workflow lacks a `workflow_dispatch` trigger. |

The free tier's 30-second job timeout does not matter: the dispatch endpoint
returns 204 immediately and does not wait for the workflow.

---

## Step 6 — healthchecks.io (the other half of the monitoring)

These two are **not** redundant. They catch opposite halves of the failure
space, and having only one leaves a blind spot big enough to lose weeks in:

| Failure | cron-job.org sees it | healthchecks.io sees it |
|---|---|---|
| Trigger never fired | **yes** | yes (missed ping) |
| PAT expired to 401 | **yes** | yes |
| Wrong workflow filename to 404 | **yes** | yes |
| Workflow queued, then the job failed | **no** — it already got its 204 | **yes** |
| Job hung until `timeout-minutes` | no | **yes** |

Create one check per pinging workflow — `collect-daily`, `collect-hourly`,
`journal` and `screen`. `screen` is on the list even though nothing triggers it
directly: it is the job that turns collected data into a published run, so it
is exactly where a silent chain break would show up.

| Check | Secret | Period | Grace |
|---|---|---|---|
| collect-daily | `HEALTHCHECK_DAILY` | **25 hours** | 1 hour |
| collect-hourly | `HEALTHCHECK_HOURLY` | 90 minutes | 30 minutes |
| journal | `HEALTHCHECK_JOURNAL` | **25 hours** | 1 hour |
| screen | `HEALTHCHECK_SCREEN` | **25 hours** | 1 hour |
| collect-supply | `HEALTHCHECK_SUPPLY` | **25 hours** -- it must run daily (D-042) | 1 hour |

`collect-supply` (holders and unlocks, D-040) is dispatched on its own, like the
collectors: at 04:10 UTC, after `collect-daily` has written the day's market data it
reads. It must run **daily**: a holder reading counts for one day only (D-042), so a
skipped day switches L1_HOLDER_CONC off the next morning. Every run re-reads every
measurable token (~32 min at GoPlus's free-tier pace); the first run also maps ~370
DefiLlama protocols.

**The daily periods are 25 hours, not 24**, so runner-provisioning drift does
not cry wolf while a real one-day gap still alerts.

Every workflow pings **only on success** (`if: success()`). Pinging on failure
would tell healthchecks.io the job ran, which is the one thing cron-job.org
already knows — and would blind the only monitor that can see a job that
started and then died.

---

## Step 7 — Prove it, with two deliberate break tests

Do not skip this. If only one alert fires, the two layers are not independent
and you have one monitor, not two.

1. **Break the PAT.** Change one character in the cron-job.org Authorization
   header. Confirm a failure email arrives. Restore it.
2. **Break a job.** Push a commit that makes `collect-daily` exit non-zero
   (e.g. a bad collector name). Confirm the **healthchecks.io** alert arrives
   even though cron-job.org reports success — it got its 204. Revert.

**Then leave it alone for 14 days.** The bar is **at least 13 of 14 successful
daily runs**. Punctual external triggering should make near-perfect delivery
normal; if it is not, better to find out now.

---

## Step 8 — Verify the guards are live

```bash
grep -rnE '^[[:space:]]*schedule:' .github/workflows/     # must print NOTHING
python -m src.cli doctor                                   # must exit 0
```

The CI workflow enforces both on every push. If a `schedule:` trigger ever
reappears, `health.json`'s trigger-lag p95 will climb from seconds into tens of
minutes — that is the symptom to watch for.

---

## The failure register for this layer

| Failure | Symptom | Guard |
|---|---|---|
| PAT expiry | Everything 401s months in | 90-day token, expiry in DECISIONS.md, reminder 7 days out |
| Silent cron failure | Two weeks of missing data, noticed by accident | Two independent alerts (step 6) |
| A stray `on: schedule` | Drift returns invisibly | CI guard + trigger-lag p95 on the Health page |
| Actions ToS breach | Actions restricted account-wide | Keep Actions at daily/hourly. 5-min goes on your own host |
| Cron-slot vs real-run time | Every downstream time calculation quietly wrong | `fetched_at_utc` is always the ACTUAL run time |
| Stale dashboard | Confident page on 3-day-old prices | `doctor` gate before screen; age banner over 26h |
| Turso row-read blowout | Free tier exhausted mid-month | Index every query pattern; never `SELECT COUNT(*)` on time-series tables |
