# Wave 4 — frozen contracts and file ownership (M4b, approval engine)

**Frozen at the Wave 3 gate commit.** Five streams run concurrently in
isolated worktrees. Everything below is fixed for the wave: a stream that
needs a change reports it to the lead rather than editing.

Plan traceability: **Phase 4 → M4b**. Requirement `REQ-PRC-003` (configurable
multilevel approvals across all applicable modules), plus `REQ-REV-008`'s
"submitted revisions create no spending capacity".

## Migration numbers — assigned, do not collide

| File | Owner |
|---|---|
| `migrations/pg/008_approval_engine.sql` | stream 1 |
| `migrations/pg/seed_parts/008_approvals.sql` | stream 1 |

`001`–`007` and `seed_demo.sql` are lead-owned and frozen.

---

## Contract 1 — tables and what is immutable

```
approval_definition   (definition_id PK, object_type, code, version,
                       status DRAFT|ACTIVE|RETIRED, entity_id NULL,
                       effective_from, effective_to,
                       UNIQUE (object_type, code, version))
approval_rule         (rule_id PK, definition_id, priority, predicate jsonb,
                       UNIQUE (definition_id, priority))
approval_stage        (stage_id PK, definition_id, stage_no, name,
                       parallel_group NULL,
                       quorum_type ALL|ANY|N_OF_M|PERCENT, quorum_n,
                       applies_when jsonb, sla_hours, escalate_after_hours,
                       escalate_to jsonb, allow_delegation,
                       requires_reason, reason_code_set,
                       UNIQUE (definition_id, stage_no))
approval_stage_approver (stage_id, ordinal, approver_kind ROLE|USER,
                       approver_ref, scope_expr jsonb)
approval_instance     (instance_id PK, object_type, object_id, object_version,
                       object_content_sha, definition_id, definition_version,
                       status, current_stage_no, snapshot jsonb,
                       supersedes_instance_id, maker_user_id,
                       entity_id, project_id, opened_at, closed_at,
                       correlation_id)
approval_stage_instance (stage_instance_id PK, instance_id, stage_no,
                       parallel_group, status, skip_reason,
                       quorum_required, quorum_met, opened_at, due_at,
                       escalated_at, closed_at,
                       UNIQUE (instance_id, stage_no))
approval_assignment   (assignment_id PK, stage_instance_id, assignee_user_id,
                       assigned_via ROLE|USER|DELEGATION|ESCALATION,
                       delegated_from, state PENDING|ACTED|WITHDRAWN,
                       UNIQUE (stage_instance_id, assignee_user_id))
approval_action       (action_id PK, stage_instance_id, instance_id,
                       actor_user_id, acting_for_user_id, action,
                       reason_code, reason_text, at, seq,
                       prev_hash, entry_hash)          -- APPEND-ONLY
approval_delegation   (delegation_id PK, delegator_user_id, delegate_user_id,
                       scope_key, active_range daterange, created_by,
                       revoked_at, revoke_reason)
reason_code           (code PK, applies_to_action, applies_to_object_type,
                       label, requires_free_text, active)
```

**Immutable once written**, enforced in the database, not by discipline:

- An `ACTIVE` or `RETIRED` `approval_definition` and every `approval_rule`,
  `approval_stage` and `approval_stage_approver` beneath it. Editing a live
  workflow retroactively rewrites decisions already taken under it. Change =
  a new `version`; the old row is retired, never edited.
- `approval_action` is append-only: `BEFORE UPDATE OR DELETE` trigger plus
  `REVOKE UPDATE, DELETE`, the same treatment `audit_log` gets.
- `approval_instance.snapshot`, `definition_version` and
  `object_content_sha`: an instance records the workflow it was routed under
  and the document it was routed for. Neither may drift.

## Contract 2 — statuses

Instance: `OPEN, APPROVED, REJECTED, RETURNED, RECALLED, CANCELLED,
SUPERSEDED, EXCEPTION_PENDING`
Stage: `PENDING, APPROVED, REJECTED, RETURNED, SKIPPED, ESCALATED`
Assignment: `PENDING, ACTED, WITHDRAWN`

These are **internal** (`C15_approval_statuses.json`). They never render on a
business screen except through a mapping to `C3_statuses.json`. A stage whose
`applies_when` is false is recorded `SKIPPED` with a `skip_reason` — never
omitted, because an auditor must see what did not run.

**No route to auto-approval.** No match → `EXCEPTION_PENDING` with
`APPROVAL_ROUTE_UNRESOLVED`. Empty approver set after the contributor filter →
`EXCEPTION_PENDING` with `NO_INDEPENDENT_APPROVER`. Both fail closed; an
unroutable object is a configuration defect, never an approval.

