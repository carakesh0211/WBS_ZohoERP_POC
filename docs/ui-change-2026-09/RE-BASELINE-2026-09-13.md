# The one re-baseline pass of 2026-09-13 — streams A–F on one tree

**What this is.** The single, explained re-recording of visual baselines
after the six frontend streams of 2026-09-13 (A internal fulfilment, B
Administrator override, C export on every register, D two sign-in choices,
E notifications, F collapsible navigation) were integrated on
`fable-5.1/full-app-hardening-uat`. Nothing was re-recorded before this
pass except Stream F's own rail baselines (commits `886ca1c`, `bab8d52`…
`960e43d`), which F accounted for in `F-collapsible-navigation.md`.

**Rule applied.** No unexplained failure was re-baselined. The release run
(`docs/fable51/evidence/release/vrt-batches-2026-09-13-run1.txt`, 14 specs ×
3 viewports) was read batch by batch; every screenshot failure was traced to
one of the four causes below by measuring WHERE the pixels changed (a script
over the old and new PNGs printed the changed row bands and column extents
per file), and the largest changes were inspected by eye before anything was
kept. Three failures were NOT pixels and were fixed in the specs instead
(next section). One thing the pass found was a defect, fixed before
recording (last section).

## The four causes, and where they show

| Cause | Stream | Pixels | Screens |
|---|---|---|---|
| Two new shell-bar buttons, **Notifications** and **Change password**, in the identity cluster | D, E | rows 9–34, cols ~702–1359 at desktop-1440; the whole right-hand cluster re-lays-out | every screenshot in every spec — this is why approved-ui failed 18/19/19 and spa-routing 5/5/5 |
| The **export control** (Export CSV / Export XLSX, now with its dataset caption) in the page-header actions | C | rows ~75–96, right of the header | every register, dashboard and closure list (14 datasets, 13 screens) |
| The **Administrator holds every permission** | B | content area | Create / Raise / Request buttons that used to be replaced by "your role cannot…" notices; Approve on approval-inbox rows, budget revisions, capitalisation; Amend / Cancel / Close on every purchase-order row (the POs page grew from 900 to 1020 px) |
| One more **rail row** in Procurement & Actuals (Internal Material Requests, A) and one in Governance (Notifications, E) | A, E | the rail column, cols 0–208, below each new row | any screenshot whose rail shows those groups open (audit, bills, GRNs, reconciliation…) |

Nothing else moved. In particular no table column, money figure, status
badge or colour changed, and no screenshot outside the four specs below
failed.

## Failures that were not pixels

* `approvals.spec.js` "every approval screen is ROUTE-gated on its own
  permission" and "a refused decision shows the SERVER refusal" — both used
  the Administrator as the principal WITHOUT a permission; it now holds all
  of them. Adapted to the Requestor, recorded in `tests/ADAPTATIONS.md`.
* `approvals.spec.js` "the measured regions still describe the screen" —
  the delegations screen's identity cluster moved (x 1175 → 966 at
  desktop-1440, the two new buttons). Re-measured deliberately with
  `CAPEX_VRT_WRITE_REGIONS=1`, as that test's own message instructs;
  `tests/vrt/evidence/delegation-regions.json` is the diff to read.
* `integration.spec.js` and `fx-rates.spec.js` at tablet-800 — the tests
  clicked a navigation group while the rail was a closed drawer (F made every
  group collapsible; below 900 px the rail is `display:none` until
  `#navToggle`). The specs now open the drawer first, the way
  `spa-routing.spec.js` already did. Not a product change.

## The defect the pass found

The Purchase Requests register mounts TWO export controls (headers and
lines), and the IMR register likewise; both rendered as two identical
"Export CSV / Export XLSX" pairs side by side. `export-button.js` now shows
the dataset's name as a caption before its buttons and names the dataset in
each button's accessible name (visible text first, so
`getByRole('button', { name: 'Export CSV' })` still resolves). Fixed before
the baselines below were recorded, so they carry the caption.

