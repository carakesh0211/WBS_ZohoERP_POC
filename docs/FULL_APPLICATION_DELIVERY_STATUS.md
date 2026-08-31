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
`user_scope_grant`, `user_scope_restriction`, RLS policies on eleven tables),
`pg/roles.py`, `pg/rls.py` as a pure-Python mirror of the SQL predicate, and
`/api/admin/*`. An earlier draft of this document said "RLS policies on every
scopable table". That was wrong, and the gap is recorded below.

**M2 — settings and master data.** Migration 005 (`item_master`,
`vendor_master`, the custom-field engine, collision-safe numbering),
`pg/masters.py`, `/api/settings/*`, `/api/masters/*`, SCR-30 and the
organisation-hierarchy admin.

## Tests

**892 passed, 124 skipped, 0 failed** locally (Milestone 1 baseline: 608/49).
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

## Adversarial review — what it found after integration

An independent reviewer went over the integrated result. Ten findings: seven
fixed, three recorded below.

| # | Finding | Outcome |
|---|---|---|
| 1 | `X-Actor-Id` survived on three **read** paths. The integration replaced the `_actor()` helper but three call sites read the header directly, so the audit entry for a tax-identity reveal named whoever the caller claimed — and named `SYSTEM` when no header was sent, which is what the frontend sends | **fixed**; the constant is deleted, so it cannot come back |
| 2 | `api/settings.py` called `_reveal_entity_tax_identity`, a name defined nowhere. Every successful entity reveal raised `NameError`, rolled back and returned 500, so no entity reveal ever succeeded and none was ever audited. **This was mine, not the stream's**: the function was present in stream 4's own commit and my auth patch deleted it as collateral, along with `_reveal_context` in both routers, by replacing a whole block of helpers between two anchors | **fixed**; all three restored from the stream's commit, and `tests/test_no_undefined_names.py` now catches the whole class |
| 3 | The vendor edit dialog round-tripped the **masked** `gst_no` into a `PUT`, violating the column CHECK, and `api/masters.py` did not catch `CheckViolation` — so editing a vendor returned an unhandled 500 and renaming one was impossible through the UI | **fixed**; a masked value is refused with an actionable 422 |
| 4 | The scope AST gate was blind to computed table names (`FROM {kind.table}`, how `pg/masters.py` writes every statement), to SQL held in a variable, and to `app/backend/api/` entirely | **fixed**; unanalysable SQL is flagged, both directories scanned, no-row-scope declared in one place |
| 5 | `FinanceApprover` sat in the whole-estate role set. That set reads as a concession for *reads*, but the Scope it builds is used on the **write** paths — so the maker-checker approver for `revision.approve` could approve revisions and close periods in every entity | **fixed**; removed from the set |
| 6 | `_PERIOD_SCOPE_COLUMNS` waived `project`, `plant` and `location`, so a project-restricted principal saw and could close every entity's periods — the same waive-versus-refuse defect as integration finding #4, on the other side | **fixed**; omitted rather than waived, so the restriction is refused |
| 9 | `test_pg_audit_api_e2e.py` skipped when `seed_demo.sql` was missing. That file is committed and frozen, so its absence is a deleted file, not an environment condition, and the skip would have taken the whole e2e suite out of CI silently | **fixed**; asserts |
| 7 | RLS missing on `accounting_period`, the five budget document tables, `item_master` and `vendor_master` | **open** — see gaps |
| 8 | `recompute_cell`'s `UPDATE` takes a cell lock implicitly, after `lock_affected_cells`; the period roll can take none at all | **open** — see gaps |
| 10 | `Scope.as_settings()` renders a literal scope id of `*` identically to "unrestricted" | **open** — see gaps |

Two of the ten were regressions this integration introduced, not defects the
streams delivered — #2 above, and the deleted `_reveal_context` that CI caught
straight after. Both are the same mistake: patching by replacing everything
between two anchors, without checking what else lived in between. The gate
that now holds it, `tests/test_no_undefined_names.py`, resolves every name
each backend module uses and fails on any bound nowhere. It runs on source in
under a second, and would have caught both instantly — the reveal paths need a
live PostgreSQL, so every local test touching them skipped and the suite
stayed green.

The reviewer also confirmed, by attacking them, that the router guards hold on
every route, that `X-Permissions` is genuinely inert, that no float or Decimal
touches a paise value, that maker-checker closes its TOCTOU window, that no
caller data reaches SQL uninterpolated, that every mutation audits inside its
own transaction, and that the frontend carries no `style=` attribute, no
`innerHTML`, and no client-side permission decision.

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
- **RLS does not cover every scopable table.** Migration 004 enables it on
  eleven. `accounting_period`, the five budget document tables, `item_master`
  and `vendor_master` have none, so for those the application layer is the
  only layer. `accounting_period` is the starkest: it carries `entity_id NOT
  NULL`, the exact shape that earns the org tables their policies. `pg/rls.py`
  derives its registry from the migration, so its tests cannot detect a table
  absent from both.
- **The §7.4 lock proof has an unstated exception.** `lock_affected_cells`
  filters `budget_paise <> 0`, but `recompute_cell`'s `UPDATE` acquires that
  row's lock regardless — so a zero-budget cell is written without having been
  locked, and `_roll_cells_for_entity` can run with an empty lock set in
  exactly the case it exists for: a period opening where all budget is still
  future-dated. No deadlock has been demonstrated, and the reviewer tried. The
  defect is that the invariant and its docstring now overclaim, so the next
  mutation built on them inherits an exception nobody wrote down.
- **A literal scope id of `*` is indistinguishable from "unrestricted".**
  `Scope.as_settings()` renders both as `*`, and the grants endpoint does not
  validate grant values, so `["*"]` is stored as a restriction but read by RLS
  as none. Not exploitable today — it needs `admin.reset`, and the stricter
  layer wins wherever `repo.query` is used — but it breaks defence in depth
  exactly where RLS is the only layer.
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
