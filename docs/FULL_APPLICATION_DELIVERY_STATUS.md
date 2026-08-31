# Full application — delivery status

**Branch:** `full-application/build` · **Milestone 1 COMPLETE** · **Wave 2
integrated** — M2 settings/masters, M3 budget control, M4a identity/scope

## Traceability to the approved plan

`docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md` numbers work by **phase**;
this build numbers by **milestone**. Recorded rather than assumed:

| Plan phase | Milestone | Status |
|---|---|---|
| Phase 1 — PostgreSQL port, behaviour-identical | M1 | complete (`c7ca3f6`) |
| Phase 2 — Control cell, periods, budget slice (SCR-09/10/13) | M3 | **integrated** |
| Phase 3 — Identity, roles, scope, admin slice | M4a | **integrated** |
| Phase 3 — Settings and master data (SCR-30, Z-06) | M2 | **integrated** |
| Phase 4 — Approval engine | M4b | not started |

## Completed vertical slices

**M1-S1 — PostgreSQL foundation.** Migrations 001/002, config with a
secret-provider boundary, pooled transactional engine with `SET LOCAL` scope,
versioned migration runner, scoped repository, ordered ancestor-chain
locking, audit chain + API, health/readiness, SCR-28 Audit Trail Viewer.

**M1-S2 — seed data and schema-adoption validation.** Demo estate with a
strict profile guard; adoption verifies functions, triggers, named
constraints, the gist exclusion and every `*_paise` column's actual type.

**M3 — budget control.** Migration 003 (`budget_line` with an ORIGINAL
immutability trigger, `budget_revision`, `budget_transfer`, `budget_version`),
`pg/budget.py`, `pg/periods.py` (`FUTURE→OPEN→SOFT_CLOSED→CLOSED` with the
open-exception guard), 13 API routes, and SCR-09/10/13 wired to them.

**M4a — identity, roles and scope.** Migration 004 (`role_grant`,
`user_scope_grant`, `user_scope_restriction`, RLS policies on every scopable
table), `pg/roles.py`, `pg/rls.py` as a pure-Python mirror of the SQL
predicate, and `/api/admin/*`.

**M2 — settings and master data.** Migration 005 (`item_master`,
`vendor_master`, the custom-field engine, collision-safe numbering),
`pg/masters.py`, `/api/settings/*`, `/api/masters/*`, SCR-30 and the
organisation-hierarchy admin.

## Tests

**828 passed, 116 skipped, 0 failed** locally (Milestone 1 baseline: 608/49).
Every skip is a live-PostgreSQL test that runs in the `pg_tests` CI job.

## What integration found, and fixed

Every Wave 2 defect lived *between* streams, not inside one — the same
pattern as Milestone 1. All are fixed and held by tests that run **without a
live database**, because each lived exactly where a database-gated test would
have skipped straight past it.

| # | Defect | Held by |
|---|---|---|
| 1 | `api/budget.py` had no authentication and no permission check: `_scope_for` returned an unconditional `read_all=True` SERVICE scope, and `_actor` trusted an `X-User-Id` header — the value written into `created_by`, approval decisions and the audit chain | `test_budget_api_guard.py` |
| 2 | `api/masters.py` and `api/settings.py` read authorization from `X-Permissions`, so a caller could send `masters.tax_identity.reveal` and unmask every GSTIN and PAN | `test_masters_settings_api_guard.py` |
| 3 | `budget.compare_versions` read `budget_version` through raw `session.fetchone`/`fetchall` keyed on a caller-supplied `project_id` — no `{scope}` at all | `test_scope_enforcement.py` |
| 4 | The cell and line queries waived entity, plant and location, so an entity-restricted caller read every entity's budget | `test_scope_enforcement.py` |
| 5 | Revision/transfer approve and reject, and `create_version_snapshot`, acted on ids with no scope check; `transition_period` let a caller close another entity's period | the authorisation matrix + `test_scope_enforcement.py` |
| 6 | `seed.py`'s fragment loader never ran — `SEED_PARTS_DIR` was computed from an undefined name behind an `if … in dir()` that silently yielded `None`, and `seed()` never called the loader. Every stream's seed fragment would have loaded nowhere | `test_pg_seed.py` |

Defect 6 was mine, from the contract-freezing commit.

**The gate that should have caught 3–5 did not exist.**
`tests/test_scope_enforcement.py` is now that gate: an AST walk asserting no
service module reads a scopable table off the chokepoint. It inspects
`fetchone` and `fetchall` as well as `execute`, because the actual bypass used
the first two and the plan's `execute`-only walk sails past them. Reads that
are correct unscoped carry a stated reason, a bare `# scope-exempt` with no
reason does not silence it, and a planted bypass proves it fails.

## Known gaps, recorded rather than implied

- **Row-level data scope is enforced at the query layer but not yet
  populated.** `_scope_for` builds a Scope from the authenticated principal;
  per-user grants exist in migration 004 but are not yet resolved into it, so
  a non-whole-estate principal is currently unrestricted. `_scope_for` says
  so in its docstring rather than implying otherwise. Wiring
  `roles.resolve_scope` into the API is the first task of the next wave.
- **Auditor holds none of the six new settings/masters permissions.** Plan
  §10.4 lists Auditor among the roles entitled to a full GSTIN, but
  `test_aud_c_006_auditor_is_read_only` pins Auditor to an allow-list of four
  permissions. Refusing is fail-closed; widening an audit-finding assertion
  to grant access is not a call to make silently. **Needs D-12.**
- **No reveal endpoint.** The reveal permissions exist server-side; the
  frozen contract carries no reveal route and no `can_reveal` field, so the
  UI control is present but permanently disabled.
- **`core/api.js` is hard-wired to `/api/audit`,** so both frontend streams
  duplicated its session/correlation/RFC-7807 handling. A base-path
  parameterised client should land before a third copy.
- **Whole-stream audit truncation is not detectable.** `audit_anchor` has no
  writer; `/api/audit/chain/verify` returns `whole_stream_truncation_note` so
  `intact: true` never implies more than it can support.
- SCR-28, SCR-09/10/13 and SCR-30 are standalone pages, not yet in the SPA
  shell's hash router.
- Review findings F8 (global secret-provider mutation) and F9 (`formatINR`
  routes paise through a JS double) remain open, both low.

## Configurable defaults recorded

| Decision | Default | Where |
|---|---|---|
| Financial year start | April (month 4) | `organisation.fy_start_month`, per-entity override |
| Base currency | INR | `organisation.base_currency` |
| Location | first-class dimension, distinct from plant and zone | `location` (resolves AMB-07) |
| WBS depth | arbitrary, ltree, `nlevel <= 100` | `wbs_element` |
| Period states | `FUTURE → OPEN → SOFT_CLOSED → CLOSED`; close blocked by an Open exception | `pg/periods.py` |
| Transfer permissions | reuse `revision.create` / `revision.approve` | both already inside `MAKER_CHECKER` |
| Demo seeding | `CAPEX_PROFILE=local-demo` **and** a disposable database name | `app/backend/pg/seed.py` |

## Next

1. Adversarial QA/security review of the integrated result.
2. Wire `roles.resolve_scope` into every API scope, closing the gap above.
3. M4b — the approval engine (plan Phase 4).
