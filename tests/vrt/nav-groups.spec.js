// tests/vrt/nav-groups.spec.js
//
// FABLE 5.1 STREAM F — EVERY NAVIGATION GROUP IS COLLAPSIBLE.
//
// app.js's renderNav() used to special-case two groups (ANALYTICS,
// INTEGRATION MAPPING) as `<button data-nav-group>` toggles and render the six
// client-approved groups as plain `<h2>` headings. Stream F removes that
// distinction: every `{ g: '…' }` marker in NAV now renders as a toggle
// button, Work and Project Control default open, everything else defaults
// closed, the choice is persisted per signed-in user, per tab, in
// sessionStorage under `capex.nav.group.<user_id>.<group>` (the app's
// "session state only" rule, pinned by
// tests/test_frontend_budget_setup_registry.py; the integration of
// 2026-09-13 reversed an earlier localStorage exception so a shared browser
// never carries one tester's rail into another's sign-in), the group holding
// the CURRENT route is always
// forced open (without persisting that forced state), and two rail-top
// controls ("Expand all" / "Collapse all") act on every group at once.
//
// This file is modelled on tests/vrt/spa-routing.spec.js (sign-in / settle
// helpers, axe injection) and tests/vrt/approved-ui.spec.js (viewport comes
// from playwright.config.js's `--project`, not an internal loop — unlike
// nav-rail-budget.spec.js, nothing here needs more than one viewport per
// test run, so it follows the no-internal-loop convention and is run across
// all three projects from the command line instead). It owns no
// configuration and does not touch the byte-frozen app/frontend/styles.css
// baseline; every rule the change needed lives in app/frontend/extensions.css.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };

/* The exact NAV group table (name -> default expanded), so a test that reads
   "everything except Work and Project Control starts collapsed" is checked
   against a value, not a description. Keep this in sync with the `{ g: … }`
   markers in app/frontend/app.js's NAV table. */
const GROUPS = [
  { name: 'Work', defaultExpanded: true },
  { name: 'Project Control', defaultExpanded: true },
  { name: 'Procurement & Actuals', defaultExpanded: false },
  { name: 'Closure', defaultExpanded: false },
  { name: 'Integration', defaultExpanded: false },
  { name: 'Governance', defaultExpanded: false },
  { name: 'ANALYTICS', defaultExpanded: false },
  { name: 'INTEGRATION MAPPING', defaultExpanded: false },
];

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item, #nav .nav-group', { state: 'attached', timeout: 15_000 });
  await settleShell(page);
}

/** Wait for the shell's own loading indicator to clear. */
async function settleShell(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
}

/** Move to a route from an already-running shell, by hash. */
async function hashTo(page, hash) {
  await page.evaluate((h) => { window.location.hash = h; }, hash);
  await settleShell(page);
}

/** Open the navigation drawer if this viewport collapses it (below 900px). */
async function openNavIfCollapsed(page) {
  const collapsed = await page.locator('#nav')
    .evaluate((n) => getComputedStyle(n).display === 'none');
  if (collapsed) await page.locator('#navToggle').click();
  return collapsed;
}

function groupButton(page, name) {
  return page.locator(`#nav [data-nav-group="${name}"]`);
}

/** { name -> aria-expanded boolean } for every group currently in the rail. */
async function readGroupStates(page) {
  return page.evaluate(() => Object.fromEntries(
    [...document.querySelectorAll('#nav [data-nav-group]')]
      .map((b) => [b.dataset.navGroup, b.getAttribute('aria-expanded') === 'true']),
  ));
}

