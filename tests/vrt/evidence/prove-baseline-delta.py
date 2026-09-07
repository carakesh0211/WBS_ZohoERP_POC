#!/usr/bin/env python
"""Prove that a VRT re-baseline changed ONLY the pixels it was allowed to change.

A wholesale `--update-snapshots` is the single most dangerous move available to
this stream: it turns every real regression into a new baseline, silently. A
previous stream did exactly that and it hid a 694px regression. So the rule is
that a re-baseline must be ACCOUNTED FOR, snapshot by snapshot, and this script
is the accounting.

For every snapshot PNG under tests/vrt/**-snapshots/ it compares the committed
baseline (from a git ref, default HEAD) with the working-tree file and reports:

  * unchanged / changed / added / removed
  * for a changed one: the number of differing pixels and their bounding box
  * whether that bounding box lies entirely inside the regions the approved
    change was allowed to touch

The two approved changes of 2026-09-03 touch exactly two regions:

  A1  the primary navigation rail  -- five entries added. The rail is a
      fixed-width column on the left, below the shell bar, and it is
      display:none below 900px. So at tablet-800 NOTHING may move for A1.
  A2  the #userAvatar circle       -- one token step darker. It sits in the
      shell bar, which is present at every width.

Anything outside those two boxes is a regression, not an approved change, and
this script exits non-zero and names the file.

The 2026-09-07 delegations re-baseline added a third kind of account, because
neither of the above could describe it honestly. Two approved corrections
landed on one screen and one of them changed the HEIGHT of a form, so
everything below the form moved. Permitting that as a box would have meant
allowing arbitrary change across most of the page.

    A BOX IS A PERMISSION. A TRANSLATION IS A PROOF.

So `--screen-regions` carries, per snapshot and per viewport, both:

  * boxes that MAY differ (the shell bar's identity block and the form), and
  * a boundary below which the new baseline MUST be the old one translated by
    a single vertical offset, pixel for pixel -- an offset this script finds
    for itself rather than being told. See best_translation().

That turns "138,000 pixels changed" into "nothing below the form changed at
all; it moved by exactly the amount the form grew", which is a claim a reader
can check and a regression cannot satisfy.

Usage:
    python tests/vrt/evidence/prove-baseline-delta.py [--ref HEAD] [--regions regions.json]

`--regions`, `--avatar-regions` and `--screen-regions` all take JSON MEASURED
from the running application -- by capture-approved-change.js, by
tests/vrt/avatar-regions.spec.js and by the 'delegations re-baseline regions'
test in tests/vrt/approvals.spec.js respectively -- rather than guessed. The
last two are re-asserted against the live screen on every VRT run, so an
allowance cannot quietly go stale and grow.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops

REPO = Path(__file__).resolve().parents[3]
SNAPSHOT_DIRS = sorted((REPO / "tests" / "vrt").glob("*-snapshots"))

# Which playwright project a snapshot belongs to, from its filename suffix.
PROJECTS = ("desktop-1440", "laptop-1024", "tablet-800")


def git_show(ref: str, rel: str) -> bytes | None:
    """The committed bytes of a file, or None if it is not in that ref."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=REPO, capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def project_of(name: str) -> str | None:
    for p in PROJECTS:
        if f"-{p}-" in name:
            return p
    return None


def diff_mask(old: Image.Image, new: Image.Image):
    """(mask, differing pixel count, bbox) over the region the two images share.

    A size change is reported separately by the caller; here the overlap is
    what is compared, so a taller page still gets its content checked rather
    than being written off as "different size, cannot tell".

    The comparison is EXACT — any non-zero channel difference counts. This is
    deliberately stricter than the visual-regression harness itself, which
    leaves Playwright's per-pixel `threshold` at 0.2 and therefore ignores
    colour shifts up to a YIQ distance of 1408. A re-baseline accounted for
    with the same blind spot that let the change through would not be an
    account of anything.
    """
    w = min(old.width, new.width)
    h = min(old.height, new.height)
    a = old.convert("RGB").crop((0, 0, w, h))
    b = new.convert("RGB").crop((0, 0, w, h))
    diff = ImageChops.difference(a, b)
    bbox = diff.getbbox()
    mono = diff.convert("L").point(lambda v: 1 if v else 0, mode="1")
    if bbox is None:
        return mono, 0, None
    # Count differing pixels, not just the box: a box says where, a count says
    # how much, and a one-pixel change inside a huge box would otherwise read
    # as a huge change.
    count = sum(1 for p in mono.getdata() if p)
    return mono, count, bbox


