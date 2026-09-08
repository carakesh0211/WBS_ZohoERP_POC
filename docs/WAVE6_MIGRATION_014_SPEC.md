# Migration 014 — the corrective migration, specified before it is written

**Status: SPECIFIED. Approved by the product owner 2026-09-07.** Six defects in
migration 013, four raised by Agent 3 and two by Agent 4, plus one raised by
Agent 2. This document freezes how each is corrected so the implementation is
reviewable against a stated intent rather than judged after the fact.

**014 is ADDITIVE AND CORRECTIVE. It does not modify 013.** 013 is already
applied wherever this build has run; editing it would change a checksum that
`schema_migrations` has recorded and make every existing database unadoptable.

---

## Nothing here is invented — every mechanism already exists in this repo

| Need | Existing mechanism | Where |
|---|---|---|
| Text normalisation for identity | `capex_normalise_text(value)`, `IMMUTABLE`, backs `GENERATED ALWAYS ... STORED` | `005_master_data.sql:40` |
| Zoho organisation identity | `integration_connection.connection_id` — carries `entity_id`, `organization_id` and `product`, `UNIQUE (entity_id, organization_id)` | `010_integration.sql` |
| Fiscal/series scope for a document number | `numbering_series.series_id` + `numbering_counter.period_key` | `005_master_data.sql:54,76` |
| `NULLS NOT DISTINCT` on a unique index | PostgreSQL 15+; CI runs **postgres:16** | `.github/workflows/ci.yml:93` |
| Raw external status, verbatim | C17's rule, already honoured by `integration_inbox.external_status_raw` | `C17_zoho_status_map.json` |

Using `capex_normalise_text` rather than a new normaliser matters for the
reason its own comment gives: a `GENERATED ... STORED` column is always
consistent with its source **regardless of which code path wrote the row**, so
identity can never drift out of step with an app-layer helper that forgot to
call it.

---

## D1 — GRN and bill number uniqueness

### The defect
`ux_grn_number UNIQUE (grn_number)` and `ux_bill_number UNIQUE (bill_number)`
are **estate-wide**. Two entities, or two Zoho organisations under one
deployment, legitimately using the same vendor bill number collide: the second
mirror fails on a unique violation rather than being accepted.

This is a **faithful port** — SQLite had `bill_number TEXT NOT NULL UNIQUE` —
which is the risk of "port it exactly": 013 reproduced a POC defect rather
than introducing one.

### The correction

**Authoritative idempotency is EXTERNAL IDENTITY, never the human-readable
number.** New columns on `grn` and `bill`:

```
connection_id       text REFERENCES integration_connection (connection_id)
entity_id           text NOT NULL REFERENCES entity (entity_id)   -- denormalised
```

`entity_id` is denormalised because both tables reach an entity only through
`purchase_order → project → entity`, and a uniqueness constraint cannot follow
a join. `bill.project_id` is already `NOT NULL` (013 added it for exactly this
class of reason); `grn` reaches project through `po_id`.

Named indexes:

```
ux_grn_external_identity   UNIQUE (connection_id, external_source, external_id)
                           WHERE external_id IS NOT NULL
ux_bill_external_identity  UNIQUE (connection_id, external_source, external_id)
                           WHERE external_id IS NOT NULL
```

Partial, because a locally created document has no external identity and must
not be forced to invent one.

**Human-readable numbers, scoped:**

```
ux_grn_number_scoped   UNIQUE NULLS NOT DISTINCT
                       (entity_id, numbering_series_id, period_key, grn_number)
ux_bill_number_scoped  UNIQUE NULLS NOT DISTINCT
                       (entity_id, vendor_key, bill_number_normalised)
```

`NULLS NOT DISTINCT` is load-bearing on both. PostgreSQL's default treats
NULLs as distinct, so without it two GRNs in the same entity with no numbering
series and the same number would not collide — the constraint would be absent
in precisely the ordinary case. That is the same defect shape as D5 below, and
it is why both are in this migration.

Supporting generated columns on `bill`:

```
bill_number_normalised  text GENERATED ALWAYS AS (capex_normalise_text(bill_number)) STORED
vendor_key              text GENERATED ALWAYS AS (COALESCE(vendor_id, vendor_name)) STORED
vendor_id               text REFERENCES vendor_master (vendor_id)   -- nullable
```

**`bill_number` and `grn_number` are preserved unmodified** for display and
audit. Normalisation exists only to make identity robust; it never replaces
what the vendor actually printed.

