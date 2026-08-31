# Full application — delivery status

**Branch:** `full-application/build` · **Milestone 1** — production PostgreSQL foundation

## Completed vertical slices

_None yet._ Milestone 1 is in progress.

## Current slice

**M1-S1 — PostgreSQL foundation.** Config + secret-provider boundary, pooled
transactional engine with `SET LOCAL` scope, versioned migration runner, first
migration (organisation hierarchy, identity, accounting calendar, hash-chained
audit log).

Built by four specialised agents on disjoint file sets, integrated by the lead.

## Next three deliverables

1. Repository layer, ordered ancestor-chain locking, audit service + API
2. DEF-01 fix — migrations removed from application startup; health/readiness
3. Audit Trail Viewer (SCR-28) with chain verification, on PostgreSQL data

## Blockers

None. The AppSail egress question is **not** a blocker: all database access sits
behind `app/backend/pg` interfaces, so the hosting route stays replaceable.

## Configurable defaults recorded

| Decision | Default | Where |
|---|---|---|
| Financial year start | April (month 4) | `organisation.fy_start_month`, per-entity override |
| Base currency | INR | `organisation.base_currency` |
| Location | first-class dimension, distinct from plant and zone | `location` table (resolves AMB-07) |
| WBS depth | arbitrary parent/child | ltree, milestone 3 |

## Tests and commits

| | |
|---|---|
| Existing suite | 452 passed, 1 xfail (DEF-01) — baseline preserved |
| PostgreSQL tests | added in M1-S1; skip locally without a DSN, run in CI |
| Latest commit | see `git log full-application/build` |