def best_translation(old: Image.Image, new: Image.Image, rect, noise: int, limit: int = 96):
    """Is `rect` in the new image the OLD image translated vertically, exactly?

    A BOX IS A PERMISSION; A TRANSLATION IS A PROOF.

    When an approved change alters the HEIGHT of something, everything below it
    moves. Handing that to `leaked_outside` as an allowed box would mean
    permitting arbitrary change across most of the page -- which is precisely
    the accounting this tool exists to refuse. But "it moved" is a far stronger
    claim than "it is allowed to differ", and it is checkable: if the region
    below the change is the previous baseline shifted by ONE CONSTANT, pixel
    for pixel, then nothing in it changed at all.

    So this searches for that constant rather than being told it. A regression
    inside the region -- one row restyled, one number different -- has no dy
    that makes the region match, and the caller reports it as unaccounted.

    Rows near the edges have no counterpart at `y - dy`; those are compared
    UNSHIFTED rather than skipped, so the bottom of the page is still checked.

    @returns (dy, max per-channel difference) for the best exact fit, or
             (None, max difference at the best dy tried).
    """
    x0, y0, x1, y1 = rect
    h = min(old.height, new.height)
    x1 = min(x1, old.width, new.width)
    y1 = min(y1, h)
    if x1 <= x0 or y1 <= y0:
        return None, 0

    o = old.convert("RGB")
    n = new.convert("RGB")
    best = (None, 255)

    for dy in range(-limit, limit + 1):
        # Rows whose counterpart at y - dy exists.
        ys, ye = max(y0, dy), min(y1, h + dy)
        worst = 0
        if ys < ye:
            a = o.crop((x0, ys - dy, x1, ye - dy))
            b = n.crop((x0, ys, x1, ye))
            worst = _max_channel_delta(a, b)
        # Rows that fall off the edge under this shift: checked in place, so a
        # change down there cannot hide in the gap the shift opened up.
        for ea, eb in ((y0, ys), (ye, y1)):
            if ea < eb:
                worst = max(worst, _max_channel_delta(
                    o.crop((x0, ea, x1, eb)), n.crop((x0, ea, x1, eb))))
        if worst < best[1]:
            best = (dy, worst)
        if worst <= noise:
            return dy, worst
    return None, best[1]


def _max_channel_delta(a: Image.Image, b: Image.Image) -> int:
    diff = ImageChops.difference(a, b)
    bands = diff.split()
    worst = bands[0]
    for band in bands[1:]:
        worst = ImageChops.lighter(worst, band)
    return max(worst.getdata()) if worst.getbbox() else 0