`vendor_key` falls back to `vendor_name` because 013's `bill` carries only a
free-text vendor and a mirrored bill may arrive before its vendor is mastered.
Refusing the bill until a `vendor_master` row exists would be a silent drop of
an accounting document.

---

## D2 — bill-line replay idempotency

### The defect
`bill_line` has **no unique index on any external identity**, unlike
`grn_line`'s `ux_grn_line_external`. Replay idempotency cannot be delegated to
the database. Combined with `DELETE` revoked from `capex_app` (013, correctly),
**a duplicated bill line is unrecoverable.**

### The correction

```
external_line_id   text
line_fingerprint   text
line_no            integer

ux_bill_line_external     UNIQUE (bill_id, external_line_id)
                          WHERE external_line_id IS NOT NULL
ux_bill_line_fingerprint  UNIQUE (bill_id, line_fingerprint)
                          WHERE external_line_id IS NULL
                            AND line_fingerprint IS NOT NULL
```

Where Zoho supplies a stable line id, `(bill_id, external_line_id)` is the
identity. Where it does not, `line_fingerprint` is a **deterministic** digest
over documented stable source fields plus the ordinal — never a random id,
never the mutable description alone, and never cleaned up by `DELETE`.

The fingerprint's inputs are fixed here so a later reader can verify a stored
value: `po_line_external_id`, `amount_paise`, `quantity`, `line_no`. Ordinal is
included because two genuinely distinct lines may be identical in every other
field — a legitimate case that must NOT collapse into one row.

---

## D3 — canonical status separated from raw status

### The defect
`grn.status`, `bill.status` and `purchase_order.status` carry **no CHECK**. Any
string can be written, including a raw unmapped Zoho value — precisely the
guess C17 forbids, with nothing in the schema to refuse it.

`compute_ledger` releases commitment on `status IN ('Cancelled','Closed')`, so
a typo'd `'cancelled'` holds commitment **forever, silently**.

### The correction
Named CHECK constraints containing only approved application statuses:

```
ck_purchase_order_status   CHECK (status IN (...))
ck_grn_status              CHECK (status IN (...))
ck_bill_status             CHECK (status IN (...))
```

The permitted sets are taken from `C3_statuses.json` and `C17`'s
`ACCOUNTING_STATUS_ONLY` rule — **read from the registries at implementation
time, not typed from memory here.** An unknown raw value never reaches these
columns; it raises `UNMAPPED_EXTERNAL_STATUS` and the record stays
non-accounting-effective, exactly as C17's rule states.

---

## D4 — `external_status_raw` coverage

### The defect
**No `external_status_raw` column on any of the eight tables**, though C17
requires the raw value stored verbatim on the mirrored row. The only verbatim
copy is `integration_inbox.external_status_raw`, so a mirrored document's
status can never be reconciled against its source on the row itself.

This is a miss in the contract frozen at `dbbaba8`, not in any agent's work.

### The correction
`external_status_raw text` on every procurement table that can represent a
mirrored external row. **Verbatim** — not trimmed, title-cased, translated or
normalised. NULL is permitted for locally created records and for source
objects that expose no status at all; nothing is invented to fill it.

`integration_inbox`'s copy stays **immutable ingestion evidence**. The
document-row copy exists for direct audit and reporting and must agree with
its originating inbox record; a live test asserts they agree byte-for-byte.

---

## D5 — `ux_grn_line_external` is NULLS DISTINCT

### The defect (raised by Agent 4, and it is the most dangerous one here)
`ux_grn_line_external UNIQUE (po_line_id, receive_external_id,
line_external_id)` uses PostgreSQL's default NULLS DISTINCT, so it **does not
constrain a receive line with no external line id** — which on Zoho **ERP is
the ordinary case**, because ERP publishes no receives list endpoint and lines
are discovered PO-anchored.

The sweeps re-walk by design on a 300-second overlap with a cycling cursor. So
every walk re-inserts, and **`received` climbs with no new receive arriving.**
Identical in shape to the `accumulate_unattributed` adding-bucket defect.

### The correction
Replace with `UNIQUE NULLS NOT DISTINCT`, and carry the receive line's
`line_no` ordinal so two genuinely distinct lines on one receive stay distinct:

```
ux_grn_line_external_v2  UNIQUE NULLS NOT DISTINCT
                         (po_line_id, receive_external_id, line_external_id, line_no)
```

The old index is dropped **by its exact name**.

---

## D6 — "a PR converts exactly once" is not structural

### The defect (raised by Agent 2)
`ix_purchase_order_pr` is a **plain** index, so the rule is enforced only under
the purchase request's own row lock in application code.

