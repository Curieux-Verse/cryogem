# Crypto Gem Discovery System — Full Implementation Specification

**Target implementer:** Claude Opus (Claude Code, agentic mode)
**Target user:** first-year CSE student, beginner Python, learning as the system is built
**Document status:** complete and executable. No phase requires further research before starting.

---

## 0. INSTRUCTIONS FOR THE IMPLEMENTING AGENT

Read this section fully before writing any code.

### 0.1 Your operating rules

1. **Build phases in order.** Each phase has explicit acceptance criteria. Do not start phase N+1
   until phase N's criteria all pass. Report which criteria passed and which did not.
2. **The user is a beginner.** For every module you write, add a `# WHY:` comment block at the top
   explaining what the module does and why it exists in the pipeline. Prefer explicit, readable
   code over clever code. Avoid one-liners that compress three ideas.
3. **Never write trading execution code.** No order placement, no exchange API keys with trade
   permissions, no private endpoints. This system is read-only. If the user asks for execution,
   decline and explain that execution belongs in a separate, deliberately-created repo after the
   research system has produced evidence. Use only public market-data endpoints.
4. **Never invent thresholds.** Every numeric threshold in this document is either sourced or
   explicitly marked `TUNE_ME`. If you need a new threshold, mark it `TUNE_ME` and add it to
   `config/thresholds.yaml`. Never hardcode a number in a module.
5. **Fail loudly on data problems, silently on nothing.** A missing field must raise or log at
   ERROR. Silent `None` propagation is the primary failure mode of systems like this.
6. **Timestamp everything at ingest.** Every row gets `fetched_at_utc`. This is what makes
   point-in-time backtesting possible later. There is no way to add it retroactively.
7. **Ask before adding a paid dependency.** Default to free tiers throughout.
8. **When an API's real behavior contradicts this document, trust the API and update the document.**
   Append a note to `docs/API_DEVIATIONS.md` describing what differed.
9. **Secrets never leave GitHub Actions secrets.** Not into the repo, not into `data/public/`, not
   into the browser bundle, not into logs. The repo is public (see §1.4.4). Before any deploy,
   grep the built site for key-like strings.
10. **Collectors are stateless functions, never loops.** They take a timestamp and write rows. A
    `while True` inside a collector makes it unrunnable in CI and is the single most common way
    this class of project fails to become autonomous. See §6.0.

### 0.2 What to do if you are not satisfied with the research

Section 15 lists open research questions with specific search queries. If you hit one while
implementing, resolve it before proceeding and record the answer in `docs/RESEARCH_LOG.md`.

### 0.3 Definition of done for the whole project

The system is done when it can answer this question with the user's own recorded data:

> "Of every coin my screener ranked in the top 15 over the last six months, what was the
> distribution of 30-day forward returns relative to BTC, and did it beat a random selection
> from the same surviving universe?"

Everything in this spec exists to make that question answerable. Nothing else is the goal.

Two secondary conditions, both of which serve the first:
- The system produces that answer **without the user doing anything** — collection, screening,
  journalling, and publishing all run on schedule, unattended.
- The answer is **published**, losses included, at a public URL that anyone can check.

---

## 1. SYSTEM OVERVIEW

### 1.1 The thesis

Derived from seven case studies (TRB Dec 2023, RAVE Apr 2026, VVV Dec 2025–Sep 2026, AERO, FORM,
the CEX-listing dataset, the token-unlock dataset):

> **The data that disqualifies a bad candidate is available before the move, and it is almost
> never chart data.**

The system is therefore **disqualification-first**. It does not search for winners. It removes
losers and reports what survives.

### 1.2 Pipeline

```
                    ┌──────────────────────────┐
                    │  COLLECTORS (Phase 1-3)  │
                    │  run continuously        │
                    └──────────┬───────────────┘
                               │ writes to SQLite
                    ┌──────────▼───────────────┐
                    │  L1: KILL SWITCH         │  binary PASS/FAIL
                    │  supply integrity        │  ~500 perps → ~150
                    └──────────┬───────────────┘
                               │ survivors only
                    ┌──────────▼───────────────┐
                    │  L2: DEMAND SCORE        │  0-100, cross-sectional
                    │  fundamentals, supply,   │  ~150 → top 15
                    │  sector, events, attn    │
                    └──────────┬───────────────┘
                               │ top decile only
                    ┌──────────▼───────────────┐
                    │  L3: TIMING + RISK       │  structure + positioning
                    │  ICT structure, OI/funding│  advisory, not automatic
                    └──────────┬───────────────┘
                               │ every candidate logged
                    ┌──────────▼───────────────┐
                    │  JOURNAL (Phase 6)       │  forward returns, forever
                    │  the only source of truth│
                    └──────────────────────────┘
```

**Design decision to preserve:** derivatives metrics (OI, funding, CVD) live in L1 as *kill
switches* and L3 as *risk checks*. They are never a buy trigger. Rationale: in the TRB case a
sophisticated on-chain account publicly read negative funding as bullish hours before a −78%
collapse. These metrics identify fragility, not direction.

### 1.3 The news/sentiment layer — three tiers

This is the part most likely to be built wrong. Build it in this shape:

| Tier | What | Where it goes | Value |
|---|---|---|---|
| 1 | **Scheduled event calendar** — unlocks, emissions changes, mainnet, governance votes | L1 kill switch + L2 score | **Highest.** No latency race. Unlock price impact begins ~30 days pre-event |
| 2 | **Attention as cross-sectional factor** — social volume z-score, social dominance, Google Trends | L2 score, weight 15 | Medium. Documented as a forecasting signal, strongest at sentiment extremes |
| 3 | **Real-time headline ingest** | Journal labelling only. **NOT a trigger** | Low for trading, high for research |

**Why Tier 3 is not a trigger:** Telegram adds ≥150ms, Twitter is minutes late, Binance's own
announcement API has shown 15s+ country-dependent lag, and asset prices on other venues can move
20–100% within seconds of a listing notice. Commercial low-latency WebSocket services exist for
exactly this race. A polling collector will always lose it.

**What Tier 3 is for:** every journal entry gets a `news_context` field listing headlines about
that ticker in the ±24h window. After six months this answers whether the system's signals lead
or lag news — a question with real strategic value that costs nothing extra to answer.

### 1.4 Deployment architecture — autonomous on GitHub

**Target:** the whole system runs itself on GitHub Actions and publishes a dashboard to GitHub
Pages. No laptop, no manual step, no server bill.

This is achievable for the core system, but one constraint decides the entire design and must be
understood before any workflow is written.

#### 1.4.1 Scheduling: external cron, never `on: schedule`

**Rule: no workflow in this project uses `on: schedule`. Every workflow is `workflow_dispatch`
only, fired externally by cron-job.org over the GitHub REST API.**

**Why GitHub's own cron is not used.** The `schedule` event is documented as best-effort, not a
guarantee. Observed reality across many reports: runs fire 5–45 minutes after the scheduled slot
in normal conditions, delays of 8–14 hours occur, days get dropped entirely during high-load
windows, and there have been platform-wide periods where scheduled runs stopped being created at
all while `workflow_dispatch` kept working throughout. Self-hosted runners do not help — the
queuing logic sits on GitHub's side, not the runner's.

**Why cron-job.org fixes it.** It executes jobs at frequencies up to once per minute, lets you
configure the request method, headers and body, keeps the last 50 executions with scheduled time,
actual execution time and response data, and emails you when a job starts failing. Free. Reported
delay is typically under a minute against GitHub's 5–45.

**Three things this buys beyond punctuality:**

1. **The 60-day inactivity auto-disable stops applying.** That rule disables *scheduled*
   workflows. With no `schedule:` trigger anywhere in the repo there is nothing to disable, and
   `publish.yml` commits daily regardless. **The keepalive workflow from the earlier design is no
   longer needed** — drop it.
2. **Recovery is a button.** During a GitHub scheduler outage, `workflow_dispatch` continues to
   work. The system is already on the path that survives.
3. **You get an independent record of intended vs actual fire time**, from a party that is not
   GitHub. That is what makes the cron-lag metric on the Health page trustworthy.

**Free-tier limits, and why none of them bite here:**

| Limit | Value | Impact |
|---|---|---|
| Minimum interval | 1 minute | Far below anything needed |
| Job timeout | 30 seconds | **Fine** — the dispatch endpoint returns `204 No Content` immediately and does not wait for the workflow |
| Response size | 64 KB | A 204 has no body |
| Execution history | Last 50 runs; bodies kept 2 days | Enough to debug; the Health page keeps the long record |
| Management API calls | 100/day | Only relevant if managing jobs programmatically, which we don't |

**Two honest caveats.** cron-job.org gives no punctuality guarantee and no SLA — it is a
community service. It also states it may deliberately delay a job that has failed repeatedly or
historically run long. Neither matters much when the request is a 204-returning webhook, but it
means cron-job.org must be monitored too, not assumed.

#### 1.4.1a The trigger call

```
POST https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/collect-daily.yml/dispatches

Accept:               application/vnd.github+json
Authorization:        Bearer github_pat_xxxxxxxx
X-GitHub-Api-Version: 2022-11-28
Content-Type:         application/json

{"ref": "main"}

→ 204 No Content on success
```

`workflow_id` may be the workflow **filename**, which is far more stable than the numeric ID.

**Token requirements — this is where people lose an afternoon:**

- `GITHUB_TOKEN` **cannot** fire `workflow_dispatch` or `repository_dispatch`. GitHub blocks it
  to prevent recursive workflow loops. A PAT or GitHub App token is mandatory.
- Use a **fine-grained PAT**, repository access set to **only this repo**, with
  **Actions: read and write**, **Contents: read and write**, and **Metadata: read** (auto-selected).
- A `403 "Resource not accessible by personal access token"` is almost always a missing
  **Contents** permission, or the token's owner lacking write access to the repo. Multiple
  reports converge on Contents being the one people omit.
- **Fine-grained PATs expire.** When the token expires, every job silently 401s and the entire
  system stops collecting — and cron-job.org's failure email is the only thing that will tell you.
  Set a calendar reminder for one week before expiry, and record the expiry date in
  `docs/DECISIONS.md`.

**Security — state this to the user before they paste anything.** Putting a PAT in a third
party's job configuration means cron-job.org holds a credential that can write to the repo. It is
an acceptable trade for a public repo holding no secrets in its content, but only with the
scoping above. Two hardening options, in order of effort:

1. **`repository_dispatch` instead** — `POST /repos/{owner}/{repo}/dispatches` with
   `{"event_type": "collect-daily"}`. One endpoint for everything, routed inside the workflows by
   `types:`. Needs only Contents: read & write. Slightly smaller blast radius, slightly more
   indirection.
2. **A GitHub App token** scoped to `actions: write` on one repo. Smallest possible permission,
   but requires an installation token exchange that a static cron request cannot perform on its
   own. Skip this unless the user specifically wants it.

Default to `workflow_dispatch` — one cron-job entry per workflow is easier to read and to debug.

#### 1.4.2 Cadence tiers

With scheduling solved, the binding constraint is no longer *can* Actions fire on time. It is the
Actions Terms of Service (§1.4.2a). Split accordingly:

> **Layer 1 and Layer 2 are entirely daily computations. Only Layer 3's intraday positioning
> checks need sub-hourly data.**

| Tier | Cadence | Runs on | Runs/day | Feeds | Required? |
|---|---|---|---|---|---|
| **C** | Daily | Actions ← cron-job.org | ~4 | Everything in L1, L2, journal, dashboard | **Required** |
| **B** | Hourly | Actions ← cron-job.org | 24 | Derivatives series, funding persistence | Recommended |
| **B+** | 15 min | Actions ← cron-job.org | 96 | Finer OI series | Only with §1.4.2a read |
| **A** | 5 min | Persistent host | 288 | L3 intraday, CVD, depth | Host only — not Actions |

Build **C first**. It is the entire product. Add B in week 4.

#### 1.4.2a Why Tier A stays off GitHub Actions

cron-job.org *can* trigger every 5 minutes. GitHub Actions *can* technically absorb 288
dispatches a day on a public repo, where minutes are unlimited. The reason not to is the Terms of
Service, which state that Actions may not be used for cryptomining, **serverless computing**,
unauthorised access, commercial Actions offerings, or any other activity unrelated to the
production, testing, deployment, or publication of the software project associated with the
repository — and that GitHub may monitor usage, with misuse resulting in terminated jobs,
restricted Actions access, or disabled repositories.

