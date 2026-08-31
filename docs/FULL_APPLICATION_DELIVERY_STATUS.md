# Full application — delivery status

**Branch:** `full-application/build` · **Milestone 1 COMPLETE** — production PostgreSQL foundation

## Completed vertical slices

**M1-S1 — PostgreSQL foundation.** Migrations 001 (org hierarchy with location
as a first-class dimension, identity, accounting calendar, hash-chained audit
log) and 002 (project, WBS with ltree, budget heads, control + ledger cells).
Config with a secret-provider boundary, pooled transactional engine with
`SET LOCAL` scope, versioned migration runner, scoped repository, ordered
ancestor-chain locking, audit chain + API, health/readiness, SCR-28 Audit Trail
Viewer.

**M1-S2 — seed data and schema-adoption validation.** Demo estate with a strict
profile guard; adoption now verifies functions, triggers, named constraints,
the gist exclusion and every `*_paise` column's actual type.

## Definition of done — satisfied and demonstrated

| | |
|---|---|
| Migration | 001, 002, `-- ROLLBACK:` sections, drift detection |
| Backend | repository + services, scoped queries, ancestor-chain locking |
| API | audit + health routers, RFC-7807, `X-Correlation-Id`, cursor paging |
| UI | SCR-28 wired to the real API, four states, tokens-only CSS |
| Permissions | router-level `audit.read`, server-side, fail-closed |
| Audit | append-only, hash-chained, verified end to end |
| Seed | `migrations/pg/seed_demo.sql` with an unbypassable profile guard |
| Tests | **608 local (49 skipped), 150 in the PostgreSQL job with ZERO skips** |
| Docs | this file |
| CI | **all 5 jobs green** |

**Executed against a real PostgreSQL, not reasoned about:** the lock query
parses and runs; a leaf spend locks *both* budget-owning ancestors in
`wbs_path` order, excluding zero-budget cells; `FOR UPDATE` serialises two
concurrent transactions on one cell; migrations apply, are idempotent, and
drift is caught; adoption refuses a missing trigger and a `numeric budget_paise`;
the seeded audit chain verifies; and every audit route refuses an
unauthenticated caller.

## Current slice

None. Milestone 1 is closed. Milestone 2 — settings and master data — is next.

## Next three deliverables

1. M2: organisation/entity/plant/location/department CRUD, screens and scopes
2. M2: roles, permissions and role-to-scope assignment
3. M2: item and vendor masters with source, external id and duplicate detection

## Blockers

None. The AppSail egress question remains open and remains non-blocking: all
database access sits behind `app/backend/pg` interfaces.

## Known gaps, recorded rather than implied

- **Whole-stream audit truncation is not detectable.** `audit_anchor` exists as
  a table with no writer. `/api/audit/chain/verify` returns
  `whole_stream_truncation_note` so `intact: true` never implies more than it
  can support.
- SCR-28 is a standalone `audit.html`, not yet wired into the SPA shell's hash
  router.
- Review findings F8 (global secret-provider mutation) and F9 (`formatINR`
  routes paise through a JS double) remain open, both low.

## Configurable defaults recorded

| Decision | Default | Where |
|---|---|---|
| Financial year start | April (month 4) | `organisation.fy_start_month`, per-entity override |
| Base currency | INR | `organisation.base_currency` |
| Location | first-class dimension, distinct from plant and zone | `location` (resolves AMB-07) |
| WBS depth | arbitrary, ltree, `nlevel <= 100` | `wbs_element` |
| Whole-estate audit read | Administrator / System Administrator / Internal Auditor / Auditor | `_WHOLE_ESTATE_AUDIT_ROLES` |
| Demo seeding | `CAPEX_PROFILE=local-demo` **and** a disposable database name, neither bypassable by `--force` | `app/backend/pg/seed.py` |
