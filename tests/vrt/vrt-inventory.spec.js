// tests/vrt/vrt-inventory.spec.js
//
// THE MANIFEST GATE DOES NOT COVER THIS SUITE.
//
// tools/build_test_manifest.py inventories `TESTS.rglob("test_*.py")` -- every
// Python test function, hashed, so removing one fails the build until the
// removal is recorded in tests/ADAPTATIONS.md. That gate is the reason a
// control test cannot be quietly dropped.
//
// It sees no JavaScript at all. Every Playwright spec in this directory, and
// every committed baseline PNG they assert against, could be deleted today and
// nothing would notice: the suite would simply run fewer tests and report
// green. A visual-regression gate that can be silently emptied is not a gate.
//
// Closing this properly belongs in tools/build_test_manifest.py, which is
// shared. This is the same protection scoped to what this stream owns: the
// inventory of specs and baselines is committed, and this asserts it is still
// whole. A deleted spec, a deleted baseline, or a screen that quietly stopped
// being screenshotted fails here and names what went missing.
//
//   CAPEX_VRT_WRITE_INVENTORY=1 npx playwright test tests/vrt/vrt-inventory.spec.js
//
// rewrites the inventory. Doing so is a deliberate act with a reviewable diff,
// and the reason belongs in tests/ADAPTATIONS.md.
const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const VRT = __dirname;
const OUT = path.join(VRT, 'evidence', 'vrt-inventory.json');
const WRITE = process.env.CAPEX_VRT_WRITE_INVENTORY === '1';

/** Every spec file in tests/vrt, by name. */
function specs() {
  return fs.readdirSync(VRT).filter((f) => f.endsWith('.spec.js')).sort();
}

/** Every committed baseline, grouped by its snapshots directory. */
function baselines() {
  const out = {};
  for (const d of fs.readdirSync(VRT).filter((f) => f.endsWith('-snapshots')).sort()) {
    const dir = path.join(VRT, d);
    if (!fs.statSync(dir).isDirectory()) continue;
    out[d] = fs.readdirSync(dir).filter((f) => f.endsWith('.png')).sort();
  }
  return out;
}

test('the VRT suite still contains every spec and baseline it is recorded as having', async ({}, testInfo) => {
  // A pure filesystem assertion: running it once is running it. The three
  // viewport projects would otherwise repeat it verbatim.
  test.skip(testInfo.project.name !== 'desktop-1440', 'inventory is viewport-independent');

  const measured = { specs: specs(), baselines: baselines() };

  if (WRITE) {
    fs.writeFileSync(OUT, JSON.stringify(measured, null, 2) + '\n');
    return;
  }

  expect(fs.existsSync(OUT),
    `${OUT} is missing. Regenerate it with CAPEX_VRT_WRITE_INVENTORY=1.`).toBe(true);
  const committed = JSON.parse(fs.readFileSync(OUT, 'utf-8'));

  // Named separately so the failure says WHICH spec went, not just "not equal".
  const goneSpecs = committed.specs.filter((s) => !measured.specs.includes(s));
  expect(goneSpecs, 'spec files recorded in the inventory no longer exist').toEqual([]);

  for (const [dir, files] of Object.entries(committed.baselines)) {
    const now = measured.baselines[dir] || [];
    const gone = files.filter((f) => !now.includes(f));
    expect(gone, `baselines missing from ${dir} - a deleted baseline is a screen `
      + 'that stopped being checked').toEqual([]);
  }

  // Additions are reported too: a new spec or baseline that nobody recorded is
  // how an unreviewed screen enters the approved set.
  const newSpecs = measured.specs.filter((s) => !committed.specs.includes(s));
  expect(newSpecs, 'spec files exist that the inventory does not record; '
    + 'regenerate it with CAPEX_VRT_WRITE_INVENTORY=1').toEqual([]);
  for (const [dir, files] of Object.entries(measured.baselines)) {
    const rec = committed.baselines[dir] || [];
    const added = files.filter((f) => !rec.includes(f));
    expect(added, `baselines in ${dir} that the inventory does not record`).toEqual([]);
  }
});