test.describe('nav groups', () => {
  test('defaults after sign-in: Work and Project Control expanded, everything else collapsed', async ({ page }) => {
    await signIn(page);
    await openNavIfCollapsed(page);
    const states = await readGroupStates(page);

    for (const g of GROUPS) {
      expect(states, `"${g.name}" is missing from the rail`).toHaveProperty(g.name);
      expect(states[g.name], `"${g.name}" aria-expanded should default to ${g.defaultExpanded}`)
        .toBe(g.defaultExpanded);
    }

    // The two open-by-default groups show their items; a closed group shows
    // none, because the collapsed panel is genuinely empty markup (`hidden`
    // on `.nav-group-items`, not merely visually hidden).
    await expect(page.locator('#nav .nav-item[data-nav="home"]')).toBeVisible();
    await expect(page.locator('#nav .nav-item[data-nav="projects"]')).toBeVisible();
    await expect(page.locator('#nav .nav-item[data-nav="prs"]')).toHaveCount(0);
    await expect(page.locator('#nav .nav-item[data-nav="cap"]')).toHaveCount(0);
  });

  test('clicking a collapsed group expands it and reveals its items; a reload keeps the choice', async ({ page }) => {
    await signIn(page);
    await openNavIfCollapsed(page);

    const closure = groupButton(page, 'Closure');
    await expect(closure).toHaveAttribute('aria-expanded', 'false');
    await expect(page.locator('#nav .nav-item[data-nav="cap"]')).toHaveCount(0);

    await closure.click();
    await expect(closure).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#nav .nav-item[data-nav="cap"]')).toBeVisible();

    // The choice is a per-user, per-tab preference in sessionStorage: it
    // must survive a full reload of the same tab, and it must never touch
    // localStorage (the app's rule; a shared browser must not carry one
    // tester's rail layout into another's sign-in).
    const stored = await page.evaluate(() => Object.keys(sessionStorage)
      .filter((k) => k.startsWith('capex.nav.group.') && k.endsWith('.Closure')));
    expect(stored.length, 'no sessionStorage key recorded the Closure toggle').toBeGreaterThan(0);
    expect(await page.evaluate(() => sessionStorage.getItem('capex.nav.group.Closure')),
      'the old un-scoped sessionStorage key must not be written any more').toBeNull();
    expect(await page.evaluate(() => Object.keys(localStorage)
      .filter((k) => k.startsWith('capex.nav.group.'))),
      'nav group state must never be written to localStorage').toEqual([]);

    await page.reload();
    await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
    await settleShell(page);
    await openNavIfCollapsed(page);
    await expect(groupButton(page, 'Closure')).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#nav .nav-item[data-nav="cap"]')).toBeVisible();
  });

  test('deep-linking into a collapsed group shows it expanded, with the active item visible, without persisting that', async ({ page }) => {
    await signIn(page);
    // 'zoho' sits in the Integration group, which defaults collapsed.
    await hashTo(page, 'zoho');
    await openNavIfCollapsed(page);

    const integration = groupButton(page, 'Integration');
    await expect(integration).toHaveAttribute('aria-expanded', 'true');
    await expect(integration).toHaveAttribute('aria-current', 'true');
    await expect(integration).toHaveClass(/nav-group-current/);
    const zohoItem = page.locator('#nav .nav-item[data-nav="zoho"]');
    await expect(zohoItem).toBeVisible();
    await expect(zohoItem).toHaveAttribute('aria-current', 'page');

    // Forcing it open for THIS render must not have persisted the choice:
    // navigating away lets Integration fall back to collapsed.
    await hashTo(page, 'home');
    await openNavIfCollapsed(page);
    await expect(groupButton(page, 'Integration')).toHaveAttribute('aria-expanded', 'false');
    const stored = await page.evaluate(() => Object.keys(sessionStorage)
      .filter((k) => k.startsWith('capex.nav.group.') && k.endsWith('.Integration')));
    expect(stored, 'the forced expansion leaked into sessionStorage').toEqual([]);
  });

  test('Expand all opens every group; Collapse all closes every group', async ({ page }) => {
    await signIn(page);
    await openNavIfCollapsed(page);

    await page.locator('#nav [data-nav-expand-all]').click();
    let states = await readGroupStates(page);
    for (const g of GROUPS) expect(states[g.name], `"${g.name}" did not open`).toBe(true);
    await expect(page.locator('#nav .nav-item[data-nav="prs"]')).toBeVisible();
    await expect(page.locator('#nav .nav-item[data-nav="cap"]')).toBeVisible();

    await page.locator('#nav [data-nav-collapse-all]').click();
    states = await readGroupStates(page);
    // Every group closes EXCEPT Work: the active view (`home`, from
    // sign-in) lives there, and the active route must stay discoverable
    // even against an explicit "Collapse all" — the same rule that forces
    // a collapsed group open on a deep link. "Collapse all" still records
    // the closed preference for Work (proven by the previous test's reload
    // check, which applies to any group, Work included); it is only THIS
    // render that keeps it visibly open.
    for (const g of GROUPS) {
      const expectClosed = g.name !== 'Work';
      expect(states[g.name], `"${g.name}" did not ${expectClosed ? 'close' : 'stay open for the active route'}`)
        .toBe(!expectClosed);
    }
    await expect(page.locator('#nav .nav-item[data-nav="home"]')).toBeVisible();
    await expect(page.locator('#nav .nav-item[data-nav="projects"]')).toHaveCount(0);
  });

  test('keyboard: focusing a group button and pressing Enter toggles it', async ({ page }) => {
    await signIn(page);
    await openNavIfCollapsed(page);

    const governance = groupButton(page, 'Governance');
    await expect(governance).toHaveAttribute('aria-expanded', 'false');
    await governance.focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#nav [data-nav-group="Governance"]')).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#nav .nav-item[data-nav="audit"]')).toBeVisible();

    // Space is the other native activation key for a <button>.
    await page.locator('#nav [data-nav-group="Governance"]').focus();
    await page.keyboard.press(' ');
    await expect(page.locator('#nav [data-nav-group="Governance"]')).toHaveAttribute('aria-expanded', 'false');
    await expect(page.locator('#nav .nav-item[data-nav="audit"]')).toHaveCount(0);
  });

  test('a collapsed group header has a visible focus ring', async ({ page }) => {
    await signIn(page);
    await openNavIfCollapsed(page);
    await page.keyboard.press('Tab'); // enter keyboard modality
    const governance = groupButton(page, 'Governance');
    await governance.focus();
    const ring = await governance.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { width: cs.outlineWidth, style: cs.outlineStyle, focusVisible: el.matches(':focus-visible') };
    });
    expect(ring.focusVisible, 'the focused group header does not match :focus-visible').toBe(true);
    expect(ring.style, 'focused group header has no outline style').not.toBe('none');
    expect(parseFloat(ring.width), 'focused group header has a zero-width outline').toBeGreaterThan(0);
  });

  test('the rail causes no horizontal overflow of the document', async ({ page }) => {
    await signIn(page);
    await openNavIfCollapsed(page);
    await page.locator('#nav [data-nav-expand-all]').click();
    await settleShell(page);
    const overflowing = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    );
    expect(overflowing, 'the document scrolls horizontally with every nav group expanded').toBe(false);
  });

  test('axe-core: the rail has no accessibility violations, collapsed and fully expanded', async ({ page }) => {
    await signIn(page);
    await openNavIfCollapsed(page);

    const collapsedResults = await new AxeBuilder({ page }).include('#nav').analyze();
    expect(collapsedResults.violations, JSON.stringify(collapsedResults.violations, null, 2)).toEqual([]);

    await page.locator('#nav [data-nav-expand-all]').click();
    const expandedResults = await new AxeBuilder({ page }).include('#nav').analyze();
    expect(expandedResults.violations, JSON.stringify(expandedResults.violations, null, 2)).toEqual([]);
  });
});
