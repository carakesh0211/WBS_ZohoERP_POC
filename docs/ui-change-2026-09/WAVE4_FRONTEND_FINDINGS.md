# Wave 4, stream 4 (frontend) — defects and gaps in files this stream does not own

Reported rather than edited, per the wave rule "Report, do not edit, defects in
files you do not own". Ordered by severity.

---

## STATUS AFTER THE FRONTEND-COMPLETION PASS (2026-09-04)

| # | Was | Now |
|---|---|---|
| 1 | `approval.*` absent from `auth.PERMISSIONS` — BLOCKING | **CLOSED.** All four landed in `1224b83`. The `grantApprovalPermissions` stub is gone and `requireApprovalPermissions` asserts instead. See §1a for what the fix changed underneath. |
| 2 | `--warning` fails AA | open — unchanged, `C6_tokens.json` is not this stream's file |
| 2b | `--n500` fails AA on a hovered row | open — unchanged, `styles.css` is byte-frozen |
| 3 | `playwright.config.js` `threshold: 0.2` | open — unchanged; re-verified at HEAD |
| 4 | Two screens both titled "My Approval Inbox" | open — unchanged, still pinned by a test |
| 5 | Six of eight screens absent from `C8_screens.json` | open — unchanged, still `scr: null` |
| 6 | `<dt>`/`<dd>` outside a `<dl>` in `app.js` | open — unchanged; the four occurrences are in client-approved shell views |

Three findings are NEW and follow at §8, §9 and §10.

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

---

## 1a. `approval.read` excludes Auditor — closed as a decision, restated as a consequence

**File:** `app/backend/auth.py:50-58` (lead-owned)

Contract 4's prose says `approval.read` is held by "every role".
`auth.PERMISSIONS` deliberately excludes **Auditor**, with the reasoning
recorded in the file: granting it would widen
`test_aud_c_006_auditor_is_read_only`, and that is a D-12 call rather than an
implementation one.

This stream has taken that as authoritative and built to it, not around it:

- No navigation entry and no approval route resolves for an Auditor. All eight
  hashes correct to `#home`.
- `approvals.spec.js` uses the Auditor as the **negative control** for the whole
  gate — a principal the server genuinely refuses, rather than one manufactured
  by withholding a stub. If the gate were absent, that test fails.

**The consequence, stated plainly so nobody has to rediscover it:** an Auditor
cannot see any approval instance, timeline or SLA screen at all. The contract
says an auditor reads approval history through the audit chain instead. Whether
that is acceptable to the client is a D-12 question that is still open, and it
is the *only* place where the implementation and Contract 4's prose disagree.

---

## 8. `approvals.py`'s Contract 4 fallback now disagrees with the authoritative table, and fails OPEN

