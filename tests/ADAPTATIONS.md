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

## 2026-08-31 — `test_no_columns_declared_means_no_filter_even_if_scope_is_restrictive`

**Change:** assertion INVERTED. Was `assert predicate == "TRUE"`; now asserts
`ScopeNotExpressible` is raised. Renamed to
`test_no_columns_declared_is_REFUSED_when_the_scope_is_restrictive`.

**Reason:** the assertion documented and locked in a data-leakage hazard.
`compile_scope` iterated over the caller's `columns` mapping rather than over
the dimensions the `Scope` actually restricts, so any dimension the caller
omitted contributed no clause. With `columns=None` — the default on both
`query()` and `query_one()` — the predicate was an unconditional `TRUE`
regardless of how restrictive the scope was.

Two concrete failures this permitted:

- A user scoped to one entity, queried through a mapping that omits `entity`,
  read every entity's rows. The `{scope}` token guard passed, because a token
  was present.
- `Scope(project_ids=frozenset())` — "no grants at all" — compiled to `TRUE`
  rather than `FALSE` against `PROJECT_SCOPE_COLUMNS`, which did not map
  `project`. That is precisely the "no grants becomes all rows" inversion the
  module's own docstring says must never happen.

**Not a weakening.** The control is strengthened: a restriction the query
cannot express is now refused rather than silently dropped. Waiving a dimension
remains possible by mapping it to `None`, which is visible at the call site and
in review — unlike an omission.

**Found by:** adversarial financial-control review of Milestone 1 (finding F2),
which specifically flagged that this test locked the hazard in.

**Approved by:** engagement lead, as part of the Milestone 1 integration pass.

**Also changed:** `PROJECT_SCOPE_COLUMNS` gained `project`; both shipped
mappings now express all four dimensions, mapped or explicitly waived. Four
tests added covering refusal, explicit waiver, unrestricted scope, and mapping
completeness.

## 2026-08-31 — `test_verify_chain_empty_stream_is_trivially_intact`

**Change:** assertion INVERTED. Was `intact: True` for an empty stream; now
asserts `intact is False` and `stream_found is False`. Renamed to
`test_verify_chain_does_NOT_report_an_unknown_stream_as_intact`.

**Reason:** an empty result means either an unknown `stream_key` or a wholly
deleted stream. Reporting either as "intact" turns a typo in the nightly
verification job into a green tick — the verification would pass while
verifying nothing.

**Not a weakening.** `verify_chain` additionally now checks `seq` contiguity,
so an interior gap is detected even if the remaining hashes were recomputed.

**Known limit, deliberately documented rather than papered over:** tail
truncation is still undetectable from within a stream — deleting the last k
entries leaves a contiguous, correctly linked prefix. That needs the daily
anchors in `audit_anchor`, which has no writer yet.
`test_tail_truncation_is_NOT_detectable_from_the_stream_alone` records the
limit, and `verify_chain` returns `whole_stream_truncation_note` so the result
cannot imply coverage it does not have. Both should be replaced with a
detection test when the anchor writer lands.

**Found by:** adversarial review of Milestone 1 (finding F6).
**Approved by:** engagement lead, Milestone 1 integration pass.

## 2026-08-31 — `test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix`

**Change:** route enumeration now descends into router containers via a new
`_walk_routes` helper, and reads `path` with `getattr` rather than bare
attribute access. **The assertion itself is unchanged.**

**Reason:** newer FastAPI places an `_IncludedRouter` object in `app.routes`
for an included router. It has no `.path`, so bare access raised
`AttributeError` in CI while passing locally on an older pinned version.

Crashing was the lucky outcome. The dangerous one is that any route reachable
only *through* such a container is invisible to the scan — so an uncovered
mutating endpoint would pass the authorisation-matrix check in silence. The
walk keeps the test meaning what its name claims.

**Not a weakening.** The check now sees strictly more routes than before.

**Approved by:** engagement lead, Milestone 1 integration pass.

## 2026-09-05 — `test_the_contract_4_permissions_match_the_frozen_contract` and `test_auth_permissions_wins_wherever_it_defines_an_approval_permission`

**Change:** both removed. Replaced by one test,
`test_auth_permissions_is_the_only_source_of_approval_permissions`, in the
same file.

**Reason:** the thing they guarded is gone. `api/approvals.py` carried a
`_CONTRACT4_PERMISSIONS` transcription of Contract 4, standing in while
`auth.py` — lead-owned and frozen at the Wave 4 baseline — did not yet define
`approval.read`, `approval.act`, `approval.configure` or `approval.delegate`.
`auth.require` raises `UNKNOWN_PERMISSION` (a 500) for a key it does not know,
so without the stand-in every guard on the router would have been a server
fault rather than a refusal. The four permissions have since landed in
`auth.PERMISSIONS`, so the router now reads that and nothing else.

**Why this is not merely tidying up a redundant table.** The transcription had
DRIFTED. It granted Auditor `approval.read`, faithfully transcribing Contract
4's "every role", while the permission that actually landed EXCLUDES Auditor
(D-12; see `test_the_router_floor_is_approval_read_and_excludes_only_auditor`).
So it was not a narrower stand-in that would quietly stop being consulted — it
was WIDER than the authoritative table. Deleting `approval.read` from
`auth.PERMISSIONS` would have GRANTED Auditor access instead of removing
everyone's: a fallback that fails open in exactly the situation a fallback
exists for.

**Both old tests passed throughout, and between them they still missed it.**
The first checked the transcription against Contract 4 — it was a faithful
transcription, so it passed. The second checked that `auth.PERMISSIONS` wins
*wherever it defines a key* — it does, so that passed too. Neither compared
the two tables to each other, which is where the disagreement lived. That is
the assertion the replacement makes.

**Not a weakening.** The replacement keeps every check the pair made that
still has a subject — the four permissions resolve, resolve to
`auth.PERMISSIONS` exactly, and name only roles in `auth.ROLES` — and adds
two the pair did not: that the second table is absent (`hasattr`), and that an
undefined permission resolves to `None` rather than to a default. The removed
assertions about the literal contents of `_CONTRACT4_PERMISSIONS` have no
subject left to assert against.

**Approved by:** engagement lead, Wave 4 integration pass.
