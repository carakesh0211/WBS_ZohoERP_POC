# A4 — the Fable 5.1 navigation rows: instructed, measured, and one decision referred

**STATUS (2026-09-11, later): DECIDED. The product owner accepted option 1 --
the administrator's 130px scroll at desktop-1440, every other role fitting
exactly -- so all three rows and both collapsible groups stay as measured
below. The rail as re-baselined in `1ebf136` is the approved rail.**

`README.md` records A1 and A2 (approved in writing). `A3-approval-navigation.md`
records why the eight approval screens were withdrawn from the rail: the rail
at desktop-1440 is 856px tall, its approved content was 840px, and no addition
fits without pushing the Governance group below the fold of a scroll container.
This document records what Fable 5.1 added to that same rail, exactly what it
costs, and for whom.

## What changed

| Row | Group | Hash | Permission | Source of the instruction |
|---|---|---|---|---|
| Budget Setup | Project Control (after Budget Planning Grid) | `#budget-setup` | `budget.read` | Product-owner brief 2026-09-11 ("visible Budget Setup with New Budget") |
| Budget Categories | Governance (after Settings & Master Data) | `#budget-categories` | `settings.read` | Product-owner brief 2026-09-11 ("Category master") |
| Exchange Rates | Governance (after Budget Categories) | `#fx-rates` | `fx.read` | Technical lead's Fable 5.1 brief (FX administration UI); **not** in the product-owner brief |
| ANALYTICS ▸ (collapsed group, 11 screens) | new group, after Governance | — | per screen | Product-owner brief 2026-09-11 (visible analytics navigation) |
| INTEGRATION MAPPING ▸ (collapsed group, 4 screens) | new group, after ANALYTICS | — | per screen | Product-owner brief 2026-09-11 (visible mapping navigation) |

Only the two NEW groups are collapsible (`<button class="nav-group">`); the
six approved groups keep their exact `<h2>` markup (`110e4cb`). Every row is
permission-gated through the existing `viewAllowed()` gate; every screen stays
deep-linkable by hash whether or not its row renders.

## What it costs, measured from the running application (desktop-1440, rail 856px)

Each item row is 30px and each group header 28px. The five additions are
3 × 30 + 2 × 28 = **146px**.

| Principal | Rail rows rendered | Content height | Overflow | Below the fold |
|---|---|---|---|---|
| U-REQ (Requestor) | 28 | 856px | **0** — fits exactly | nothing |
| U-FIN (Finance) | 28 | 856px | **0** — fits exactly | nothing |
| U-ADM (Administrator), both new groups collapsed | 33 | 986px | **130px** | the last rows of Governance and both collapsed group headers |
| U-ADM, both new groups expanded | 48 | 1436px | 580px | as expected for an expanded 15-screen list |

The administrator is the only seeded principal who sees every row (the
connector and audit rows are `connector.read` / `audit.read`); for every other
reviewer role the rail fits without scrolling. The rail is `overflow-y: auto`
(byte-frozen `styles.css`), so the overflow scrolls with the browser's own
scrollbar; no fade or cue was added, because that would be a change to the
approved shell and this stream did not have the approval to make one.

laptop-1024 (rail 714px) was already 127px over before Fable 5.1 (A1's open
item) and is now 273px over for the administrator; tablet-800 renders no rail.

## What is asked

One decision, with the numbers above rather than an argument:

1. **Accept** the administrator's 130px scroll at desktop-1440 (every other role fits), or
2. **Move** Budget Categories and Exchange Rates off the rail into the Settings & Master Data screen, where master data already lives — saves 60px, leaves 70px over for the administrator — or
3. **Approve** a denser rail or collapsible approved groups, which is a change to the approved shell.

Until it is taken, the rail stays as measured here. Reverting any row is one
`scr('…')` splice in `app/frontend/app.js`; the routes survive the deletion.

## The baselines

Every full-page snapshot that includes the rail moves by the rail region only.
The re-baseline is accounted for by `tests/vrt/evidence/prove-baseline-delta.py`
against the rail box in `after-regions.json` (`navRect` 0,44,224,856 at
desktop-1440); the account is in `BASELINE-DELTA-2026-09-11.txt` beside this
file once the bounded VRT run (`tools/run_vrt_batches.sh`) has produced the
complete inventory of moved snapshots. A snapshot whose difference lies
outside the rail is a regression and is not re-recorded.

## The account (2026-09-11, after the bounded run)

The bounded run (`tools/run_vrt_batches.sh`, 45 batches, 1h35) failed eleven
batches: approvals, approved-ui and spa-routing snapshots at desktop-1440 and
laptop-1024 (the rail), approved-ui at tablet-800 (one snapshot, see below),
and four non-screenshot guards (the approved-navigation sequence, the
routable-screen registry, the integration stream's last-rail-row assertion,
and a raw `fetch()` in `budget-api.js`) — each answered in `3ca2db3`, not
routed around.

The three snapshot specs were then re-captured deliberately (`--update-snapshots`
scoped to their snapshot tests only) and `tests/vrt/evidence/prove-baseline-delta.py
--ref HEAD --regions after-regions.json` produced `BASELINE-DELTA-2026-09-11.txt`:

| | |
|---|---|
| snapshots compared | 102 (39 unchanged, 63 changed) |
| changed and confined to the rail box (`navRect`) | **59** |
| changed with pixels outside the rail | **4**: `wbs` at all three viewports, `pos` at laptop-1024 |

The four are not the rail and not a regression. Their out-of-rail pixels
(30, 52, 109 and 93 px; bounding boxes inside a table row) carry a maximum
channel delta of **56/255**, which is exactly `--n700` (#3A4048) against
`--n500` (#6B7280): (107−58, 114−64, 128−72) = (49, 50, 56). That is the
approved hover-contrast correction on this branch (`5e69a44`: muted text in
a hovered row moves from 4.32:1 to 9.35:1), landing on the row the pointer
rests on when the screenshot is taken. The tool exits non-zero for them by
design — it cannot know that a colour step is approved — and they are
accepted here by name, with the arithmetic, rather than by widening a box.
