# F — every navigation group becomes collapsible

Fable 5.1 Stream F made every group in the primary navigation rail
(`app/frontend/app.js`'s `renderNav()`) collapsible. Previously only the two
Fable 5.1 groups added for the analytics and mapping screens (`ANALYTICS`,
`INTEGRATION MAPPING`) rendered as `<button class="nav-group" aria-expanded>`
toggles; the six client-approved groups (`Work`, `Project Control`,
`Procurement & Actuals`, `Closure`, `Integration`, `Governance`) rendered as
static `<h2 class="nav-group">` headings, always open, per the client's
original sign-off. This was a binding product decision, not a proposal: every
group is now a real `<button>` with a caret, `aria-expanded`, and an
`aria-controls` target pointing at a wrapper (`id="capex-nav-<slug>"`) holding
its rows. `Work` and `Project Control` default open (the screens used every
day); `Procurement & Actuals`, `Closure`, `Integration`, `Governance`,
`ANALYTICS` and `INTEGRATION MAPPING` default closed. The choice is now
persisted **per signed-in user, per browser** in `localStorage` under
`capex.nav.group.<user_id>.<group>` — the one deliberate exception to this
file's usual "session state only, never localStorage" rule — with the old
`sessionStorage` key removed outright rather than kept as a fallback. The
group holding the current route is always forced open for that render only
(never persisted) and carries `aria-current="true"` plus a `.nav-group-current`
visual marker, so a deep link or an in-app navigation into a collapsed group
never hides the screen the user is actually on. Two new controls
(`data-nav-expand-all` / `data-nav-collapse-all`, styled with styles.css's
existing `.btn-sm`/`.btn-ghost` classes) sit above the groups to act on all of
them at once. All new CSS lives in `app/frontend/extensions.css`;
`app/frontend/styles.css` is untouched and its checksum gate still passes.

The change measurably fixes the debt `tests/vrt/nav-rail-budget.spec.js` had
tracked since before Fable 5.1: at the rail's new default render (Work +
Project Control open, everything else closed), laptop-1024's resting content
drops from roughly 986px to ~631px against a 713px rail, so the rail no longer
overflows by default for any seeded principal — a fact that test file's
laptop-1024 assertion now requires instead of the old "already overflowing"
one. Several existing specs asserted the exact set or order of `.nav-item`
elements rendered by default (`spa-routing.spec.js`, `fx-rates.spec.js`,
`integration.spec.js`, `budget-setup.spec.js`); these were updated to open the
specific groups (or click "Expand all") their assertions actually depend on,
rather than assuming everything is already open. `nav-rail-budget.spec.js`,
`budget-setup.spec.js` and `integration.spec.js` also had a `nav.children`
"first-to-last bounding rect" content measurement that silently broke once a
collapsed group's item panel (present in the DOM, behind `hidden`, for
`aria-controls`) could be the rail's last child — fixed by filtering out
`hidden` children before measuring. A new file, `tests/vrt/nav-groups.spec.js`,
covers the feature itself (defaults, persistence, deep-link discoverability,
expand/collapse-all, keyboard activation, focus visibility, no horizontal
overflow, axe-core) across all three approved viewports.

## Re-recorded baselines (desktop-1440 only, per the verification command run)

Every baseline below was re-recorded because the rail's markup changed (new
"Expand all"/"Collapse all" row; the six previously-static groups are now
buttons with carets; most groups are collapsed by default, so far fewer
`.nav-item` rows render). Each diff was inspected before re-recording and
confirmed pixel-for-pixel confined to the rail region (0–224px) — the page
content to the right of the rail is byte-identical in every case.

