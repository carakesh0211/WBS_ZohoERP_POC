# Contract gaps register

**Closes AUD-M-001** — *"Contracts and implementation can still drift without detection."*

`tests/test_contracts.py` is a **ratchet**. Every gap below is an existing, reviewed divergence between the frozen contracts in `research/30_contracts/` and the implementation. The test asserts the gap set is **exactly** this list: a new divergence fails the build, and a closed gap fails the build until it is removed from here.

The point is not that the gap count is zero today. It is that it can only go down without a deliberate, reviewed decision.

| Status legend | |
|---|---|
| **OPEN** | Divergence exists and is accepted for now |
| **CLOSED** | Resolved; row retained for audit history |

---

## GAP-01 — `Cancelled` is a runtime status absent from the frozen 21

**Status:** OPEN · **Severity:** medium · **Contract:** `C3_statuses.json` · **Owner:** product owner (D-10 conversation)

`purchase_order.status` takes the value `'Cancelled'` at runtime — `services.cancel_po` writes it, and `domain.COMMITMENT_RELEASING_STATES = {"Cancelled", "Closed"}` reads it as a commitment-releasing state, which makes it **financially load-bearing**. It is not one of the 21 frozen C3 business statuses.

`C3_statuses.json` has `CLOSED` ("Terminal. Procurement and posting blocked.") but nothing for a cancellation, and the two are not the same business event: a closed PO released its residual after partial fulfilment; a cancelled PO was abandoned. Reporting on open-PO ageing and on commitment release needs to tell them apart.

**Options:** (a) add `CANCELLED` as a 22nd C3 status — the registry says "exactly these 21 status labels", so this needs explicit client agreement; (b) map `Cancelled` onto `CLOSED` and lose the distinction — **not recommended**, it destroys reporting signal; (c) record cancellation as a separate attribute alongside `CLOSED`.

**Recommendation: (a).** Raise at the D-10 approval-matrix conversation, where the status vocabulary is already being reviewed.

**Do not silently resolve this by editing `C3_statuses.json`** — it is a frozen contract, and freezing means the client agrees before it changes.

---

## GAP-02 — nine off-token colours outside `:root` in `styles.css`

**Status:** OPEN · **Severity:** low · **Contract:** `C6_tokens.json` · **Owner:** design/product owner (D-13)

The `:root` block of `app/frontend/styles.css` matches the frozen palette **exactly — 25 of 25 hex values**. That gate is clean and is asserted strictly.

Nine further hex literals appear elsewhere in the stylesheet and are absent from `C6_tokens.json`:

| Hex | Used for |
|---|---|
| `#7D1A15` | `.msg-error` text |
| `#F0C4C1` | `.msg-error` border |
| `#6D4600` | `.msg-warning` text, and — since the Wave 8 approved contrast correction — all warning TEXT: `.st-warning`, `.nav-item .pill.warn`, `.mock-chip` |
| `#ECD6A8` | `.msg-warning` border, `.mock-chip` border |
| `#145232` | `.msg-success` text |
| `#BFE0CC` | `.msg-success` border |
| `#1F3F7A` | `.msg-info` text |
| `#C5D4F0` | `.msg-info` border |
| `#7A1A15` | `.bar.breach > i.actual` |

These are message-strip text/border tones and one chart-bar breach tone — real design decisions that were never lifted into the token registry. `styles.css` is the approved visual baseline; it changes only through the documented approved-change procedure (product-owner approval recorded in the same commit that re-pins `APPROVED_STYLES_CSS_SHA256`), so the fix for this gap is still to complete `C6_tokens.json`, not to edit the stylesheet on a whim.

The count did not change in Wave 8: the approved warning-contrast correction re-used `#6D4600`, which was already on this list, rather than introducing a tenth colour.

**Recommendation:** add these nine as `colour.feedback.*` tokens in `C6_tokens.json` during the D-13 conversation, which is already open on the same file (see GAP-03). Until then they are allowlisted in `tests/test_contracts.py` so that any **new** off-token colour still fails.

---

## GAP-03 — `C6_tokens.json` declares `dark_mode`; `styles.css` does not implement it

**Status:** OPEN · **Severity:** low · **Contract:** `C6_tokens.json` · **Owner:** design/product owner (D-13)

`C6_tokens.json` carries a `dark_mode` key. `styles.css` implements only `@media (forced-colors: active)` — there is no `prefers-color-scheme` block. Either dark mode is deliberately deferred, or the generated stylesheet is incomplete against its own token contract.

**Recommendation:** confirm as deferred and annotate the token file, or schedule the implementation. Non-blocking either way.

---

## GAP-04 — business roles (13) and technical roles (7) are unreconciled

**Status:** OPEN · **Severity:** high · **Contract:** `C9_roles.json` · **Owner:** client (**blocking decision D-12**)

`C9_roles.json` freezes 13 business roles. `auth.ROLES` implements 7 technical roles. **Only `Requestor` appears in both.**

| | |
|---|---|
| **C9 business roles** | Requestor, Project Manager, Plant Head, Department Head, Procurement, Finance, Project Finance Controller, CAPEX Committee, CFO, Management Approver, System Administrator, Internal Auditor, Read-only Management User |
| **`auth.ROLES` technical** | Requestor, BudgetController, ProcurementApprover, FinanceApprover, CapitalisationApprover, Auditor, Administrator |

This is not a defect in the POC — the technical roles were deliberately scoped to the permissions the control logic needs. But **`C8_screens.json` requires every screen's "Permission requirements" to name roles from `C9_roles.json` only**, so the two must be reconciled before screens can be specified.

**This is blocking decision D-12 and it gates Phase 3.** Resolution is a `role_grant` table mapping business roles to permissions, replacing the hardcoded `auth.PERMISSIONS` dict, with `auth.require()` keeping its signature and its fail-closed `UNKNOWN_PERMISSION` behaviour.

Until then the test asserts the technical role set is **exactly** the known 7, so nobody adds an eighth silently while the mapping is unresolved.

---

## GAP-05 — 107 requirements carry no traceability

**Status:** OPEN (reduced) · **Severity:** medium · **Contract:** `C1_req_ids.json` → `C14_traceability.json` · **Owner:** delivery team

**Updated 2026-08-28 after the Phase 0A backfill.** All 107 `CLIENT-PDF` requirements have been migrated into `C14_traceability.json` with their real metadata — id, description, source section, page, business purpose, actor. Current totals:

| Provenance | Count | Traceability |
|---|---|---|
| `CLIENT-PDF` | 107 | **PENDING** |
| `CLIENT-NOTES-2026-08-28` | 13 | TRACED |
| `PRODUCT-OWNER-REQUEST-2026-08-28` | 2 | TRACED |
| **Total** | **122** | 15 traced, 107 pending |

The 107 rows carry **empty** `screens`, `tables`, `services`, `tests` and `acceptance_criteria`, and `traceability_status: "PENDING"`. That is deliberate. Tracing a requirement is analysis, not data entry, and a plausible-looking but wrong traceability row silently reports coverage that does not exist — which is worse than a visibly empty one.

**Ratchet:** `test_untraced_requirement_count_only_ever_falls` asserts the pending count never rises, against `MAX_PENDING_TRACEABILITY = 107` in `tests/test_contracts.py`. `test_no_pending_requirement_pretends_to_have_coverage` asserts a PENDING row stays visibly empty rather than half-filled. Lower the constant as requirements are traced.

**Remaining work:** trace each requirement as it enters its delivery phase. This is not a Phase 0A completion item; it is a standing obligation with a mechanical guard.

---

## Closed gaps

*None yet.*