Where this lands:

- **Daily and hourly (≈28 runs/day)**: comfortably fine. The workflows build and publish the
  repository's own software project — a dashboard — which is squarely "publication of the
  software project."
- **15-minute (96/day)**: defensible, but each run spends more time on runner provisioning,
  checkout, and dependency install than on actual work. Wasteful even when free. If chosen,
  document the reasoning in `DECISIONS.md`.
- **5-minute (288/day)**: this is using Actions as a general compute platform on a continuous
  schedule. That is the "serverless computing" clause. **Do not do it.** The downside is not a
  fine, it is losing Actions on the account that runs everything else.

So Tier A remains host-only, and remains optional. Skipping it costs the intraday positioning
layer and nothing else.

#### 1.4.3 Persistent host options for Tier A (all optional)

The 2026 free-hosting landscape has narrowed considerably, so do not assume old advice holds:

| Option | Never sleeps | Card required | Notes |
|---|---|---|---|
| Oracle Cloud Always Free ARM VM | Yes | Yes | Best free option. Account creation is sometimes denied, and Oracle has reclaimed idle instances — keep it busy |
| Raspberry Pi / spare laptop | Yes | No | Most reliable for a student. Needs stable power and network |
| Render free web service | **No** | No | Spins down after 15 min idle, ~1 min restart. Unusable for a fixed-interval collector |
| Fly.io | n/a | Yes | No general free tier for new accounts as of 2026 — trial only |
| Railway | n/a | Yes | Monthly credit is a trial, not a free tier |
| PythonAnywhere free | Partial | No | Free tier scheduling is too limited for 5-minute work |

If none is available, Tier A is skipped. The system still works.

#### 1.4.4 Repository visibility — a real decision, not a detail

**GitHub Actions is unlimited and free on public repositories. Private repos on the Free plan get
2,000 Linux minutes and 500 MB of artifact storage per month.**

Budget check for a private repo: an hourly collector at ~3 min/run is 24 × 3 × 30 ≈ 2,160
minutes/month. That alone exceeds the entire free allowance before the daily screen, the journal
job, or any site build. GitHub also rounds each job up to the nearest minute, so many small jobs
waste more than they appear to.

**Therefore: make the repo public.** Consequences to state plainly to the user before proceeding:

- Every screener output, every journal entry, and every forward return becomes publicly visible.
- API keys stay safe — they live in GitHub Actions **secrets**, never in the repo.
- The journal being public is arguably the strongest feature of the whole project. It is a
  timestamped, append-only, unfilterable public track record. That is precisely what no signal
  channel publishes, and precisely what §17's audit exists to reconstruct for other people. Being
  able to point at it is worth more than the privacy given up.
- GitHub Pages on a free personal account requires a public repository anyway.

If the user insists on privacy, the fallback is: private data repo on a daily-only cadence
(~150 min/month, comfortably inside the allowance) plus a separate public repo holding only the
built site. Document the choice in `DECISIONS.md`.

#### 1.4.5 Where the data lives

Do **not** commit a growing SQLite file to the repo. Git history retains every version forever,
so a database that changes daily will bloat the repo permanently even after deletion, and GitHub
Pages source repos carry a recommended 1 GB limit.

```
Time-series store   Turso (managed libSQL) — SQLite-compatible, HTTP access, works from
                    Actions runners with no persistent filesystem. Free tier is generous;
                    VERIFY CURRENT LIMITS AT IMPLEMENTATION TIME (published figures vary
                    between 1 GB / 3 databases and 5 GB / 500 databases depending on source
                    and date).

                    ⚠ Turso meters ROW READS, not requests. A screener doing full-table
                    scans for cross-sectional ranking can burn through the allowance in ways
                    that surprise people. Index every query pattern, and never run
                    SELECT COUNT(*) over the full derivatives table.

Site data           Small JSON files committed to the repo under data/public/. Only the
                    latest screen, rolling journal statistics, and a 90-day history window.
                    Prune older JSON into one compressed archive monthly.

Backups             Weekly Turso dump → GitHub Actions artifact + a release asset.
                    Artifacts are not permanent storage; releases are.
```

#### 1.4.6 Full autonomous topology

```
   ┌──── cron-job.org (free) ────┐
   │  daily-collect   03:10 UTC  │
   │  hourly-collect  :25         │   POST .../workflows/<file>/dispatches
   │  daily-journal   04:10 UTC  │   Authorization: Bearer <fine-grained PAT>
   │  weekly-backup   Sun 05:00  │   Body: {"ref":"main"}   → 204
   └──────────────┬──────────────┘
                  │  (emails you when a job stops returning 204)
                  ▼
   ┌──────────────────────── GitHub (public repo) ───────────────────────┐
   │                                                                      │
   │  .github/workflows/   — every one is `on: workflow_dispatch` ONLY    │
   │   ├── collect-daily.yml   ◄── cron-job.org                           │
   │   ├── collect-hourly.yml  ◄── cron-job.org                           │
   │   ├── journal.yml         ◄── cron-job.org                           │
   │   ├── backup.yml          ◄── cron-job.org                           │
   │   ├── screen.yml          ◄── workflow_run: collect-daily            │
   │   ├── publish.yml         ◄── workflow_run: screen                   │
   │   │      └─► writes data/public/*.json, commits (no-op if unchanged) │
   │   └── build-site.yml      ◄── push: data/public/**, web/**           │
   │          └─► vite build ──► actions/deploy-pages                     │
   │                                                                      │
   │  NO `on: schedule` anywhere. NO keepalive workflow needed.           │
   │                                                                      │
   │  Secrets: TURSO_*, COINGECKO_*, LUNARCRUSH_*, CRYPTOPANIC_*,         │
   │           HEALTHCHECK_*, TELEGRAM_*                                  │
   └────────────────────────────────┬─────────────────────────────────────┘
                                    │
                ┌───────────────────▼──────────────────┐
                │  GitHub Pages — static React SPA     │
                │  reads data/public/*.json            │
                │  NO API keys, NO server calls        │
                └──────────────────────────────────────┘

   Optional Tier A: persistent host runs the 5-min collector, writes to the same
   Turso database over HTTP. Entirely decoupled — if it dies, everything else works.

   TWO-LAYER MONITORING, because they catch different failures:
     cron-job.org alert  → the trigger did not fire, or GitHub rejected it (401/403/404)
     healthchecks.io     → the trigger fired, the workflow started, and then the job
                           failed or hung. A 204 says nothing about what happened next.
   Set the healthchecks period to 25h for a daily job.
```

---

## 2. TECH STACK AND REPO STRUCTURE

### 2.1 Stack

**Backend / pipeline**
```
Python           3.11+
Database         libsql-client (Turso) in CI; plain sqlite3 for local dev.
                 Write one DB adapter so both work — src/db/connection.py picks by env var.
HTTP             httpx (async support, better retries than requests)
Retry            tenacity
Rate limiting    pyrate-limiter
Scheduling       APScheduler for the optional Tier-A daemon only. Tier B/C use Actions cron.
Data             pandas, numpy
Validation       pydantic v2
Config           pydantic-settings + YAML
Logging          structlog (JSON to file, human-readable to console)
Testing          pytest, pytest-asyncio, responses (HTTP mocking)
CLI              typer
Sentiment (P3)   transformers + a finance-tuned model, CPU-only. Optional; rule-based fallback.
```

**Frontend (Phase 10)**
```
Build            Vite
Framework        React 18 + TypeScript
Styling          Tailwind CSS
Charts           Recharts (composable, small, good defaults for this data)
Tables           TanStack Table (sorting/filtering on the screen view)
Routing          React Router with HashRouter — GitHub Pages has no server-side rewrite,
                 so BrowserRouter breaks on refresh of any deep link. Use HashRouter.
Data             Static fetch of data/public/*.json. No API client, no keys in the browser.
Deploy           actions/upload-pages-artifact + actions/deploy-pages
```

**Infrastructure**
```
CI/CD            GitHub Actions
Hosting          GitHub Pages (static)
Store            Turso (managed libSQL)
Monitoring       healthchecks.io free tier (dead-man's switch)
Alerts           Telegram bot (optional, notification only)
```

Pin all versions in `requirements.txt` and `web/package-lock.json`. Use a venv for Python.

### 2.2 Repo layout

```
gem-screener/
├── README.md
├── requirements.txt
├── .env.example                  # API keys — NEVER commit .env
├── .gitignore                    # must include .env, *.db, data/, logs/
├── config/
│   ├── settings.yaml             # non-secret config
│   ├── thresholds.yaml           # ALL numeric thresholds live here
│   └── sectors.yaml              # ticker → sector mapping
├── src/
│   ├── __init__.py
│   ├── config.py                 # loads settings + thresholds, validates
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.sql            # full DDL
│   │   ├── connection.py         # connection factory, WAL, foreign keys ON
│   │   └── writes.py             # idempotent upserts
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── base.py               # BaseCollector: retry, rate limit, logging
│   │   ├── binance.py            # Phase 1
│   │   ├── hyperliquid.py        # Phase 1
│   │   ├── coingecko.py          # Phase 1
│   │   ├── defillama.py          # Phase 1
│   │   ├── coinalyze.py          # Phase 1 (optional, needs free key)
│   │   ├── unlocks.py            # Phase 2
│   │   ├── announcements.py      # Phase 2
│   │   ├── attention.py          # Phase 3
│   │   └── news.py               # Phase 3
│   ├── screening/
│   │   ├── __init__.py
│   │   ├── layer1_kill.py        # Phase 4
│   │   ├── layer2_score.py       # Phase 5
│   │   └── layer3_structure.py   # Phase 7
│   ├── journal/
│   │   ├── __init__.py
│   │   └── forward_returns.py    # Phase 6
│   ├── backtest/
│   │   ├── __init__.py
│   │   ├── universe.py           # point-in-time universe reconstruction
│   │   └── harness.py            # Phase 8
│   ├── report/
│   │   ├── __init__.py
│   │   ├── daily.py              # Phase 9
│   │   └── telegram.py           # Phase 9
│   └── cli.py                    # typer entrypoint
├── scripts/
│   ├── run_collectors.py         # the daemon
│   └── backfill.py
├── tests/
│   ├── fixtures/                 # saved API responses for offline tests
│   ├── test_collectors.py
│   ├── test_layer1.py
│   ├── test_layer2.py
│   └── test_case_studies.py      # THE regression tests — see §11.4
├── docs/
│   ├── API_DEVIATIONS.md
│   ├── RESEARCH_LOG.md
│   └── DECISIONS.md              # ADR-style log of every design choice
├── data/
│   ├── gem.db                    # gitignored — local dev only
│   └── public/                   # COMMITTED. This is what the site reads.
│       ├── latest.json           # today's screen
│       ├── rejected.json         # L1 failures with reasons
│       ├── journal.json          # rolling forward-return statistics
│       ├── events.json           # 90-day catalyst calendar
│       ├── health.json           # collector uptime, completeness, news lag
│       ├── assets/<TICKER>.json  # per-asset detail
│       └── history/YYYY-MM.json.gz  # archived beyond 90 days
├── .github/
│   └── workflows/                # every one is `on: workflow_dispatch` only
│       ├── collect-daily.yml     # ← cron-job.org
│       ├── collect-hourly.yml    # ← cron-job.org
│       ├── journal.yml           # ← cron-job.org
│       ├── backup.yml            # ← cron-job.org
│       ├── screen.yml            # ← workflow_run(collect-daily)
│       ├── publish.yml           # ← workflow_run(screen)
│       └── build-site.yml        # ← push to data/public/** or web/**
└── web/                          # Phase 10 — the dashboard
    ├── package.json
    ├── vite.config.ts            # base: '/gem-screener/' for project Pages
    ├── tailwind.config.ts
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── lib/
        │   ├── data.ts           # typed fetch + cache of data/public/*.json
        │   └── format.ts         # number/percent/date formatting, one place
        ├── components/
        │   ├── ScoreBar.tsx
        │   ├── CheckGrid.tsx     # the 9 L1 checks, pass/fail
        │   ├── BlockRadar.tsx    # 6 L2 blocks
        │   ├── ReturnHistogram.tsx
        │   └── EventTimeline.tsx
        └── routes/
            ├── Screen.tsx        # today's survivors
            ├── Asset.tsx         # per-asset drill-down
            ├── Rejected.tsx      # the disqualification wall
            ├── Journal.tsx       # the receipts — most important page
            ├── Events.tsx        # catalyst calendar
            └── Health.tsx        # system status
```

