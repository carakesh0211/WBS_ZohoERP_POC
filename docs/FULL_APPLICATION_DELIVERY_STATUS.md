# Full application — delivery status

**Branch:** `full-application/build` · **Waves 1-3 COMPLETE** — PostgreSQL
foundation, settings/masters, budget control, identity/scope, and security
closure · **Wave 4 in progress** — M4b configurable approval engine

## Traceability to the approved plan

`docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md` numbers work by **phase**;
this build numbers by **milestone**. Recorded rather than assumed:

| Plan phase | Milestone | Status |
|---|---|---|
| Phase 1 — PostgreSQL port, behaviour-identical | M1 | complete (`c7ca3f6`) |
| Phase 2 — Control cell, periods, budget slice (SCR-09/10/13) | M3 | **integrated** |
| Phase 3 — Identity, roles, scope, admin slice | M4a | **integrated** |
| Phase 3 — Settings and master data (SCR-30, Z-06) | M2 | **integrated** |
| Phase 4 — Approval engine | M4b | **in progress (Wave 4)** |

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

**892 passed, 125 skipped, 0 failed** locally (Milestone 1 baseline: 608/49),
and **all five CI jobs green**. Every local skip is a live-PostgreSQL test
that runs in the `pg_tests` job.

That job now proves it, and prints the proof:

    live PostgreSQL suite: 283 collected, 283 executed, 0 skipped, 0 failed

A green pytest shows nothing ran *wrong*, not that anything ran: a suite that
skipped every live test exits 0 and reports success, which is how this job
once passed while nine end-to-end tests had quietly opted out of it. The job
attaches a `postgres:16` service and sets `CAPEX_DB_URL`, so the live suite is
guaranteed runnable there, and the step after it reads collected, executed and
skipped counts from the JUnit report and fails if the suite goes quiet.

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

## Wave 3 security-closure gate — walked line by line

Verified against code and CI evidence at `e559d60`, not from memory.

| Gate line | Evidence |
|---|---|
| Restricted principals receive resolved scopes at every router | all five routers call `principal_scope.scope_for_request`; `admin_access.py` was the last, closed in the pre-Wave-4 walk |
| A no-grant principal fails closed | `denied_scope()` compiles to the literal `FALSE`, now including when a query waives every dimension |
| All identified tables carry RLS | the eight named tables covered by migration 006; 19 RLS tables total |
| A literal `*` grant is impossible | refused by `validate_scope_value`, by `compile_scope` before the `read_all` short-circuit, by `roles.set_scope`, and by a CHECK constraint in 007 |
| Zero-budget locking proven under concurrency | the `budget_paise <> 0` filter is gone from `_LOCK_SQL`; live concurrency tests execute in CI |
| All five screens routable in the SPA | `#audit-trail`, `#budget-grid`, `#budget-compare`, `#budget-availability`, `#settings` |
| Full local suite passes | 1131 passed, 154 skipped, 0 failed |
| Live PostgreSQL executes with zero skips | `436 collected, 436 executed, 0 skipped, 0 failed, 0 errored` |
| All five CI jobs green | run for `288df9f` |
| No unresolved critical or high finding | both HIGH fixed; M1, M2, M3, L2, L6 also fixed; L1/L3/L4/L5 recorded below |

Two things the gate walk caught that a green CI had not, both worth naming
because they are the kind that hide behind a passing suite:

- `admin_access.py` built its session `Scope` by hand. The `read_all` was
  already gone, so nothing leaked — but Contract 4 says every router resolves
  through one function, and a hand-built scope is how the fifth router drifted
  out of a claim this document made about "all four".
- The manifest's body hash used `ast.dump`, which renders CPython's own AST
  repr. Moving this machine from Python 3.11 to 3.14 mid-session changed all
  773 recorded hashes at once with not one test edited. Committing that would
  have broken CI, which runs 3.11 — and a gate that fires on every test
  because the interpreter moved is a gate someone switches off. It now uses
  `ast.unparse`, whose output is the Python language rather than an internal
  repr; CI at 3.11 verifying a manifest generated at 3.14 is the proof.

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

## Wave 3 — security closure (four of five streams integrated)

Closes the gaps Wave 2 recorded as open. No new business capability. All five
CI jobs green at `6ef4bc4`, with **438 live PostgreSQL tests collected, 438
executed, 0 skipped** — the first execution of migrations 006 and 007 against
a real server.

| Gap | Closed by | Status |
|---|---|---|
| Restricted principals received unrestricted scopes | `pg/principal_scope.py`, wired into all four routers | **closed** |
| `read_all` derived from role NAMES | removed; it now comes from `user_access_flag` only | **closed** |
| Eight scopable tables carried no RLS | migration 006 | **closed** |
| A literal `*` grant disabled scoping | migration 007 + three rejection layers | **closed** |
| Zero-budget cells locked implicitly, outside the declared set | `_LOCK_SQL` no longer filters on `budget_paise` | **closed** |
| Five screens outside the SPA shell | stream 5 | **in progress** |

