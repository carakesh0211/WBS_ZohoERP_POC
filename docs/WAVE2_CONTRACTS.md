# Wave 2 — frozen contracts and file ownership

**Frozen at `c7ca3f6`.** Five implementation streams run concurrently in isolated
worktrees. Everything below is fixed for the duration of the wave: an agent that
needs a change reports it to the lead rather than editing.

## Traceability to the approved plan

`docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md` numbers work by **phase**; this
build numbers by **milestone**. They are not the same axis, so the mapping is
recorded rather than assumed.

| Plan phase | Milestone | Wave | Status |
|---|---|---|---|
| Phase 1 — PostgreSQL port, behaviour-identical | M1 | 1 | **complete** (`c7ca3f6`) |
| Phase 2 — Control cell, periods, budget slice (SCR-09/10/13) | **M3** | 2 | streams 1, 2 |
| Phase 3 — Identity, roles, scope, admin slice | **M4a** | 2 | stream 3 |
| Phase 3 — Settings and master data (§18.5 Z-06, REQ-SEC-006, REQ-INT-025/026) | **M2** | 2 | streams 4, 5 |
| Phase 4 — Approval engine | M4b | 3 | not started |

Wave 2 deliberately runs M2, M3 and M4a together because their file sets are
disjoint. Plan §16's dependency graph (`1 → {2,3}`) permits it: both depend only
on Phase 1, which is done.

Requirements covered: `REQ-BUD-001`, `REQ-BUD-004`, `REQ-BUD-020`, `REQ-REV-008`,
`REQ-WBS-001`, `REQ-SEC-006`, `REQ-SEC-007`, `REQ-INT-025`, `REQ-INT-026`,
`REQ-RPT-010` (location dimension).

## Migration numbers — assigned, do not collide

| File | Owner |
|---|---|
| `migrations/pg/003_budget_planning.sql` | stream 1 |
| `migrations/pg/004_identity_scope.sql` | stream 3 |
| `migrations/pg/005_master_data.sql` | stream 4 |

`001`, `002`, `seed_demo.sql` and the runner are **lead-owned and frozen**.

**Seed fragments.** Each backend stream writes
`migrations/pg/seed_parts/<NNN>_<area>.sql`, loaded by the lead after
`seed_demo.sql`. `discover()` ignores the subdirectory (it only scans `*.sql`
entries at the top level), so this cannot break migration discovery — the
mistake `seed_demo.sql` made in M1-S2.

## Money

Integer paise everywhere. `bigint` in schema, `int` in Python, integer in JSON.
No float, no `Decimal`, no rupee value, in code, fixtures or seeds.

---

## API contract — Budget (stream 1 owns, stream 2 consumes)

```
GET /api/budget/periods?entity_id=&state=
 -> {"items":[{"period_id","entity_id","period_start","period_end",
               "state":"FUTURE|OPEN|SOFT_CLOSED|CLOSED","closed_at","closed_by"}]}

POST /api/budget/periods/{period_id}/transition   {"to_state": "..."}
 -> {"period_id","state","transitioned_at"}
 409 PERIOD_HAS_OPEN_EXCEPTIONS when an Open reconciliation exception exists

GET /api/budget/cells?project_id=&wbs_id=&budget_head_id=&cursor=&limit=
 -> {"items":[{"wbs_id","wbs_path","budget_head_id",
               "budget_paise","original_paise","revisions_paise","future_budget_paise",
               "ordered_paise","commitment_paise","actual_paise","received_paise",
               "received_not_billed_paise","pr_reserved_paise",
               "exposure_paise","available_paise","recomputed_at"}],
     "next_cursor","has_more"}

GET /api/budget/availability?wbs_id=&budget_head_id=&amount_paise=
 -> {"wbs_id","budget_head_id","owning_wbs_id",
     "budget_paise","exposure_paise","available_paise","requested_paise",
     "verdict":"OK|WATCH|CRITICAL|EXCEEDS_BUDGET",
     "shortfall_paise","checked_at"}

GET /api/budget/lines?project_id=&wbs_id=&version=
 -> {"items":[{"budget_line_id","wbs_id","budget_head_id",
               "kind":"ORIGINAL|REVISION|TRANSFER","amount_paise",
               "effective_from","effective_to","status","justification",
               "created_at","created_by","version_no"}]}

POST /api/budget/revisions
     {"wbs_id","budget_head_id","delta_paise","effective_from","justification"}
 -> {"revision_id","status":"DRAFT","delta_paise"}

POST /api/budget/transfers
     {"from_wbs_id","from_head_id","to_wbs_id","to_head_id",
      "amount_paise","effective_from","justification"}
 -> {"transfer_id","status":"DRAFT","amount_paise"}

GET /api/budget/versions?project_id=            (SCR-10 comparison)
 -> {"items":[{"version","label","created_at","total_paise"}]}
GET /api/budget/compare?project_id=&left=&right=
 -> {"rows":[{"wbs_id","budget_head_id","left_paise","right_paise","delta_paise"}]}
```

`exposure = commitment + actual + pr_reserved`; `available = budget - exposure`.
Both are **subtree sums** over `wbs_path`. `verdict` thresholds come from
`domain.WATCH_PCT` / `CRITICAL_PCT` — configuration, not literals.

Errors: RFC-7807 with `code`; every response carries `X-Correlation-Id`.

## API contract — Settings and masters (stream 4 owns, stream 5 consumes)

