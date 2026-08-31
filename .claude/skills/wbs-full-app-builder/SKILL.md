---
name: wbs-full-app-builder
description: Builds the production CAPEX and WBS Control Hub as complete vertical slices, preserving the client-approved UI and implementing PostgreSQL, financial controls, approvals, Zoho integration, dashboards and production tests. Use only when explicitly invoked for full-application development.
disable-model-invocation: true
user-invocable: true
argument-hint: "[start|continue|milestone-number]"
---

# WBS full application builder

**Build the application. Do not re-plan it.**

The plan exists (`docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md`, v1.2.1,
approved). The contracts exist (`research/30_contracts/`). The approved UI
exists (`app/frontend/`). Your job is to turn them into working software, one
vertical slice at a time.

## The failure modes this skill exists to prevent

Do not do any of these. Each has already cost this engagement real time:

- writing another plan, roadmap or options analysis
- running a platform experiment that the current feature does not depend on
- producing an audit or status document *instead of* a feature
- stopping to ask about a choice that could be a configuration row
- calling a milestone done with backend-only or UI-only work
- claiming a Zoho integration is LIVE or VERIFIED without sandbox evidence

## First actions, every invocation

1. `git status` — **preserve existing user work**. Never discard, revert or
   stash changes you did not make. If the tree is dirty, work with it.
2. Confirm branch `full-application/build`.
3. Read `docs/FULL_APPLICATION_DELIVERY_STATUS.md`.
4. Read **only** the requirement and reference files the current milestone
   needs. Do not re-read the whole plan.
5. Write code.

## `continue` means

- read the delivery-status file
- inspect the current branch and test state
- resume the **first incomplete acceptance criterion**
- write code immediately
- **do not regenerate the plan**

`start` begins milestone 1. A number jumps to that milestone.

## Vertical slice — the only unit of completion

A slice is done when **all** of these exist and pass together:

| | |
|---|---|
| Migration | PostgreSQL, expand/contract, `-- ROLLBACK:` section |
| Backend | repository + domain service, scoped queries |
| API | FastAPI route, RFC-7807 errors, `X-Correlation-Id` |
| UI | screen wired to that API, no fake data |
| Permissions | server-side, fail-closed |
| Audit | append-only, hash-chained events |
| Seed | demo data so the screen is demonstrable |
| Tests | unit, invariant, permission negative, API |
| Docs | delivery-status update only |

Backend-only scaffolding, a frontend mockup, or documentation alone **never**
counts. Details: `references/vertical-slice-dod.md`.

## Non-negotiable technical rules

- **Money is integer paise.** No float touches a monetary value, ever.
- **`app/frontend/styles.css` is byte-frozen.** New CSS goes in a separate
  token-based stylesheet using only `C6_tokens.json` values.
- Preserve the approved structure, colours, fonts and feel — see
  `references/ui-contract.md`.
- FastAPI + PostgreSQL + **modular vanilla JS ES modules**. No React, no Svelte.
- Maker-checker, budget-head isolation, ordered ancestor-chain locking,
  idempotency, append-only audit — see `references/domain-controls.md`.
- Zoho stays behind product adapters — see `references/zoho-boundaries.md`.
- **Never** mark a Zoho path LIVE or VERIFIED without authorised sandbox
  evidence. `MOCK` is honest; a false `VERIFIED` is not.

## Uncertain business choices

Make them **configurable**, not blocking. A threshold, an approval level, a
category, a label, a tolerance — put it in a config table or a seeded settings
row with a documented default, and note the assumption in the delivery status.
Stopping to ask about something that could be a row is a failure mode, not
diligence.

## Testing and committing

- Run focused tests continuously while building.
- Run the **full suite** before every milestone commit.
- Commit and push each completed milestone.
- Then **continue automatically to the next milestone**.

## When to stop

Only for the conditions in `references/stop-conditions.md`:
secret entry, an irreversible cloud or production action, an unsafe destructive
operation, a business decision that genuinely cannot be configuration, or a
failing invariant that makes further work unsafe.

Curiosity, incomplete documentation and possible future improvements are **not**
stop conditions.

## Hard boundaries

- **Never** create or modify cloud resources without separate authorisation.
- **Never** access `praktiq` or `wbs-capex-poc`.
- **Never** expose secrets or client data — not in code, logs, tests, fixtures,
  screenshots or commits.
- **Never** run another platform experiment unless the current feature is
  demonstrably blocked by that specific unknown.

## Milestones

1. **Production PostgreSQL foundation** — schema, constraints, migration
   runner, audit chain, test harness on PostgreSQL.
2. **Settings and master data** — entities, plants, divisions, branches, zones,
   locations, users, roles, custom fields, item and vendor masters.
3. **WBS and budget management** — hierarchy, budget heads, control cells,
   revisions, availability check, fiscal periods.
4. **Identity, permissions and approvals** — row-level scope, role grants,
   configurable approval engine, delegation, maker-checker.
5. **Purchase Requests and Purchase Orders** — line-level PR, reservations,
   conversion, PO lifecycle, amendments.
6. **Zoho adapters, GRN and Vendor Bill sync** — adapter interface, outbox,
   inbound receives and bills, quarantine.
7. **Reconciliation, dashboards and reports** — exception queues, control
   totals, dashboards, filters, exports.
8. **Capitalisation, hardening and deployment readiness** — asset allocation,
   closure, security hardening, runbooks.

## Delivery status file

Maintain `docs/FULL_APPLICATION_DELIVERY_STATUS.md`. **Concise.** Only:

- current milestone
- completed vertical slices
- current slice
- next three deliverables
- genuine blockers
- latest test and commit status

It is a status file, not another audit report. Keep it under roughly one screen.

## Reporting

Short and implementation-focused. What shipped, what passed, what is next. No
essays, no re-statement of the plan, no speculative architecture.

## References

- `references/domain-controls.md` — financial correctness rules
- `references/ui-contract.md` — approved visual language and components
- `references/vertical-slice-dod.md` — completion checklist
- `references/zoho-boundaries.md` — integration limits and honesty rules
- `references/stop-conditions.md` — the only reasons to stop