### What the wave found that was not on its own list

**`api/settings.py` was leaking the organisation hierarchy.** It returned an
unrestricted scope on the stated grounds that "no row-level scope dimension
applies to the organisation hierarchy itself". That router serves `entity`,
`plant` and `location` — three of the four dimensions, all carrying RLS since
migration 004. An entity-restricted caller reading `GET /api/settings/entities`
saw every entity, and because the all-`None` scope rendered as the old `*`
wildcard, RLS waved it through too. Both enforcement layers agreed, and both
were wrong. `tests/test_scope_enforcement.py` had the module declared as
`NO_ROW_SCOPE`, which is why nothing caught it.

**`budget_head` is a scope-carrying table with no policy.** It has `entity_id
NOT NULL REFERENCES entity` and appears in neither 004 nor 006, so an
entity-restricted caller can enumerate every other entity's budget heads. It
was outside the contract's eight, so it is recorded in `scope_inventory.py`
as an explicit gap rather than fixed by widening a migration mid-wave — which
is the independent inventory doing precisely the job it exists for.

**The adoption verifier does not check RLS.** `migrate_pg._adoption_problems`
verifies tables, functions, triggers, named constraints and `*_paise` column
types, but never `CREATE POLICY` or `ENABLE/FORCE ROW LEVEL SECURITY`. A
database carrying 006's function but none of its policies is recorded as
"006 (adopted)" and serves with RLS silently absent. This applies to 004 too.

### Cross-stream reconciliation

Streams 1 and 3 collided, visible only once both were merged. Stream 1
asserted a pre-Wave-3 `*` grant survives as an ordinary id; stream 3 made the
value refused. The refusal lives at `repo.compile_scope`, not at `Scope`
construction, so such a grant built a Scope happily and raised on the first
query — a 500 on every route that principal touched. It now denies at
resolution, because `*` meant "unrestricted" to the old RLS predicate and "an
id matching nothing" to `compile_scope`, and a security boundary is not a
place to guess between opposites.

## Known gaps, recorded rather than implied

- **`budget_head` carries `entity_id` and has no RLS policy.** Out of Wave 3's
  contracted eight, so it was recorded rather than fixed mid-wave. An
  entity-restricted caller can enumerate other entities' budget heads. The fix
  is one policy shaped exactly like `accounting_period_scope`. Tracked in
  `app/backend/pg/scope_inventory.py` as an explicit gap, with a test that
  fails if someone closes it without reclassifying the entry.
- **The adoption verifier does not check RLS.**
  `migrate_pg._adoption_problems` never inspects `pg_policy` or
  `relrowsecurity`, so a database with 006's function and none of its policies
  is recorded as adopted while serving with RLS absent. Applies to 004 too.
- **`item_master` / `vendor_master` are policied on principal presence, not
  scope.** Neither carries nor reaches a scope dimension, so this is correct
  for today's schema — but it is the same reasoning that was already false for
  `api/settings.py`, so it is written down rather than assumed permanent.

- **Auditor holds none of the six new settings/masters permissions.** Plan
  §10.4 lists Auditor among the roles entitled to a full GSTIN, but
  `test_aud_c_006_auditor_is_read_only` pins Auditor to an allow-list of four
  permissions. Refusing is fail-closed; widening an audit-finding assertion
  to grant access is not a call to make silently. **Needs D-12.**
- **No reveal endpoint.** The reveal permissions exist server-side; the
  frozen contract carries no reveal route and no `can_reveal` field, so the
  UI control is present but permanently disabled.
- **Whole-stream audit truncation is not detectable.** `audit_anchor` has no
  writer; `/api/audit/chain/verify` returns `whole_stream_truncation_note` so
  `intact: true` never implies more than it can support.
- **The five SCR screens are routable but carry no primary-navigation entry.**
  `#audit-trail`, `#budget-grid`, `#budget-compare`, `#budget-availability`
  and `#settings` are reachable, deep-linkable and permission-gated. Adding
  rows to the navigation rail changes the client-approved UI -- it renders
  inside every approved screenshot -- so it needs client sign-off and a
  deliberate re-baselining rather than a stream overwriting the evidence.
- **`settings.css` has two unscoped selectors** (`input:disabled`,
  `.field label`) that move unrelated screens when the stylesheet is loaded
  globally. Measured at 694 pixels on one screen; the stylesheets now load per
  route, which contains the symptom rather than fixing the cause.
- **`#userAvatar` fails WCAG 2 AA contrast** at 4.02:1 against the required
  4.5:1. It is excluded from the a11y tests by exactly one selector, with a
  guard asserting that stays the only exclusion. Fixing it means editing the
  byte-frozen `styles.css`, so it needs sign-off.
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
