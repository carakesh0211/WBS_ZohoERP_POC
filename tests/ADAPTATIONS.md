# Test adaptations register

## Policy

> **All control intent and traceability must be preserved. Documented PostgreSQL-specific test adaptations are permitted, but no assertion may be weakened or removed without approval.**
>
> — Plan v1.2.1, §15.1

The 220 baseline test functions are named after the audit findings they close. Losing one silently loses the evidence that a critical finding stays closed. This register is where every deviation is recorded, and `tests/test_manifest.py` enforces that nothing changes without an entry here.

## Procedure

1. Record the change in the table below with a reason and a named reviewer.
2. Run `python tools/build_test_manifest.py`.
3. Commit the adaptation entry and the regenerated manifest **together**.

An adaptation with no named reviewer fails `test_every_adaptation_entry_names_a_reviewer`.

## What counts as which

| Category | Permitted? | Example |
|---|---|---|
| **Dialect adaptation** | Yes, with an entry | `sqlite3.IntegrityError` → PostgreSQL SQLSTATE. Same assertion, different dialect |
| **Mechanism adaptation** | Yes, with an entry | `conftest` file-copy isolation → template databases. Same guarantee |
| **Target adaptation** | Yes, with an entry | Assertion re-pointed from a dropped column to the view replacing it |
| **Behaviour change** | **Requires explicit sign-off**, not just an entry | A test whose observable outcome changes — see ADAPT-004 |
| **Weakening an assertion** | **No** | Loosening a tolerance, removing a case, converting an assert to a warning |
| **Removing a test** | **No** | Unless the requirement it protects is formally descoped through the §18.4 register |

---

## Register

Rows in this table MUST use an `ADAPT-nnn` id in the first cell and a NAMED PERSON in the last. `tests/test_manifest.py::test_every_adaptation_entry_names_a_reviewer` enforces both, and rejects placeholder reviewers such as a role name or "reviewer at time of change" — a role is not an approver.

| ID | Test(s) | Category | Original assertion | Adapted assertion | Reason | Reviewer |
|---|---|---|---|---|---|---|
| *(none yet — Phase 0A introduced no adaptations; the baseline suite runs unmodified on SQLite)* |

---

## Anticipated adaptations — Phase 1

Identified during planning. **Each still requires an entry above and a named reviewer at the time it is made.** Listing them here is forecasting, not pre-approval.

| Ref | Test(s) | Category | Nature of the change | Approval needed |
|---|---|---|---|---|
| **ADAPT-001** | Constraint-violation tests across `test_financial_controls.py`, `test_audit.py`, `test_capitalisation.py` | Dialect | `sqlite3.IntegrityError` becomes a PostgreSQL SQLSTATE assertion (`23505` unique, `23503` FK, `23514` check). The assertion — *the database refuses this write independently of the service layer* — is unchanged | Reviewer at time of change |
| **ADAPT-002** | `test_migrations.py` (20 functions) | Mechanism | Retargeted from the SQLite migration runner to the PostgreSQL runner. Intent — migrations are idempotent, recorded, reversible, and leave `foreign_key_check` clean — is unchanged | Reviewer at time of change |
| **ADAPT-003** | `tests/conftest.py` fixtures | Mechanism | Template-DB-per-session and byte-copy-per-test become `CREATE DATABASE … TEMPLATE …`. `_assert_disposable` becomes a database-**name** check refusing anything not matching `^capex_t\d+$` or `^capex_tmpl_`. Same isolation guarantee, same failure mode | Reviewer at time of change |
| **ADAPT-004** | `test_aud_m_005_deep_wbs_ledger_does_not_raise_recursion_error` and siblings | **Behaviour change** | A 1,100-deep hierarchy currently produces *controlled behaviour on read*. With `ltree` and `CHECK (nlevel(wbs_path) <= 100)` it becomes a *rejected write*. This is an improvement — the data error is caught at entry rather than tolerated at query time — **but the observable outcome changes**, so it needs explicit sign-off, not a silent adaptation | **Product owner + technical lead** |
| **ADAPT-005** | Tests asserting `wbs_element.allow_procurement` / `allow_posting` | Target | Those columns are retired (they are a denormalisation of `lifecycle_state` that `domain.lifecycle_permits` already bypasses). Assertions re-point at the `v_wbs_permits` view. **Sequenced under expand/contract** — the columns survive a full compatibility window before removal | Reviewer at time of change |

---

## Additions, as distinct from adaptations

New tests are additive and do not need an adaptation entry, but the manifest must be regenerated so the inventory stays the record.

| Added in | Tests | Purpose |
|---|---|---|
| Phase 0A | `tests/test_contracts.py` | Executable contract validation. Closes **AUD-M-001** |
| Phase 0A | `tests/test_manifest.py` | Guards this policy mechanically, including an assertion-count ratchet |
| Phase 0A | `tests/test_observability.py` | Structured logging, redaction and metrics. Closes **AUD-M-007** |
| Phase 0A | `tests/test_known_defects.py` | `xfail(strict=True)` record of DEF-01 |

Counts are deliberately omitted here — `tests/TEST_MANIFEST.json` is the inventory of record and cannot go stale.
