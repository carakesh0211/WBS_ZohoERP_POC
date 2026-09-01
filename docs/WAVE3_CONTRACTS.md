# Wave 3 — frozen contracts and file ownership

**Frozen at `ac9eae1`.** Five implementation streams run concurrently in
isolated worktrees. Everything below is fixed for the duration of the wave: a
stream that needs a change reports it to the lead rather than editing.

Wave 3 closes the security and correctness gaps Wave 2 recorded as open. It
adds no new business capability. The approval engine (M4b) does not begin
until the security-closure gate at the end of this document passes.

## Migration numbers — assigned, do not collide

| File | Owner |
|---|---|
| `migrations/pg/006_rls_coverage.sql` | stream 2 |
| `migrations/pg/007_scope_sentinel.sql` | stream 3 |

`001`–`005`, `seed_demo.sql` and the runner are **lead-owned and frozen**.
Seed fragments go in `migrations/pg/seed_parts/<NNN>_<area>.sql`, loaded after
`seed_demo.sql` in filename order.

---

## Contract 1 — the scope wire format (replaces the `*` sentinel)

This is the single most cross-cutting change in the wave: streams 1, 2 and 3
all depend on it, so it is fixed here rather than discovered at integration.

**Today** `Scope.as_settings()` renders an unrestricted dimension as the
literal string `*`, and the SQL predicate treats `*` as "unrestricted". A
grant whose `scope_value` is literally `*` is therefore indistinguishable
from no restriction at all, and the two enforcement layers disagree about it:
`repo.compile_scope` treats it as an id and matches nothing, while RLS treats
it as a wildcard and matches everything.

**From this wave**, mode and identity are separate settings. There is no
value any id can take that means "unrestricted".

Per dimension `d ∈ {entity, plant, project, location}`:

```
capex.<d>_mode   'all' | 'none' | 'list'
capex.<d>_ids    comma-separated ids; meaningful only when mode = 'list'
```

Plus, unchanged: `capex.user_id`, `capex.principal_kind`, `capex.read_all`.

| `Scope` field | mode | ids |
|---|---|---|
| `None` (unrestricted) | `all` | `''` |
| `frozenset()` (nothing) | `none` | `''` |
| `frozenset({...})` | `list` | sorted, comma-joined |

**`capex_dimension_permits(p_setting_key text, p_value text)` keeps its
signature** — so migration 004's policies and stream 2's new policies both
call it unchanged — and gains these semantics:

```
p_value IS NULL                     -> true   (the table shape waives this dimension)
mode = 'all'                        -> true
mode = 'list'                       -> p_value = ANY(string_to_array(ids, ','))
mode = 'none' | absent | unknown    -> false  (fail closed)
```

The mode key is derived inside the function by replacing the `_ids` suffix of
`p_setting_key` with `_mode`. An absent or unrecognised mode denies. A
session that sets no scope at all therefore sees nothing, which is the
existing behaviour and must not regress.

## Contract 2 — what "restricted" means, and what must never collapse

Three states, distinguished end to end, in `roles.resolve_scope`, `Scope`,
`repo.compile_scope`, `as_settings` and RLS alike:

| State | Meaning | Compiles to |
|---|---|---|
| `None` | no restriction row for this dimension | `TRUE` |
| `frozenset()` | restricted, zero grants | `FALSE` — never `TRUE`, never absent |
| `frozenset({ids})` | restricted to those ids | membership test |

`user_scope_restriction` records *that* a dimension is restricted;
`user_scope_grant` records *to what*. A user with a restriction row and no
grant rows sees **nothing** on that dimension. A user with no restriction row
is unrestricted. Collapsing these two is the defect class this wave exists to
close, and no stream may introduce a fallback that widens either.

**A principal whose scope cannot be resolved fails closed.** No default to
unrestricted, no `read_all` by role as a substitute for a grant.

## Contract 3 — the lock order

One total order, no exceptions:

```
(wbs_path, budget_head_id) ascending, over EVERY affected cell
```

"Affected" now includes cells whose `budget_paise` is zero. The current
`_LOCK_SQL` filters `budget_paise <> 0`, which is why `recompute_cell`'s
`UPDATE` takes a lock the declared set never contained, and why the period
roll can run having taken no cell lock at all.

Every mutating service function calls `lock_affected_cells` exactly once, as
its first locking action, with the complete affected set, and acquires no
cell lock afterwards. `Session.locks_taken` must reflect what was actually
locked, so an ordering assertion cannot pass vacuously on an empty list.

