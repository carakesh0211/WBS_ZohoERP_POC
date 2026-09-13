/* tests/vrt/evidence/capture-approved-change.js
   Before/after evidence for the TWO APPROVED UI CHANGES (Wave 4, stream 4).

   This is deliberately NOT a *.spec.js file, so `npx playwright test` does not
   collect it. It is a capture tool, not an assertion: the assertions live in
   approved-ui.spec.js and spa-routing.spec.js, which are the things that must
   stay green. An evidence script that ran inside the suite would either add a
   test that can never fail, or a skip that hides one.

   Usage (with the application already listening):

     CAPEX_EVIDENCE_LABEL=before CAPEX_EVIDENCE_PORT=8799 \
       node tests/vrt/evidence/capture-approved-change.js

   Writes to docs/ui-change-2026-09/<label>-*.png at the same three viewports
   the visual-regression baselines use.
*/

const fs = require('fs');
const path = require('path');
const { chromium } = require('@playwright/test');

const LABEL = process.env.CAPEX_EVIDENCE_LABEL || 'before';
const PORT = process.env.CAPEX_EVIDENCE_PORT || 8799;
const BASE = `http://127.0.0.1:${PORT}`;
const OUT = path.join(process.cwd(), 'docs', 'ui-change-2026-09');

const USER = 'U-ADM';
const PASSWORD = 'U-ADM!demo';

// The same three widths as playwright.config.js. 800px matters most here: the
// navigation rail is display:none below 900px, so a change to the rail must
// NOT move that viewport, and a change to the shell bar must.
const VIEWPORTS = [
  ['desktop-1440', 1440, 900],
  ['laptop-1024', 1024, 768],
  ['tablet-800', 800, 1024],
];

async function signIn(page) {
  await page.goto(`${BASE}/`);
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', USER);
  await page.fill('#loginPass', PASSWORD);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15000 });
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15000 });
  await page.waitForLoadState('networkidle');
}

/** The measured contrast of #userAvatar's own text on its own background. */
async function measureAvatarContrast(page) {
  return page.evaluate(() => {
    const el = document.getElementById('userAvatar');
    if (!el) return null;
    const cs = getComputedStyle(el);

    const parse = (value) => {
      const m = String(value).match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const parts = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
      return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
    };

    // Walk up for the first opaque background, because a transparent avatar
    // would otherwise report a contrast against nothing.
    let bg = parse(cs.backgroundColor);
    let node = el;
    while (bg && bg.a === 0 && node.parentElement) {
      node = node.parentElement;
      bg = parse(getComputedStyle(node).backgroundColor);
    }
    const fg = parse(cs.color);
    if (!fg || !bg) return null;

    const lin = (c) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    const L = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
    const l1 = L(fg);
    const l2 = L(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);

    const toHex = (c) => '#' + [c.r, c.g, c.b].map((v) => v.toString(16).padStart(2, '0')).join('').toUpperCase();
    return {
      colour: toHex(fg),
      background: toHex(bg),
      fontSize: cs.fontSize,
      fontWeight: cs.fontWeight,
      ratio: Math.round(ratio * 10000) / 10000,
    };
  });
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const report = { label: LABEL, capturedAt: new Date().toISOString(), viewports: {} };

  for (const [name, width, height] of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width, height },
      colorScheme: 'light',
      timezoneId: 'Asia/Kolkata',
      locale: 'en-IN',
    });
    const page = await context.newPage();
    await signIn(page);

    await page.screenshot({
      path: path.join(OUT, `${LABEL}-home-${name}.png`),
      fullPage: true,
      animations: 'disabled',
      caret: 'hide',
    });

    // The shell bar on its own, so the avatar change is readable without
    // hunting for 26 pixels inside a full-page capture.
    await page.locator('.shellbar').screenshot({
      path: path.join(OUT, `${LABEL}-shellbar-${name}.png`),
      animations: 'disabled',
    });

    // The navigation rail on its own. Below 900px it is display:none, so this
    // is skipped there — which is itself the point being evidenced.
    const railVisible = await page.locator('#nav').evaluate((n) => getComputedStyle(n).display !== 'none');
    if (railVisible) {
      // Fable 5.1 Stream F made every NAV group collapsible, and only Work
      // and Project Control default open — so `#nav .nav-item` by itself now
      // finds a fraction of the rail's real contents (a collapsed group
      // renders no `.nav-item` at all; see app.js's renderNav()). The specs
      // that need a full rail inventory (tests/vrt/spa-routing.spec.js,
      // tests/vrt/integration.spec.js) all open every relevant group first —
      // "Expand all" (`[data-nav-expand-all]`) is the same control, applied
      // to all of them at once, so this evidence capture stays a complete
      // inventory of the navigation rather than a snapshot of whatever
      // happened to default open. `.count()` guards a capture taken against
      // a build that predates Stream F, where the button does not exist.
      const expandAll = page.locator('#nav [data-nav-expand-all]');
      if (await expandAll.count()) await expandAll.click();

      await page.locator('#nav').screenshot({
        path: path.join(OUT, `${LABEL}-nav-${name}.png`),
        animations: 'disabled',
      });
    }

    const navIds = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );

    // The two regions the approved change is allowed to move, MEASURED from
    // the running application rather than guessed. prove-baseline-delta.py
    // reads these and fails any baseline whose changed pixels fall outside
    // them, which is what stops a re-baseline from hiding a real regression.
    const rects = await page.evaluate(() => {
      const rect = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      };
      return { avatarRect: rect('#userAvatar'), navRect: rect('#nav') };
    });

    report.viewports[name] = {
      navRailRendered: railVisible,
      navItemCount: navIds.length,
      navItems: navIds,
      avatar: await measureAvatarContrast(page),
      avatarRect: rects.avatarRect,
      navRect: rects.navRect,
    };

    await context.close();
  }

  await browser.close();
  fs.writeFileSync(path.join(OUT, `${LABEL}-report.json`), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  // prove-baseline-delta.py reads the regions file by a fixed name.
  fs.writeFileSync(path.join(OUT, `${LABEL}-regions.json`), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
})().catch((e) => {
  process.stderr.write(`${e && e.stack ? e.stack : e}\n`);
  process.exit(1);
});
