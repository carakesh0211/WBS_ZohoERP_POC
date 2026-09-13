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

`laptop-1024` and `tablet-800` baselines for these same three spec files were
**not** re-recorded — they were out of scope for the desktop-1440 verification
run this stream was asked to perform — and will fail if that project is run
before someone re-records them the same way.