A second one, at laptop-1024 and tablet-800 only: approved-ui's "the
document never scrolls horizontally" invariant failed on Budget Revisions.
Measured with a Playwright script rather than guessed: the register's table
(888 px, now carrying the Administrator's Approve column) scrolled correctly
inside its 766 px `.table-wrap`, but the `sr-only` spans in its cells are
`position:absolute`, the wrap was not positioned, so their containing block
was `.body` and their boxes escaped the wrap's clipping and widened the
document to 1112 px. Latent since the first wide table; visible now.
`extensions.css` gives `.table-wrap` `position: relative` (styles.css is
frozen); every register measured 1024/800 wide afterwards, and approved-ui
passes 20/20 at both widths.

The same leak had been inflating two DESKTOP baselines vertically for as
long as they have existed: `pos` (1440×1020) and `zoho` (1440×3245) were
full-page captures whose extra height was the same escaped `sr-only` boxes,
not content — `#content` scrolls internally (742 px tall on the Zoho
connector screen, 3181 px of content inside it), so everything below the
first viewport in those images was shell background. Measured with the
positioning toggled on and off through the CSSOM on a live page: with the
fix the document is 900 px on both screens, without it 1020 and 3245. The
confirmation run and CI both caught the two stale files
("Expected an image 1440px by 3245px, received 1440px by 900px"); they were
re-recorded at their honest height, and `avatar-regions.spec.js`'s
desktop-1440 entry was re-measured (`CAPEX_VRT_WRITE_REGIONS=1`) for the
same shifted identity cluster the delegation regions moved with.

## What was recorded

Recorded with `--update-snapshots` (Playwright rewrites only a mismatched
file), one spec and one viewport at a time, on a fresh local-demo server,
then CONFIRMED by a second full run of the release plan
(`docs/fable51/evidence/release/vrt-batches-2026-09-13-run2.txt`).

102 baselines in four specs, plus `tests/vrt/evidence/delegation-regions.json` (re-measured at all three viewports).

### `approvals.spec.js` — 24 files

* `approvals-approval-delegations-desktop-1440-win32.png`
* `approvals-approval-delegations-laptop-1024-win32.png`
* `approvals-approval-delegations-tablet-800-win32.png`
* `approvals-approval-inbox-desktop-1440-win32.png`
* `approvals-approval-inbox-laptop-1024-win32.png`
* `approvals-approval-inbox-tablet-800-win32.png`
* `approvals-approval-matrix-desktop-1440-win32.png`
* `approvals-approval-matrix-laptop-1024-win32.png`
* `approvals-approval-matrix-tablet-800-win32.png`
* `approvals-approval-request-desktop-1440-win32.png`
* `approvals-approval-request-laptop-1024-win32.png`
* `approvals-approval-request-tablet-800-win32.png`
* `approvals-approval-simulator-desktop-1440-win32.png`
* `approvals-approval-simulator-laptop-1024-win32.png`
* `approvals-approval-simulator-tablet-800-win32.png`
* `approvals-approval-sla-desktop-1440-win32.png`
* `approvals-approval-sla-laptop-1024-win32.png`
* `approvals-approval-sla-tablet-800-win32.png`
* `approvals-approval-timeline-desktop-1440-win32.png`
* `approvals-approval-timeline-laptop-1024-win32.png`
* `approvals-approval-timeline-tablet-800-win32.png`
* `approvals-approval-versions-desktop-1440-win32.png`
* `approvals-approval-versions-laptop-1024-win32.png`
* `approvals-approval-versions-tablet-800-win32.png`

### `approved-ui.spec.js` — 54 files

* `alerts-desktop-1440-win32.png`
* `alerts-laptop-1024-win32.png`
* `alerts-tablet-800-win32.png`
* `approvals-desktop-1440-win32.png`
* `approvals-laptop-1024-win32.png`
* `approvals-tablet-800-win32.png`
* `audit-desktop-1440-win32.png`
* `audit-laptop-1024-win32.png`
* `audit-tablet-800-win32.png`
* `bills-desktop-1440-win32.png`
* `bills-laptop-1024-win32.png`
* `bills-tablet-800-win32.png`
* `budget-desktop-1440-win32.png`
* `budget-laptop-1024-win32.png`
* `budget-tablet-800-win32.png`
* `cap-desktop-1440-win32.png`
* `cap-laptop-1024-win32.png`
* `cap-tablet-800-win32.png`
* `check-desktop-1440-win32.png`
* `check-laptop-1024-win32.png`
* `check-tablet-800-win32.png`
* `grns-desktop-1440-win32.png`
* `grns-laptop-1024-win32.png`
* `grns-tablet-800-win32.png`
* `home-desktop-1440-win32.png`
* `home-laptop-1024-win32.png`
* `home-tablet-800-win32.png`
* `inventory-desktop-1440-win32.png`
* `inventory-laptop-1024-win32.png`
* `inventory-tablet-800-win32.png`
* `pos-desktop-1440-win32.png`
* `pos-laptop-1024-win32.png`
* `pos-tablet-800-win32.png`
* `projects-desktop-1440-win32.png`
* `projects-laptop-1024-win32.png`
* `projects-tablet-800-win32.png`
* `prs-desktop-1440-win32.png`
* `prs-laptop-1024-win32.png`
* `prs-tablet-800-win32.png`
* `recon-desktop-1440-win32.png`
* `recon-laptop-1024-win32.png`
* `recon-tablet-800-win32.png`
* `revisions-desktop-1440-win32.png`
* `revisions-laptop-1024-win32.png`
* `revisions-tablet-800-win32.png`
* `wbs-cosy-desktop-1440-win32.png`
* `wbs-cosy-laptop-1024-win32.png`
* `wbs-cosy-tablet-800-win32.png`
* `wbs-desktop-1440-win32.png`
* `wbs-laptop-1024-win32.png`
* `wbs-tablet-800-win32.png`
* `zoho-desktop-1440-win32.png`
* `zoho-laptop-1024-win32.png`
* `zoho-tablet-800-win32.png`

### `budget-setup.spec.js` — 9 files

* `budget-setup-categories-desktop-1440-win32.png`
* `budget-setup-categories-laptop-1024-win32.png`
* `budget-setup-categories-tablet-800-win32.png`
* `budget-setup-editor-desktop-1440-win32.png`
* `budget-setup-editor-laptop-1024-win32.png`
* `budget-setup-editor-tablet-800-win32.png`
* `budget-setup-list-desktop-1440-win32.png`
* `budget-setup-list-laptop-1024-win32.png`
* `budget-setup-list-tablet-800-win32.png`

### `spa-routing.spec.js` — 15 files

* `spa-audit-trail-desktop-1440-win32.png`
* `spa-audit-trail-laptop-1024-win32.png`
* `spa-audit-trail-tablet-800-win32.png`
* `spa-budget-availability-desktop-1440-win32.png`
* `spa-budget-availability-laptop-1024-win32.png`
* `spa-budget-availability-tablet-800-win32.png`
* `spa-budget-compare-desktop-1440-win32.png`
* `spa-budget-compare-laptop-1024-win32.png`
* `spa-budget-compare-tablet-800-win32.png`
* `spa-budget-grid-desktop-1440-win32.png`
* `spa-budget-grid-laptop-1024-win32.png`
* `spa-budget-grid-tablet-800-win32.png`
* `spa-settings-desktop-1440-win32.png`
* `spa-settings-laptop-1024-win32.png`
* `spa-settings-tablet-800-win32.png`