---

## 3. DATABASE SCHEMA

Write this verbatim to `src/db/schema.sql`. Apply on first run; make it idempotent with
`CREATE TABLE IF NOT EXISTS`.

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- UNIVERSE: point-in-time symbol list. Critical for backtesting.
-- One row per (symbol, day). Never delete rows.
-- ============================================================
CREATE TABLE IF NOT EXISTS universe_snapshot (
    snapshot_date   TEXT NOT NULL,          -- YYYY-MM-DD
    exchange        TEXT NOT NULL,          -- 'binance' | 'hyperliquid'
    symbol          TEXT NOT NULL,          -- e.g. '1000PEPEUSDT'
    base_asset      TEXT NOT NULL,          -- normalised, prefix stripped: 'PEPE'
    quote_asset     TEXT NOT NULL,
    contract_type   TEXT,                   -- 'PERPETUAL'
    onboard_date    TEXT,                   -- ISO date the contract listed
    status          TEXT,                   -- 'TRADING' | 'SETTLING' | ...
    price_multiplier INTEGER DEFAULT 1,     -- 1000 for 1000PEPE contracts
    funding_interval_hours REAL,            -- 8, 4, or 1. MUST be fetched, not assumed.
    fetched_at_utc  TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, exchange, symbol)
);

-- ============================================================
-- DERIVATIVES: high-frequency snapshots (every 5 min)
-- ============================================================
CREATE TABLE IF NOT EXISTS derivatives_snapshot (
    ts_utc              TEXT NOT NULL,
    exchange            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    mark_price          REAL,
    index_price         REAL,
    open_interest_base  REAL,               -- IN CONTRACTS/COINS. Primary field.
    open_interest_usd   REAL,               -- derived; contaminated by price. Secondary.
    funding_rate        REAL,               -- raw rate for THIS symbol's interval
    funding_interval_hours REAL,            -- copied here so the row is self-describing
    funding_apr         REAL,               -- interval-normalised: rate * (8760/interval)
    next_funding_ts     TEXT,
    premium             REAL,               -- (mark - index) / index
    volume_24h_usd      REAL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (ts_utc, exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_deriv_symbol_ts ON derivatives_snapshot(symbol, ts_utc);

-- ============================================================
-- SPOT / MARKET: daily
-- ============================================================
CREATE TABLE IF NOT EXISTS market_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    coingecko_id        TEXT,
    price_usd           REAL,
    market_cap_usd      REAL,
    fdv_usd             REAL,
    circulating_supply  REAL,
    total_supply        REAL,
    max_supply          REAL,
    spot_volume_24h_usd REAL,
    ath_usd             REAL,
    ath_date            TEXT,
    pct_below_ath       REAL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);

-- ============================================================
-- ORDER BOOK DEPTH: your real exit size
-- ============================================================
CREATE TABLE IF NOT EXISTS depth_snapshot (
    ts_utc          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    market_type     TEXT NOT NULL,          -- 'spot' | 'perp'
    bid_depth_0p5   REAL,                   -- USD notional within 0.5% of mid
    bid_depth_1p0   REAL,
    bid_depth_2p0   REAL,
    ask_depth_0p5   REAL,
    ask_depth_1p0   REAL,
    ask_depth_2p0   REAL,
    fetched_at_utc  TEXT NOT NULL,
    PRIMARY KEY (ts_utc, exchange, symbol, market_type)
);

-- ============================================================
-- FUNDAMENTALS: daily, from DefiLlama / Artemis
-- ============================================================
CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    protocol_slug       TEXT,
    tvl_usd             REAL,
    fees_24h_usd        REAL,
    fees_7d_usd         REAL,
    fees_30d_usd        REAL,
    revenue_24h_usd     REAL,
    revenue_7d_usd      REAL,
    revenue_30d_usd     REAL,
    revenue_annualised  REAL,
    active_addresses_24h INTEGER,
    has_fundamentals    INTEGER NOT NULL,   -- 0 = no revenue model; score this block 0
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);

-- ============================================================
-- SUPPLY / HOLDERS
-- ============================================================
CREATE TABLE IF NOT EXISTS holder_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    chain               TEXT,
    contract_address    TEXT,
    top10_share         REAL,               -- 0-1, EXCLUDING known locked/bridge/staking
    top50_share         REAL,
    holder_count        INTEGER,
    excluded_addresses  TEXT,               -- JSON list of addresses excluded + why
    data_quality        TEXT,               -- 'good' | 'partial' | 'unavailable'
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);

-- ============================================================
-- EVENT CALENDAR (Tier-1 news). The highest-value table here.
-- ============================================================
CREATE TABLE IF NOT EXISTS scheduled_event (
    event_id            TEXT PRIMARY KEY,   -- deterministic hash of source+asset+date+type
    base_asset          TEXT NOT NULL,
    event_type          TEXT NOT NULL,      -- see enum below
    event_date_utc      TEXT NOT NULL,
    recipient_type      TEXT,               -- 'team'|'investor'|'ecosystem'|'community'|NULL
    magnitude_tokens    REAL,
    magnitude_usd       REAL,
    pct_of_circulating  REAL,
    description         TEXT,
    source              TEXT NOT NULL,
    confidence          TEXT NOT NULL,      -- 'confirmed'|'expected'|'rumoured'
    first_seen_utc      TEXT NOT NULL,      -- when WE learned of it (point-in-time!)
    fetched_at_utc      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_asset_date ON scheduled_event(base_asset, event_date_utc);

-- event_type enum:
--   'unlock_cliff', 'unlock_linear', 'emissions_change', 'burn',
--   'mainnet', 'upgrade', 'governance_vote', 'listing', 'delisting',
--   'monitoring_tag_add', 'monitoring_tag_remove', 'conference', 'earnings_report'

-- ============================================================
-- ATTENTION (Tier-2 news)
-- ============================================================
CREATE TABLE IF NOT EXISTS attention_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    social_volume       REAL,
    social_dominance    REAL,               -- share of total crypto conversation
    social_engagement   REAL,
    sentiment_score     REAL,               -- -1 to +1
    galaxy_score        REAL,               -- LunarCrush, if available
    alt_rank            INTEGER,
    google_trends       REAL,
    source              TEXT NOT NULL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset, source)
);

