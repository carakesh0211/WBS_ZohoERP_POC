// tests/vrt/avatar-regions.spec.js
//
// WHERE #userAvatar ACTUALLY IS, IN EVERY STATE A BASELINE DEPICTS.
//
// docs/ui-change-2026-09/after-regions.json records ONE avatarRect per
// viewport, measured on the home screen on 2026-09-03. That is what
// evidence/prove-baseline-delta.py treats as the region the approved avatar
// contrast correction was allowed to touch.
//
// It is not the whole truth, and the gap is not theoretical. At tablet-800 the
// shell bar lays the avatar out at x=106.6 on a normally-loaded screen but at
// x=10 once the density toggle has been pressed -- the state `approved-ui`'s
// wbs-cosy.png depicts. So the wbs-cosy baseline's avatar change fell entirely
// outside the only region the prover knew about, and the prover called a
// correctly re-recorded baseline a regression. A tool that cries wolf on the
// approved change is a tool people learn to pass `--noise 999` to.
//
// The fix is to MEASURE the other states rather than widen the box by guess.
// This spec is that measurement, and it is a gate in both directions:
//
//   * by default it ASSERTS the committed evidence/avatar-regions.json still
//     matches where the avatar really renders. If a later change moves the
//     avatar, this fails and names the drift, instead of silently enlarging
//     what a future re-baseline is allowed to touch.
//   * with CAPEX_VRT_WRITE_REGIONS=1 it REWRITES that file. Regenerating the
//     allowance is therefore a deliberate act with a diff, not a side effect.
const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const USER = 'U-ADM';
const PASSWORD = 'U-ADM!demo';
const OUT = path.join(__dirname, 'evidence', 'avatar-regions.json');
const WRITE = process.env.CAPEX_VRT_WRITE_REGIONS === '1';

async function signIn(page) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', USER);
  await page.fill('#loginPass', PASSWORD);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

async function settle(page) {
  await page.waitForFunction(
    () => !document.querySelector('#view .loading, #view [data-state="loading"]'),
    null,
    { timeout: 15_000 },
  ).catch(() => {});
  await page.waitForTimeout(150);
}

/** #userAvatar's box, rounded outward to whole pixels. */
const readRect = () => {
  const el = document.getElementById('userAvatar');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.x, y: r.y, width: r.width, height: r.height };
};

test('the avatar is where the recorded evidence says it is, in every captured state', async ({ page }, testInfo) => {
  const project = testInfo.project.name;

  await signIn(page);
  await page.goto('/#wbs');
  await settle(page);
  const wbsDefault = await page.evaluate(readRect);

  // The density toggle is what wbs-cosy.png depicts, and at tablet-800 it is
  // what moves the avatar.
  await page.click('#densityBtn');
  await settle(page);
  const wbsCosy = await page.evaluate(readRect);

  expect(wbsDefault, '#userAvatar is not rendered on #wbs').not.toBeNull();
  expect(wbsCosy, '#userAvatar is not rendered after the density toggle').not.toBeNull();

  const measured = { wbsDefault, wbsCosy };

  if (WRITE) {
    const all = fs.existsSync(OUT) ? JSON.parse(fs.readFileSync(OUT, 'utf-8')) : {};
    all[project] = measured;
    fs.writeFileSync(OUT, JSON.stringify(all, null, 2) + '\n');
    test.info().annotations.push({ type: 'rewrote', description: OUT });
    return;
  }

  expect(fs.existsSync(OUT),
    `${OUT} is missing. Regenerate it with CAPEX_VRT_WRITE_REGIONS=1.`).toBe(true);
  const committed = JSON.parse(fs.readFileSync(OUT, 'utf-8'))[project];
  expect(committed,
    `evidence/avatar-regions.json has no entry for ${project}`).toBeTruthy();
  expect(measured,
    'the avatar has moved since evidence/avatar-regions.json was captured, so the '
    + 'region a re-baseline is allowed to touch is stale. Re-measure with '
    + 'CAPEX_VRT_WRITE_REGIONS=1 and review the diff.').toEqual(committed);
});
