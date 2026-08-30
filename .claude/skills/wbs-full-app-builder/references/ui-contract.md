# UI contract — the approved visual language

The client approved this interface. Its structure, information density,
navigation model, colour theme, typography, spacing, tables, status treatment
and enterprise feel are the **baseline**, not a starting point for redesign.

New screens must look like they were always part of it.

## `styles.css` is frozen

`app/frontend/styles.css` is **byte-frozen** and pinned by a SHA-256 check in
CI (line-ending normalised, so it holds on Windows and Linux).

- Do not edit it. Do not reformat it. Do not "tidy" it.
- New CSS goes in a **separate extension stylesheet**, loaded after it.
- The extension stylesheet uses **only** design tokens from
  `research/30_contracts/C6_tokens.json`. No raw hex outside `:root`.
- A CI token gate fails the build on any `var(--…)` not present in the registry.

If a change genuinely requires touching `styles.css`, that is a design-approval
conversation, not a code change — and the pin must be updated in the same commit
with a note saying who approved it.

## The CSP constraint is load-bearing

`main.py` sets `style-src 'self'` with no `'unsafe-inline'`. A markup
`style="…"` attribute is therefore **blocked**, while CSSOM assignment
(`el.style.left = …`) is not governed by CSP at all.

The existing `enhance()` depends on exactly that distinction for bar geometry
and tree indentation. Preserve it:

- **Never** emit a `style="` attribute into a template string. An AST gate
  rejects it.
- Set geometry through the CSSOM instead.

This is also why vanilla JS and Web Components are the chosen approach — a
framework that inlines styles would break the policy.

## Components

Build these once, reuse everywhere. Do not hand-roll a second table.

| Component | Must handle |
|---|---|
| Table | sorting, column config, virtualisation at 100k rows, row actions |
| Tree table | hierarchy, expand/collapse, indentation via CSSOM |
| Form | validation, dirty state, server error mapping to fields |
| Dialog | focus trap, ESC, restore focus on close |
| Filter bar | composable filters, cascade to cards, charts, tables **and exports** |
| Status chip | `C3_statuses.json` values only, **non-colour indicator required** |
| Timeline | approval and audit history, actor and timestamp |
| Pagination | cursor-based, consistent with the API |
| Money input | integer paise, never a float, `to_paise` on the boundary |
| Band bar | budget utilisation, geometry via CSSOM |

## Required states

Every screen implements all four. A screen that only handles the happy path is
not done.

- **Loading** — skeleton or spinner, never a blank panel
- **Empty** — says what would appear here and how to create it
- **Error** — the server's `message_id` from `C10_messages.json`, actionable,
  never a raw traceback or a bare "something went wrong"
- **Permission** — out-of-scope **read** renders not-found (a 403 on an id is an
  existence oracle); out-of-scope **write** renders forbidden

## Data

- Every screen is connected to a real API.
- **No static fake data.** The only permitted data is seeded demo data from
  `pg/seed_demo.sql`, which is generated from the exported POC dataset so what
  stakeholders approved is substantively what runs.
- Mock or unverified integration states render an honest badge — the existing
  `mockBadge()` / `mockBanner()` indicators become real mode indicators and are
  never deleted to make a screen look finished.

## Visual verification

Playwright visual regression at **1440 / 1024 / 800 px**, in both densities,
against baselines captured before any refactor. Any pixel diff on an approved
screen fails CI.

The VRT job is pinned to a Windows runner because the baselines were captured
there. Do not "fix" a diff by re-recording baselines — investigate it.

## Accessibility

- axe-core clean on every screen
- keyboard-only traversal complete, visible focus throughout
- dialog focus management (trap, restore)
- **status distinguishable without colour** — icon, text or shape, not hue alone
- form labels bound to controls; errors associated with their field
- WCAG 2.2 AA is the target (no third-party certification is claimed)

## Structure

```
app/frontend/
  index.html          shell, structure unchanged
  styles.css          FROZEN
  extensions.css      new tokens-only styles
  src/
    main.ts registry.ts       boot; SCR-nn -> module, route, nav, permissions
    core/                     state api router format dialog a11y scope mask
    components/               the table above, as Web Components
    features/                 project budget procurement receipts billing
                              closure analytics integration governance
```

Migrating an existing view is a **mechanical move**: each `V.*` function becomes
a feature module, `NAV` becomes `registry.ts`. **No behaviour change in the
first step**, verified by screenshot diff.

## Masking

Classified fields (GSTIN, PAN, vendor contact, bank details) render through the
shared `mask()` formatter unless the reveal permission is present. Masking is
presentation-only and applied centrally so it cannot be forgotten per screen.
Every full reveal writes an audit entry.