-- ============================================================
-- NEWS (Tier-3). Research/labelling only. NOT a trigger source.
-- ============================================================
CREATE TABLE IF NOT EXISTS news_item (
    news_id             TEXT PRIMARY KEY,   -- hash of url
    published_at_utc    TEXT NOT NULL,
    fetched_at_utc      TEXT NOT NULL,      -- YOUR lag = fetched - published. Log it.
    lag_seconds         REAL,
    title               TEXT NOT NULL,
    url                 TEXT,
    source_name         TEXT,
    assets              TEXT,               -- JSON list of base_assets mentioned
    sentiment_label     TEXT,               -- 'positive'|'neutral'|'negative'
    sentiment_score     REAL,
    event_type_guess    TEXT,               -- maps to scheduled_event.event_type if inferable
    raw                 TEXT                -- full JSON payload
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_item(published_at_utc);

-- ============================================================
-- SCREENING OUTPUT
-- ============================================================
CREATE TABLE IF NOT EXISTS layer1_result (
    run_date        TEXT NOT NULL,
    base_asset      TEXT NOT NULL,
    passed          INTEGER NOT NULL,       -- 0/1
    failed_checks   TEXT,                   -- JSON list of check IDs that failed
    check_values    TEXT,                   -- JSON dict of every computed value
    fetched_at_utc  TEXT NOT NULL,
    PRIMARY KEY (run_date, base_asset)
);

CREATE TABLE IF NOT EXISTS layer2_result (
    run_date            TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    total_score         REAL NOT NULL,
    score_fundamental   REAL,
    score_supply        REAL,
    score_sector        REAL,
    score_drawdown      REAL,
    score_events        REAL,
    score_attention     REAL,
    rank                INTEGER,
    universe_size       INTEGER NOT NULL,   -- how many survivors it was ranked against
    percentiles         TEXT,               -- JSON dict, per-metric percentile
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (run_date, base_asset)
);

-- ============================================================
-- JOURNAL: the only source of truth. Append-only. NEVER delete.
-- ============================================================
CREATE TABLE IF NOT EXISTS journal_entry (
    entry_id            TEXT PRIMARY KEY,
    run_date            TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    rank                INTEGER NOT NULL,
    total_score         REAL NOT NULL,
    price_at_signal     REAL NOT NULL,
    btc_price_at_signal REAL NOT NULL,
    layer1_values       TEXT,               -- JSON, full snapshot
    layer2_values       TEXT,
    layer3_values       TEXT,
    news_context        TEXT,               -- JSON list of news_ids in +/-24h
    events_context      TEXT,               -- JSON list of event_ids within +/-30d
    created_at_utc      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_return (
    entry_id        TEXT NOT NULL,
    horizon         TEXT NOT NULL,          -- '1d'|'7d'|'30d'|'90d'
    price_at_horizon REAL,
    return_raw      REAL,
    return_vs_btc   REAL,
    max_favourable  REAL,                   -- best point reached in window
    max_adverse     REAL,                   -- worst point reached in window
    computed_at_utc TEXT NOT NULL,
    PRIMARY KEY (entry_id, horizon),
    FOREIGN KEY (entry_id) REFERENCES journal_entry(entry_id)
);

-- ============================================================
-- OPS
-- ============================================================
CREATE TABLE IF NOT EXISTS collector_run (
    run_id          TEXT PRIMARY KEY,
    collector_name  TEXT NOT NULL,
    started_at_utc  TEXT NOT NULL,
    ended_at_utc    TEXT,
    status          TEXT NOT NULL,          -- 'success'|'partial'|'failed'
    rows_written    INTEGER,
    error_message   TEXT
);
```

---

## 4. CONFIGURATION

### 4.1 `config/thresholds.yaml`

Every number lives here. Sourced values are annotated; unsourced are `TUNE_ME`.

```yaml
layer1:
  # Sourced: published funding-arb risk screener thresholds
  oi_to_mcap_fail: 1.0              # warning at 0.5
  perp_to_spot_vol_fail: 40.0       # warning at 15
  min_mcap_usd: 30_000_000
  min_contract_age_days: 60

  # Sourced: RAVE (9 wallets ~95%, 3 team wallets 89.74%), TRB (~20 whales ~95%)
  top10_holder_share_fail: 0.60     # TUNE_ME — 0.60 chosen to catch both cases with margin
  min_circulating_ratio: 0.30       # TUNE_ME

  # Sourced: Keyrock 16k unlocks — ~90% negative, impact starts ~30d pre-event,
  # team unlocks worst (to -25%)
  unlock_lookahead_days: 30
  unlock_pct_circulating_fail: 0.05
  unlock_fail_recipient_types: ["team", "investor"]

  # Sourced: RAVE — ~$6B mcap destroyed on ~$52M liquidations
  mcap_to_liquidation_ratio_fail: 50.0
  mcap_to_liq_trigger_move_pct: 1.0    # only evaluate on 24h moves > 100%

  orphan_perp_fails: true              # no spot pair = auto-fail
  require_mcap_data: true              # not in CG top ~2000 = auto-fail

layer2_weights:
  fundamental: 35
  supply: 25
  sector: 15
  events: 10
  attention: 15
  drawdown: 10
  # NOTE: sums to 110 deliberately — normalise to 100 after computing.
  # If a block is unavailable for an asset, renormalise the remaining weights.

layer3:
  funding_negative_extreme_pctile: 5.0
  oi_change_extreme_pctile: 95.0
  min_depth_2pct_usd: 50_000        # TUNE_ME — below this, no position is exitable

collectors:
  derivatives_interval_minutes: 5
  market_interval_hours: 24
  fundamentals_interval_hours: 24
  events_interval_hours: 12
  attention_interval_hours: 6
  news_interval_minutes: 15

journal:
  horizons: ["1d", "7d", "30d", "90d"]
  top_n_to_journal: 25              # journal more than you report, for statistics
  report_top_n: 15
```

### 4.2 `.env.example`

```
COINALYZE_API_KEY=
COINGECKO_API_KEY=
CRYPTOPANIC_AUTH_TOKEN=
LUNARCRUSH_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

All optional. The system must run with an empty `.env`, degrading gracefully — log a WARN and
score the affected block as unavailable rather than crashing.

---

## 5. PHASE 0 — SCAFFOLDING

**Duration:** 2–3 days

### Tasks
1. Create repo structure exactly as §2.2.
2. `requirements.txt` with pinned versions.
3. `src/config.py` — pydantic-settings loader. Must validate that `layer2_weights` are all
   positive and that every `layer1` threshold is present. Fail at import time if not.
4. `src/db/connection.py` — context-managed connection factory, WAL enabled, foreign keys on.
5. `src/db/schema.sql` — verbatim from §3. Apply idempotently on startup.
6. `src/collectors/base.py` — `BaseCollector` abstract class:
   ```python
   class BaseCollector(ABC):
       name: str
       rate_limit_per_minute: int

       async def fetch(self) -> list[dict]: ...      # abstract
       def transform(self, raw: list[dict]) -> list[dict]: ...  # abstract
       def write(self, rows: list[dict]) -> int: ...  # abstract

       async def run(self) -> CollectorRunResult:
           # wraps fetch/transform/write with: rate limiting, tenacity retry
           # (exponential backoff, 5 attempts, retry on 429/5xx/timeout),
           # structlog context, and a collector_run row written on both
           # success and failure paths.
   ```
7. `src/cli.py` — typer app with commands: `init-db`, `collect <name>`, `screen`, `journal`,
   `report`. Stub them.
8. `pytest` runs green on an empty test suite.

### Acceptance criteria
- [ ] `python -m src.cli init-db` creates `data/gem.db` with every table in §3
- [ ] Running it twice does not error
- [ ] `pytest` exits 0
- [ ] `python -m src.cli --help` lists all five commands
- [ ] `.gitignore` contains `.env`, `*.db`, `data/`, `logs/`

---

## 6. PHASE 1 — CORE COLLECTORS

**Duration:** 2–3 weeks. **This is the most important phase in the project.**

> **Explain this to the user before starting:** Binance keeps only the latest 30 days of
> `openInterestHist`. Coinalyze retains only 1,500–2,000 intraday datapoints and deletes older
> data daily. This data cannot be reconstructed after the fact. Every day the collector is not
> running is a day that can never be backtested. Deploy it before building anything else.

### 6.0 Cadence tiers — read §1.4 first

Write every collector as a **stateless function that takes a timestamp and writes rows**. Never
embed a scheduling loop inside a collector. This is what lets the same code run under Actions
cron, under APScheduler on a persistent host, and under pytest, without modification.

```python
# Correct — schedule-agnostic
async def collect_derivatives(as_of: datetime) -> CollectorRunResult: ...

# Wrong — cannot run in CI
while True:
    collect()
    time.sleep(300)
```

| Tier | Cadence | Runner | Collectors | Build order |
|---|---|---|---|---|
| **C** | Daily | Actions `collect-daily.yml` | coingecko, defillama, holders, unlocks, attention, plus one derivatives snapshot | **First** |
| **B** | Hourly | Actions `collect-hourly.yml` | binance, hyperliquid derivatives; announcements; news | Second |
| **A** | 5 min | Persistent host (optional) | binance/hyperliquid derivatives + aggTrade CVD + depth | Last, if a host exists |

Tier C alone is enough to run L1, L2, the journal, and the dashboard. Do not block on Tier A.

**Actions-specific requirements for every collector workflow:**
- **`on: workflow_dispatch` only. Never `on: schedule`.** Triggering is cron-job.org's job (§1.4.1).
- `timeout-minutes: 20` on every job. A hung HTTP call otherwise burns a runner slot.
- `concurrency: group: <workflow>, cancel-in-progress: false` — a delayed trigger can land on top
  of a running job, and two collectors writing the same snapshot key will conflict.
- Write the actual run time to `fetched_at_utc`, never a nominal slot time. Runs still drift by
  the runner provisioning delay even with punctual triggering, so the two differ.
- Accept an optional `tier` input so one workflow file can serve several cron-job entries:
  ```yaml
  on:
    workflow_dispatch:
      inputs:
        tier:
          description: "daily | hourly"
          required: false
          default: "daily"
  ```
- Ping the workflow's healthcheck URL at the end of the job, on success only.

### 6.1 `collectors/binance.py`

Base URL: `https://fapi.binance.com`

| Endpoint | Purpose | Notes |
|---|---|---|
| `GET /fapi/v1/exchangeInfo` | Symbol universe, `onboardDate`, contract type | Once daily → `universe_snapshot` |
| `GET /fapi/v1/premiumIndex` | Mark price, index price, **last funding rate**, next funding time | All symbols in one call. Every 5 min |
| `GET /fapi/v1/openInterest` | Current OI **in contracts** | **One symbol per call.** This is the bottleneck. Use a bounded thread/async pool, ~10 concurrent |
| `GET /fapi/v1/ticker/24hr` | Perp 24h volume | All symbols in one call |
| `GET /futures/data/openInterestHist` | OI history, `sumOpenInterest` (contracts) + `sumOpenInterestValue` | **Only 30 days available.** Backfill once at setup, then rely on your own snapshots |
| `GET /fapi/v1/fundingRate` | Funding history | For computing funding persistence |
| `GET /fapi/v1/depth?limit=500` | Order book | Perp depth. Every 15 min for L2 survivors only |
| Binance **spot** `/api/v3/ticker/24hr` | Spot volume + existence check | Different base URL: `https://api.binance.com` |

**Critical implementation requirements:**

1. **Funding interval must be fetched, not assumed.** Binance settlement is no longer uniformly
   8h — it varies per symbol (4h, sometimes 1h). Derive it from consecutive `fundingTime` values
   in `/fapi/v1/fundingRate`, cache per symbol in `universe_snapshot.funding_interval_hours`,
   and refresh weekly.
   ```python
   funding_apr = funding_rate * (8760 / funding_interval_hours)
   ```
   Never `rate * 3 * 365`. That mis-ranks the entire universe.

2. **Strip the `1000` prefix** before any market-cap join:
   ```python
   base = symbol.removesuffix("USDT")
   cg_base = base[4:] if base.startswith("1000") else base
   price_multiplier = 1000 if base.startswith("1000") else 1
   ```
   Store both. Missing this silently zeroes market cap on every 1000-prefixed contract.

3. **Orphan perps.** If no Binance spot pair exists for the base asset, set
   `perp_to_spot_ratio = float("inf")`, not `None`. A perp whose hedge would live on another
   exchange is materially worse, and `None` will silently pass the filter.

4. **Store OI in contracts as the primary field.** `open_interest_usd = contracts × mark_price`,
   so USD OI rises in a rally with zero new positioning. Always rank on the contract field, or on
   USD OI residualised against price change.

### 6.2 `collectors/hyperliquid.py`

Base URL: `https://api.hyperliquid.xyz/info` — POST, JSON body, **no API key required**.

```python
# All perps, one call: funding, OI, mark/oracle premium, 24h volume
{"type": "metaAndAssetCtxs"}

# Per-coin funding history
{"type": "fundingHistory", "coin": "ETH", "startTime": <ms>}
```

Hyperliquid funding is hourly. Normalise: `funding_apr = rate * 8760`.

Prefer Hyperliquid for order-flow research. Its tape is published at trade level and positions
are on-chain, which makes it cleaner than venues that batch or throttle their public feeds.

### 6.3 `collectors/coingecko.py`

- `/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=N`
- Free tier: pause ~1.2s between pages. Paginate to ~2000 coins (8 pages).

**Ticker collision handling — mandatory.** Ticker symbols are not unique; a $20M token can share
a symbol with a $2B one. Iterate pages in market-cap-descending order and keep the **first**
(largest) match per symbol:
```python
if sym in wanted and c.get("market_cap"):
    mcap.setdefault(sym, float(c["market_cap"]))
```
This biases toward *under*-flagging, which is the safe direction for a filter whose job is to
justify a "no". Document the limitation in a code comment.

Also capture `ath`, `ath_date`, and compute `pct_below_ath` — required for the Type-D
(fallen-angel) scoring block.

### 6.4 `collectors/defillama.py`

Free, no API key.
- `https://api.llama.fi/protocols` — all protocols, TVL, chains, category, token symbol
- `https://api.llama.fi/overview/fees?dataType=dailyFees`
- `https://api.llama.fi/overview/fees?dataType=dailyRevenue`

Build a `base_asset → protocol_slug` mapping. Store it in `config/protocol_map.yaml` and treat
unmapped assets as `has_fundamentals = 0`. Do not guess mappings.

### 6.5 `collectors/coinalyze.py` (optional)

`https://api.coinalyze.net/v1/` — free API key, **40 calls/min**, use `pyrate-limiter`.
Endpoints: open-interest history, funding-rate history, predicted funding, liquidation history,
long/short ratio, OHLCV.

**Data-quality warnings to embed as code comments:**
- Liquidation feeds are throttled at source. Binance's `forceOrder` stream pushes only the largest
  single liquidation per symbol per 1000ms. Binance and Bybit moved to one liquidation per second
  around mid-2021; OKX capped at one per second per contract; Bybit only restored full data in
  Feb 2025. **All liquidation totals are floors, not measurements.** Never present them as exact.
- Coinalyze deletes intraday data daily. Poll and persist; do not rely on their retention.

### 6.6 `scripts/run_collectors.py`

APScheduler daemon. Intervals from `config/thresholds.yaml`. Every run writes a `collector_run`
row. On three consecutive failures for one collector, log ERROR and (if Telegram configured)
alert.

### Acceptance criteria
- [ ] Daemon runs 72 hours continuously with no unhandled exception
- [ ] `derivatives_snapshot` has ≥ 95% of expected rows (symbols × intervals) over that window
- [ ] `funding_interval_hours` is populated and **not uniformly 8.0** — verify at least one
      symbol shows 4.0 or 1.0. If all are 8.0, the derivation is wrong.
- [ ] Every `1000`-prefixed symbol has a non-null `market_cap_usd` in the daily join
- [ ] At least one symbol has `perp_to_spot_ratio == inf` (orphan perp) — if zero, the spot
      existence check is broken
- [ ] `universe_snapshot` has one row set per day, and symbol counts differ across days
      (proving delistings/listings are captured)
- [ ] Unit tests use saved fixtures in `tests/fixtures/`; no network calls in the test suite

---

## 7. PHASE 2 — SCHEDULED EVENT CALENDAR (Tier-1 news)

**Duration:** 1–2 weeks. **Highest-value news work in the project.**

### 7.1 Why this and not headlines

Unlock research across 16,000+ events found roughly 90% were followed by negative price pressure,
with team unlocks worst (drawdowns to ~25%), investor unlocks milder (funds use OTC or hedge in
advance), and **ecosystem unlocks averaging a small positive return** because those tokens fund
grants and liquidity rather than immediate sells. Crucially, the price decline typically begins
**about 30 days before** the date and stabilises within ~two weeks after, so by release day the
market has often already repriced.

Independent sample of 236 events: one-month raw return **median −16.26%, mean −8.10%**, largest
cluster in the −30% to −20% band.

That is a schedule, not a news feed. No latency race exists. This is the tractable edge.

### 7.2 `collectors/unlocks.py`

Sources (implement in priority order, degrade gracefully):
1. Tokenomist API / TokenUnlocks — recipient-level allocation data
2. CoinGecko's per-coin unlock fields where present
3. CoinGlass token-unlock endpoint (if the user has a key)

**Must capture `recipient_type`.** The recipient distinction is the single most predictive field
in the unlock literature, and most free sources omit it. If unavailable, store `NULL` and set
`confidence = 'expected'` — never guess.

**`first_seen_utc` is mandatory.** Backtests must only use events that were knowable at the time.
An unlock schedule published in June cannot inform a May decision.

### 7.3 `collectors/announcements.py`

Poll official announcement endpoints on a 60s interval. **This is for the calendar and the
journal, not for reaction trading.**

- Binance announcement catalog (public CMS endpoint used by open-source listing bots):
  `https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId=48&pageNo=1&pageSize=15`
- Project GitHub releases for tracked protocols (`api.github.com/repos/{org}/{repo}/releases`)
- Governance forum RSS where available

Classify each into `event_type`. Capture **listing**, **delisting**, and **monitoring-tag
add/remove** — the monitoring tag is a soft exchange warning for elevated-volatility assets and is
often a structural precursor to delisting or recovery.

**Record your own lag.** Store `published_at` and `fetched_at` and compute `lag_seconds`. After a
month, report the distribution. This gives the user an empirical, personal answer to "can I trade
news?" instead of an assertion. Expect a median well above 15 seconds.

### 7.4 Derived features for L1 and L2

```python
days_to_next_major_unlock(asset)     -> int | None   # L1 kill switch input
days_since_last_major_unlock(asset)  -> int | None   # L2 positive signal
unlock_overhang_cleared(asset)       -> bool         # all cliffs passed = strong positive
emissions_trajectory(asset)          -> 'falling' | 'flat' | 'rising'
event_density_30d(asset)             -> int
```

`unlock_overhang_cleared` is the inverse signal almost nobody screens for: supply pressure
structurally ends and any demand improvement now meets a market with no scheduled seller. It is
plausibly the strongest single positive feature in the whole system. Weight it accordingly and
measure it in the journal.

### Acceptance criteria
- [ ] `scheduled_event` populated for ≥ 100 assets
- [ ] `recipient_type` present for ≥ 30% of unlock rows (rest NULL, never guessed)
- [ ] `first_seen_utc` set on every row and never backdated
- [ ] Announcement poller captures at least one real listing/delisting within two weeks
- [ ] `docs/RESEARCH_LOG.md` contains the measured `lag_seconds` distribution after 14 days
- [ ] All five derived features return correct values on a hand-built fixture

---

## 8. PHASE 3 — ATTENTION AND NEWS (Tiers 2 and 3)

**Duration:** 1 week

### 8.1 `collectors/attention.py` (Tier 2 — scored)

Sources, in order of preference given free tiers:
1. **Google Trends** via `pytrends` — free, and specifically supported: a study across 40
   cryptocurrencies found Google search-based sentiment acts as a robust forecasting signal
   alongside network activity and hashrate, with dynamic long-short portfolios delivering large
   risk-adjusted excess returns.
2. **LunarCrush API** — social volume, social dominance, Galaxy Score, AltRank across X, Reddit,
   YouTube, TikTok. Strongest coverage for long-tail alts where other sources are thin. Check
   current free-tier limits at implementation time.
3. **Alternative.me Fear & Greed** — free, market-wide regime variable, not per-asset.
4. **Santiment** — social dominance is the standout metric, but free/Pro tiers carry a 30-day lag
   on restricted metrics, which makes it useless for live screening. Use for backtesting only.

**Scoring rule:** never use raw social volume. Use the **z-score of social volume against that
asset's own trailing 30-day distribution**, then percentile-rank cross-sectionally. Raw volume
just ranks by market cap.

**Mandatory caveats in code comments:**
- Bot contamination is unresolved. One study estimated ~14% of crypto tweets came from bot
  accounts, and no vendor eliminates bot influence.
- Sentiment's predictive power is concentrated at extreme market states, not in the middle of the
  distribution. Consider gating this block to only contribute when |z| > 2.

### 8.2 `collectors/news.py` (Tier 3 — journal labelling only)

**Hard rule: nothing in this module may feed L1 or L2. It writes to `news_item` only.**

- CryptoPanic v2: `https://cryptopanic.com/api/{plan}/v2/posts/?auth_token=...&currencies=BTC,ETH`
  Rate limited per IP at 5–10 req/sec depending on plan, with server-side caching that makes
  polling more often than every 30 seconds pointless. **Verify current plan availability at
  implementation time** — the free Developer plan has been marked discontinued in their public
  docs, so check the account page rather than assuming.
- RSS fallback: CoinDesk, The Block, Cointelegraph. Free, no key, adequate for labelling.

Sentiment classification: start with a keyword/rule-based classifier over a crypto-specific
lexicon. Only add a transformer model (FinBERT or a crypto-tuned BERT, CPU inference) if the
rule-based version proves inadequate on a hand-labelled sample of 200 headlines. Do not start
with the heavy option.

### 8.3 Journal integration

At journal-write time, attach:
```python
news_context = news items mentioning this asset in [signal_ts - 24h, signal_ts + 24h]
events_context = scheduled events for this asset in [signal_ts - 30d, signal_ts + 30d]
```

After six months, this answers the question the user actually cares about: **did the news precede
the signal or follow it?** If signals consistently follow news, the system is a lagging indicator
and the design needs rethinking. That finding would be worth more than any alert.

### Acceptance criteria
- [ ] `attention_snapshot` populated daily for all L1 survivors
- [ ] Social volume stored as z-score, not raw
- [ ] `news_item` accumulating with `lag_seconds` computed on every row
- [ ] Rule-based sentiment achieves ≥ 70% agreement with a hand-labelled 200-headline sample
- [ ] **Verified by code review:** no import path connects `news.py` to `layer1_kill.py` or
      `layer2_score.py`

---

## 9. PHASE 4 — LAYER 1 KILL SWITCH

**Duration:** 1 week

Binary. Any single FAIL removes the asset for that day. No partial credit, no overrides.

```python
@dataclass
class CheckResult:
    check_id: str
    passed: bool
    value: float | None
    threshold: float
    reason: str

def run_layer1(asset: str, run_date: str) -> Layer1Result:
    """Returns passed + list of failed check IDs + all computed values."""
```

### The nine checks

| ID | Check | FAIL when | Source of threshold |
|---|---|---|---|
| `L1_HOLDER_CONC` | Top-10 holder share, excluding known locked/bridge/staking contracts | > 0.60 | RAVE: 9 wallets ~95%, 3 team wallets 89.74%. TRB: ~20 whales ~95% |
| `L1_FLOAT` | circulating ÷ total supply | < 0.30 | Low-float/high-FDV trap literature |
| `L1_OI_MCAP` | perp OI notional ÷ circulating mcap | > 1.0 | Published risk screener: warn 0.5, danger 1.0 |
| `L1_PERP_SPOT` | perp 24h vol ÷ spot 24h vol | > 40 or spot absent | Same screener: warn 15, danger 40 |
| `L1_AGE` | days since perp `onboardDate` | < 60 | Same screener |
| `L1_MCAP` | circulating mcap | < $30M | Same screener |
| `L1_UNLOCK` | unlock in next 30d, team/investor, > 5% of circulating | true | Keyrock: ~90% negative, team worst, impact starts ~30d prior |
| `L1_NO_MCAP` | asset not resolvable in CoinGecko top ~2000 | true | Same screener's bonus signal |
| `L1_MCAP_LIQ` | on any 24h move > 100%: mcap Δ ÷ liquidations | > 50:1 | RAVE: ~$6B destroyed on ~$52M liquidations |

**Note on `L1_MCAP_LIQ`:** because liquidation feeds are throttled, this ratio is a lower bound on
the true ratio, which means a FAIL is high-confidence and a PASS is not. Document this asymmetry
in the code and in the report.

### Data-quality handling
If a check's input is unavailable, do **not** silently pass. Emit `CheckResult(passed=False,
reason="data_unavailable")` and count it as a FAIL. A screener that cannot see is a screener that
should say no.

### Acceptance criteria
- [ ] Runs daily, writes `layer1_result` for every symbol in `universe_snapshot`
- [ ] Survivor rate between 20% and 50% of the universe. Outside that band, thresholds need review
- [ ] `check_values` JSON contains a value for all nine checks on every row
- [ ] **Case-study regression tests pass** (see §11.4)

---

## 10. PHASE 5 — LAYER 2 DEMAND SCORE

**Duration:** 2 weeks

### 10.1 The cardinal rule

**Percentile-rank every metric within the current day's surviving universe. Never use absolute
thresholds in L2.**

Justification: funding-rate research found predictive power limited for single-asset prediction
(explaining ~12.5% of 7-day price variation, declining thereafter) but noted the data is more
useful applied cross-sectionally across multiple assets. Absolute cutoffs also break the moment
market regime shifts; percentile ranks do not.

```python
def cross_sectional_percentile(series: pd.Series) -> pd.Series:
    """Rank 0-100 within today's universe. NaN-safe: NaN stays NaN, does not become 0."""
    return series.rank(pct=True, na_option="keep") * 100
```

### 10.2 Blocks

**Fundamental (weight 35)**
| Metric | Direction | Note |
|---|---|---|
| revenue_30d vs prior 30d, % change | higher better | The VVV signal: $70M → $100M annualised in one month |
| fees_7d vs fees_30d/4 (acceleration) | higher better | Leading indicator of revenue |
| price-to-sales percentile | lower better | Cheapness on real cash flow |
| active addresses 30d trend | higher better | Usage independent of price |
| TVL trend | higher better | **Low weight** — capital snapshot, not activity |

If `has_fundamentals == 0`, this block scores `None` and its weight is redistributed. Record that
the asset carried no fundamental support — it is a meaningful attribute, not a gap.

**Supply (weight 25)**
| Metric | Direction |
|---|---|
| emissions trajectory (falling/flat/rising) | falling best |
| cumulative burn as % of total supply | higher better |
| days since last major unlock cliff | higher better |
| `unlock_overhang_cleared` boolean | true = large bonus |
| staked/locked ratio | higher better, **but verify not team-controlled** |

Reference case: VVV cut emissions 10M → 8M → 6M → 3M → 2.5M → 2M annually while burning
approximately 33.87M tokens (41.85% of total supply).

**Sector (weight 15)**
Build sector indices from `config/sectors.yaml` (AI, DeFi, DePIN, RWA, gaming, L1, L2, privacy,
memecoin). Score each asset on its sector's 7d and 30d relative strength vs BTC.

This matters more than intuition suggests: sector flows dominate price action over weeks and
months even when an individual project's fundamentals are sound. Do not rank a good chart in a
bleeding sector highly.

**Events (weight 10)**
- `days_to_next_major_unlock` — more is better
- `unlock_overhang_cleared` — bonus
- positive scheduled catalyst in next 30d (mainnet, emissions cut, upgrade) — bonus
- `monitoring_tag_add` present — strong negative

**Attention (weight 15)**
- social volume z-score, percentile-ranked
- social dominance change
- Google Trends percentile
- **Gate:** contributes only when |z| > 2 (sentiment's predictive power concentrates at extremes)

**Drawdown / reversal (weight 10)**
- % below ATH (AERO −76%, FORM ~−90% are the reference setups)
- weeks spent in current range
- `unlock_overhang_cleared` interaction

Justification: a nine-year study of 1,160 cryptocurrencies found a size effect, a **distinctive
reversal effect that challenges the established momentum effect**, and an illiquidity premium.
Note in comments that this is a *cross-sectional, weekly-rebalanced average* effect, not a
promise about any individual chart.

### 10.3 Redundancy check (required)

After the first 30 days of scores, compute the correlation matrix of the six block scores. Any
pair with |ρ| > 0.8 is measuring one thing twice — report it and propose a merge.

Justification: applying iterative factor selection to 36 crypto return-predictive factors found
**two to three factors eliminated all significant portfolio alphas**, with the most influential
being turnover volatility, bid-ask spreads, and blockchain-native metrics. A six-block score with
high internal correlation is a three-block score wearing a costume.

### Acceptance criteria
- [ ] Daily `layer2_result` for every L1 survivor
- [ ] `percentiles` JSON has an entry for every input metric
- [ ] Weight renormalisation verified: an asset missing the fundamental block still totals 100
- [ ] Correlation matrix report generated after 30 days of data
- [ ] Top-15 list is not dominated by one sector on any given day (if it is, the sector block is
      overweighted)

---

## 11. PHASE 6 — THE JOURNAL

**Duration:** 3 days to build, then runs forever.

> This is the second-most-important phase. It is the **only** component that produces truth.
> Everything else produces plausible-looking output.

### 11.1 Rules
1. Every asset in the daily top-N (`top_n_to_journal`, default 25) gets a `journal_entry`.
2. **Append only.** No deletions, no edits, no exclusions. Not even "the one where I'd have known
   better." That exclusion is the exact mechanism by which every signal channel looks profitable.
3. Forward returns computed automatically at 1d/7d/30d/90d, raw and vs BTC.
4. Also record max favourable and max adverse excursion within each window — this is what tells
   the user whether a stop would have been hit before the target.

### 11.2 `journal/forward_returns.py`

Daily job: find `journal_entry` rows whose horizon has now elapsed and whose `forward_return` row
is missing, fetch historical price, compute, write.

### 11.3 Reporting

`python -m src.cli journal --report` outputs:
- N entries, N with complete 30d returns
- Median and mean return, raw and vs BTC, **reported separately**
- Hit rate (% positive vs BTC)
- Distribution histogram
- Same statistics for a random control sample drawn from L1 survivors not in the top-15
- Breakdown by which L2 block contributed most

**Why median and mean separately:** in the unlock study the mean was −8.10% while the median was
−16.26%, because the mean was pulled by a few outliers while the median described the typical
experience. The user's results will show the same asymmetry in the opposite direction. A strategy
whose mean is positive and median negative is a lottery-ticket strategy — legitimate, but it must
be sized as one, and the user cannot know that without both numbers.

### 11.4 Case-study regression tests — `tests/test_case_studies.py`

**These are the system's unit tests with known answers. They must pass before Phase 4 is
accepted.**

```python
def test_rave_fails_layer1_before_peak():
    """RAVE, April 2026. ~9 wallets held ~95% of 1B supply; 3 team-linked wallets
    held 89.74%. Launched on Binance Alpha late 2025 with low float. Peaked $27.88
    on 2026-04-18, fell to ~$1 within 24h.
    ASSERT: L1 FAILs on L1_HOLDER_CONC and L1_FLOAT at every date from launch to peak."""

def test_trb_fails_layer1_before_peak():
    """TRB, December 2023. 2.75M circulating, ~20 whales holding ~95%, thin books.
    Ran to $619 then to $136 in 13 hours.
    ASSERT: L1 FAILs on L1_HOLDER_CONC and L1_MCAP (or L1_OI_MCAP) pre-peak."""

def test_vvv_passes_layer1_and_scores_well():
    """VVV, Dec 2025 - Sep 2026. Real revenue ($70M -> $100M annualised run rate),
    41.85% of supply burned, emissions cut repeatedly, +1500% over the period.
    ASSERT: L1 PASS. L2 fundamental and supply blocks both in the top quartile.
    NOTE: top-100 holders reportedly control ~98% of supply, so verify whether the
    holder check trips. If it does, that is a REAL finding about threshold calibration,
    not a bug — record it in DECISIONS.md rather than loosening the threshold."""

def test_listing_day_entry_is_penalised():
    """Across 389 tokens on 6 CEXs in 2024: Binance listings pumped 87% on average
    but 98% eventually dumped, losing ~70% from listing price; 37% hit their ATH on
    listing day and never reclaimed it.
    ASSERT: an asset within 60 days of perp listing FAILs L1_AGE."""

def test_unlock_30d_ahead_fails():
    """Keyrock, 16,000+ events: ~90% negative price pressure, impact begins ~30 days
    before the date, team unlocks worst.
    ASSERT: team unlock of >5% circulating within 30d FAILs L1_UNLOCK."""
```

If `test_rave_fails_layer1_before_peak` or `test_trb_fails_layer1_before_peak` does not pass, the
thresholds are wrong and Phase 4 is not done. These are the two cases with known answers.

### Acceptance criteria
- [ ] Journal entries written daily, append-only enforced at the DB layer
- [ ] Forward returns backfilled automatically as horizons elapse
- [ ] Report command produces the full statistics block including the random control
- [ ] All five case-study tests pass

---

## 12. PHASE 7 — LAYER 3 STRUCTURE AND RISK

**Duration:** 2–3 weeks

### 12.1 Structure detection (`layer3_structure.py`)

Detect the Type-D setup the user is targeting:

```python
def detect_descending_trendline(highs: pd.Series, min_touches: int = 3) -> Trendline | None:
    """Fit a descending line to swing highs. Require >= min_touches within tolerance."""

def detect_break(close: pd.Series, trendline: Trendline) -> BreakEvent | None:
    """CLOSE-based break, not wick. Weekly or 3D timeframe."""

def detect_retest(close, low, break_event) -> RetestEvent | None: ...
def detect_fvg(ohlc: pd.DataFrame) -> list[FVG]: ...
def detect_order_block(ohlc: pd.DataFrame) -> list[OrderBlock]: ...
def compute_invalidation(setup) -> float:
    """Explicit level. Every candidate MUST have one or it is not a candidate."""
```

**Development approach:** prototype the pattern logic in Pine Script on TradingView first, where
the user can see it drawn on the chart, then port to Python once the rules are right. Pine gives
a far faster visual feedback loop than matplotlib for this class of problem.

### 12.2 Positioning risk checks (advisory, never a trigger)

| Metric | Interpretation |
|---|---|
| OI change percentile (contracts, multi-window: 1h/4h/12h/24h) | extreme = fragile |
| Funding percentile, interval-normalised | see asymmetry note below |
| Funding persistence (consecutive periods same sign) | one spike ≠ a regime |
| Spot CVD vs perp CVD | spot-led healthier, perp-led fragile |
| Spot depth ±2% vs intended position size | if position > depth, there is no exit |

**Funding asymmetry — implement correctly.** The formula is
`F = Premium + clamp(Interest − Premium, −0.05%, +0.05%)`, with the interest component fixed at
0.01% per 8h (~10.95% annualised) on both Binance and Hyperliquid. This gives the formula a
structural positive bias — funding only reaches zero when the average premium index is −0.05% and
only turns negative below that. **Negative funding is therefore a materially stronger signal than
positive funding of the same magnitude.** Do not treat ±0.05% as mirror images.

**CVD caveats to embed as comments:**
- Where the aggressor flag is absent, tools fall back on the tick rule (trades at/above ask = buy,
  at/below bid = sell), which degrades badly in fast markets where the book moves between prints.
- CVD works poorly on thin altcoin perps, which is exactly this universe.
- Divergence can persist through an entire trend. It is not a reversal timer.
- Absolute CVD is meaningless — only slope, divergence against price, and behaviour at levels.

### Acceptance criteria
- [ ] Trendline detection reproduces the AERO and FORM setups on their published charts
- [ ] Every candidate emits an explicit invalidation level or is rejected
- [ ] Funding is interval-normalised (verified against a 4h-interval symbol)
- [ ] Depth check vetoes any candidate where ±2% depth < `min_depth_2pct_usd`

---

## 13. PHASE 8 — BACKTEST HARNESS

**Duration:** 1 week to build. **Do not run it before month 6.** Before that you do not have
enough recorded data and the result will be noise you mistake for signal.

### 13.1 Non-negotiable hygiene

1. **Point-in-time universe.** Reconstruct from `universe_snapshot` for each date. Delisted symbols
   vanish from `exchangeInfo`, and they are exactly the ones the screener would have flagged.
   Using today's symbol list over last year bakes in survivorship bias.
2. **Point-in-time events.** Filter `scheduled_event` on `first_seen_utc <= as_of_date`.
3. **Costs.** Taker fees + funding accrued while held + slippage sized against the *recorded*
   `depth_snapshot`, not the printed price.
4. **All signals.** Full distribution of forward returns. No filtering.
5. **Median and mean separately.** See §11.3.
6. **Regime split.** Test separately in BTC-up, BTC-flat, BTC-down windows. A strategy that only
   works in one regime is a beta bet in disguise.
7. **Out-of-sample holdout.** Develop on the first 2/3, test once on the final 1/3. One shot. If
   you tune after looking at the holdout, it is no longer a holdout.

### 13.2 Required outputs
- Equity curve vs BTC buy-and-hold
- Max drawdown, Sharpe, Sortino, hit rate, average win/loss ratio
- Turnover and total cost drag as a % of gross return
- Per-block attribution: which L2 block actually contributed
- Random-control comparison from the same L1 survivor pool

### Acceptance criteria
- [ ] Backtest and journal produce identical numbers when run over the same window on the same
      signals. If they disagree, one of them has look-ahead bias — find it before proceeding.
- [ ] Universe reconstruction verified against a date where a known delisting occurred
- [ ] Cost model produces a non-trivial drag (if costs are ~0, the model is not applied)

---

## 14. PHASE 9 — REPORTING AND ALERTS

**Duration:** 3 days

`python -m src.cli report --date today` produces markdown:

```markdown
# Daily Screen — 2026-09-15

## Universe
512 perps → 147 survived L1 (28.7%)

## Top 15
| # | Asset | Score | Fund | Supp | Sect | Evnt | Attn | DD  | Flags |
|---|-------|-------|------|------|------|------|------|-----|-------|
| 1 | AERO  | 78.4  | 82   | 71   | 88   | 90   | 45   | 74  | overhang_cleared |
...

## Notable L1 failures (highest L2 potential, disqualified)
| Asset | Failed | Value | Threshold |
|-------|--------|-------|-----------|
| XYZ   | L1_HOLDER_CONC | 0.81 | 0.60 |

## Upcoming events (next 30d, survivors only)
| Asset | Date | Type | Recipient | % Circ |

## Data quality
- derivatives_snapshot: 98.2% completeness
- 12 assets missing holder data (counted as L1 FAIL)
- news lag p50: 47s, p95: 310s
```

Telegram delivery optional via `python-telegram-bot`, **notification only**, no interactive
commands, no execution.

**The "notable L1 failures" section is deliberate.** It shows the user what they are being
protected from, which builds trust in the filter and surfaces threshold miscalibration early.

### Acceptance criteria
- [ ] Markdown report generated daily to `reports/YYYY-MM-DD.md`
- [ ] Data-quality section present and accurate
- [ ] Telegram optional and degrades silently when unconfigured

---

## 15. PHASE 10 — AUTONOMOUS OPERATION ON GITHUB ACTIONS

**Duration:** 1 week. **Build this immediately after Phase 1, not at the end.** A collector that
only runs when the user's laptop is open is a collector with gaps, and gaps in Phase 1 are
permanent.

### 15.1 Turso migration

1. `turso db create gem-screener`, create an auth token, store `TURSO_DATABASE_URL` and
   `TURSO_AUTH_TOKEN` as repository secrets.
2. `src/db/connection.py` selects backend by env:
   ```python
   def get_connection():
       """Returns a DB-API-ish connection. Local dev uses sqlite3; CI uses libsql.
       Both must accept the same SQL — do not use any SQLite feature libsql lacks."""
       if os.getenv("TURSO_DATABASE_URL"):
           return libsql_client.create_client_sync(url=..., auth_token=...)
       return sqlite3.connect(settings.local_db_path)
   ```
3. Apply `schema.sql` to Turso once, idempotently.
4. **Add an index for every query pattern before the first production run.** Turso meters row
   reads, and an unindexed cross-sectional scan over `derivatives_snapshot` will consume the free
   allowance far faster than expected. Specifically index `(symbol, ts_utc)`,
   `(snapshot_date, base_asset)` on every daily table, and `(base_asset, event_date_utc)`.
5. Never write `SELECT COUNT(*)` over a full time-series table. Maintain counts in a small
   `table_stats` row updated on write.

### 15.2 cron-job.org setup

Do this **before** writing any workflow, so the first workflow can be tested end to end.

1. Sign up at cron-job.org. Free.
2. **Create the fine-grained PAT** (GitHub → Settings → Developer settings → Fine-grained tokens):
   - Repository access: **Only select repositories** → this repo only
   - Permissions: **Actions: read and write**, **Contents: read and write**, **Metadata: read**
   - Expiry: 90 days. Longer feels convenient and is how the system dies silently in month 8.
   - Record the expiry date in `docs/DECISIONS.md` and set a calendar reminder for 7 days before.
3. **Create one cron-job entry per workflow.** Identical shape, different URL and schedule:

   ```
   Title     collect-daily
   URL       https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/collect-daily.yml/dispatches
   Method    POST
   Schedule  03:10 UTC daily
   Headers   Accept: application/vnd.github+json
             Authorization: Bearer github_pat_xxxxxxxx
             X-GitHub-Api-Version: 2022-11-28
             Content-Type: application/json
   Body      {"ref":"main"}
   Notify    on failure  ← turn this on for every job
   ```

   | Entry | Schedule (UTC) | Workflow |
   |---|---|---|
   | collect-daily | `10 3 * * *` | `collect-daily.yml` |
   | collect-hourly | `25 * * * *` | `collect-hourly.yml` |
   | journal | `10 4 * * *` | `journal.yml` |
   | backup | `0 5 * * 0` | `backup.yml` |

4. **Test with "Execute now"** before trusting the schedule. Expect `204` with an empty body. Any
   other code means the token or URL is wrong — debug it here, where the response is visible,
   rather than from a silent 3am failure.
5. Enable failure notification on every entry. This is half the monitoring story (§15.3).

**Response codes you will actually see:**

| Code | Meaning |
|---|---|
| `204` | Success. Workflow queued. |
| `401` | Token expired or malformed. The most likely cause of a system that stops in month 4. |
| `403` | `Resource not accessible by personal access token` — almost always a missing **Contents** permission, or the token owner lacks write access. |
| `404` | Wrong owner/repo/filename, **or** the workflow file is not yet on the default branch. `workflow_dispatch` workflows must exist on the branch you dispatch against. |
| `422` | Bad `ref`, or the workflow lacks a `workflow_dispatch` trigger. |

The `404`-when-not-on-default-branch behaviour catches everyone once: a new workflow on a feature
branch cannot be dispatched until it is merged to `main`.

### 15.3 Workflows

**`collect-daily.yml`**
```yaml
name: collect-daily
on:
  workflow_dispatch:          # ← cron-job.org. NEVER add `schedule:` here.
    inputs:
      tier:
        required: false
        default: "daily"
concurrency:
  group: collect-daily
  cancel-in-progress: false
jobs:
  collect:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements.txt
      - run: python -m src.cli collect --tier ${{ inputs.tier || 'daily' }}
        env:
          TURSO_DATABASE_URL: ${{ secrets.TURSO_DATABASE_URL }}
          TURSO_AUTH_TOKEN:   ${{ secrets.TURSO_AUTH_TOKEN }}
          COINGECKO_API_KEY:  ${{ secrets.COINGECKO_API_KEY }}
      - if: success()
        run: curl -fsS -m 10 --retry 3 https://hc-ping.com/${{ secrets.HEALTHCHECK_DAILY }}
```

**`collect-hourly.yml`** — same shape, `--tier hourly`, its own healthcheck UUID.

**`screen.yml`** — `on: workflow_run: {workflows: [collect-daily], types: [completed]}` plus
`workflow_dispatch`. Guard with `if: github.event.workflow_run.conclusion == 'success'`, or a
failed collection will produce a screen from stale data.

**`journal.yml`** — triggered by cron-job.org at 04:10 UTC. Writes journal entries, backfills
elapsed forward returns.

**`publish.yml`** — `workflow_run` on `screen`. Serialises Turso → `data/public/*.json`, commits
with `git-auto-commit-action`. Must be idempotent: no change, no commit, or the repo accumulates
empty commits forever.

**`build-site.yml`** — `on: push: paths: [data/public/**, web/**]`. Runs `npm ci && npm run
build`, then `actions/upload-pages-artifact` + `actions/deploy-pages`.

> Deploying via a custom Actions workflow removes the 10-builds-per-hour Pages limit that applies
> to the default Jekyll pipeline.

**`backup.yml`** — weekly via cron-job.org. `turso db dump` → upload as a **release asset**, not
just an artifact. Artifacts expire; releases do not.

**No `keepalive.yml`.** The 60-day inactivity rule disables *scheduled* workflows, and this repo
has none. `publish.yml` also commits most days. If the earlier plan's keepalive file exists,
delete it.

### 15.4 Monitoring — two layers, because they fail differently

cron-job.org's failure alert and healthchecks.io are **not redundant**. They catch opposite halves
of the failure space, and having only one leaves a blind spot big enough to lose weeks in:

| Failure | cron-job.org catches it? | healthchecks.io catches it? |
|---|---|---|
| Trigger never fired | **Yes** | Yes (missed ping) |
| PAT expired → 401 | **Yes** | Yes |
| Wrong workflow filename → 404 | **Yes** | Yes |
| Workflow queued, then the job failed | **No** — it already got its 204 | **Yes** |
| Workflow ran but an API returned garbage | No | Only if the job fails loudly on it |
| Job hung until `timeout-minutes` | No | **Yes** |

So: **cron-job.org notification on every entry**, *and* a healthchecks.io check per workflow with
its period set to **25 hours** for daily jobs. That extra hour absorbs runner-provisioning drift
without masking a real failure.

Plus, inside the pipeline:

1. **Data-freshness assertion in `screen.yml`.** Before screening, assert the newest
   `derivatives_snapshot` row is under 26 hours old. Fail the job loudly otherwise. A confident
   dashboard built on three-day-old prices is the worst possible output.
2. **Publish a trigger-lag metric.** cron-job.org records both the scheduled and the actual
   execution time for the last 50 runs. Compare its intended time against the workflow's real
   start time and surface the distribution on the Health page. This is now measuring *runner
   provisioning delay*, which should be seconds — if it starts reading in minutes, something is
   wrong upstream and you will see it before it costs you data.

### Acceptance criteria
- [ ] `grep -r "on:" .github/workflows/ | grep -c schedule` returns **0**
- [ ] Every workflow has `workflow_dispatch` and `timeout-minutes`
- [ ] Each cron-job.org entry returns `204` on "Execute now" and has failure notification enabled
- [ ] PAT is fine-grained, single-repo, expires in ≤90 days, and its expiry date is in
      `DECISIONS.md` with a calendar reminder set
- [ ] Deliberately breaking the PAT produces a cron-job.org failure email within one cycle
- [ ] Deliberately failing a job (not the trigger) produces a healthchecks.io alert — proving the
      two layers are independent
- [ ] 14 consecutive days with ≥ 13 successful daily runs (the bar rises from 12 — punctual
      triggering should make near-perfect delivery normal)
- [ ] `health.json` trigger-lag p95 is under 3 minutes. If it is in tens of minutes, a `schedule:`
      trigger is still live somewhere — find it.
- [ ] Turso row-read usage after 30 days is under 20% of the free allowance
- [ ] `publish.yml` makes no commit on a no-change run
- [ ] Secrets appear only in `secrets.*` references, never in code, never in `data/public/`

---

## 16. PHASE 11 — THE DASHBOARD

**Duration:** 2 weeks

### 16.1 What this is for

Six pages. Their job is to make a disqualification-first system **legible** — to show what
survived, what did not and why, and whether any of it has worked. It is an instrument panel, not
a marketing site.

The user's brief was "impressive." The way this specific product becomes impressive is not
gradients and animated counters. It is that it shows its own failures. Every crypto dashboard on
the internet shows winners. Almost none shows a rejection wall with reasons, or a forward-return
histogram including the losers. Those two pages are the differentiator — build them best.

### 16.2 Design direction

Subject matter: a research instrument for reading market microstructure. Audience: the user and
anyone auditing their reasoning. Primary job: make a verdict and its evidence readable at a glance.

**Palette** — deep neutral ground, one accent used only for the survivor path, one for
disqualification. Deliberately not the two-tone dark-with-acid-green look that every trading
dashboard defaults to.

```
--ground     #12141A   page
--surface    #1A1D26   panels
--line       #2A2F3C   rules and grid
--text       #E4E7EE   primary
--muted      #7B8394   secondary, labels
--pass       #5FB49C   survived  (desaturated teal, not neon)
--fail       #C2664D   disqualified (clay, not red — this is information, not an error)
```

**Type** — one family with a strong numeric variant. Suggested: **Söhne Mono / IBM Plex Mono** for
all figures and tabular data, **Inter** for prose. Numbers must be tabular-lining and
right-aligned in every table; a column of scores that does not align on the decimal is unreadable
at 150 rows. Do not use all-caps labels, and do not add an eyebrow above every heading.

**Layout** — dense left-aligned tables, generous vertical rhythm between sections, no cards.
Cards fragment comparison, and comparison is the entire point of a cross-sectional screener. Use
rules and whitespace for grouping.

**Motion** — one place only: the funnel on the Screen page animating from universe count to
survivor count on load. Nothing else moves unless the user acts. Respect
`prefers-reduced-motion`.

### 16.3 Pages

**`/` — Screen (today)**

Hero is the funnel, because the funnel is the thesis:
```
   512 perps  ──────────────────►  147 survived  ──────►  15 ranked
   universe        365 disqualified      L1 pass            L2 top decile
```
Below: the ranked table. Columns — rank, ticker, score, six block scores as inline micro-bars,
flags. Sortable, filterable by sector. Clicking a row goes to the asset page.

**`/asset/:ticker` — Detail**

- Verdict banner: passed or the specific checks that failed
- `CheckGrid`: all nine L1 checks as a 3×3 grid, each showing computed value against threshold.
  A failing check shows the number, not just a red mark — the number is the argument.
- `BlockRadar`: six L2 blocks with the asset's percentile in each
- Price chart with structure levels and the invalidation line drawn explicitly
- `EventTimeline`: unlocks and catalysts, ±90 days
- News context for the last 7 days, clearly labelled as context and not signal

**`/rejected` — The disqualification wall**

Everything that failed L1, sorted by what its L2 score *would have been*. This is the page that
demonstrates the system has a spine. Group by failed check so a pattern is visible: "31 assets
failed on holder concentration today."

**`/journal` — The receipts** ← *the most important page*

- Forward-return histogram at 1d / 7d / 30d / 90d, relative to BTC
- **Median and mean displayed side by side, always.** They diverge, and the divergence is the
  finding. A system whose mean is positive and median negative is a lottery strategy and the user
  must be able to see that at a glance.
- Hit rate vs a random control drawn from the same L1 survivor pool
- Every past signal, with its outcome, filterable — including the bad ones
- An explicit entry count and coverage note: "182 entries, 94 with complete 30d returns"

Empty state before six months of data: state plainly that there is not yet enough data to draw a
conclusion, and show how many days remain. Do not show an encouraging partial number. The whole
point of this page is that it does not flatter the system.

**`/events` — Calendar**

90-day timeline of unlocks and catalysts across survivors. Colour by recipient type, since that is
the field the unlock research identified as most predictive.

**`/health` — System status**

Collector success rate, data completeness per table, cron lag distribution, news lag percentiles,
Turso usage against the free allowance, last successful run per workflow.

### 16.4 Hard constraints

1. **No secrets in the browser, ever.** The site is static and reads only pre-baked JSON. If any
   implementation requires an API key client-side, the design is wrong — move the fetch into
   `publish.yml`. A key in a public Pages bundle is a key that is compromised.
2. **HashRouter, not BrowserRouter.** Pages has no server-side rewrite; deep links break on
   refresh otherwise.
3. **`base` in `vite.config.ts` must match the repo name** for a project Pages site, or every
   asset 404s.
4. **Every number on the site must be traceable** to a field in `data/public/*.json`. No
   computed-in-the-browser figures that cannot be reproduced from the pipeline.
5. **The site must render honestly with no data.** First deploy happens before any journal
   entries exist. Empty states are part of the build, not an afterthought.
6. **Never display a stale screen as current.** If `latest.json` is over 26 hours old, show a
   banner stating the data's actual age at the top of every page.

### Acceptance criteria
- [ ] Deploys to `https://<user>.github.io/<repo>/` from Actions
- [ ] All six routes render with real data, and render correctly with empty data
- [ ] Deep-linking to `/#/asset/AERO` works after a hard refresh
- [ ] Lighthouse accessibility ≥ 90; keyboard focus visible on every interactive element
- [ ] Usable at 375 px width — the screen table becomes stacked rows, not a horizontal scroll
- [ ] `prefers-reduced-motion` disables the funnel animation
- [ ] Bundle under 500 KB gzipped
- [ ] `grep -ri "api_key\|secret\|token" web/dist/` returns nothing

---

## 17. OPEN RESEARCH QUESTIONS

Resolve if hit during implementation. Record answers in `docs/RESEARCH_LOG.md`.

| # | Question | Suggested search |
|---|---|---|
| R1 | Which free API gives reliable top-10 holder concentration across ETH/BSC/Base/Solana? | `"token holder distribution API free etherscan solscan top holders"` |
| R2 | Current CryptoPanic plan availability and free-tier limits | Check the account page directly; public docs mark the free Developer plan discontinued |
| R3 | Current LunarCrush free-tier request limits | `lunarcrush.com/pricing` |
| R4 | Does a free source expose unlock `recipient_type`? | `"token unlock API recipient type team investor allocation free"` |
| R5 | Correct list of Binance addresses to exclude from holder concentration (bridges, staking, exchange cold wallets) | `"exchange wallet labels API free arkham etherscan labels"` |
| R6 | Best free historical OHLCV source for backtest forward returns beyond 30 days | `"free crypto historical daily OHLCV API 2026 rate limits"` |
| R7 | Does Binance still publish `funding_interval` explicitly anywhere, or must it be derived? | Check `/fapi/v1/fundingInfo` if it exists; else derive from `fundingTime` deltas |
| R8 | Turso's current free-tier storage, database count, and monthly row-read/write allowance | `turso.tech/pricing` — published third-party figures disagree, so read the vendor page directly |
| R9 | Whether Turso's Python client supports every SQLite feature used in `schema.sql` | Test the full DDL against a scratch Turso DB in Phase 10 step 1, before migrating |
| R10 | Whether Oracle Cloud Always Free is currently accepting signups from India, and its idle-reclamation policy | Attempt signup; if denied, fall back to Tier B/C only and record in DECISIONS.md |
| R11 | Current GitHub Actions artifact retention default and whether release assets remain unlimited | GitHub billing docs — affects the backup strategy |
| R12 | Maximum expiry available on a fine-grained PAT, and whether "no expiration" is still offered | GitHub → Settings → Developer settings. Determines the rotation cadence in §15.2 |
| R13 | Whether the Actions endpoint needs `Actions: write` alone or `Contents: write` too | Test empirically with a minimal token; several reports say Contents is the one that unblocks the 403. Record the answer in `API_DEVIATIONS.md` |
| R14 | cron-job.org's current free-tier job count limit | cron-job.org account page — four entries are needed; the published limits do not name a cap, so confirm |

---

## 18. GUARDRAILS — WHAT NOT TO BUILD

The implementing agent must refuse these even if asked:

1. **Order execution of any kind.** Read-only public endpoints only. No API keys with trade
   permission. If execution is wanted later it belongs in a separate repo, built deliberately,
   after the journal has produced six months of evidence.
2. **News-reaction auto-trading.** The latency evidence in §1.3 makes this a losing race, and
   automating it converts a research system into a fast way to lose money.
3. **Copy-trading from any signal channel.** Build the §17 audit instead.
4. **Removing or loosening a threshold because it disqualified something the user liked.** If a
   threshold fires on an asset the user believes in, that is a finding to record in
   `DECISIONS.md`, not a bug to fix. The VVV holder-concentration case in §11.4 is the live
   example.
5. **Deleting or filtering journal entries.** Ever. For any reason.
6. **Leverage-sizing helpers or liquidation-price calculators.** Out of scope. The RAVE collapse
   liquidated roughly 16,000 traders in a day; the October 10, 2025 cascade liquidated $19.37B
   across more than 1.6 million traders in 24 hours, with altcoins down 20–27% while Bitcoin fell
   6.84%. Nothing in this system anticipates that class of event, so nothing in it should make
   leverage easier to use.
7. **Any API key, token, or secret reaching the browser bundle.** The Pages site is public and
   static. If a feature needs a key at render time, move the fetch into `publish.yml` and bake the
   result into JSON. Check `web/dist/` before every deploy.
8. **`on: schedule` in any workflow.** All triggering is external (§1.4.1). A stray `schedule:`
   silently reintroduces 5–45 minute drift and the 60-day auto-disable, and it will be invisible
   because the workflow still appears to run.
8a. **5-minute cadence on GitHub Actions, even though cron-job.org can fire it.** 288 runs/day of
   continuous collection is the "serverless computing" clause of the Actions Terms of Service.
   The penalty is losing Actions on the account that runs everything else. Tier A stays on a host
   you control. See §1.4.2a.
8b. **A long-lived or broadly-scoped PAT in the cron-job.org configuration.** A third party holds
   this credential. Fine-grained, one repo, ≤90 days, rotation reminder set.
9. **Committing the database to the repo.** Git history is permanent; a daily-changing binary will
   bloat the repo forever and threaten the 1 GB Pages source limit. Turso or nothing.
10. **A public dashboard that presents stale data as current, or that displays only winning
    signals.** Both are the exact failure mode §19 exists to expose in other people. If the
    Journal page ever filters out losers, the project has become the thing it was built to audit.

---

## 19. SIDE PROJECT — SIGNAL CHANNEL AUDIT

Independent, ~2 weeks, excellent hackathon submission. Answers: does the channel the user follows
actually have edge?

1. Scrape all channel messages with timestamps. Store immutably.
2. Parse into: ticker, direction, entry, target, invalidation, timestamp.
3. Bucket into three classes:
   - **Forward calls with invalidation** (like the published `$FORM` post specifying "Weekly close
     below $0.1768") — fully scorable
   - **Forward calls without invalidation** — count as "unfalsifiable," report separately
   - **Retrospective posts** (like the `$VVV` +1,600% post) — never scorable, exclude entirely
4. For each scorable call, pull price from the timestamp and compute mechanically: did it reach
   target before invalidation?
5. Report: hit rate, mean and median R, equal-weight portfolio equity curve, max drawdown,
   performance vs simply holding BTC over the same window, and the ratio of retrospective to
   forward posts.

**Base rate to compare against:** a study of ~36,000 tweets from 180 prominent crypto influencers
covering over 1,600 assets found tweets were initially associated with positive returns, followed
by **significant negative longer-horizon returns**, with effects strongest for self-described
experts, smaller-cap assets, and accounts with large followings. Separately, investors relying on
Twitter information have been found to sell late in post-dump phases and take significant losses
relative to other participants.

The channel may be an exception. The point is to know from data rather than from a screenshot of
the call that worked.

---

## 20. TIMELINE

**Reordered from the original plan.** Phase 10 (automation) moves to position 3, immediately after
the collectors exist. Rationale: until collection is autonomous it only runs when the user's
laptop is open, and Phase 1's whole premise is that missed days are permanently lost. Getting to
"it runs without me" is more urgent than getting to "it screens well."

| # | Phase | Duration | Cumulative | Gate |
|---|---|---|---|---|
| 0 | Scaffolding | 3 days | week 1 | `init-db` works |
| 1 | Core collectors (Tier C, then B) | 2 weeks | week 3 | Collectors are stateless functions |
| **10** | **Autonomous operation** | **1 week** | **week 4** | **14 days, ≥12 successful runs** |
| 2 | Event calendar | 2 weeks | week 6 | 100+ assets with events |
| 3 | Attention/news | 1 week | week 7 | z-scores computed, news isolated from scoring |
| 4 | L1 kill switch | 1 week | week 8 | **Case-study tests pass** |
| 5 | L2 scoring | 2 weeks | week 10 | Daily top-15 produced |
| 6 | Journal | 3 days + forever | week 11 | Append-only enforced |
| **11** | **Dashboard** | **2 weeks** | **week 13** | **Six routes live on Pages** |
| 7 | L3 structure | 3 weeks | week 16 | Reproduces AERO/FORM setups |
| 8 | Backtest harness | 1 week | week 17 | Matches journal exactly |
| 9 | Reporting/alerts | 3 days | week 18 | Daily markdown + Telegram |
| — | Data accumulation | months 5–10 | month 10 | **First honest backtest** |
| — | Tier A high-freq collector | anytime | — | Only if a persistent host exists |

**Week 4 is the deadline that matters.** After Phase 10 the system accumulates data whether or
not the user touches it, and every later phase becomes an improvement to something already
running rather than a prerequisite for starting. Everything after week 4 can be rebuilt in a
weekend; the recorded dataset cannot.

The dashboard lands at week 13 deliberately — after the journal exists, so it has something
honest to display. Building it earlier produces a beautiful interface with nothing behind it,
which is the failure mode this entire project is a reaction against.

---

## 21. WHAT THIS PRODUCES REGARDLESS OF WHETHER IT MAKES MONEY

A distributed data collector, a normalised multi-source pipeline, a cross-sectional ranking
engine, a backtest harness with correct point-in-time hygiene, a full CI/CD deployment running
unattended on scheduled infrastructure, a typed React dashboard on a public URL, and a
proprietary derivatives dataset that is not purchasable cheaply.

That is a systems-engineering portfolio, and it exists whether or not the screener makes a rupee.
A working URL that has been updating itself daily for eight months — including on the days the
signals were wrong, visibly — is a stronger thing to show an interviewer or a hackathon judge
than any backtest curve.

Build it for that reason first, and let any trading edge be a bonus rather than the premise.

---

## APPENDIX — EVIDENCE BASE

**Case studies:** TRB (Dec 2023), RAVE (Apr 2026), VVV (Dec 2025–Sep 2026), AERO, FORM,
Oct 10 2025 liquidation cascade.

**Quantified datasets underpinning thresholds:**
- Keyrock — 16,000+ token unlock events, 40 tokens: ~90% negative price pressure; team unlocks
  worst (to −25%); ecosystem unlocks slightly positive; impact begins ~30 days pre-event
- Tokenomist — 236 unlock events: 1-month median −16.26%, mean −8.10%, modal band −30% to −20%
- CryptoNinjas/Storible — 389 tokens, 6 CEXs, 2024: Binance +87% at listing, 98% eventually dump,
  −70% average from listing price; 37% hit ATH on listing day and never reclaim
- Empirica — 7 years of Binance listings: −6.34% first week, +8% at 6 months
- Presto Labs — funding-rate changes explain ~12.5% of 7-day price variation, declining after;
  more useful cross-sectionally than per-asset
- Review of Accounting Studies — ~36,000 influencer tweets, 180 influencers, 1,600+ assets:
  initial positive returns, significant negative longer-horizon returns
- China Accounting and Finance Review — 1,160 cryptocurrencies over 9 years: size effect,
  distinctive reversal effect, illiquidity premium
- Journal of Banking & Finance (2026) — 40 cryptocurrencies: network activity, hashrate, and
  Google-search sentiment as robust out-of-sample forecasting signals
- Kraaijeveld & De Smedt — ~14% of crypto tweets attributable to bot accounts

**Mechanism references:**
- Funding formula, interest component, and ±0.05% clamp — Binance and Hyperliquid documentation
- Binance funding settlement frequency now varies per symbol (8h/4h/1h)
- Liquidation feed throttling — one order per second per symbol since mid-2021 across Binance,
  Bybit, OKX; Bybit restored full data Feb 2025
- Announcement API latency — 15s+ observed, country-dependent; Telegram ≥150ms; Twitter minutes

**Infrastructure references:**
- GitHub Actions `schedule` is best-effort — 5–45 min typical drift, 8–14 hour delays and dropped
  days reported, plus platform-wide periods where scheduled runs stopped being created while
  `workflow_dispatch` kept working; self-hosted runners do not help
- Scheduled workflows are auto-disabled after 60 days of repository inactivity (scheduled
  triggers only)
- cron-job.org free tier — up to 1 run/minute, configurable method/headers/body, last 50
  executions with scheduled vs actual times, failure notifications, 30s job timeout, 64 KB
  response cap; no SLA, and jobs that repeatedly fail or run long may be deliberately delayed
- `GITHUB_TOKEN` cannot fire `workflow_dispatch` or `repository_dispatch`; fine-grained PAT needs
  Contents read/write (the permission most commonly omitted, producing a 403)
- GitHub Actions ToS — no cryptomining, serverless computing, or activity unrelated to producing,
  testing, deploying or publishing the repository's software project; usage is monitored
- Actions minutes: unlimited on public repos; 2,000 Linux min/month and 500 MB storage on private
  Free plan
- GitHub Pages: 1 GB source repo (recommended) and 1 GB published site, 100 GB/month soft
  bandwidth, 10 builds/hour — the build limit does not apply to custom Actions deploys
