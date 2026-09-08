# Wave 7 — frozen contract and file ownership

**Frozen before agents spawn.** Four agents, disjoint file ownership, reserved
migration numbers. Wave 6 repairs are in flight on `pg/procurement.py`,
`integration/sweeps.py`, `pg/budget.py`, `pg/procurement_services.py` and
`migrations/pg/014_*` — **no Wave 7 agent may touch any of those.**

## Screen inventory — 21 of C8's 40 are unbuilt

Registered today: SCR-03, 09, 10, 13, 15, 16, 17, 18, 26, 27, 28, 29, 30, 31,
32, 33, 34, 38, 39, 41.

| Agent | Screens |
|---|---|
| **A3** | SCR-01 Executive CAPEX Dashboard · SCR-02 Project Controller Workbench · SCR-04 CAPEX Project List · SCR-05 CAPEX Project Object Page · SCR-06 WBS Hierarchy Explorer · SCR-07 WBS Tree Table · SCR-08 WBS Element Detail · SCR-19 CWIP Ledger · SCR-23 Open Commitment Ageing · SCR-24 CWIP Ageing · SCR-25 Exception and Overrun Monitor |
| **A4** | SCR-11 Budget Revision Request · SCR-12 Budget Transfer · SCR-14 Purchase Request Control View · SCR-20 Project Completion Review · SCR-21 Capitalisation Workbench · SCR-22 Asset Allocation · SCR-35 Master Data Mapping Workbench · SCR-36 Transaction Field Mapping Workbench · SCR-37 Sync Direction and Scheduling · SCR-40 Connector Audit and Credential Activity Log |

## File ownership — exclusive

| Agent | Owns exclusively | Reserved migration |
|---|---|---|
| **A1** reporting backend | `app/backend/pg/reporting.py`, `app/backend/api/reports.py`, `tests/test_pg_reporting.py`, `tests/test_reporting_filterset.py` | **015** |
| **A2** export jobs | `app/backend/pg/exports.py`, `app/backend/api/exports.py`, `tests/test_pg_exports.py`, `tests/test_export_scope.py` | **016** |
| **A3** dashboards UI | `app/frontend/src/features/analytics/**`, `tests/vrt/analytics.spec.js` | none |
| **A4** closure + remaining | `app/backend/pg/closure.py`, `app/backend/api/closure.py`, `app/frontend/src/features/closure/**`, `app/frontend/src/features/mapping/**`, `tests/vrt/closure.spec.js`, `tests/test_pg_closure.py` | **017** |

**Shared files — additive edits only, expect a merge:** `app/backend/main.py`
(router mount), `tests/test_api_auth.py::MUTATING_ROUTES`,
`tests/TEST_MANIFEST.json`, `tools/build_test_manifest.py`, the SPA screen
registry and nav. Never revert another agent's line in these.

## The canonical FilterSet — ONE definition, A1 owns it

Dashboards, reports and exports all consume the same object. A second filter
shape is how a dashboard card and its own drill-down come to disagree.

```
FilterSet:
  entity_ids, plant_ids, location_ids, project_ids   # org dimensions
  wbs_paths                                          # ltree subtree, not ids
  budget_head_ids
  category_ids                                       # AMB-04: budget head is
                                                     # the primary reading
  vendor_ids, item_ids
  document_types      # PR | PO | GRN | BILL
  lifecycle_statuses  # C3 only
  approval_statuses   # C15 only
  date_from, date_to
  period_ids          # accounting_period, NOT a raw date range
  group_by            # ordered list of dimensions
  cursor, limit, sort # deterministic, tie-broken on a unique column
```

**Rules that are not negotiable:**

* **Scope is server-side.** `FilterSet` narrows *within* the caller's resolved
  scope and can never widen it. A client-supplied entity the caller has no
  grant for yields no rows, never an error that confirms it exists.
* **Money stays integer paise through every aggregation.** `SUM()` over
  `bigint` returns numeric in PostgreSQL — cast `::bigint`.
* **No double counting.** PR reservation, PO commitment, GRN receipt and bill
  actual are four distinct buckets. `domain.compute_ledger` is the frozen
  `C5_formulas.json` registry in code; every metric derives from it verbatim.
  Open commitment is **ordered less billed**, never ordered less received.
* **Three states are distinct and must never collapse:** zero data, denied
  scope, service failure. An empty table because you have no grant is not the
  same as an empty table because nothing matched.
* **Freshness is rendered.** Every screen showing synced data shows its
  last-sync time and source label. A number with no provenance is not shown.

## Metric definitions — derived, never re-implemented

`budget`, `original`, `revisions`, `ordered`, `commitment`, `actual`,
`received`, `received_not_billed`, `pr_reserved`, `available`.

All ten come from `domain.compute_ledger`'s formulas. **Do not re-derive them
in SQL from memory** — the reviewer has already found two functions in one
file computing `open_paise` differently, and a `received_paise` that counts
Void GRNs because it forgot a join.

## Drill-down contract

Every card, chart segment and total is clickable to the rows behind it, and
the drill-down carries the **same** `FilterSet` plus the clicked dimension.
A drill-down that does not sum back to the figure clicked is a defect, and a
test asserts the round trip.

## Export authorisation contract

* Long exports answer **202 with a job id**; no route exceeds the 30-second
  AppSail budget.
* The requester's identity and **resolved scope** are captured immutably on
  the job row at creation and rehydrated by the worker.
* **The worker runs under the requester's scope, never its own.** A background
  export must not become a scope-escalation path.
* Deterministic column order; money formatted exactly, from paise.
* Every export writes an audit event.
* A test proves a plant-scoped user's export contains no out-of-scope row.

## Hard rules for every Wave 7 agent

* `app/frontend/styles.css` is **byte-frozen and SHA-256 pinned**. New CSS goes
  in a separate token-based stylesheet using only `C6_tokens.json` values.
* **Never regenerate a VRT PNG baseline** to obtain a green test.
* Modular vanilla JS ES modules. No React, no Svelte, no framework rewrite.
* Every screen renders loading, empty, **unavailable**, error, permission-denied
  and success. Unavailable is distinct from empty.
* **Never fabricate a total.** If a source metric is unavailable, render an
  explicit unavailable/data-quality state naming what is missing.
* Every mutating route joins `tests/test_api_auth.py::MUTATING_ROUTES` in the
  same commit that mounts it — that matrix is built from the live OpenAPI
  schema.
* No live Zoho call, no cloud resource, no deployment, no client data.
