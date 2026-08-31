# Full application — delivery status

**Branch:** `full-application/build` · **Milestone 1** — production PostgreSQL foundation

## Completed vertical slices

**M1-S1 — PostgreSQL foundation.** Migrations 001 (org hierarchy, identity,
accounting calendar, hash-chained audit log) and 002 (project, WBS with ltree,
budget heads, control + ledger cells). Config with a secret-provider boundary,
pooled transactional engine with `SET LOCAL` scope, versioned migration runner,
scoped repository, ordered ancestor-chain locking, audit chain + API,
health/readiness, SCR-28 Audit Trail Viewer.

Built by four agents on disjoint files, integrated by the lead, then reviewed
adversarially.

## Current slice

**M1-S2 — closing the DoD gap.** Two items outstanding before Milestone 1 can
be called done:

1. **PostgreSQL seed data.** `pg_template` runs `upgrade()` with no seed, so
   there is no demo dataset behind the PostgreSQL path. The DoD requires seeded
   data so a screen is demonstrable.
2. **Review finding F5** — schema adoption verifies table *existence* only, not
   triggers, constraints or column types. Its worst case adopts a
   `numeric budget_paise` as valid, which is the one float-leakage path in the
   system.

## Next three deliverables

1. `migrations/pg/seed_demo.sql` + demo-profile guard
2. F5: verify non-table objects and `*_paise` column types before adoption
3. Milestone 2 — settings and master data

## Blockers

None. The AppSail egress question is still open and still not blocking: all
database access sits behind `app/backend/pg` interfaces.

## Known gaps, recorded rather than implied

- **Whole-stream audit truncation is not detectable.** `audit_anchor` exists as
  a table with no writer. `verify_chain` returns
  `whole_stream_truncation_note` so an `intact: true` never implies more than
  it can support.
- The Audit Trail Viewer is a standalone `audit.html`, not yet wired into the
  main SPA shell's hash router.
- Review findings F8 (global secret-provider mutation) and F9 (`formatINR`
  routes paise through a JS double) are open, both low.

## Configurable defaults recorded

| Decision | Default | Where |
|---|---|---|
| Financial year start | April (month 4) | `organisation.fy_start_month`, per-entity override |
| Base currency | INR | `organisation.base_currency` |
| Location | first-class dimension, distinct from plant and zone | `location` table (resolves AMB-07) |
| WBS depth | arbitrary, ltree, `nlevel <= 100` | `wbs_element` |
| Whole-estate audit read | Administrator / System Administrator / Internal Auditor / Auditor | `_WHOLE_ESTATE_AUDIT_ROLES` |

## Tests and commits

| | |
|---|---|
| Full suite | **586 passed, 27 skipped** locally |
| CI | **all 5 jobs green**, including the PostgreSQL suite against postgres:16 |
| Manifest | 433 test functions, 973 assertions, 22 files |
| Latest | `full-application/build` — see `git log` |

**Proven against a real database, not merely reasoned about:** the lock query
parses and executes; a leaf spend locks *both* budget-owning ancestors in
`wbs_path` order and excludes zero-budget cells; `FOR UPDATE` serialises two
concurrent transactions on one cell; migrations apply, are idempotent, and
drift is caught; the audit chain round-trips.
