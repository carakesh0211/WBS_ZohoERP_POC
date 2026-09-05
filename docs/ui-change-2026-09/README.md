# The two approved UI changes — 2026-09-03

The product owner approved, in writing, exactly two visual changes to the
client-approved UI. They are recorded here with before/after evidence, the
measured effect, and the accounting for every visual-regression baseline that
moved.

They are **not** a redesign. Structure, colour theme, fonts, density, spacing,
tables and overall feel are unchanged. Nothing was added to or removed from the
design-token registry.

Source: `docs/WAVE4_CONTRACTS.md`, section "The two APPROVED UI changes —
stream 4 only".

> **There is now a THIRD change to this rail, and it is not one of these two.**
> Wave 4's frontend-completion pass listed the eight approval-engine screens in
> the primary navigation, on the lead's instruction. It has **no product-owner
> approval**, it moves 46 baselines, and it is recorded separately in
> `A3-approval-navigation.md` — deliberately not in this document, so that
> "approved" keeps meaning approved. Read that file before accepting any
> re-baseline of the snapshots accounted for below.

---

## A1 — Primary-navigation entries for the five completed screens

### What changed

Five entries were added to the primary navigation rail, permission-gated using
the existing gate:

| Entry | Group | Hash | Permission required |
|---|---|---|---|
| Budget Planning Grid (cells) | Project Control | `#budget-grid` | `budget.read` |
| Budget Version Comparison | Project Control | `#budget-compare` | `budget.read` |
| Budget Availability Check (cells) | Project Control | `#budget-availability` | `budget.check` |
| Audit Trail Viewer | Governance | `#audit-trail` | `audit.read` |
| Settings & Master Data | Governance | `#settings` | `settings.read` **or** `masters.read` |

The rail goes from **17 entries to 22** for a principal holding all five
permissions. No existing entry was moved, renamed or removed; the five are
appended to the end of the two groups they belong to, so every previously
approved entry keeps its position and its neighbours.

These five screens were already routable and deep-linkable. What they lacked
was any way to reach them from inside the running application.

### The exact visual difference