## Contract 3 — API routes

```
GET  /api/approvals/inbox?state=&object_type=&cursor=&limit=
POST /api/approvals/{instance_id}/decide
     {"action":"APPROVE|REJECT|RETURN", "reason_code":…, "reason_text":…,
      "idempotency_key": <required>, "object_version": <required>}
POST /api/approvals/{instance_id}/recall        {"reason_text"}
POST /api/approvals/{instance_id}/cancel        {"reason_text"}
POST /api/approvals/{instance_id}/resubmit      {"reason_text"}
GET  /api/approvals/{instance_id}
GET  /api/approvals/{instance_id}/timeline
GET  /api/approvals/definitions?object_type=&status=
POST /api/approvals/definitions                 (creates a DRAFT version)
POST /api/approvals/definitions/{id}/activate
POST /api/approvals/definitions/{id}/simulate   {"object": {...}}
GET  /api/approvals/definitions/{id}/versions
GET  /api/approvals/delegations
POST /api/approvals/delegations                 {"delegate_user_id","scope_key","from","to"}
POST /api/approvals/delegations/{id}/revoke     {"reason_text"}
GET  /api/approvals/sla?overdue=true
```

RFC-7807 errors carrying `code`; every response carries `X-Correlation-Id`;
cursor pagination on every list.

Error codes, frozen: `APPROVAL_ROUTE_UNRESOLVED`, `NO_INDEPENDENT_APPROVER`,
`SELF_APPROVAL`, `NOT_AN_ASSIGNEE`, `STAGE_NOT_OPEN`, `REASON_REQUIRED`,
`OBJECT_VERSION_STALE`, `IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENT_REPLAY`,
`DEFINITION_NOT_ACTIVE`, `DEFINITION_IMMUTABLE`, `DELEGATION_WINDOW_INVALID`,
`BUDGET_MOVED`.

## Contract 4 — permissions

New, added to `auth.PERMISSIONS`:

```
approval.read           every role (an approver must see their own inbox)
approval.act            Requestor, BudgetController, ProcurementApprover,
                        FinanceApprover, CapitalisationApprover
approval.configure      Administrator
approval.delegate       BudgetController, ProcurementApprover,
                        FinanceApprover, CapitalisationApprover
```

`approval.act` is the floor. **Whether a specific caller may act on a specific
instance is not a permission question** — it is assignment plus maker-checker,
checked per decision inside the transaction.

## Contract 5 — maker-checker, and why it is checked twice

`auth.MAKER_CHECKER` and `require_separation` are **not modified**. The engine
adds a second, independent enforcement point at decision time, and
`contributor_set(object)` — maker, editors, prior actors — is removed from
every stage's assignee set. A delegation checks **both** identities: refused
if `actor_user_id` **or** `acting_for_user_id` is a contributor, so a
delegation can never launder a self-approval.

## Contract 6 — scope semantics

`approval_instance` carries `entity_id` and `project_id`, denormalised at
creation, so it is scopable directly. RLS policy in 008; the inbox query goes
through `repo.query` with `{scope}`, mapping all four dimensions. A caller
sees only instances within their scope, **and** only assignments addressed to
them unless they hold `approval.configure`.

## Contract 7 — lock order

Extends Wave 3's Contract 3 rather than replacing it:

```
1. budget cells        lock_affected_cells, once, first, complete set,
                       ordered (wbs_path, budget_head_id)
2. approval instance   SELECT ... FOR UPDATE on approval_instance
3. document row        the object's own row
4. advisory            pg_advisory_xact_lock, audit stream, taken last
```

A decision re-runs `budget_check` **inside the same transaction** as the
approval, after the locks. Availability may have moved since routing; if it
has, the decision fails with `BUDGET_MOVED` rather than approving against
stale numbers.

## Contract 8 — idempotency and staleness

Every decision carries an `idempotency_key` and the `object_version` it was
taken against.

- Replay of the same key returns the ORIGINAL outcome with
  `IDEMPOTENT_REPLAY`; it never applies twice.
- A decision whose `object_version` no longer matches is refused
  `OBJECT_VERSION_STALE`. A document that changed under an open instance is
  auto-`SUPERSEDED` and re-routed — never silently approved.

## Contract 9 — audit

`approval_action` is hash-chained on stream `approval:{instance_id}` using the
**existing frozen payload format** `prev|at|actor|action|type|id|detail` —
unchanged, because changing it invalidates every stored hash. Every action
writes its entry **inside the same transaction** as the state change.

---

## File ownership — strictly disjoint

