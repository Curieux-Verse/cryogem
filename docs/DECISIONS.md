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
