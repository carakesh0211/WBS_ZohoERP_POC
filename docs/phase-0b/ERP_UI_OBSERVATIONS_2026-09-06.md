# Zoho ERP UI — first direct observations, 2026-09-06

**Source.** A Zoho ERP organisation ("RAPGURU") on `https://erp.zoho.in`, logged
in by the lead in the in-app browser and offered for integration work.
Observed **read-only**: no record was created, edited or deleted, no credential
was entered, no API key was generated and no OAuth grant was completed. The
New Purchase Request form was opened to read its field list and abandoned
without saving.

**What this org is, and is not.** It is the lead's own organisation. It is
**not** Atha Group's tenant. So it is evidence about **Zoho ERP the product**
and is **not** evidence for **D-14** — which product the client runs. Those stay
separate.

**A standing caveat that applies to every status below.** These are **UI
labels**, not API raw values. A screen showing `ISSUED` does not establish that
`GET /purchaseorders` returns `"issued"`. Everything here is evidence that our
map is INCOMPLETE; none of it is confirmation of a raw value. Raw values still
need the API, which is Phase 0B-2.

**Also noted:** the org displays "Your free trial is over". Module data was
still readable, but capability observed here may not reflect a subscribed
plan's, and nothing about plan tier or the daily API ceiling (§11.4's binding
constraint) can be concluded from it.

---

## 1. The Purchase Request module EXISTS. CONF-01 resolves "both true".

**This is the finding that matters most.** `research/30_contracts/C13_conflicts.json::CONF-01`
records the tension: the client PDF classifies Purchase Request as a "Standard"
Zoho ERP capability, while a full sweep of the ERP v3, Books v3 and Inventory v1
module indexes found no Purchase Request module and `/purchase-requests/`,
`/purchaserequests/` and `/requisitions/` all 404 on the ERP docs.

The plan insisted the resolution might be **"both true"** — a module can exist
in the ERP UI as standard and still have no documented public REST API — and
warned against collapsing it to "one wrong".

**Observed: it is both true.** `Procurement → Purchase Requests` is a working
module with its own list views (`Awaiting Approval`, `Approved`, `All Requests`,
`View Requested Items`) and five live records, `PR-00001` … `PR-00005`.

Consequences:

* **AMB-05 is settled on its UI half.** The client's "PR creation field same as
  Zoho ERP" refers to a screen that demonstrably exists.
* **The API half is UNCHANGED and still governs the design.** Nothing observed
  here shows a REST endpoint. WBS remains the PR system of record.
* **Exclusion X-04 gets materially harder to sign.** It reads "No Zoho-side PR
  object. WBS is the PR system of record", with the consequence "procurement
  staff raise PRs in WBS, not in the Zoho ERP PR screen they can currently
  see". That screen is now confirmed to exist, to be in use, and — see §2 — to
  carry its own approval workflow. Signing X-04 asks the client to stop using a
  working module, not merely to forgo one they had not adopted.

## 2. The ERP Purchase Request module has its OWN approval workflow

The list carries `SUBMITTER` and `APPROVER` columns, a status of `APPROVED`,
and views named `Awaiting Approval` / `Approved`. The creation form offers
**"Save and Submit"** beside "Save", and a **"Notes to Approver"** field.

So Zoho ERP already runs a submit-and-approve cycle over purchase requests.
Wave 4 built a configurable approval engine for the same object. **Two approval
systems over one business process is a reconciliation problem nobody has
scoped**, and it is not addressed anywhere in the approved plan. Raised for the
lead; not resolved here.

## 3. Purchase Request creation fields — REQ-PRC-004's requirement input

Captured verbatim from the New Purchase Request form.

**Header:** Page Layout · Expected Date · Location · Delivery Address ·
Purchase Request# · Reference# · Reason · Notes to Approver

**Line items:** Serial Number · Item Name · Category · Description ·
Preferred Vendor · Quantity · Estimated Rate · Estimated Amount

**Actions:** Save · Save and Submit · Cancel

Two observations worth carrying forward:

* **`Category` is a NATIVE LINE-LEVEL field.** Our `po_line` is keyed on
  `(wbs_id, budget_head_id)` and **D-7** asks whether the plan supports
  line-level custom fields. This does not answer D-7 — Category is native, not
  a custom field — but it establishes that ERP models line-level dimensions at
  all, which is the premise D-7 tests.
* **"Page Layout" is a header field**, so this module supports multiple
  layouts. Any field-parity claim (REQ-PRC-004) must name the layout it was
  measured against.

## 4. Purchase Order statuses do not match our map

Observed on `Procurement → Purchase Orders`: **`ISSUED`**, **`ACCEPTED`**,
**`CLOSED`**, and a PO overdue indicator.

`C17_zoho_status_map.json` maps ERP purchase order `draft` → DRAFT, `open` →
RELEASED, `billed` → FULLY_ACTUALISED, `cancelled` → CLOSED, sourced from the
captured OpenAPI bundle's documented `status` query-parameter values.

**`ISSUED` and `ACCEPTED` appear nowhere in that map.** Subject to the UI-label
caveat above, this is direct evidence that the ERP purchase-order lifecycle is
richer than the four filterable values, and that a poll would meet states the
resolver would refuse as `UNMAPPED_EXTERNAL_STATUS`.

That refusal is the fail-closed design working. But it means **stream 3's
warning is now concrete rather than theoretical**: the status map is incomplete
in a way that will stop real polls, and closing it needs API-level enumeration,
not more UI reading.

## 5. Modules and structure observed

`Vendors` · `Expenses` · `Procurement` → (`Purchase Requests`, `Purchase
Orders`, `Bills`, `Payments`). A PO carried `REFERENCE# = RFQ-00003`, so an
**RFQ module** exists too, and another carried `REFERENCE# = PR-00002`,
evidencing a PR → PO link inside ERP.

The Purchase Orders list has an **"In Transit Receives"** view — relevant to
§11.4, though it says nothing about whether receives are enumerable via API,
which is the actual constraint.

Setup progress groups seen: Organisation, Financial Operations, Manufacturing,
Distribution, Payroll.

---

## What is still open, and what would close it

| Question | Status after this session |
|---|---|
| **D-14** — which product does Atha Group run? | **Untouched.** This is not their tenant. |
| Raw API status values | **Open.** UI labels are not API values. Needs `GET /purchaseorders` against a tenant. |
| Is there a PR REST endpoint? | **Open.** UI presence is not API presence; the documented absence stands. |
| **D-7** line-level custom fields | **Open.** Native line fields exist; custom ones unproven. |
| Plan tier / daily API ceiling | **Open**, and unobservable here — the trial has expired. |
| **AUD-H-004** `purchaseorder_item_id` populated? | **Open.** Needs the Phase 0B-3 spike with real payloads. |