### The correction
```
ux_purchase_order_pr  UNIQUE (pr_id) WHERE pr_id IS NOT NULL
```
Partial, because a PO need not descend from a PR — `pr_id` is nullable in both
the POC and 013.

---

## Migration safety — non-negotiable

1. **Drop the incorrect indexes by their exact names**: `ux_grn_number`,
   `ux_bill_number`, `ux_grn_line_external`, `ix_purchase_order_pr`.
2. **Every replacement carries an explicit stable name.** Anonymous
   constraints are invisible to `migrate_pg.py::_named_constraints_by` and
   certify as adopted while absent.
3. **Every existing row is preserved.** No `DELETE`, no merge, no silent
   renumbering — anywhere, for any reason.
4. **Ambiguous duplicates are REFUSED, not resolved.** If existing data would
   prevent a new index being created, the migration fails loudly.
5. **A preflight report** names every offending row before any DDL runs, so an
   operator sees what to fix rather than a constraint-violation error code.
6. **Adoption verification extended** to the new columns, their types, the new
   named CHECKs and the new partial unique indexes.
7. **Both upgrade paths proved**: a clean database applying 001→014, and a
   database already at 013 upgrading to 014.
8. **Rerun/idempotency and checksum-drift protection proved.**

## Tests — live PostgreSQL, and each names the thing it forbids

- two entities may legitimately share a GRN number
- two vendors may legitimately share a bill number
- the same vendor and entity cannot import the same bill twice
- the same external document under two Zoho organisations does not collide
- a bill-line replay cannot duplicate a line — identical replay, reordered
  payload, two genuinely distinct identical-looking lines, missing external
  line id, a credit-note negative-paise line, concurrent duplicate ingestion
- canonical status columns reject raw/unmapped values
- `external_status_raw` is byte-for-byte unchanged
- an unknown external status raises the exception and has **no accounting
  effect**
- **no correction path uses `DELETE`** on a financial record

---

## Out of scope, and named so it is not mistaken for done

`budget_ledger_cell.actual_paise` and `.pr_reserved_paise` still have **no
writer** in the PostgreSQL path. `commitment_paise` did not either until Agent
2's `recompute_commitment`; the other two remain. This is a live control gap,
not a schema defect, so it is not corrected here — but it is a genuine blocker
and is carried as one.

---

# PART TWO — the six control gaps, added 2026-09-08

The product owner's Wave 6 closure list names six items beyond the schema
defects above. They are not index corrections; they are **controls the SQLite
build has and the PostgreSQL build does not**. Each is specified here rather
than discovered during implementation.

## C1 — `pr_reservation` has no PostgreSQL table

`app/backend/migrations/002_financial_controls.sql:187` defines it and
`domain.compute_ledger:171` reads it. Nothing in `migrations/pg/` creates it,
so Agent 2's `create_pr(reserve=True)` correctly refuses with
`PR_RESERVATION_NOT_MIGRATED` (501) rather than writing nothing and reporting
success.

**Why it matters, in one sentence:** without reservations, two requestors can
each pass `budget_check` against the same rupees, because neither request has
taken anything out of availability.

Ported to 014 verbatim in intent, with the PostgreSQL forms:

```
pr_reservation (
    reservation_id  text PRIMARY KEY,
    pr_id           text NOT NULL REFERENCES purchase_request (pr_id),
    wbs_id          text NOT NULL REFERENCES wbs_element (wbs_id),
    budget_head_id  text NOT NULL REFERENCES budget_head (budget_head_id),
    project_id      text NOT NULL,                       -- for the composite FK
amount_paise    bigint NOT NULL,
    state           text NOT NULL,
    po_id           text REFERENCES purchase_order (po_id),
    ... created_at/by, updated_at/by, version_no ...
    CONSTRAINT ck_pr_reservation_amount_positive  CHECK (amount_paise > 0),
    CONSTRAINT ck_pr_reservation_state            CHECK (state IN
        ('Reserved','Converted','Released','Expired')),
    CONSTRAINT ck_pr_reservation_converted_has_po CHECK
        (state <> 'Converted' OR po_id IS NOT NULL),
    CONSTRAINT fk_pr_reservation_wbs_project FOREIGN KEY (wbs_id, project_id)
        REFERENCES wbs_element (wbs_id, project_id)
)

ux_pr_reservation_live  UNIQUE (pr_id) WHERE state = 'Reserved'
```

`ux_pr_reservation_live` is the whole control: **exactly one live reservation
per PR**, enforced by a partial unique index rather than by a service
remembering to check. It ports 1:1 — plan §6.2 lists it among the three
partial unique indexes that do.