| Stream | Owns exclusively |
|---|---|
| **1 Data model** | `migrations/pg/008_approval_engine.sql`, `migrations/pg/seed_parts/008_approvals.sql`, `app/backend/pg/approval_schema.py`, `tests/test_pg_approval_schema.py` |
| **2 Engine** | `app/backend/pg/approvals.py`, `app/backend/pg/approval_rules.py`, `app/backend/pg/delegation.py`, `tests/test_pg_approvals.py`, `tests/test_pg_approval_concurrency.py` |
| **3 API/security** | `app/backend/api/approvals.py`, `tests/test_approvals_api_guard.py`, `tests/test_approval_negative_matrix.py` |
| **4 Frontend** | `app/frontend/src/features/approvals/**`, `app/frontend/src/components/approvals/**`, `app/frontend/approvals.css`, `app/frontend/src/core/router.js`, `app/frontend/app.js`, `app/frontend/index.html`, `app/frontend/styles.css` (the two APPROVED changes only), `tests/vrt/**` |
| **5 Test/reviewer** | `tests/test_approval_e2e.py`, `tests/test_approval_maker_checker.py` |

### Lead-owned — frozen, nobody else edits

```
app/backend/main.py                 router mounting
app/backend/auth.py                 permissions
app/backend/pg/{engine,repo,roles,principal_scope,locking,audit}.py
app/backend/pg/{budget,periods,masters}.py
migrations/pg/001..007, seed_demo.sql
tests/conftest.py, tests/conftest_pg.py, tests/TEST_MANIFEST.json
tools/build_test_manifest.py, .github/workflows/ci.yml
docs/FULL_APPLICATION_DELIVERY_STATUS.md
```

## The two APPROVED UI changes — stream 4 only

The product owner has approved, in writing, exactly two visual changes. They
are **not** licence for a redesign: structure, colour theme, fonts, density,
spacing, tables and overall feel are preserved.

1. **Primary-navigation entries** for the five completed screens: Audit Trail
   Viewer, Budget Planning Grid, Budget Version Comparison, Budget
   Availability Check, Settings & Master Data. Permission-gated.
2. **The smallest `#userAvatar` colour correction** that reaches WCAG AA
   4.5:1. It is 4.02:1 today.

For both: capture before/after screenshots, document the exact visual
difference, update the `styles.css` SHA-256 pin **deliberately and in the same
commit**, re-baseline **only** the VRT snapshots that genuinely changed, and
prove every unrelated approved screen is still pixel-clean.

## Lead amendments during the wave

**A1 — the demo scenario runs on budget revisions, not purchase requests.**
The scenario asks for an over-budget PR. There is no PostgreSQL-backed
purchase-request module yet — `api/` carries budget, masters, settings, audit,
admin_access and health — so step (a) has no HTTP home. Rather than build a PR
module inside the approval wave, the scenario re-targets to
`POST /api/budget/revisions`: an over-budget revision routes to exception
approval, a different authorised user approves it with a reason, availability
is revalidated in the approving transaction, and self-approval is refused.
Every property the scenario demonstrates is preserved; only the document type
changes. The PR module lands with Phase 5.

**A2 — `bill.void` maker-checker is inert, and stays recorded rather than
silently fixed.** `services.py:411` passes `b.get("created_by")`, but the
`bill` table has no such column (`db.py:189`), so the maker is always `None`
and `auth.require_separation` short-circuits on a falsy maker. Verified: a
FinanceApprover voiding their own bill gets 200.

The honest framing matters. This is not "an approver can approve their own
work" — a bill is mirrored from Zoho, not raised by a user, so there IS no
maker to be separated from. The control is inert **by construction**, and
`bill.void`'s presence in `auth.MAKER_CHECKER` implies a protection that
cannot exist against the current data model.

Not fixed here, because the fix is a business decision rather than a default:
what separation means for a mirrored document. The defensible answer is a
second person on the void itself, which is precisely what this wave's engine
provides — so routing `bill.void` through it is the real fix, and it needs the
product owner to say so. Held by an `xfail(strict=True)` meanwhile, so the day
it is fixed the test flips to a failure and forces this note to be updated.

## Rules every stream follows

- Behavioural tests, never framework introspection. Read the OpenAPI schema
  when an inventory is needed.
- A skip is not a pass. A precondition the environment guarantees must assert.
- Never weaken, delete or rename an existing test. All 1,131 must remain.
- Money is integer paise. `SUM()` over a paise column needs `::bigint`, or it
  returns `numeric` and psycopg hands back `Decimal`.
- Report, do not edit, defects in files you do not own.
- No cloud: no Catalyst, Supabase, Zoho tenant, `wbs-capex-poc`, `praktiq`.
  External integrations are adapters and fixtures only.
- No secret, no client data, no unsafe HTML, no inline `style=` attribute (CSP
  `style-src 'self'` blocks it), no client-side authorization decision.
