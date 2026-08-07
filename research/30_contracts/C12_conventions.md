# C12 — Markdown & Authoring Conventions (FROZEN 2026-08-06)

Binding on every authoring agent. A QA gate checks most of these mechanically.

## Headings & anchors
- Section headings use `## <number>. <heading_verbatim>` taken **exactly** from `C11_outline.json`. Do not reword, renumber or merge.
- Sub-headings inside a section start at `###`.
- Screen specs use `#### SCR-nn — <name_verbatim>` then each of the 24 attributes as a `##### <Attribute>` sub-heading.
- Cross-reference by section number and heading text (`see §22 Budget and Commitment Calculation Logic`), never by page or by a bare link.

## Identifiers
| Kind | Format | Registry |
|---|---|---|
| Requirement | `REQ-<MOD>-<nnn>` | `C1_req_ids.json` |
| Screen | `SCR-01`..`SCR-40` | `C8_screens.json` |
| Message | `MSG-<AREA>-<nnn>` | `C10_messages.json` |
| Entity | exact name | `C4_entities.json` |
| Status | exact label | `C3_statuses.json` |
| Role | exact name | `C9_roles.json` |
| Conflict / open question | `CONF-nn` / `OQ-nn` | `C13_conflicts.json` |
| Risk | `RISK-nn` | authored in §42 |

Never invent an identifier outside these registries. If you need one that does not exist, return a `contract_gap` note instead of improvising.

## Evidence and citation
- Cite **only** claims with `citable=true` in `research/20_verified/claims.verified.json`.
- Every Zoho endpoint, path, method and OAuth scope must come from `research/20_verified/zoho_endpoint_inventory.json`. That file is machine-generated from Zoho's own OpenAPI bundle, so anything absent from it does not exist as far as this document is concerned.
- Citation format: `[source: <url> — verified 2026-08-05]`. For spec-derived rows: `[source: Zoho ERP OpenAPI bundle openapi-all.zip, sha256 E95A0399… — verified 2026-08-05]`.
- Anything not evidenced is written verbatim as **`UNVERIFIED - REQUIRES ZOHO CONFIRMATION`** (Zoho) or **`UNVERIFIED - REQUIRES CONFIRMATION`** (everything else). Never soften, never omit, never guess.
- Where a research claim was downgraded to OVERSTATED, use the `narrower_true_statement` from the verifier, not the original claim.
- **Never state a capability as present because a page could not be fetched.** Inaccessible is not the same as true.

## SAP variant labelling
SAP behaviour differs between classic on-premise ECC/S4 on-premise and S/4HANA Cloud Public Edition. Verification found 18 records that stated Cloud-only rules as general SAP rules. Therefore: **every SAP claim must name the variant its source documents**, e.g. "(S/4HANA Cloud Public Edition)". If the variant is unclear, say so.

## Intellectual property
- Describe SAP Fiori patterns in your own words. No verbatim SAP text over 15 words, no SAP theme hex values, no screenshots, no asset URLs, no SAP logos or marks.
- Never imply SAP endorsement, certification or partnership.
- Design tokens come from `C6_tokens.json` only.

## Numbers, currency, dates
- Indian digit grouping: `1,50,00,000`. Never `150,000,00` or `15,000,000`.
- Rupee symbol prefixed, no space.
- Lakh/Crore abbreviations only on dashboard tiles and chart axes; full grouped figures everywhere else.
- Two decimals on transaction screens, none on dashboard tiles, one on percentages.
- Negatives in parentheses.
- Dates `dd-MMM-yyyy` in prose and UI; ISO-8601 in payloads, exports and audit records.
- **Quote the frozen worked examples in `C5_formulas.json` verbatim.** Do not recompute them or substitute your own numbers.

## Tables
- Use a table wherever the master prompt asks for a comparison, matrix, inventory or catalogue.
- Keep column order consistent with the registry that defines it (e.g. the 27 endpoint-inventory columns in `C11_outline.json`).
- Every row asserting a Zoho capability carries a non-empty verified date. A QA gate fails the build otherwise.
- Wide tables: keep them; the reader can scroll. Do not drop columns to fit.

## Mermaid
Pinned subset only — `flowchart TD|LR`, `stateDiagram-v2`, `erDiagram`, `sequenceDiagram`.
- No `%%{init}%%` directives, no styling blocks, no click handlers, no HTML in labels.
- Node labels: alphanumerics, spaces, hyphens only. Wrap any label containing punctuation in double quotes.
- The 44-entity ER diagram is split into three domain-scoped diagrams (Project & Budget, Procurement & Actuals, Integration & Audit) — one diagram of 44 entities is unreadable.

## Voice
- Enterprise solution-document register. Specific, decision-oriented, no filler.
- State what the system does and what the user sees. Avoid "robust", "seamless", "leverage", "cutting-edge".
- When something is a recommendation rather than a confirmed requirement, say so in the sentence — do not rely on the reader inferring it from a section heading.

## Provenance discipline
The client PDF contains **no WBS hierarchy at all** — only project plus budget head. Every WBS, PR-reservation, webhook and multi-entity requirement originates in the master prompt or is derived. Tag these `provenance=MASTER-PROMPT` or `DERIVED-RECOMMENDATION` and never present them as confirmed client requirements.