## Contract 4 — scope construction at the API boundary

Every router obtains its `Scope` from **one** function:

```python
app/backend/pg/principal_scope.py

def scope_for_principal(session, principal: dict, *, principal_kind="USER") -> Scope
```

It resolves grants through `roles.resolve_scope` and returns a `Scope`
carrying the three-state distinction above. It never returns an unrestricted
scope for a principal that has restriction rows, and never falls back to
`read_all` on a resolution failure.

Stream 1 owns this module. **The lead** makes the one-line call-site change in
each router, because the routers are shared.

---

## File ownership — strictly disjoint

| Stream | Owns exclusively |
|---|---|
| **1 Scope resolution** | `app/backend/pg/principal_scope.py`, `tests/test_pg_principal_scope.py`, `tests/test_scope_negative_matrix.py` |
| **2 PostgreSQL RLS** | `migrations/pg/006_rls_coverage.sql`, `app/backend/pg/scope_inventory.py`, `app/backend/pg/rls.py`, `tests/test_pg_rls_coverage.py` |
| **3 Scope sentinel** | `migrations/pg/007_scope_sentinel.sql`, `app/backend/pg/engine.py`, `app/backend/pg/repo.py`, `app/backend/pg/roles.py`, `tests/test_scope_sentinel.py` |
| **4 Locking / periods** | `app/backend/pg/locking.py`, `app/backend/pg/budget.py`, `app/backend/pg/periods.py`, `tests/test_pg_locking_order.py`, `tests/test_pg_period_concurrency.py` |
| **5 Frontend integration** | `app/frontend/src/core/api-client.js`, `app/frontend/src/core/router.js`, `app/frontend/index.html`, `app/frontend/app.js`, `app/frontend/src/features/**`, `app/frontend/src/components/**`, `tests/vrt/**` |

### Lead-owned — frozen, nobody else edits

```
app/backend/main.py                     router mounting
app/backend/api/*.py                    the shared routers
app/backend/auth.py
migrations/pg/001..005, seed_demo.sql   migration ordering
tests/conftest.py, tests/conftest_pg.py
tests/TEST_MANIFEST.json, tools/build_test_manifest.py
.github/workflows/ci.yml
docs/FULL_APPLICATION_DELIVERY_STATUS.md
app/frontend/styles.css                 BYTE-FROZEN, SHA-256 pinned in CI
```

Streams 3 and 4 own files Wave 2 listed as lead-owned (`engine.py`, `repo.py`,
`roles.py`, `budget.py`, `periods.py`, `locking.py`). That is deliberate: each
is owned by exactly one stream this wave, which is what disjointness requires.
`rls.py` moves to stream 2 for the same reason.

## Rules every stream follows

- **Behavioural tests, never framework introspection.** Route-table walks have
  twice reported "not mounted" for routers that were serving; ask the
  application, and read the OpenAPI schema when an inventory is needed.
- **A skip is not a pass.** A precondition the environment guarantees must
  assert. Push coverage into tests that run with no database wherever the
  defect could live there — the last two production-shaped defects were
  invisible locally because every test touching them skipped.
- **Never weaken or delete a test to get green.** An assertion that has to
  change needs the reason recorded in the commit.
- **Report, do not edit, defects in files you do not own.**
- **No cloud.** No Catalyst, Supabase, Zoho tenant, `wbs-capex-poc` or
  `praktiq`. External integrations are adapters and fixtures only.
- Commit in your worktree, and state what you actually ran.

---

## Security-closure gate

M4b does not begin until every line is true:

- [ ] restricted principals receive resolved scopes at every router
- [ ] a principal with no grants fails closed
- [ ] `accounting_period`, `budget_line`, `budget_revision`, `budget_transfer`,
      `budget_version`, `budget_version_cell`, `item_master`, `vendor_master`
      all carry RLS
- [ ] application scope and RLS agree, proven by denial when either is bypassed
- [ ] a literal `*` grant is impossible at the API, the repository and the
      database
- [ ] zero-budget cell locking proven under concurrency
- [ ] SCR-28, SCR-09, SCR-10, SCR-13, SCR-30 routable in the SPA shell
- [ ] full local suite passes
- [ ] every live PostgreSQL test executes, zero skips, counts from JUnit
- [ ] all five CI jobs green
- [ ] adversarial review carries no unresolved critical or high finding
