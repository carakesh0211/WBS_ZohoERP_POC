# Wave 4 — lead decisions taken during the parallel streams

Written for the integration pass. Three streams are running concurrently (A1
engine seam, A2 document wiring, A3 approval screens) and none of them can see
these decisions, so each is recorded here with its reasoning and its blast
radius. Anything a stream landed that contradicts one of these is reconciled in
favour of this file, and the reconciliation is noted in the merge commit.

---

## D-A. `open_instance` RETURNS its fail-closed outcome; it does not raise

**Settled 2026-09-05. Applies to all four fail-closed branches:** unroutable
object, definition with no stages, empty approver set after the contributor
filter, and every stage skipped.

The engine recorded the outcome as an `EXCEPTION_PENDING` `approval_instance`
row and then re-raised. The caller owns the transaction and `Database.session`
rolls back on any exception leaving the block, so **the exception rolled back
the row it had just written**. The object ended with no approval instance at
all — the single outcome Contract 2 exists to prevent — and the evidence was
destroyed as a direct consequence of reporting it.

Only a live database shows this. In memory nothing rolls back, which is why it
survived a green in-process suite for two waves.

An unroutable object is a recorded OUTCOME held for an administrator (SCR-25),
not exceptional control flow.

**What callers must do.** Check `instance["status"]`. `EXCEPTION_PENDING` is a
refusal: surface it, name the reason, and do **not** roll back — rolling back is
the defect. The fail-closed code is on the instance's last
`approval_action.outcome->>'code'`.

**The safety property is unchanged and is asserted more directly than the
exception ever asserted it:** the returned status is `EXCEPTION_PENDING`, never
`APPROVED`, in the return value *and* in the committed row.

**Landed at `9666920`,** with `tests/ADAPTATIONS.md` covering the three tests
that still expected the raise — including one that expected the raise *and*
asserted the row survived it, two claims that cannot both hold.

---

## D-B. `approval_stage_instance.opened_at` — biconditional, and no DEFAULT

`CHECK ((status = 'SKIPPED') = (opened_at IS NULL))`, column nullable, **no
`DEFAULT now()`**.

`NOT NULL DEFAULT now()` forced a SKIPPED stage — one that never ran — to carry
a timestamp saying when it did, on the table an auditor reads precisely to see
what did *not* happen. Nullable alone was not enough:

- the one-directional form (`SKIPPED OR opened_at IS NOT NULL`) still permitted
  a SKIPPED row carrying a time;
- a `DEFAULT` would have stamped `now()` onto any insert that forgot the column
  and quietly satisfied the weaker check.

Safe because SKIPPED is only ever written at INSERT — no `UPDATE` in
`pg/approvals.py` moves a stage into SKIPPED — so no row ever surrenders an open
time it legitimately earned.

**Consequence for stream A1's mixed-wave fix.** `_insert_stage_instance`'s
`opened` argument and its `status` argument are now coupled by a database
constraint, not by convention: `status=SKIPPED` ⟺ `opened=False`. If the
mixed-wave fix opens only the unopened members of a wave, every row it inserts
is PENDING and must carry an `opened_at`. Enforce the pairing in Python as well,
so the failure is a clear assertion rather than a `CheckViolation` from a
statement three frames away.

**Landed at `9666920`,** with
`test_a_stage_has_an_open_time_if_and_only_if_it_is_not_skipped_live` asserting
both directions — a one-sided test passes against either form, and the weaker
form is the one that was wrong.

---

## D-C. The write-back seam between A1 and A2

One seam, specified before either stream started so neither invents it:

```python
# app/backend/pg/approval_writeback.py   -- created and owned by A2
def apply_outcome(session, instance: Mapping[str, Any]) -> None: ...
```

- Called with a **closed** instance (APPROVED / REJECTED / RETURNED / CANCELLED
  / SUPERSEDED), inside the engine's transaction.
- Called from **one** place: `pg/approvals.py`, at the single point an instance
  closes. A1 writes that call, lazily imported so a deployment without the
  module is not an import error.
- Maps to a **business status from `C3_statuses.json`** (the frozen 21). An
  approval-engine status (§8.2) must never reach a document.
- A no-op for an unknown `object_type`, and idempotent.

---

## Open, found during this pass, not yet fixed

1. **The "every stage skipped" branch records no machine-readable code.** The
   other three fail-closed branches go through `_write_exception_instance`,
   which writes `outcome = {"code": …, "detail": …}`. The every-stage-skipped
   branch appends its ESCALATE action with prose in `detail` and **no
   `outcome`**, so an administrator triaging SCR-25 cannot filter on the cause
   the way they can for the other three. One-line fix in `open_instance`; left
   to A1 because A1 is editing that function.

2. **No local PostgreSQL and no Docker on this machine.** 239 tests are
   live-only and skip here, so CI (~20 min per cycle) is the sole oracle for
   every schema constraint, RLS policy and transaction-rollback assertion.
   A skip is not a pass, and no claim about a live-only assertion should be made
   from a green local run.
