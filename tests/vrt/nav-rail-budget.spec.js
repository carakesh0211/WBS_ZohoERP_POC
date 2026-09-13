// tests/vrt/nav-rail-budget.spec.js
//
// THE NAVIGATION RAIL'S REMAINING BUDGET, MEASURED RATHER THAN ASSERTED.
//
// Why this file exists
// --------------------
// A previous stream added eight entries to the primary navigation and had them
// withdrawn: they overflowed the rail by 194px at 1440 and 336px at 1024, and
// pushed a client-approved entry below a fold with no scroll cue. The lesson
// recorded was not "do not add nav entries" — it was "MEASURE BEFORE
// PROPOSING", and every stream since has restated the same numbers from that
// stream's report rather than taking them again.
//
// This measures them, from the running application, at the three configured
// viewports, and writes them to the run's output. It asserts only what it can
// honestly assert:
//
//   * the row height, so the cost of ONE more entry is a measurement and not a
//     remembered figure;
//   * that laptop-1024 does not overflow at the rail's DEFAULT render (see
//     the Fable 5.1 Stream F update below);
//   * that the rail's approved sequence is unchanged by Wave 5, which
//     registers twelve routes and no rail entries at all.
//
// It deliberately does NOT pin the absolute content height. That number moves
// whenever an approved label rewraps, and a suite that fails for that reason
// teaches everyone to ignore it — which is how a real overflow ships.
//
// FABLE 5.1 STREAM F UPDATE: the laptop-1024 assertion used to require the
// rail to ALREADY be overflowing — true from before Fable 5.1 through the
// analytics/mapping groups, and recorded as "not this stream's debt" each
// time. Stream F made every NAV group collapsible (Work and Project Control
// still default open; Procurement & Actuals, Closure, Integration and
// Governance now default closed too), which is what the debt actually
// needed: the rail's resting content at laptop-1024 dropped from ~986px to
// ~631px against a 713px rail, so the assertion now requires NO overflow at
// the default render instead. See docs/ui-change-2026-09/F-collapsible-navigation.md.
//
// This file owns no configuration and changes no approved baseline.

const { test, expect } = require('@playwright/test');

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };

/* The twelve routes Wave 5 registers. None of them is a rail entry; this file
   costs out what it WOULD take to make them one. */
const WAVE5_ROUTE_COUNT = 12;

async function signIn(page) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', ADMIN.user);
  await page.fill('#loginPass', ADMIN.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
}

/**
 * Measure the rail the way an overflow is actually experienced: the height of
 * everything inside it against the height it has.
 *
 * `rowHeight` is the MEDIAN outer height of a `.nav-item`, taken from the live
 * elements rather than from the stylesheet, because the margin between two
 * rows is part of what a thirteenth row costs and a CSS `height` is not.
 */
function measureRail(page) {
  return page.evaluate(() => {
    const nav = document.getElementById('nav');
    const cs = getComputedStyle(nav);
    const items = [...nav.querySelectorAll('.nav-item')];
    if (cs.display === 'none') {
      return { hidden: true, rows: items.length, rail: 0, content: 0, overflow: 0, rowHeight: 0 };
    }
    // Fable 5.1 Stream F: a collapsed group's `.nav-group-items` panel is a
    // real direct child of #nav (its id has to exist for aria-controls), just
    // one carrying `hidden`. Left in, it can be the LAST child in DOM order
    // (INTEGRATION MAPPING renders last and defaults collapsed), and a
    // hidden element's bounding rect is all zeros -- which read as negative
    // content height before this filter. `.hidden` is exactly the mechanism
    // renderNav() uses to hide it, so filtering on it is exact, not a guess.
    const kids = [...nav.children].filter((k) => !k.hidden);
    let content = 0;
    if (kids.length) {
      const first = kids[0].getBoundingClientRect().top;
      const last = kids[kids.length - 1].getBoundingClientRect().bottom;
      content = (last - first) + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    }
    // Row-to-row pitch, which is what a further row actually adds.
    const pitches = [];
    for (let i = 1; i < items.length; i += 1) {
      const gap = items[i].getBoundingClientRect().top - items[i - 1].getBoundingClientRect().top;
      if (gap > 0) pitches.push(gap);
    }
    pitches.sort((a, b) => a - b);
    const rowHeight = pitches.length ? pitches[Math.floor(pitches.length / 2)] : 0;
    return {
      hidden: false,
      rows: items.length,
      ids: items.map((b) => b.dataset.nav),
      rail: nav.clientHeight,
      content: Math.round(content),
      overflow: Math.round(content - nav.clientHeight),
      rowHeight: Math.round(rowHeight),
    };
  });
}

