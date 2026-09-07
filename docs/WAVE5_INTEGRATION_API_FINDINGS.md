# `/api/integrations/*` — what is backed, what is not, and why

Findings from a survey of the existing code before the router was written.
Recorded so the next pass does not repeat the investigation, and so the
lead's decisions are visible rather than embedded in a module nobody re-reads.

## The contract surface is already frozen — in the frontend

`app/frontend/src/features/integration/integration-api.js` is not a sketch. It
names every route template exactly, probes `/openapi.json` to decide whether a
route is mounted, and labels every response `wave5` / `wave4-compat` /
`ledger-compat` so no screen renders data without also rendering its source.

**Consequence:** the router's path-parameter names must match those templates
character for character, or the probe reports the routes absent while they are
in fact serving.

## Permissions — a real gap in the authoritative table

`auth.PERMISSIONS` carries exactly two relevant entries:

* `connector.read` — Administrator, Auditor
* `connector.manage` — Administrator

Both exist, so the router uses them and invents nothing. But **there is no
operator-level integration permission**, which means retrying a dead letter or
resolving a reconciliation exception requires full `Administrator`.

That is a gap in `auth.py`, not something to paper over. A previous wave
shipped a local permission fallback in a router and it **failed open**,
granting a role the authoritative table excluded. The lead decides whether to
add an operator permission; the router does not invent one.

## Endpoints that are genuinely backed

`GET`+`POST /connections` (with the caveat below), `/scopes`, `/validate`,
`/health`, `/events`, `/inbox`, `/outbox`, `/exceptions`, `/exceptions/{id}`,
`POST /exceptions/{id}/resolve`, `/reconciliation`.

`/scopes` and `/validate` are better than expected — they give real offline
answers from `adapter_for`, and `accounts_server` genuinely **refuses** an
undocumented data centre rather than guessing one.

## Endpoints that are NOT backed, each verified rather than assumed

| Route | Why it cannot honestly answer yet |
|---|---|
| `POST /connections/{id}/authorize` | `integration_connection` has **no token columns at all**. No OAuth client registration, no redirect URI config, no callback handler, nowhere to put a refresh token. A URL-minting route with no token home is a plausible-looking thing that cannot work. |
| `GET /connections/{id}/organizations` | `adapter.py`'s own header says that until `GET /organizations` answers in Phase 0B-2, nobody knows. No adapter carries the method. |
| `PUT /connections/{id}/organization` | No store method exists, and MSG-INT-005 says the change re-points every synced record and requires approval. Writing that UPDATE inline in a router is exactly the shortcut this project punishes. |
| `POST /dead-letters/{queue}/{row_id}/retry` | `mark_outbox_failed` refuses rows that have left PENDING/FAILED, and the `attempts` CHECKs mean a manual retry must either reset `attempts` or raise `max_attempts`. **Those mean different things in the audit trail** — "failed 8 times" stops being true — and that policy is undecided. |
| `GET /control-totals` | See below. |

### Control totals — the honesty case

`SweepControlTotals` needs a `ControlTotalSource`. There is no PostgreSQL
`SweepStore` (the only implementation is `tests/integration_fakes.InMemoryStore`),
no control-total table, and the source half needs a tenant report endpoint.

**Local sums must not be surfaced here dressed as a control total.** The
frontend's own comment calls that "the most dangerous single number this
application could render" — a figure computed from our data and compared
against our data always balances and proves nothing.

The route therefore mounts and answers a coded 503 naming precisely what is
missing. Not a 404, which the frontend would read as "not built yet"; and not
a number.

## Three decisions taken deliberately

1. **`POST /connections` cannot satisfy the frontend as written.**
   `organization_id` is `NOT NULL` with a non-blank CHECK and a
   `UNIQUE (entity_id, organization_id)`; the frontend deliberately omits it
   ("chosen on SCR-33, after discovery"). Synthesising a placeholder would
   collide on that unique index. The router accepts it as optional and returns
   a precise coded refusal when absent. The frontend also sends `client_id`,
   for which no column exists.

2. **`message_id` comes from `C10_messages.json` only.** `test_contracts.py`
   greps backend source for `MSG-[A-Z]+-\d+` and fails on anything absent from
   the registry. Where no registered message fits, `message_id` is left null
   rather than minting one — C10 is frozen evidence this stream does not own.

3. **Unattributed rows fail closed, and the two scope layers disagree.**
   `reconciliation_exception.entity_id` is nullable, and migration 011's RLS
   policy treats a NULL dimension as unrestricted. But `repo.compile_scope`
   generates `= ANY(...)`, which **excludes** NULL. So RLS would show an
   unattributed exception to everyone while the compiled predicate hides it
   from everyone.

   The router takes the stricter side rather than widening access at the API
   layer. **This disagreement is a real defect and needs resolving properly** —
   an exception nobody can attribute still has to reach whoever can resolve it,
   and neither layer currently achieves that on purpose.

## Mechanics the mounting must satisfy

* `tests/test_api_auth.py` builds its `MUTATING_ROUTES` matrix from the **live
  OpenAPI schema**, so every mutating path must be added there **in the same
  commit as `include_router`** — otherwise
  `test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix`
  fails the moment the router mounts.
* `tests/test_scope_enforcement.py` walks all of `app/backend/` and flags any
  `fetchall`/`fetchone`/`execute` on a scopable table without a literal
  `{scope}` token. Everything goes through `repo.query` with `columns=` naming
  all four dimensions, waiving any by explicit `None`.
* `SUM()` over `bigint` returns numeric in PostgreSQL — cast `::bigint`.