def leaked_outside(old: Image.Image, new: Image.Image, boxes, noise: int):
    """What changed OUTSIDE every allowed box, and by how much.

    Checking the bounding box against one region is not enough: a change that
    legitimately touches two separated regions — the nav rail on the left and
    the avatar on the right — has a bounding box spanning the whole page, which
    is inside neither. So the allowed regions are painted out and whatever
    survives is the unexplained change.

    MAGNITUDE IS REPORTED, NOT SWALLOWED. Re-rendering a page shifts a handful
    of antialiased edge pixels by 1/255 in a single channel; treating that as a
    regression would make this tool cry wolf until someone stopped reading it,
    and treating it as invisible without saying so would be the same
    hand-waving this tool exists to replace. So `noise` sets the threshold for
    FAILING, and the largest per-channel difference actually seen outside the
    regions is returned either way, so the number is on the record.

    @returns (failing pixel count, bbox of failures, max channel delta outside)
    """
    from PIL import ImageDraw

    w = min(old.width, new.width)
    h = min(old.height, new.height)
    diff = ImageChops.difference(old.convert("RGB").crop((0, 0, w, h)),
                                 new.convert("RGB").crop((0, 0, w, h)))
    # Largest single-channel difference per pixel.
    bands = diff.split()
    worst = bands[0]
    for b in bands[1:]:
        worst = ImageChops.lighter(worst, b)

    # Paint the approved regions to zero, then look at what is left.
    draw = ImageDraw.Draw(worst)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle([x0, y0, min(x1, w), min(y1, h)], fill=0)

    max_outside = max(worst.getdata()) if worst.getbbox() else 0
    failing = worst.point(lambda v: 255 if v > noise else 0)
    bbox = failing.getbbox()
    if bbox is None:
        return 0, None, max_outside
    return sum(1 for p in failing.getdata() if p), bbox, max_outside


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="HEAD", help="git ref holding the previous baselines")
    ap.add_argument("--regions", default="docs/ui-change-2026-09/after-regions.json")
    ap.add_argument(
        "--avatar-regions", default="tests/vrt/evidence/avatar-regions.json",
        help="additional MEASURED #userAvatar rects, one set per project, covering the "
             "states the baselines depict beyond the home screen `--regions` was captured "
             "on. Written by tests/vrt/avatar-regions.spec.js, which also asserts it is "
             "still true. Pass an empty string to consider only `--regions`.")
    ap.add_argument(
        "--screen-regions", default="tests/vrt/evidence/delegation-regions.json",
        help="MEASURED regions for ONE screen's re-baseline, written and continuously "
             "re-asserted by the 'delegations re-baseline regions' test in "
             "tests/vrt/approvals.spec.js. The file names the snapshots it speaks for in "
             "its own `applies_to` key, so this tool carries no screen name of its own. "
             "Pass an empty string to ignore it.")
    ap.add_argument("--noise", type=int, default=1,
                    help="largest per-channel difference outside the approved regions that "
                         "is treated as re-render antialiasing rather than a regression "
                         "(default 1/255). The observed maximum is always reported.")
    args = ap.parse_args()

    regions_path = REPO / args.regions
    if not regions_path.exists():
        print(f"regions file not found: {regions_path}", file=sys.stderr)
        print("Run capture-approved-change.js first.", file=sys.stderr)
        return 2
    regions = json.loads(regions_path.read_text(encoding="utf-8"))

    # The avatar does not sit in one place. `--regions` was captured on the home
    # screen, but the shell bar lays #userAvatar out differently in other states
    # a baseline depicts -- at tablet-800 the density toggle moves it from
    # x=106.6 to x=10, which is exactly the state wbs-cosy.png records. Those
    # extra positions are MEASURED (tests/vrt/avatar-regions.spec.js, which also
    # fails if they stop being true) rather than allowed for by widening the box,
    # so this stays an account of where the avatar is and not a bigger hole.
    extra_avatars: dict = {}
    if args.avatar_regions:
        extra_path = REPO / args.avatar_regions
        if extra_path.exists():
            extra_avatars = json.loads(extra_path.read_text(encoding="utf-8"))
        else:
            print(f"note: {extra_path} not found; considering --regions only",
                  file=sys.stderr)

    # The 2026-09-07 delegations re-baseline. TWO approved corrections landed on
    # one screen -- the fixture identity (the baseline depicted a principal who
    # cannot open the screen) and the replacement of the native <input
    # type="date"> -- and between them they move the shell bar's identity
    # cluster, the form, and everything the form's new height pushed down.
    #
    # Only the first two are PERMITTED here. The third is PROVED: below the
    # table's measured top, the new baseline must be the old one translated by a
    # single constant this tool finds for itself, pixel for pixel.
    screen_regions: dict = {}
    screen_applies_to = None
    if args.screen_regions:
        sr_path = REPO / args.screen_regions
        if sr_path.exists():
            screen_regions = json.loads(sr_path.read_text(encoding="utf-8"))
            screen_applies_to = screen_regions.pop("applies_to", None)
            if not screen_applies_to:
                print(f"{sr_path} has no `applies_to`; it names no snapshots, so it is "
                      "ignored.", file=sys.stderr)
                screen_regions = {}
        else:
            print(f"note: {sr_path} not found; no per-screen regions applied",
                  file=sys.stderr)

    def avatar_box(av):
        # One pixel of slack on each side: a border-radius edge antialiases
        # against its neighbour, so the visibly-changed area can be a
        # fraction wider than the element box.
        return (int(av["x"]) - 2, int(av["y"]) - 2,
                int(av["x"] + av["width"]) + 2, int(av["y"] + av["height"]) + 2)

    def allowed_boxes(project: str):
        """The union of boxes this project's snapshots may differ inside."""
        r = regions.get("viewports", {}).get(project, {})
        boxes = []
        av = r.get("avatarRect")
        if av:
            boxes.append(avatar_box(av))
        for state_rect in (extra_avatars.get(project) or {}).values():
            if state_rect:
                box = avatar_box(state_rect)
                if box not in boxes:
                    boxes.append(box)
        nav = r.get("navRect")
        if nav and r.get("navRailRendered"):
            boxes.append((int(nav["x"]), int(nav["y"]),
                          int(nav["x"] + nav["width"]) + 1, 10 ** 6))
        return boxes

    def pad(rect, by=2):
        """A measured element box, in whole pixels, with an antialiasing margin."""
        return (int(rect["x"]) - by, int(rect["y"]) - by,
                int(rect["x"] + rect["width"]) + by,
                int(rect["y"] + rect["height"]) + by)

    def screen_account(name: str, project: str):
        """(extra allowed boxes, translation rect) for a per-screen re-baseline."""
        if not screen_applies_to or not name.startswith(screen_applies_to):
            return [], None
        r = screen_regions.get(project)
        if not r:
            return [], None
        boxes = [pad(r["principalRect"])]
        boxes += [pad(c) for c in r.get("headerClusterRects", [])]
        # The form, across the card's full width and down to the table's top:
        # the band the date-field change is allowed to redraw.
        card, form, table = r["cardRect"], r["formRect"], r["tableTop"]
        boxes.append((int(card["x"]) - 2, int(form["y"]) - 2,
                      int(card["x"] + card["width"]) + 2, int(round(table["y"]))))
        translate = (int(card["x"]) - 2, int(round(table["y"])),
                     int(card["x"] + card["width"]) + 2, 10 ** 6)
        return boxes, translate

    changed: list[tuple[str, int, tuple, str]] = []
    unchanged: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    offending: list[str] = []
    worst_outside = 0

    for d in SNAPSHOT_DIRS:
        for png in sorted(d.glob("*.png")):
            rel = png.relative_to(REPO).as_posix()
            old_bytes = git_show(args.ref, rel)
            if old_bytes is None:
                added.append(rel)
                continue
            old = Image.open(io.BytesIO(old_bytes))
            new = Image.open(png)
            size_note = ""
            if old.size != new.size:
                size_note = f" size {old.size}->{new.size}"
            _mask, count, bbox = diff_mask(old, new)
            if count == 0 and not size_note:
                unchanged.append(rel)
                continue

            project = project_of(png.name) or "?"
            boxes = allowed_boxes(project)

            extra, translate = screen_account(png.stem, project)
            boxes += extra
            shift_note = ""
            if translate is not None:
                dy, worst = best_translation(old, new, translate, args.noise)
                if dy is None:
                    offending.append(
                        f"{rel}: below y={translate[1]} the new baseline is NOT the old one "
                        f"translated by any single offset within +/-96 px (closest fit still "
                        f"differs by {worst}/255). Something in the region the approved change "
                        f"only MOVED has actually changed."
                    )
                else:
                    shift_note = (f" [below y={translate[1]}: an exact translation of "
                                  f"{dy:+d} px, max difference {worst}/255]")
                    boxes.append(translate)

            leaked, leak_bbox, max_outside = leaked_outside(old, new, boxes, args.noise)
            worst_outside = max(worst_outside, max_outside)
            changed.append((rel, count, bbox, size_note + shift_note, leaked, max_outside))

            if leaked:
                offending.append(
                    f"{rel}: {leaked} of {count} differing px fall OUTSIDE the approved "
                    f"regions by more than {args.noise}/255, at {leak_bbox}{size_note}. "
                    f"Allowed: {boxes}"
                )
            elif size_note and not regions.get("viewports", {}).get(project, {}).get("navRailRendered", False):
                # The page got taller where the rail is not even rendered, so
                # the rail cannot be what grew.
                offending.append(f"{rel}: page size changed{size_note} at a width "
                                 "where the navigation rail is not rendered")

    # Baselines committed in the ref but no longer on disk.
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", args.ref, "tests/vrt"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.splitlines()
    for rel in tracked:
        if rel.endswith(".png") and not (REPO / rel).exists():
            removed.append(rel)

    print(f"baseline delta against {args.ref}")
    print(f"  unchanged : {len(unchanged)}")
    print(f"  changed   : {len(changed)}")
    print(f"  added     : {len(added)}")
    print(f"  removed   : {len(removed)}")
    print()
    for rel, count, bbox, note, leaked, max_outside in changed:
        flag = "" if not leaked else f"  <-- {leaked} px OUTSIDE the approved regions"
        print(f"  CHANGED {rel}: {count} px at {bbox}{note} "
              f"(max delta outside the approved regions: {max_outside}/255){flag}")
    for rel in added:
        print(f"  ADDED   {rel}")
    for rel in removed:
        print(f"  REMOVED {rel}")

    if removed:
        print("\nFAIL: a baseline was deleted. A removed baseline is a screen that "
              "stopped being checked.", file=sys.stderr)
        return 1
    if offending:
        print("\nFAIL: changed pixels outside the approved regions:", file=sys.stderr)
        for line in offending:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("\nOK: every changed baseline changed only inside the approved regions.")
    print(f"    Largest per-channel difference anywhere OUTSIDE them: {worst_outside}/255 "
          f"(tolerance {args.noise}/255 — re-render antialiasing on the rail's own edge).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