for (const viewport of [
  { name: 'desktop-1440', width: 1440, height: 900 },
  { name: 'laptop-1024', width: 1024, height: 768 },
  { name: 'tablet-800', width: 800, height: 1024 },
]) {
  test(`nav rail budget at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await signIn(page);
    const m = await measureRail(page);

    if (m.hidden) {
      // Below 800 the rail collapses to a drawer, so a rail entry costs
      // nothing here and the question does not arise.
      // eslint-disable-next-line no-console
      console.log(`[rail-budget] ${viewport.name}: display:none — the rail is a drawer at this `
        + 'width and has no height budget.');
      expect(m.rows).toBeGreaterThan(0);
      return;
    }

    const twelve = m.content + (WAVE5_ROUTE_COUNT * m.rowHeight) - m.rail;
    const one = m.content + m.rowHeight - m.rail;

    // eslint-disable-next-line no-console
    console.log(
      `[rail-budget] ${viewport.name}: rows=${m.rows} rowPitch=${m.rowHeight}px `
      + `content=${m.content}px rail=${m.rail}px `
      + `${m.overflow > 0 ? `OVERFLOWING by ${m.overflow}px` : `headroom ${-m.overflow}px`} | `
      + `cost of all ${WAVE5_ROUTE_COUNT} Wave 5 rows: ${twelve > 0 ? `${twelve}px OVERFLOW` : `fits, ${-twelve}px spare`} | `
      + `cost of ONE consolidated row: ${one > 0 ? `${one}px OVERFLOW` : `fits, ${-one}px spare`}`,
    );

    // A row has a real, positive pitch. If this ever measured zero the two
    // costs above would both read "fits", which is the failure mode that
    // matters: a budget calculation that silently says yes.
    expect(m.rowHeight).toBeGreaterThan(0);

    // Wave 5 added no rail entry. Whatever the rail holds, none of it is ours.
    expect(m.ids.filter((id) => id.startsWith('integration-'))).toEqual([]);

    if (viewport.name === 'laptop-1024') {
      // THE FACT THAT DECIDED THIS QUESTION CHANGED, and it changed on
      // purpose: this assertion used to require laptop-1024 to already
      // overflow at its resting height (a fact this file measured rather
      // than argued, going back to before Fable 5.1). Fable 5.1 Stream F is
      // what changed it -- Work and Project Control are still open by
      // default, but Procurement & Actuals, Closure, Integration and
      // Governance now default CLOSED like the two later groups already did,
      // and that alone takes the rail's resting content from ~986px to
      // ~631px at this viewport (measured above, in `m.content`). The rail
      // NO LONGER OVERFLOWS at its default render, for any seeded principal
      // wide enough to reach every group (Administrator included) -- which is
      // the property this stream exists to deliver, so the assertion now
      // requires it rather than requiring the old overflow. See
      // docs/ui-change-2026-09/F-collapsible-navigation.md.
      expect(m.overflow, 'laptop-1024 overflows again at its default render — Stream F regressed')
        .toBeLessThanOrEqual(0);
    }

    // And at every width where the rail is visible, twelve more rows do not
    // fit. That is the measured answer to "should these be nav entries".
    expect(twelve, `${WAVE5_ROUTE_COUNT} more rows unexpectedly fit at ${viewport.name}`)
      .toBeGreaterThan(0);
  });
}