**File:** `app/backend/api/approvals.py:121-131` (stream 3's file)

`_CONTRACT4_PERMISSIONS` is a verbatim transcription of Contract 4, consulted
by `_holders_of()` only for a permission `auth.PERMISSIONS` does not define. It
was correct and necessary while the permissions were missing. They have landed,
so **all four entries are now unreachable** — with one exception that matters:

```python
"approval.read": ("Requestor", "BudgetController", "ProcurementApprover",
                  "FinanceApprover", "CapitalisationApprover", "Auditor",
                  "Administrator"),
```

The fallback **includes Auditor**. `auth.PERMISSIONS` **excludes** it (§1a).
The two tables disagree on exactly the D-12 decision, and the disagreement is
currently masked because `auth.PERMISSIONS` is consulted first.

**Why that is a defect and not just dead code:** the fallback fails OPEN
relative to the authoritative table. If `approval.read` were ever removed from
`auth.PERMISSIONS` — a revert, a bad merge, a deliberate revisit of D-12 — the
API would not fail closed. It would silently resume serving the inbox to every
role, Auditor included, off a frozen copy that no longer reflects any decision
anyone made. A removal intended to *narrow* access would *widen* it.

**Action (stream 3 / lead):** delete `_CONTRACT4_PERMISSIONS` and let
`_holders_of()` return `None` for an undefined permission — which every caller
already treats as a refusal, and which `_authorise()` already turns into a
500 `UNKNOWN_PERMISSION`. That is the fail-closed behaviour the module docstring
says it wants. At minimum, correct the `approval.read` row so the two tables
cannot disagree.

---

## 9. The primary navigation rail no longer fits its viewport

**File:** `app/frontend/styles.css` (byte-frozen) plus the size of `NAV`

Measured from the running application at HEAD as `U-ADM` (29 rail entries):

| Viewport | Rail height | Content | Overflow | Entries below the fold |
|---|---|---|---|---|
| desktop-1440 | 856px | 1050px | **194px** | `audit`, `audit-trail`, `approval-timeline`, `approval-matrix`, `approval-versions`, `approval-simulator`, `settings` |
| laptop-1024 | 713px | 1050px | **337px** | the above plus `cap`, `zoho`, `inventory` |
| tablet-800 | — | — | — | rail is `display:none`; the drawer scrolls |

`.nav` is `overflow-y: auto`, so nothing breaks and the page never widens —
AUD-M-004 still holds. But the whole **Governance** group is now below the fold
at both desktop widths, and that includes `Settings & Master Data`, one of the
five entries APPROVED UI CHANGE 1 was added to expose. It is reachable only by
scrolling a column that gives the user no visual cue that it scrolls.

From the same measurements: removing the seven approval entries an
Administrator sees returns the content to 840px — which fits desktop-1440
(856px) and still overflows laptop-1024 (713px) by 127px. So **A1 alone already
put laptop-1024 into overflow**; the approval entries are what push
desktop-1440 over as well.

Not fixed here. The fix is a navigation pattern — collapsible groups, a denser
rail, or not listing all eight — and inventing one is far outside "the two
approved UI changes". See `A3-approval-navigation.md`.

---

## 10. `--update-snapshots` is unavailable to this stream, and 49 baselines are stale because of it

**Files:** `tests/vrt/approved-ui.spec.js-snapshots/` (36),
`tests/vrt/spa-routing.spec.js-snapshots/` (10),
`tests/vrt/approvals.spec.js-snapshots/` (3)

Listing the eight approval screens in the rail moves every desktop and laptop
baseline, because three of the entries sit near the top of the rail and shift
everything below them. `approval-delegations` moves for a different and
unrelated reason: its committed baseline was captured under an Administrator
holding `approval.delegate`, which Contract 4 does not grant, so it depicts a
principal that cannot exist.

The full accounting, the reason for each, and the confinement proof that must
be run before any of them is accepted are in `A3-approval-navigation.md`.

**Nothing was regenerated.** The wave rule forbids re-baselining an approved
screenshot without written approval, and the working environment blocks
`--update-snapshots` outright. The suite is therefore RED on those 49 rather
than green on overwritten evidence, which is the intended failure mode.

---

## 11. A swallowed mount failure was reported as a rendered screen — FIXED, in a file this stream owns

**File:** `app/frontend/app.js` (this stream's file, so fixed rather than reported)

Recorded here because it is the root cause of the `spa-routing.spec.js`
keyboard-traversal failure that looked like flakiness.

`render()` used to do this:

```js
try { await result.mount(el); }
catch (e) { el.appendChild(errorNode(...)); }   // error OUTSIDE the host
result.node.dataset.mounted = '1';              // ...and mounted anyway
```

`data-mounted` is the whole application's "this screen has rendered" signal.
Setting it after a failed `mount()` meant a screen whose feature module failed
to load was announced as rendered while being empty, and the error box was
appended to `#content` — outside the `.scr-host` that every screen-scoped
assertion and every screen-scoped stylesheet is written against.

Proven, not inferred. Aborting the dynamic import of one feature module and
measuring what the application does:

| | `data-mounted` | `data-mount-failed` | error inside the host | focusable controls |
|---|---|---|---|---|
| before | `"1"` | — | no | **0** |
| after | — | `"1"` | yes | 0 |

A test waiting on `data-mounted` therefore proceeded happily and then failed on
the next thing it looked at — "this screen rendered no focusable control",
which describes the design rather than the fault, and which reads as flakiness
rather than as a load error.

Now a failed mount sets `data-mount-failed`, renders the error inside the host,
and `settleScreen()` in both VRT specs ends its wait on either marker and
throws naming the real failure.