**tests/vrt/approved-ui.spec.js-snapshots/** (18 files):
`home`, `approvals`, `alerts`, `projects`, `wbs`, `budget`, `check`,
`revisions`, `prs`, `pos`, `grns`, `bills`, `recon`, `cap`, `zoho`,
`inventory`, `audit`, `wbs-cosy` (all `-desktop-1440-win32.png`).

**tests/vrt/spa-routing.spec.js-snapshots/** (5 files):
`spa-audit-trail`, `spa-budget-grid`, `spa-budget-compare`,
`spa-budget-availability`, `spa-settings` (all `-desktop-1440-win32.png`).

**tests/vrt/budget-setup.spec.js-snapshots/** (3 files):
`budget-setup-list`, `budget-setup-editor`, `budget-setup-categories`
(all `-desktop-1440-win32.png`).

**tests/vrt/approvals.spec.js-snapshots/** (8 files, found in a broader sweep
after the initial verification — this file was not in the task's named
verification command, but does carry full-page baselines for the eight
approval-engine screens):
`approvals-approval-inbox`, `approvals-approval-request`,
`approvals-approval-timeline`, `approvals-approval-matrix`,
`approvals-approval-versions`, `approvals-approval-simulator`,
`approvals-approval-delegations`, `approvals-approval-sla`
(all `-desktop-1440-win32.png`).

Every other `tests/vrt/*.spec.js` file was checked for `toHaveScreenshot`
calls; only `approved-ui.spec.js`, `spa-routing.spec.js`, `budget-setup.spec.js`
and `approvals.spec.js` have any, and all four are accounted for above.

## Re-recorded baselines — laptop-1024 and tablet-800

The desktop-1440 pass above left `laptop-1024` and `tablet-800` baselines for
these same four spec files unrecorded. This section accounts for both,
following the same rule: every diff was inspected and confirmed
pixel-for-pixel confined to the rail region before recording (or, at
tablet-800, confirmed there was no diff at all — see below).

**laptop-1024** behaves exactly like desktop-1440 — the rail renders at full
width there too (`.nav` is not `display:none` until 900px), so the same set of
full-page screenshots move by the rail region only:

**tests/vrt/approved-ui.spec.js-snapshots/** (18 files):
`home`, `approvals`, `alerts`, `projects`, `wbs`, `budget`, `check`,
`revisions`, `prs`, `pos`, `grns`, `bills`, `recon`, `cap`, `zoho`,
`inventory`, `audit`, `wbs-cosy` (all `-laptop-1024-win32.png`).

**tablet-800 needed NO re-recording for `approved-ui.spec.js`.** Below 900px
`styles.css` makes `.nav` an overlay drawer that is `display:none` until a
user opens it (AUD-M-004), and none of `approved-ui.spec.js`'s full-page
screenshots opens it — so the rail was never part of its tablet-800 baseline
picture, before or after Stream F. Verified by running the file at
`--project=tablet-800` with no `--update-snapshots`: **20/20 passed, 0
failures, 0 diffs.**

### spa-routing.spec.js

**laptop-1024** moved the same way as desktop-1440: the five SCR-nn
full-page screenshots, each confirmed rail-confined before recording.

**tests/vrt/spa-routing.spec.js-snapshots/** (5 files):
`spa-audit-trail`, `spa-budget-grid`, `spa-budget-compare`,
`spa-budget-availability`, `spa-settings` (all `-laptop-1024-win32.png`).

The rest of the file (50 non-screenshot tests: routing, permission gates,
required states, accessibility, CSP) passed unchanged at laptop-1024 — full
file run, 55/55 passed.

**tablet-800 surfaced a real bug, not a baseline diff.** Two tests this
stream had already patched to open the Governance group before reading
`.nav-item` (`the primary navigation is exactly the approved navigation`,
`a navigation entry is permission-gated, not merely present`) called
`page.click('[data-nav-group="..."]')` directly, with no
`openNavIfCollapsed()` first. Below 900px the rail is a closed overlay
drawer (AUD-M-004): the group header is not clickable until the drawer is
open, so both tests failed at tablet-800 with a Playwright timeout on the
click, not a screenshot mismatch. Fixed by adding `await
openNavIfCollapsed(page)` (and, in the permission-gate test, `await
openNavIfCollapsed(auditor)` for its second page) before each group click —
the same helper the file already used everywhere else a group needed to be
interacted with below 900px. tablet-800 needed **no** baseline re-recording
(same reasoning as `approved-ui.spec.js`): the five visual-regression
screenshots never open the rail. Verified with the fix in place: full file
at `--project=tablet-800`, **55/55 passed, 0 failures.**

### budget-setup.spec.js

**laptop-1024** moved the same way as desktop-1440: the three full-page
screenshots, each confirmed rail-confined before recording.

**tests/vrt/budget-setup.spec.js-snapshots/** (3 files):
`budget-setup-list`, `budget-setup-editor`, `budget-setup-categories`
(all `-laptop-1024-win32.png`).

Full file at laptop-1024 after recording: **29/29 passed.**

**tablet-800 needed no re-recording** — same reasoning as the other three
files: the rail is `display:none` below 900px and none of these three
screenshots open it. Full file at `--project=tablet-800` with no
`--update-snapshots`: **29/29 passed, 0 failures, 0 diffs.**

### approvals.spec.js

**laptop-1024** moved the same way as desktop-1440: the eight approval-engine
full-page screenshots, each confirmed rail-confined before recording.

**tests/vrt/approvals.spec.js-snapshots/** (8 files):
`approvals-approval-inbox`, `approvals-approval-request`,
`approvals-approval-timeline`, `approvals-approval-matrix`,
`approvals-approval-versions`, `approvals-approval-simulator`,
`approvals-approval-delegations`, `approvals-approval-sla`
(all `-laptop-1024-win32.png`).

Full file at laptop-1024 after recording: **103 passed, 1 skipped, 0
failed** (104 tests total). The one skip is pre-existing and unrelated to
Stream F: `it renders and resolves identically across every locale this
harness can set` (line 1883) is deliberately `test.skip()`-guarded to
desktop-1440 only ("the locale sweep is viewport-independent; it runs
once") — it also skips at laptop-1024 and tablet-800 unconditionally,
regardless of anything this stream touched.

**tablet-800 needed no re-recording** — same reasoning as the other three
files: the rail is `display:none` below 900px and none of these eight
screenshots open it. Full file at `--project=tablet-800` with no
`--update-snapshots`: **103 passed, 1 skipped, 0 failed** (the same
pre-existing desktop-only skip).

## Summary across all four files, all three projects (after every fix and re-recording above)

| Spec file | desktop-1440 | laptop-1024 | tablet-800 |
|---|---|---|---|
| `approved-ui.spec.js` | 20 passed | 20 passed | 20 passed |
| `spa-routing.spec.js` | 55 passed | 55 passed | 55 passed |
| `budget-setup.spec.js` | 29 passed | 29 passed | 29 passed |
| `approvals.spec.js` | 104 passed | 103 passed, 1 skipped | 103 passed, 1 skipped |

No failures anywhere in this table. The one skip recurring in
`approvals.spec.js` is the pre-existing, desktop-only locale sweep noted
above, not a Stream F side effect. Every count above is from running that
one spec file alone at that one project (`--project=<name>`), per file, per
viewport — not inferred from a combined run.
