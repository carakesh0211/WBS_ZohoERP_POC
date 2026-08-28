# Client decision questionnaire — open ambiguities

**Raised:** 2026-08-28 · **Source:** the two handwritten client pages of 2026-08-28
**Register:** [`client_notes_2026-08-28.md` §4](client_notes_2026-08-28.md)

Eight questions. Each arises from wording in your own notes that carries more than one reasonable reading, and each has been given a **working assumption** so that delivery is not blocked while you decide.

**Nothing here is a delay request.** Work is proceeding on the assumptions shown. What we need is confirmation before the stated gate, because after that gate a wrong assumption costs rework rather than a conversation.

**Please also assign a named individual to each question.** A role cannot answer a question. Until a person is named, these are unowned, and we will keep saying so rather than reporting them as covered.

---

## Q1 · AMB-01 — What does "MCP of the Portal" mean?

*Your note:* "API client id & client secret, MCP of the Portal."

We cannot tell whether "MCP" is a portal identifier, an abbreviation local to your team, or a reference to a Zoho MCP connector.

- **Our assumption:** it means the **organisation/portal mapping** — the identifier binding a WBS entity to a Zoho organisation. We are explicitly *not* assuming an MCP server integration.
- **Gate:** Phase 0B · **Owner:** Client IT / Zoho tenant administrator
- **If we are wrong:** the connection data model is keyed on the wrong identifier, and the Phase 5 integration is built on that key. Correcting it later means reworking connection setup, organisation mapping and every sync watermark.

**Answer:** ☐ organisation/portal mapping ☐ something else — please describe ☐ MCP connector

---

## Q2 · AMB-02 — Was "ZPAS" or the struck-through "Other page – charts" a real requirement?

*Your note:* a heading "ZPAS." and a struck-through line "1) Other page – charts" above the WBS list.

- **Our assumption:** a prior or discarded topic on the same sheet. **No requirement derived.**
- **Gate:** Phase 0A close · **Owner:** Client product owner
- **If we are wrong:** a requirement you consider given is currently unrecorded and will surface late as new scope.

**Answer:** ☐ correct, disregard ☐ it was a requirement — please describe

---

## Q3 · AMB-03 — How should the WBS hierarchy be structured?

*Your note:* "Budget Module for WBS – Multilayer, Auto numbering."

Your notes confirm a **multi-level budget hierarchy**. They do not define the WBS *element* model: maximum depth, parent-child rules, which attributes an element carries (owner, dates, status, progress), or the numbering format.

- **Our assumption:** configurable depth; the element model as designed in the plan, which is **not** client-confirmed and is tagged `MASTER-PROMPT` throughout.
- **Gate:** **Phase 2** · **Owner:** Client project controls / Finance
- **If we are wrong:** this is the most expensive item on the list. The WBS element model is the spine of the financial ledger — `budget_ledger_cell`, the `wbs_path` hierarchy and every rollup depend on it. Changing it after Phase 2 means reworking the control-cell design and re-running the whole invariant test suite.

**Please confirm:** maximum depth ______ · numbering format ______ · required element attributes ______

---

## Q4 · AMB-04 — What does "Category wise Report" mean?

*Your note:* "Category wise Report."

Three things in the model could be called a category: **budget head**, **asset category**, **item category**.

- **Our assumption:** **budget head**, consistent with "facilities of categories" in your budget-module note. We are carrying the other two as additional report dimensions so a wrong guess is cheap.
- **Gate:** Phase 6 (influences Phase 2) · **Owner:** Client Finance
- **If we are wrong:** low impact, deliberately — mitigated at some cost in reporting-model complexity.

**Answer:** ☐ budget head ☐ asset category ☐ item category ☐ more than one

---

## Q5 · AMB-05 — Purchase Requests: confirming a change to working practice

*Your note:* "PR Module – API sync with Zoho ERP for Pushing from WBS to Zoho ERP" and "PR creation field same as Zoho ERP."

