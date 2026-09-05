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

---

# Decisions waiting on the product owner / lead

## 0. TWENTY-THREE approved-UI baselines still show the pre-approval avatar

**This one is not really a judgement call — it is an approved change that was
left half-applied, and the gate could not see it.**

Your avatar WCAG fix landed in `1382250`: `--primary-500` -> `--primary-600`,
raising white-on-teal from 4.02:1 to 5.69:1. Forty-six of sixty-nine baselines
were re-captured. **The other twenty-three — every tablet-800 baseline — still
show the OLD, WCAG-failing tone.**

They were not re-captured because the suite could not tell they had changed.
`playwright.config.js` set `maxDiffPixelRatio: 0`, which reads as "no pixel may
differ", but Playwright applies its per-pixel `threshold` FIRST and that
defaults to `0.2`. The avatar change alters 555 pixels at a YIQ distance of
264.89/1408.60 = **0.188**, just under. So every tablet-800 baseline passed
through a real colour change.

That commit's own message said so, recommended `threshold: 0`, and filed it
under "reported, not fixed (files this stream does not own)". Nobody owned it.

`threshold` is now pinned at **0.05** — measured against both failure modes,
not guessed: it refuses the 0.188 shift with wide margin while tolerating the
~0.004 (1/255) re-render antialiasing recorded in `BASELINE-DELTA.txt`. Exactly
`0` would fail on antialiasing and train everyone to ignore the job.

The 23 now fail, correctly. The diff image is one bright region on an otherwise
identical page: the avatar circle. Re-recording them makes the baselines match
the state you approved; leaving them pins the contrast defect you approved
fixing.

Same command as decision 1 below, and it covers both:
`npx playwright test tests/vrt/ --update-snapshots`, or per-spec to take them
separately.

## 1. Seventeen `approvals.spec.js` baselines are red, and I did not re-record them

**State:** CI's visual-regression job is `664 passed, 17 failed`. Every one of the
17 is a screenshot of one of the eight NEW approval screens. All 60 approved-UI
baselines and all 165 spa-routing baselines pass, verified locally on an
isolated port against a freshly started server as well as in CI. **Nothing was
regenerated to achieve that.**

**Why they fail:** the 24 `approvals.spec.js` baselines were captured in
`69f45e1` — the previous frontend agent's WIP — *with the eight unapproved
navigation entries in place*. Those entries have now been withdrawn (they
overflowed the nav rail by 194px at 1440 and 336px at 1024, pushing
`Settings & Master Data` below a fold with no scroll cue). So the baselines
encode a layout the project has decided not to ship.

**Why I stopped rather than re-recording:** the standing instruction is "never
regenerate or update an approved screenshot merely to make VRT pass". These
eight screens have never been shown to the client and are not part of the
approved 14 views, and the reason would not be "to make VRT pass" but "the
baseline pins a withdrawn layout" — so the case for re-recording is defensible.
It is close enough to the line that it is yours to take, not mine, and the
tooling refused the command, which was the right outcome.

**The cost of leaving it:** a permanently red VRT job. That job is how a real
regression in the approved UI would announce itself, and a gate that is always
red stops being read. Wave 4's "CI green" acceptance criterion cannot be met
while it stands.

**The two options:**

  a. **Re-record the 24 `approvals.spec.js` snapshots only** (`npx playwright
     test tests/vrt/approvals.spec.js --update-snapshots`). Touches no approved
     baseline. Recommended.
  b. **Leave them red** and treat the VRT job as informational until the nav
     question below is settled and the screens are re-shot once.

## 2. The eight approval screens have no navigation entry at all

They are reachable by route (`#approval-inbox`, `#approval-request`,
`#approval-sla`, `#approval-timeline`, `#approval-matrix`, `#approval-versions`,
`#approval-simulator`, `#approval-delegations`), permission-gated and
deep-linkable. They appear in no nav rail.

Measured from the running application as `U-ADM`:

| variant | desktop-1440 | laptop-1024 |
|---|---|---|
| eight entries (as found) | 1050 vs 856 → **194px over** | 1050 vs 714 → **336px over** |
| **zero entries (landed)** | 840 vs 856 → **fits** | 840 vs 713 → 127px over (pre-existing) |
| one consolidated "Approvals" | 870 vs 856 → **14px over** | 870 vs 713 → 157px over |

Even a single row does not fit at 1440. Exposing these screens in the rail
therefore needs a layout decision, not just an approval — and the approvals on
record explicitly do not extend to a redesign. Adding a row later is one
`scr('…')` splice per line.

## 3. Two items carried forward, unchanged

* **`--warning` fails WCAG AA 4.5:1 against every background in the palette.**
  The token is untouched; the new screens carry status with a text glyph and
  label so it is distinguishable without colour.
* **`bill.void` maker-checker is inert.**

## 4. `'CANCELLED'` is admitted by the schema and is not one of C3's frozen 21

`009_document_approval_states.sql` widened both document status domains to add
SUBMITTED and RETURNED. It deliberately did **not** remove `'CANCELLED'`:
dropping a permitted value is a CONTRACT step and §17.1 forbids bundling one
with the expand that replaces it. Nothing in the application writes it — an
administrative cancel returns the document to DRAFT — so it is inert, but the
DDL and the frozen registry still disagree until a later contract migration.