`amount_paise > 0` is named and kept: a reservation of zero or less is not a
reservation, and a negative one would *increase* availability.

RLS: joined via `wbs_element → project`, both registries, same as 013's line
tables.

## C2 — `lifecycle_state` has no PostgreSQL table

`domain.lifecycle_permits` (`domain.py:287`) reads it to answer whether an
object's state permits procurement or posting. AUD-C-008. Without the table
the gate cannot be evaluated at all.

Agent 2's `lifecycle_gate` currently probes for the table and returns
`LIFECYCLE_UNAVAILABLE` — **as a sentence, so a skipped gate cannot read as a
passed one.** That is the right interim behaviour and 014 makes it
unnecessary.

```
lifecycle_state (
    object_type        text NOT NULL,
    state              text NOT NULL,
    allows_procurement boolean NOT NULL,
    allows_posting     boolean NOT NULL,
    is_terminal        boolean NOT NULL,
    PRIMARY KEY (object_type, state)
)
```

`integer CHECK (x IN (0,1))` becomes a real `boolean` — the SQLite form is a
dialect workaround, not an intent. Rows are seeded from the POC's own
`INSERT INTO lifecycle_state` list, **read from that file at implementation
time, not retyped from memory**.

**Unknown states deny by default.** `lifecycle_permits` returns False for a
state absent from the table; that fail-closed behaviour is preserved exactly.

This is a reference table: no scope dimensions, `reach="reference"` in
`scope_inventory`.

## C3 — `actual_paise` and `pr_reserved_paise` have no writer

`budget.recompute_cell` writes only `original_paise`, `revisions_paise` and
`future_budget_paise`. Agent 2 added `recompute_commitment` for
`commitment_paise`. The other two remain at their `DEFAULT 0`, and
`check_availability` computes

    available = budget − (commitment + actual + pr_reserved)

so both being zero makes availability **overstated**, in the permissive
direction.

014's service work adds writers for both, deriving from
`domain.compute_ledger`'s formulas **verbatim** — the frozen `C5_formulas.json`
registry in code — never a reimplementation:

* `actual_paise` from `bill_line`, honouring `bill.accounting_status IN
  ('Approved','Reversal')` as the accounting-effective set (AUD-C-004) and
  `is_reversal` negating **by flag, not by data entry**.
* `pr_reserved_paise` from `pr_reservation WHERE state = 'Reserved'`.

`ordered_paise` and `received_paise` are written by the same pass, because
leaving two of six derived columns unwritten is how this defect arose.

**A test asserts the six ledger-cell columns each have a writer**, so the next
column added cannot silently join the unwritten set.

## C4 — concurrency-safe document numbering

Agent 2 reports `SELECT COUNT(*)+1`, which races `ux_purchase_request_number`:
two concurrent counts read the same value and both proceed.

`numbering_series` / `numbering_counter` / `numbering_issued` **already exist**
(`005_master_data.sql:54-104`) and `masters.issue_number` already advances the
counter with a single atomic `INSERT ... ON CONFLICT ... DO UPDATE ...
RETURNING`, whose row lock serialises concurrent issuers. No new mechanism is
needed and none is invented: procurement document numbers are issued through
that existing function.

Series are seeded for PR, PO, GRN and bill. Issued values are immutable once
minted — `numbering_issued` is append-only.

## C5 — fractional PO quantity policy, stated not silent

`po_line.quantity` is `numeric`; `outbound.PoLine.quantity` is `int`. Agent 2
refuses a fractional quantity with `NON_INTEGER_QUANTITY` rather than rounding.

**That refusal is kept as the default and made configurable, not removed.**
Rounding a quantity silently changes what was ordered; refusing tells someone.
The policy becomes a setting with the documented working default `REFUSE`, and
the alternative is explicit and audited. It is not left as a blocker, per the
standing rule that configurable business choices become configuration.

## C6 — procurement lifecycle transitions

With `lifecycle_state` present (C2), the PR, PO, GRN and bill state machines
in plan §12 become enforceable. Valid transitions are data, not code, and an
unknown transition is refused rather than permitted.

---

## What Part Two does NOT do

It does not invent an unattributed-bucket table or a bill hydration queue.
Agent 4 reported `accumulate_unattributed`, `bills_awaiting_detail` and
`mark_detail_hydrated` as still refusing in its branch; Agent 3's merged
implementation resolves the first against `reconciliation_exception` — which
is the correct home, because a separate bucket would be a second source of
truth for the number that blocks capitalisation. The hydration queue is
assessed during integration and, if genuinely absent, recorded as a gap rather
than guessed at.
