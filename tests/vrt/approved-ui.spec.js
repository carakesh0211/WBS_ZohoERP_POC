// Visual regression baselines for the client-approved UI.
//
// The client has approved the POC's structure, information density, navigation
// model, colour theme, typography, spacing, tables, status treatment and
// overall enterprise feel. These baselines are captured BEFORE any refactor so
// the Vite/TypeScript migration in later phases has something to prove itself
// against.
//
// tests/test_contracts.py already pins the SHA-256 of styles.css. That catches
// a changed stylesheet. It does NOT catch a JavaScript regression that renders
// the same CSS differently - a dropped column, a lost tabular-nums alignment, a
// status pill that stopped rendering its non-colour indicator. These do.
//
// A pixel diff is a failure, not a warning (maxDiffPixelRatio: 0).

const { test, expect } = require('@playwright/test');

// Administrator holds budget.check, audit.read and connector.read, so a single
// session can reach every screen. Demo credentials are user_id + '!demo',
// provisioned idempotently by auth.provision_dev_identities.
const USER = 'U-ADM';
const PASSWORD = 'U-ADM!demo';

// Every view in app.js's NAV table, in navigation order.
const VIEWS = [
  ['home', 'Executive Dashboard'],
  ['approvals', 'My Approvals'],
  ['alerts', 'Alerts and Exceptions'],
  ['projects', 'CAPEX Projects'],
  ['wbs', 'WBS Explorer'],
  ['budget', 'Budget Planning Grid'],
  ['check', 'Budget Availability Check'],
  ['revisions', 'Budget Revisions'],
  ['prs', 'Purchase Requests'],
  ['pos', 'Commitments PO'],
  ['grns', 'GRN and Receipts'],
  ['bills', 'Vendor Bills and CWIP'],
  ['recon', 'Commitment Reconciliation'],
  ['cap', 'Capitalisation'],
  ['zoho', 'Zoho ERP Connector'],
  ['inventory', 'API Inventory'],
  ['audit', 'Audit Trail'],
];

async function signIn(page) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', USER);
  await page.fill('#loginPass', PASSWORD);
  await page.click('#loginBtn');
  // The shell replaces the auth card once the session is established.
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  // 'attached', not 'visible': below the 900px breakpoint the navigation
  // collapses to an overlay drawer that is display:none until opened
  // (AUD-M-004). The nav items exist in the DOM either way.
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

async function settle(page) {
  // Views render asynchronously from the API. Wait for the loading state to
  // clear rather than for a fixed timeout, so the baseline is deterministic.
  await page.waitForFunction(
    () => {
      const c = document.getElementById('content');
      return c && !c.querySelector('.loading');
    },
    null,
    { timeout: 15_000 },
  );
  await page.waitForLoadState('networkidle');
}

test.describe('approved UI baselines', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  for (const [route, label] of VIEWS) {
    test(`${route} - ${label}`, async ({ page }) => {
      await page.goto(`/#${route}`);
      await settle(page);
      await expect(page).toHaveScreenshot(`${route}.png`, { fullPage: true });
    });
  }
});

test.describe('shell behaviour that the approved design depends on', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test('the document never scrolls horizontally', async ({ page }) => {
    // AUD-M-004. Wide tables scroll inside their own container; the page itself
    // must not. This held at 800px when the audit closed and must keep holding.
    for (const [route] of VIEWS) {
      await page.goto(`/#${route}`);
      await settle(page);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      );
      expect(overflows, `${route} scrolls horizontally`).toBe(false);
    }
  });

  test('the cosy density toggle changes row height and nothing else', async ({ page }) => {
    await page.goto('/#wbs');
    await settle(page);
    const compact = await page.evaluate(
      () => getComputedStyle(document.body).getPropertyValue('--row-h').trim(),
    );
    await page.click('#densityBtn');
    const cosy = await page.evaluate(
      () => getComputedStyle(document.body).getPropertyValue('--row-h').trim(),
    );
    expect(compact).toBe('28px');
    expect(cosy).toBe('36px');
    await expect(page).toHaveScreenshot('wbs-cosy.png', { fullPage: true });
  });

  test('every status carries a non-colour indicator', async ({ page }) => {
    // C3_statuses.json requires each status to be distinguishable without
    // colour. The design does this with a ::before dot plus text, so a status
    // element must never be colour-only.
    await page.goto('/#pos');
    await settle(page);
    const statuses = await page.locator('.status').count();
    expect(statuses).toBeGreaterThan(0);
    const textless = await page.evaluate(
      () => [...document.querySelectorAll('.status')]
        .filter((el) => !el.textContent.trim()).length,
    );
    expect(textless, 'a status rendered with no text label').toBe(0);
  });
});
