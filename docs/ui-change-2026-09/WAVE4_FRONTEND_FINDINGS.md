# Wave 4, stream 4 (frontend) — defects and gaps in files this stream does not own

Reported rather than edited, per the wave rule "Report, do not edit, defects in
files you do not own". Ordered by severity.

---

## 1. `approval.*` permissions are absent from `auth.PERMISSIONS` — BLOCKING

**File:** `app/backend/auth.py` (lead-owned, frozen)

Wave 4 Contract 4 specifies four new permissions:

```
approval.read       every role
approval.act        Requestor, BudgetController, ProcurementApprover,
                    FinanceApprover, CapitalisationApprover
approval.configure  Administrator
approval.delegate   BudgetController, ProcurementApprover, FinanceApprover,
                    CapitalisationApprover
```

None of them exists at the Wave 4 baseline (`6e3f576`). `/api/bootstrap` derives
the caller's permission set from `auth.PERMISSIONS`, so **no principal currently
holds any approval permission**, and every one of the eight approval screens is
correctly invisible and unroutable in a real session.

**Consequence, and why it is not hidden:** this is why Part B moved **zero** of
the 69 approved visual-regression baselines. The eight new navigation entries
are permission-gated and no real principal passes the gate, so the approved
screenshots are untouched. That is the correct behaviour today and it doubles as
proof that the gate works.

**In the tests:** `tests/vrt/approvals.spec.js` fetches the REAL bootstrap and
adds the four permissions to the response (`grantApprovalPermissions`). Nothing
else is faked — session, identity, entities and projects are the real server's.
The negative half of every gating assertion is proved against an *unmodified*
bootstrap, so the gate cannot pass by being absent.

**Action:** add the four permissions to `auth.PERMISSIONS`, then DELETE
`grantApprovalPermissions` from the spec. It is marked in the file.

---

## 2. `--warning` (#A66A00) fails WCAG 2.2 AA at body size, on every surface

**Files:** `research/30_contracts/C6_tokens.json`, `app/frontend/styles.css`

`--warning` is the only semantic token that fails the 4.5:1 body-text minimum,
and it fails against every background in the design:

| Foreground | Background | Ratio | |
|---|---|---|---|
| `--warning` #A66A00 | `--n0` #FFFFFF | **4.4843:1** | FAIL |
| `--warning` #A66A00 | `--n50` #F7F8F9 | **4.2173:1** | FAIL |
| `--warning` #A66A00 | `--warning-bg` #FDF3E2 | **4.0775:1** | FAIL |
| `--warning` #A66A00 | `--n100` #EFF1F3 | **3.9604:1** | FAIL |
| `--positive` #1E7A45 | `--n0` | 5.3504:1 | pass |
| `--negative` #B3261E | `--n0` | 6.5365:1 | pass |
| `--progress` #5B4BB5 | `--n0` | 6.7340:1 | pass |
| `--info` #2E5AAC | `--n0` | 6.6155:1 | pass |
| `--n500` #6B7280 | `--n0` | 4.8345:1 | pass |

C6_tokens.json's `colour.contrast_rule` states: "Every text/background pairing
above meets WCAG 2.2 AA (4.5:1 for body text, 3:1 for large text and UI
boundaries)." **That claim is false for `warning-600`.** `styles.css` inherits
it through `.st-warning { color: var(--warning) }`, so every warning status
anywhere in the application is below AA.

It went unnoticed because no existing screen's test fixture renders a warning
status under axe. The approval screens do (ESCALATED stages, overdue SLA rows),
so axe found it immediately.

**No compliant fix exists inside the registry** — there is no darker amber
token, and inventing a hex is exactly what
`test_no_new_off_token_colour_is_introduced` exists to prevent. A new token is
needed, roughly `#8A5800` or darker (≥ 4.5:1 on `--n0`).

**Interim mitigation, in this stream's own file only:** `approvals.css` sets
`.approval-card .status.st-warning { color: var(--n900) }`. The amber **dot** is
kept — a 7px dot is a non-text indicator needing only 3:1, which #A66A00 clears
— so the semantic colour still carries alongside the `!` glyph and the text
label. Delete that rule once the token is corrected.

---

## 2b. `--n500` fails AA on a hovered table row, application-wide

**File:** `app/frontend/styles.css` (byte-frozen)

`styles.css` line 289: `tbody tr:hover { background: var(--primary-50) }`.
Against that tint, the muted text colour drops below AA:

| Foreground | on `--n0` #FFFFFF | on `--primary-50` #EAF4F6 (hover) |
|---|---|---|
| `--n500` #6B7280 | 4.8345:1 pass | **4.3210:1 FAIL** |
| `--warning` #A66A00 | 4.4843:1 FAIL | **4.0080:1 FAIL** |
| `--positive` #1E7A45 | 5.3504 | 4.7821 pass |
| `--negative` #B3261E | 6.5365 | 5.8422 pass |
| `--progress` #5B4BB5 | 6.7340 | 6.0188 pass |
| `--info` #2E5AAC | 6.6155 | 5.9129 pass |
| `--n700` #3A4048 | — | 9.3528 pass |
| `--n900` #1B1F24 | — | 14.8016 pass |

So **any `.muted` text inside a table row fails AA while that row is hovered**,
anywhere in the application. Only `--n500` and `--warning` are affected; every
other semantic colour survives the tint.

