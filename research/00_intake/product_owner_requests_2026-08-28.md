# Product-owner requests — 2026-08-28

**Provenance value:** `PRODUCT-OWNER-REQUEST-2026-08-28`
**Artefact:** direct instruction from the engagement product owner, 2026-08-28.
**Status:** allocated. 2 requirement IDs.

## Why this is a separate provenance class

A requirement's **authority** and its **evidence** are recorded separately. Both of the requirements below are mandatory and in scope; they are not client-note-derived, and recording them as such would misattribute the evidencing artefact.

This class was created by a provenance correction on 2026-08-28. The two requirements were briefly carried as `CLIENT-NOTES-2026-08-28` in plan v1.2, flagged at the time because they do not appear on either handwritten page (ambiguity AMB-08). The product owner confirmed the correct source and the attribution was corrected in plan v1.2.1. **No third handwritten page exists**; AMB-08 is closed.

---

## Requirement allocation

| Req ID | Requirement | Primary provenance | Secondary evidence |
|---|---|---|---|
| `REQ-GRN-002` | GRN / Purchase Receive inbound synchronisation from Zoho into WBS | `PRODUCT-OWNER-REQUEST-2026-08-28` | `REQ-GRN-001` (`CLIENT-PDF`) |
| `REQ-BILL-003` | Vendor Bill inbound synchronisation from Zoho into WBS | `PRODUCT-OWNER-REQUEST-2026-08-28` | `REQ-BILL-001`, `REQ-BILL-002` (`CLIENT-PDF`) |

The secondary `CLIENT-PDF` links are retained deliberately. They mean the underlying business need is independently corroborated by the client's own requirements document, even though the instruction to build these specific synchronisations came from the product owner. Neither link is a substitute for the other, and neither is merged away.

---

## REQ-GRN-002 — GRN / Purchase Receive inbound synchronisation

**Direction:** Zoho → WBS. Zoho is the transaction source of truth for receipts.

**Acceptance criteria**
- Receives are pulled from Zoho and persisted with organisation, receive, PO, vendor, location and line identifiers, quantities, dates, statuses, billed status and source payload version/hash.
- Every receive line either resolves to a known `po_line` through the officially documented line identifier, **or** lands in `reconciliation_exception` with kind `GRN_LINE_UNATTRIBUTED`.
- **Zero pro-rata spreading. Zero silent drops.** Unattributed value accumulates in a visible project-level bucket and blocks capitalisation.
- Reversal, deletion, status change, duplicate delivery, late-arriving data and received-not-billed ageing are all handled explicitly.

**Screens:** SCR-16 (GRN and Unbilled Receipt View), SCR-27 (Reconciliation Exception Queue).
**Tables:** `grn`, `grn_line`, `reconciliation_exception`, `external_document`.
**Services:** `integration.sweeps.po_anchored`.

**Known platform constraint.** If the target product is Zoho ERP (client decision D-14, still provisional), **ERP Purchase Receives has no list endpoint** — four endpoints, none of them a collection. Receives cannot be enumerated. PO-anchored discovery is then the *sole* acquisition mechanism, its cost scales with open-PO count rather than receive volume, and on ERP Standard's 2,000 calls/day ceiling the sync frequency may require negotiation. This is sized in Phase 0B-3. On Books + Inventory the constraint does not apply.

**This constraint does not reduce the requirement.** It changes the mechanism and its cost profile, both of which are surfaced for a client decision rather than absorbed silently.

---

## REQ-BILL-003 — Vendor Bill inbound synchronisation

**Direction:** Zoho → WBS. Zoho is the accounting source of truth for bills.

**Acceptance criteria**
- Initial full sync followed by incremental sync using the documented `last_modified_time` filter, with a 300-second overlap window, persisted watermark and idempotent upsert.
- Bill detail is fetched where list responses omit `line_items`.
- Bill and line IDs, PO and PO-line links, vendor, dates, currency and exchange rate, status, taxes and charges, custom fields, void and credit adjustments, and source payload version/hash are all persisted.
- PO-to-bill line matching uses the officially documented line key. **Unmatched and direct (non-PO) bills go to a controlled mapping/reconciliation workflow — never guessed.**
- Open commitment converts to actual CWIP **without double counting**. Ordered, received, billed, credited/voided and open amounts reconcile at line, PO, WBS, budget-head and project level.

**Screens:** SCR-17 (Vendor Bill and Actual CWIP View), SCR-18 (Commitment-to-Actual Reconciliation).
**Tables:** `bill`, `bill_line`, `external_document`, `reconciliation_exception`.
**Services:** `poll_bills`, `sweep_bill_detail`.

**Verified capability note.** `last_modified_time` **is** documented as a filter on `GET /bills` in both Zoho Books v3 and Zoho ERP v3. It is **not** an allowed `sort_column` value on either, so a resumable keyset walk on modification time is unavailable. The design therefore uses bounded windowed filtering with overlap, plus the nightly control-total and completeness sweeps — which are retained in full, because a filter cannot prove it returned everything.