```
{collection} ∈ organisations | entities | divisions | branches | zones
              | plants | locations | departments

GET  /api/settings/{collection}?cursor=&limit=&q=&is_active=
 -> {"items":[{ "<id field>", "code","name", ...area-specific...,
                "is_active","created_at","created_by",
                "updated_at","updated_by","version_no"}],
     "next_cursor","has_more"}
POST /api/settings/{collection}                       -> the created row
PUT  /api/settings/{collection}/{id}                  -> the updated row
     body MUST carry version_no; mismatch -> 409 VERSION_CONFLICT
POST /api/settings/{collection}/{id}/deactivate       -> {"id","is_active":false}

GET  /api/masters/items | /api/masters/vendors
     ?cursor=&limit=&q=&source=&mapping_status=&is_active=
 -> {"items":[{ "item_id"|"vendor_id","code","name",
                "source":"LOCAL|IMPORT|ZOHO",
                "external_source","external_id","external_last_modified",
                "source_of_truth_status","duplicate_of","mapping_status",
                "is_active","version_no", ...}],
     "next_cursor","has_more"}
POST /api/masters/{kind}          source is forced to LOCAL; ZOHO rows are never
                                  created through this API
PUT  /api/masters/{kind}/{id}     refuses to edit a ZOHO-sourced field
POST /api/masters/{kind}/{id}/deactivate
GET  /api/masters/{kind}/duplicates
 -> {"items":[{"id","code","name","duplicate_of","reason"}]}
```

**Vendor tax identity is masked by default.** `gst_no` renders
`27ABCDE****1Z5`, `pan_no` renders `ABCDE****F`, unless the caller holds the
reveal permission — and a full reveal writes an audit entry. Never hashed:
hashing destroys the matching and statutory-reporting function these exist for.

## API contract — Access administration (stream 3 owns)

```
GET  /api/admin/roles
 -> {"items":[{"role","permissions":[...]}]}
GET  /api/admin/users/{user_id}/grants
 -> {"user_id","roles":[...],
     "scopes":{"entity_ids":[...],"plant_ids":[...],
               "project_ids":[...],"location_ids":[...],"read_all":bool}}
PUT  /api/admin/users/{user_id}/grants
     {"roles":[...],"scopes":{...}}   -> the updated grant
```

`null` on a scope dimension means unrestricted; `[]` means **nothing**. That
distinction is a security property — see `pg/repo.py::compile_scope`, which
refuses a restriction a query cannot express rather than widening it.

---

## File ownership — strictly disjoint

| Stream | Owns exclusively |
|---|---|
| **1 Budget backend** | `migrations/pg/003_budget_planning.sql`, `migrations/pg/seed_parts/003_budget.sql`, `app/backend/pg/budget.py`, `app/backend/pg/periods.py`, `app/backend/api/budget.py`, `tests/test_pg_budget.py`, `tests/test_pg_periods.py` |
| **2 Budget frontend** | `app/frontend/src/features/budget/**`, `app/frontend/src/components/budget/**`, `app/frontend/budget.css`, `app/frontend/budget.html`, `tests/vrt/budget.spec.js` |
| **3 Identity & scope** | `migrations/pg/004_identity_scope.sql`, `migrations/pg/seed_parts/004_access.sql`, `app/backend/pg/roles.py`, `app/backend/pg/rls.py`, `app/backend/api/admin_access.py`, `tests/test_pg_roles.py`, `tests/test_pg_rls.py`, `tests/test_negative_access_matrix.py` |
| **4 Masters backend** | `migrations/pg/005_master_data.sql`, `migrations/pg/seed_parts/005_masters.sql`, `app/backend/pg/masters.py`, `app/backend/api/masters.py`, `app/backend/api/settings.py`, `tests/test_pg_masters.py`, `tests/test_pg_settings.py` |
| **5 Settings frontend** | `app/frontend/src/features/settings/**`, `app/frontend/src/components/settings/**`, `app/frontend/settings.css`, `app/frontend/settings.html`, `tests/vrt/settings.spec.js` |

### Lead-owned — frozen, nobody else edits

```
app/backend/main.py                 router mounting
app/backend/pg/{config,engine,migrate_pg,repo,locking,audit,seed}.py
app/backend/api/{audit,health}.py
migrations/pg/{001,002}_*.sql, migrations/pg/seed_demo.sql
tests/conftest.py, tests/conftest_pg.py, tests/TEST_MANIFEST.json
tools/build_test_manifest.py, .github/workflows/ci.yml
app/frontend/styles.css             BYTE-FROZEN, SHA-256 pinned in CI
app/frontend/index.html, app/frontend/app.js
app/frontend/extensions.css, app/frontend/src/core/**,
app/frontend/src/components/*.js    (the shared components)
```

Routers export `router = APIRouter()` and are **not** mounted by their author —
the lead mounts them. Mounting is unconditional; a router whose database is not
configured returns 503 from its own dependency.

New CSS goes in the stream's own stylesheet, using only tokens already present
in `research/30_contracts/C6_tokens.json`. No raw hex outside `:root`. Never
emit a `style="…"` attribute — CSP `style-src 'self'` blocks it; set geometry
through the CSSOM.

### Test fixtures

`tests/conftest_pg.py` provides `pg_url`, `pg_template`, `pg_connection`,
`pg_database`, `pg_scope`. It is **not** auto-discovered — import it explicitly,
copying the block at the top of `tests/test_pg_locking.py`. Omitting it makes
live tests fail at setup only where `CAPEX_DB_URL` is set: green locally,
broken in CI.

Live tests carry `@pytest.mark.pg` **and** `skipif(not CAPEX_DB_URL)`. The CI
job globs `tests/test_pg_*.py` and runs with `-rs`, so **every skip prints its
reason**. A skip is not a pass: a precondition guaranteed by the environment
must assert, not skip.