**We must be direct about this one.** We verified against Zoho's complete published module indexes for Zoho ERP v3, Books v3 and Inventory v1: **there is no Purchase Request API in any of them.** A PR cannot be pushed to Zoho by any documented endpoint.

Your second line — "PR creation field same as Zoho ERP" — tells us you can see a PR screen in Zoho. That is entirely consistent: the module can exist in the user interface with no public API behind it. Both statements are true.

- **Our approach:** the PR lives in WBS as the system of record, with **field parity to your Zoho PR screen**, full budget control and approvals. On approval it creates the **Purchase Order** in Zoho, which does have a documented API.
- **Gate:** Phase 5; formal sign-off at go-live as exclusion **X-04** · **Owner:** Client product owner + Procurement
- **What this means for your team:** procurement staff raise PRs in WBS, not in the Zoho screen they use today. **That is a change to how people work, not a technical detail.** We would rather you knew now than at go-live.

**Answer:** ☐ accepted ☐ we need to discuss alternatives

---

## Q6 · AMB-06 — "Multiple user filter": filter *by* user, or per-user dashboards?

*Your note:* "Dashboard – Multiple user filter, Date filter."

- **Our assumption:** **both** — a user dimension in the filter set *and* role-specific dashboard layouts. Building both is inexpensive and removes the risk.
- **Gate:** Phase 6 · **Owner:** Client product owner
- **If we are wrong:** low impact; we may have built something you did not need.

**Answer:** ☐ filter by user ☐ role-specific dashboards ☐ both, as assumed

---

## Q7 · AMB-07 — Is "Location" distinct from Plant and Zone?

*Your note:* "Location wise Report" — but your Settings list is "User, Role, Division, Branch, Entity, Plant, Zone." **Location is a reporting dimension in your notes but not an organisational level.**

- **Our assumption:** `location` is a distinct organisational level, added so it can be reported on.
- **Gate:** **Phase 3** · **Owner:** Client operations / Plant administration
- **If we are wrong:** medium-high impact. The organisational hierarchy drives **row-level data scope** — who can see which records. A wrong dimension means re-scoping every query and re-running the access-control matrix.

**Answer:** ☐ distinct level ☐ another name for Plant ☐ another name for Zone ☐ something else

*Related and also outstanding:* **D-6** — for each of the 13 business roles, which organisational level bounds what they can see? This gates Phase 3 and does not need a Zoho tenant to answer.

---

## Q8 · AMB-09 — "Custom Approval": configurable routing, or user-written logic?

*Your note:* "Approvals – Multi level approval, Custom Approval in all the modules."

- **Our assumption:** **configurable approval routing** — an administrator configures stages, thresholds, roles, quorum and escalation through a rule engine.
- **Gate:** **Phase 4** · **Owner:** Client product owner + Finance
- **If we are wrong:** high impact. If "custom" means users writing their own approval *logic* — scripts or formulas — that is a materially different product requiring a scripting runtime, sandboxing and a safety model the current design does not include. It would need its own scoping and estimate.

**Answer:** ☐ configurable routing ☐ user-written logic ☐ unsure — let us discuss

---

## Summary

| # | ID | Gate | Cost of a wrong assumption |
|---|---|---|---|
| Q3 | AMB-03 | Phase 2 | **High** — reworks the ledger spine |
| Q5 | AMB-05 | Phase 5 / go-live | **High** — changes how procurement staff work |
| Q8 | AMB-09 | Phase 4 | **High** — potentially a different product |
| Q7 | AMB-07 | Phase 3 | Medium-high — re-scopes access control |
| Q1 | AMB-01 | Phase 0B | Medium — wrong integration key |
| Q4 | AMB-04 | Phase 6 | Low-medium — mitigated by carrying all readings |
| Q6 | AMB-06 | Phase 6 | Low — mitigated by building both |
| Q2 | AMB-02 | Phase 0A | Low — but unrecorded scope if wrong |

**Q3, Q5 and Q8 are the three worth a meeting.** The rest can be answered in writing.

Please also name an individual against each. Return this document annotated, or reply per question — either is fine.
