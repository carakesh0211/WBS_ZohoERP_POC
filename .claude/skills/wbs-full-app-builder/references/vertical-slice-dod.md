# Vertical slice — definition of done

A milestone is complete when **every** slice in it satisfies **every** row
below. Not most rows. Not the hard rows.

## The checklist

### 1. Migration

- [ ] PostgreSQL migration under `migrations/pg/`
- [ ] Expand/contract — no destructive DDL in the same deployment that
      introduces its replacement
- [ ] A `-- ROLLBACK:` section, matching the convention at the top of
      `002_financial_controls.sql`
- [ ] Constraints declarative where possible: composite FK, CHECK, partial
      unique index, `EXCLUDE USING gist` — a trigger only where a constraint
      genuinely cannot express it
- [ ] `migrate.py --status` reports the new revision

### 2. Backend

- [ ] Repository function with a mandatory `{scope}` token
- [ ] Domain service holding the business rule
- [ ] Locking per `domain-controls.md` — `lock_affected_cells` once, first,
      complete set
- [ ] Money integer paise end to end
- [ ] Idempotent where it can be retried

### 3. API

- [ ] FastAPI route, Pydantic request model with `extra="forbid"`
- [ ] RFC-7807 error carrying the existing `code` + `message_id`
- [ ] `X-Correlation-Id` on every response
- [ ] Cursor pagination on every list
- [ ] `Idempotency-Key` honoured on every mutating route
- [ ] Completes well inside **30 s**; anything longer returns `202` + job id

### 4. UI

- [ ] Screen registered in `registry.ts` against its `SCR-nn`
- [ ] Wired to the real API — **no static fake data**
- [ ] Loading, empty, error and permission states all implemented
- [ ] Uses shared components, not a bespoke table
- [ ] New CSS in the extension stylesheet, tokens only
- [ ] No `style="` attribute anywhere
- [ ] Visual check at 1440 / 1024 / 800

### 5. Permissions

- [ ] Enforced **server-side**. A UI-only check is not a permission.
- [ ] Row-level scope applied through `repo.query()`
- [ ] Out-of-scope read → not-found; out-of-scope write → forbidden
- [ ] Fail-closed on an unknown permission
- [ ] Negative test per affected role

### 6. Audit

- [ ] Append-only entry with the frozen payload format
- [ ] Correct `stream_key`, `seq` under the advisory lock
- [ ] Chain verifies after the operation
- [ ] Every classified-field reveal and statutory export audited

### 7. Tests

- [ ] Unit — the rule itself
- [ ] Invariant — the `domain-controls.md` assertions hold after the mutation
- [ ] Concurrency — where two actors can race the same cell
- [ ] Permission — negative matrix for affected roles
- [ ] API — success, validation failure, permission failure
- [ ] Contract — registry values, message ids, tokens
- [ ] No existing assertion weakened or removed without a recorded reason

### 8. Seed data

- [ ] `pg/seed_demo.sql` extended so the screen is demonstrable
- [ ] Realistic enough to show the control working, including a refusal case
- [ ] Guarded by the demo-profile gate so it can never touch a non-demo database

### 9. Documentation

- [ ] `docs/FULL_APPLICATION_DELIVERY_STATUS.md` updated — **that is all**
- [ ] Traceability row updated in `C14_traceability.json` with real test names
- [ ] Any configurable default recorded with its assumption

**Do not write a new design document, audit report or milestone essay.**

### 10. Green and shipped

- [ ] Focused tests pass during development
- [ ] **Full suite passes** before the milestone commit
- [ ] Contract gate, manifest gate, VRT, supply chain all green
- [ ] Committed and **pushed**
- [ ] CI verified green on the pushed commit

## What never counts as done

| Not done | Why |
|---|---|
| Migration + service, no UI | Nobody can use it |
| Screen with hardcoded rows | Proves nothing about the system |
| API with no permission check | A security defect wearing a feature's clothes |
| Feature with no audit entry | Unreviewable, and the product's claim is auditability |
| Tests written but failing | Red is not done |
| Committed but not pushed | Not shipped |
| A document describing what would be built | The failure mode this skill exists to prevent |

## Slice sizing

If a slice cannot satisfy the checklist in one pass, it is too big — split it by
**capability**, never by layer.

- Good: "budget revision request" → migration + service + API + screen + tests
- Bad: "all budget migrations", then "all budget services", then "all budget UI"

The second shape is what produces three weeks of work with nothing
demonstrable, and it is precisely what this document exists to prevent.