It is invisible to the existing suites because axe is only run on screens whose
fixtures put no muted text in a table row, and because a contrast failure that
requires hover is easy to miss by eye.

**Not fixed here:** `styles.css` is byte-frozen and the product owner approved
exactly two visual changes, neither of them this.

**Avoided in the approval screens, and it is the better design anyway:** no
table-cell content is de-emphasised. A value in a data table is something the
reviewer is meant to read, not a footnote. `.approval-cell-note` in
`approvals.css` keeps the smaller size that distinguishes an annotation from a
primary value but takes `--n700`, which is 9.35:1 on the hover tint.

**Action (lead):** either darken `--n500`, or lighten the hover tint, or scope
`.muted` out of table rows.

---

## 3. `playwright.config.js` cannot detect a colour regression

**File:** `playwright.config.js` (repo root, not owned by this stream)

Covered in full in `README.md` of this directory. In short: `maxDiffPixelRatio:
0` is set under a comment promising that "a pixel diff is a failure", but
`threshold` is left at its default `0.2` and is applied first. Any colour shift
with a YIQ distance below 1408 is invisible to the whole suite. The avatar
change (A2, distance 264.89) passed every tablet-800 baseline unchanged despite
moving 555 pixels.

**Action:** set `threshold: 0` (or ≤ 0.05).

---

## 4. Two screens both claim SCR-03's name, "My Approval Inbox"

**File:** `app/frontend/app.js` (this stream's file, but the screen is
client-approved with pixel baselines)

`V.approvals` (`#approvals`) sets the page title to "My Approval Inbox" —
C8_screens.json's `name_verbatim` for SCR-03 — but it is a **pre-engine
placeholder**: it reads `/api/dashboard`'s alert buckets (budget exceptions,
pending revisions, awaiting capitalisation) and knows nothing about approval
instances, stages, quorum or delegation.

`#approval-inbox` is the real SCR-03, reading `GET /api/approvals/inbox`.

Two different screens therefore carry the same page title. The navigation rail
does distinguish them ("My Approvals" vs "My Approval Inbox"), and both states
are pinned by
`approvals.spec.js` → *the engine-backed inbox and the pre-engine shell view are
distinct screens*, so neither can drift silently.

Retitling `#approvals` was **not** done here: it is a client-approved screen
with its own baselines, and the product owner approved exactly two visual
changes, neither of them this.

**Action (lead):** either retire the placeholder once the engine lands, or
rename it. The test above is what has to be updated to make that change, which
is the intent.

---

## 5. Six of the eight approval screens are not in `C8_screens.json`

**File:** `research/30_contracts/C8_screens.json` (frozen, 40 screens)

C8 names only two of the eight surfaces M4b asks for:

| Screen | C8 |
|---|---|
| My Approval Inbox | **SCR-03** |
| Approval Matrix Configuration | **SCR-29** |
| Approval Request Detail | *not in C8* |
| Approval Timeline and Audit History | *not in C8* |
| Workflow Version History | *not in C8* |
| Approval Rule Simulator | *not in C8* |
| Delegation Management | *not in C8* |
| Escalation and SLA Monitor | *not in C8* |

`src/core/router.js` carries `scr: null` for the six rather than an invented
number. A plausible-looking "SCR-41" would manufacture traceability the registry
does not provide, and the traceability matrix is supposed to catch exactly that.

**Action (lead):** extend C8, or record these six as out-of-registry surfaces.

---

## 6. `<dt>` / `<dd>` outside a `<dl>` in four places in `app.js`

**File:** `app/frontend/app.js`

`app.js` builds `.kv` blocks as `<div class="kv">` containing `<dt>`/`<dd>`.
`<dt>` and `<dd>` are only meaningful inside a `<dl>`; axe's `dlitem` rule fails
them and a screen reader announces orphaned terms rather than a description
list. Four occurrences.

Not changed here: all four are inside client-approved shell views with pixel
baselines, and `<dl>` carries a UA-default `margin: 1em 0` that `<div>` does
not, so the change is not pixel-neutral without an accompanying CSS rule — and
`styles.css` is byte-frozen.

The approval screens use a real `<dl class="kv approval-kv">`, with
`.approval-kv { margin: 0 }` in `approvals.css` to cancel the UA margin. That is
the pattern to copy when the shell views are fixed.

---

## 7. `simulate` is a read carried over POST, so 403 and 404 remain distinguishable

**Files:** `app/frontend/src/core/api-client.js` (not owned), plus the server's
status choice

`classifyStatus` collapses 403 into `notfound` for GET and HEAD only, on the
documented grounds that a write has "no id to leak that the caller did not
already name". `POST /api/approvals/definitions/{id}/simulate` is semantically a
**read** — it evaluates routing and creates nothing — carried over POST only
because it takes a body. A caller who guesses a definition id can therefore
still distinguish "exists but forbidden" from "does not exist".

Low severity: definition ids are configuration, not per-record data. But the
anti-oracle rule is applied by HTTP verb rather than by whether the call
actually mutates, and this is the case where those two diverge.

The behaviour is asserted rather than skipped, in
`approvals.spec.js` → *a 403 read and a 404 read render identically*, which
branches on `writeShaped` and asserts the write-path behaviour explicitly.

**Action (lead):** decide whether `simulate` should be a GET, or whether
`classifyStatus` should key off intent rather than verb.
