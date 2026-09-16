# gem-screener

A disqualification-first screener for perpetual futures.

Most screeners rank. This one removes first, and only ranks what is left. The
distinction is the whole design:

```
Layer 1   binary kill switch      9 checks, no override, ever
Layer 2   cross-sectional score   6 weighted blocks, survivors only
Layer 3   structure and risk      advisory; annotates and vetoes, never promotes
Journal   forward returns         append-only, with a random control group
```

An asset that fails Layer 1 never reaches Layer 2. There is no score high
enough to overturn a disqualification, and no code path that tries.

## Why it is built this way

Five findings from the research drove the architecture, and each one is a rule
enforced somewhere in the code rather than a paragraph in a document:

**Derivatives are a risk check, never a buy trigger.** TRB's funding went
negative — usually read as bullish crowd positioning — hours before a 78%
collapse. So funding and open interest appear only in Layer 3, only as
fragility flags, and no reading of either can raise a score.

**Unlocks are overwhelmingly negative, and the recipient matters most.** Across
16,000+ events roughly 90% were negative, team allocations worst. The mean was
−8.10% while the median was −16.26%, which is why every statistic in this
system reports median AND mean separately: the gap between them is the finding.

**Survivorship bias is invisible and total.** A delisted symbol vanishes from
Binance's `exchangeInfo` — it is not marked dead, it is simply absent. So daily
universe snapshots are retained forever, and the backtest reconstructs the
symbol list as it stood on each morning.

**Point-in-time integrity or nothing.** Every row carries `fetched_at_utc`, and
events are filtered on `first_seen_utc <= as_of`. A screener that can see one
day into the future finds every setup perfectly.

**The journal is the only component that produces truth.** Everything before it
produces plausible-looking output. It is append-only, enforced by database
triggers, and every ranked signal is journalled beside a random Layer 1
survivor that did *not* make the list — because "my picks returned +8%" is
unreadable without knowing what a coin drawn at random did.

## Getting started

```bash
python -m venv .venv && .venv/Scripts/activate    # or bin/activate
pip install -r requirements.txt
cp .env.example .env                              # every secret is optional
python -m src.cli init-db
python -m src.cli collect daily
python -m src.cli collect supply                   # holders + unlocks; slow on first run
python -m src.cli screen
python -m src.cli report
python -m src.cli publish
```

It runs with an empty `.env`. Missing optional keys log a warning and mark the
affected block unavailable; they never silently zero it.

### The dashboard

```bash
cd web && npm install && npm run dev
```

`npm run build` copies `data/public/` into the bundle and emits `web/dist/`.

## Commands

| Command | What it does |
|---|---|
| `init-db` | Creates every table, index and trigger. Idempotent. |
| `collect {daily,hourly,supply,fast}` | Runs a tier of collectors, or one by name. Stateless: no loop, no state. `supply` is holder concentration and the unlock calendar, on its own workflow (D-040). |
| `screen` | Layer 1, then Layer 2, then Layer 3 on the ranked head. |
| `journal` / `journal --report` | Records signals and controls / prints statistics. |
| `report` | Markdown to `reports/YYYY-MM-DD.md`. |
| `publish` | Bakes `data/public/*.json` for the dashboard. |
| `backtest --start --end` | Refuses until ~6 months of recorded days exist. |
| `doctor` | Checks config, database and data freshness. |

## How it stays running

Every workflow is `workflow_dispatch` only. **There is no `on: schedule`
anywhere, and CI fails if one appears.** GitHub's scheduler drifts 5–45
minutes, drops days entirely, and auto-disables scheduled workflows after 60
days of repository inactivity — which is exactly when a data-collection project
looks idle.

Triggering is external (cron-job.org), and monitoring is two independent
layers: cron-job.org catches "the trigger never fired", healthchecks.io catches
"the job started and then failed". One layer cannot detect its own silence.

See [docs/RUNBOOK_AUTOMATION.md](docs/RUNBOOK_AUTOMATION.md) for the setup,
including two deliberate break tests.

## Documentation

| File | Contents |
|---|---|
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every consequential decision and defect, with the reasoning. Read this one. |
| [docs/API_DEVIATIONS.md](docs/API_DEVIATIONS.md) | Where the APIs differ from their documentation. |
| [docs/RESEARCH_LOG.md](docs/RESEARCH_LOG.md) | Open questions and what was found. |
| [docs/RUNBOOK_AUTOMATION.md](docs/RUNBOOK_AUTOMATION.md) | Turso, cron-job.org, healthchecks.io, PAT rotation. |

## Non-negotiables

These are enforced in code or CI, not by convention:

- **No trading execution, ever.** No API key with trade permission. Read-only
  public endpoints only.
- **No `on: schedule`** in any workflow. CI greps for it.
- **Secrets never leave GitHub Actions secrets** — not into the repo, not into
  `data/public/`, not into the browser bundle, not into logs. Two independent
  gates scan for key shapes before anything is committed or deployed.
- **The database is never committed.**
- **Journal entries are never deleted, edited or filtered.** Database triggers
  refuse both `DELETE` and `UPDATE`.
- **A threshold is never loosened because it disqualified something
  interesting.** It gets a `docs/DECISIONS.md` entry instead.

## Status

Phases 0–11 implemented; 487 Python tests passing. The parts that need calendar
time rather than code are, by design, not done: the 14-day autonomy proof, six
months of journal data, and the first honest backtest. The backtest harness is
built and deliberately refuses to run until the data supports it.

## Licence

Private project. Not investment advice.