- The navigation rail gains five rows: three at the end of **Project Control**
  (after "Budget Revisions"), two at the end of **Governance** (after "Audit
  Trail").
- Nothing else on any page moves. The rail is a fixed-width column
  (`--nav-w: 224px`); adding rows changes pixels inside that column only.
- **Below 900px the rail is `display: none`** (AUD-M-004 — it collapses to an
  overlay drawer). So at the 800px baseline width, A1 changes nothing at all.
  This is confirmed by `before-report.json` / `after-report.json`:
  `tablet-800.navRailRendered` is `false` in both.

### Permission gating

The gating is not new code. `navAllowed()` already suppressed an entry whose
`need` the principal does not hold, and suppressed a group heading whose every
item is out of reach.

What *is* new is that `SCR_ROUTES` now declares `ico` and `label` alongside
`need`, and the `NAV` table splices those **same row objects** in by reference.
The navigation gate and the route gate are therefore literally the same object,
so a permission loosened for the rail cannot drift from the permission enforced
on the route. Previously `need` was restated in two tables and a test was
relied on to notice when the two restatements disagreed.

---

## A2 — The `#userAvatar` colour correction

### The defect

`#userAvatar` renders the signed-in user's initials as **white, 11px,
600-weight** text on the avatar circle. Under WCAG 2.2, 11px/600 is body text,
not large text (large is 18.66px bold or 24px regular), so the requirement is
**4.5:1**.

| | Token | Hex | Measured contrast with `#FFFFFF` |
|---|---|---|---|
| Before | `--primary-500` | `#2E8A9A` | **4.0244 : 1** — fails AA |
| After | `--primary-600` | `#24707E` | **5.6850 : 1** — passes AA |

Both figures are measured from the element's own computed styles in a real
browser by `tests/vrt/evidence/capture-approved-change.js`, using the WCAG 2
relative-luminance formula, and are recorded in `before-report.json` and
`after-report.json`. The "before" figure reproduces the 4.02:1 stated in the
contract exactly.

### Why this specific change, and why it is the smallest one available

The brief asked for the **smallest** change reaching 4.5:1, preferring a
minimal darkening of the existing teal over a change of hue.

The frozen brand ramp offers, going darker from `--primary-500`:

| Token | Hex | Contrast with white |
|---|---|---|
| `--primary-500` | `#2E8A9A` | 4.0244 : 1 |
| `--primary-600` | `#24707E` | **5.6850 : 1** |
| `--primary-700` | `#1B5762` | 8.1199 : 1 |
| `--primary-800` | `#123B43` | 12.1222 : 1 |

`--primary-600` is the **first** step that clears 4.5:1, so it is the smallest
darkening available. It is also already the design's declared "default
interactive tone" (C6 `colour.brand.usage`), so the avatar now shares the tone
of the primary buttons and the active nav item rather than introducing anything.

A finer-grained intermediate value was not an option, and deliberately so.
`tests/test_contracts.py::test_the_root_palette_is_exactly_the_frozen_token_palette`
requires the `:root` palette to be *exactly* the C6 token set, and
`test_no_new_off_token_colour_is_introduced` requires the off-token set to stay
exactly the nine known GAP-02 values. Any hand-tuned hex — say a
4.6:1 tone between the two — would have to be added to
`research/30_contracts/C6_tokens.json`, which is not this stream's file and
would be a change to the token registry rather than a correction within it.
Choosing an existing token is what makes this a contrast fix instead of a
palette change.

Recolouring the *text* instead was rejected for the same reason: white on the
shell bar is the approved treatment for every other control in it, and darkening
one label would have made the avatar the only element in the bar not using it.

### The exact visual difference

One 26px circle in the shell bar becomes one step darker. Its size, position,
border-radius, font, weight and text are unchanged. It is present at all three
baseline widths, which is why A2 — unlike A1 — moves the 800px baselines too.

---

---

## A finding: the VRT harness cannot see the A2 colour change

This was discovered while accounting for the baselines, and it is reported
rather than fixed, because `playwright.config.js` is not this stream's file.

`playwright.config.js` sets `maxDiffPixelRatio: 0` under a comment that reads
"A pixel diff is a failure, not a warning. The whole point is that the approved
design does not drift." **It does not achieve that.** It leaves Playwright's
per-pixel `threshold` at its default of `0.2`, and `threshold` is applied
*before* `maxDiffPixelRatio`: a pixel whose colour moved by less than the
threshold is not counted as differing at all, so the ratio never sees it.

Playwright gates on pixelmatch's YIQ colour distance, where
`maxDelta = 35215 × threshold²`:

| `threshold` | `maxDelta` | Avatar change (delta **264.89**) |
|---|---|---|
| 0.2 (current default) | 1408.60 | **ignored** |
| 0.1 | 352.15 | ignored |
| 0.05 | 88.04 | detected |
| 0 | 0 | detected |

The consequence is visible in the results: at **tablet-800** the navigation rail
is `display: none`, so the avatar is the *only* thing that changed — and all 23
tablet-800 baselines **passed unchanged**, even though a pixel-exact comparison
finds 555 differing pixels in exactly the 26×26 avatar circle at (107,33)-(133,59).

So:

- A2 is **not** protected by the visual-regression baselines. It is protected by
  `spa-routing.spec.js` → `#userAvatar meets WCAG 2.2 AA, measured rather than
  assumed`, which computes the contrast ratio from the element's own computed
  colours and asserts ≥ 4.5:1. That test was added for exactly this reason.
- More generally, **any** colour drift up to that magnitude currently passes the
  whole VRT suite silently. Recommend setting `threshold: 0` (or ≤ 0.05) in
  `playwright.config.js` so the config matches its stated intent. Owner: lead.

---

## Baseline accounting

A wholesale `--update-snapshots` is forbidden: it turns every real regression
into a new baseline silently, and a previous stream did exactly that and hid a
694px regression.

So every re-captured baseline is accounted for, and the accounting is
mechanical rather than asserted. `tests/vrt/evidence/prove-baseline-delta.py`
compares each committed baseline with its replacement and reports the bounding
box of the differing pixels. It **fails** if any changed pixel falls outside the
two regions the approved change was allowed to touch — the avatar rect and the
nav rail column — where both rects are *measured from the running application*
by the capture script rather than hard-coded.

Run it with:

```
python tests/vrt/evidence/prove-baseline-delta.py --ref <commit-before-the-change>
```

`BASELINE-DELTA.txt` in this directory is the recorded output of that run.

## Files here

| File | What it is |
|---|---|
| `before-report.json` | Measured nav entries, rail visibility, and avatar colours and contrast, at all three widths, BEFORE the change |
| `after-report.json` | The same after the change, plus the measured avatar and nav-rail rects |
| `after-regions.json` | A copy of `after-report.json` under the fixed name `prove-baseline-delta.py` reads |
| `before-*.png`, `after-*.png` | Full-page, shell-bar-only and nav-rail-only captures at 1440 / 1024 / 800 |
| `BASELINE-DELTA.txt` | The pixel-confinement proof for the re-captured baselines |

`before-report.json` carries no `avatarRect` / `navRect`: the rect measurement
was added to the capture script after the "before" run, and re-running it would
have meant reverting the change to re-measure geometry that does not move. The
prover needs the *after* geometry anyway — the avatar rect is identical in both
states, and the nav-rail box is deliberately unbounded in height so that a
taller rail is still inside it.

Regenerate with the application listening on `$CAPEX_EVIDENCE_PORT`:

```
CAPEX_EVIDENCE_LABEL=after CAPEX_EVIDENCE_PORT=8801 \
  node tests/vrt/evidence/capture-approved-change.js
```
