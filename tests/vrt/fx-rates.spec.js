// tests/vrt/fx-rates.spec.js
//
// The Exchange Rates screen (Fable 5.1, migration 028): registered in
// src/core/router.js as the SPA hash #fx-rates under Governance, gated on
// fx.read, with no SCR number (C8_screens.json is the client's numbered
// inventory and is not extended by an implementation stream). This is the
// spec the registry guard in spa-routing.spec.js requires every routable
// screen to bring: it holds the registry row, the permission gate at the
// rail AND at the route, the rendering of the rate book's lifecycle states
// (AWAITING_ACTIVATION is deliberately not the word "PENDING" -- see
// tests/test_contracts_integration_statuses.py), the coded refusal the
// lookup shows verbatim, and axe-core. Every API call is stubbed: the screen
// is the subject, not the server.
const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };
const AUDITOR = { user: 'U-AUD', password: 'U-AUD!demo' };

const RATES = {
  items: [
    {
      fx_rate_id: 'FXR-1', from_currency: 'USD', to_currency: 'INR', rate_date: '2026-08-05',
      rate: '83.77000000', rate_source: 'RBI_REFERENCE', source_reference: 'bulletin',
      active: true, status: 'ACTIVE', activated_at: '2026-08-05T09:00:00+05:30', activated_by: 'U-FIN',
      created_at: '2026-08-05T08:00:00+05:30', created_by: 'U-ADM', version_no: 2,
    },
    {
      fx_rate_id: 'FXR-2', from_currency: 'JPY', to_currency: 'INR', rate_date: '2026-08-07',
      rate: '0.56100000', rate_source: 'BANK_ADVICE', source_reference: null,
      active: false, status: 'AWAITING_ACTIVATION', activated_at: null, activated_by: null,
      created_at: '2026-08-07T08:00:00+05:30', created_by: 'U-FIN', version_no: 1,
    },
  ],
  next_cursor: null,
  has_more: false,
};

function fulfillJson(route, status, body) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function installFxRouter(page) {
  await page.route('**/api/fx/rates**', (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === '/api/fx/rates/lookup') {
      return fulfillJson(route, 404, {
        detail: {
          code: 'FX_RATE_UNAVAILABLE',
          message: 'no ACTIVE USD/INR rate is on file for 2026-08-06.',
        },
      });
    }
    if (pathname === '/api/fx/rates/history') return fulfillJson(route, 200, { items: [] });
    if (pathname === '/api/fx/rates') return fulfillJson(route, 200, RATES);
    return fulfillJson(route, 404, { detail: `unhandled fx route in test: ${pathname}` });
  });
}

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

async function settleScreen(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 15_000 },
  );
  const failed = page.locator('#content .scr-host[data-mount-failed="1"]');
  if (await failed.count()) {
    throw new Error(`the screen's mount() threw, so it never rendered: ${(await failed.innerText()).trim()}`);
  }
  await page.waitForLoadState('networkidle');
}

test.describe('Exchange Rates — registry, gate, rendering', () => {
  test('the registry row: #fx-rates, Governance, fx.read, no SCR number', async ({ page }) => {
    await installFxRouter(page);
    await signIn(page);
    const row = await page.evaluate(async () => {
      const mod = await import('/static/src/core/router.js');
      const s = mod.SCREENS.find((x) => x.id === 'fx-rates');
      return s && { id: s.id, scr: s.scr, group: s.group, need: s.need, label: s.label };
    });
    expect(row).toEqual({ id: 'fx-rates', scr: null, group: 'Governance', need: ['fx.read'], label: 'Exchange Rates' });
  });

  test('an administrator sees the rail row after Budget Categories and the screen renders without a page error', async ({ page }) => {
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err));
    await installFxRouter(page);
    await signIn(page);
    // Both rows sit in Governance, which Fable 5.1 Stream F now defaults to
    // collapsed for every principal — open it so the rail actually renders them.
    await page.click('[data-nav-group="Governance"]');
    const ids = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    expect(ids.indexOf('fx-rates')).toBe(ids.indexOf('budget-categories') + 1);
    await page.goto('/#fx-rates');
    await settleScreen(page);
    const host = page.locator('#content .scr-host[data-mounted="1"]');
    await expect(host).toContainText('USD');
    await expect(host).toContainText('Active');
    await expect(host).toContainText('Awaiting activation');
    await expect(host).not.toContainText('PENDING');
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });

  test('the Auditor reads the rate book but holds no fx.manage: rail row present, no Record button', async ({ page }) => {
    // 2026-09-11: the product owner decided the Auditor reads the rate book
    // (recorded against D-12 / AUD-C-006). fx.manage stays with Finance and
    // the Administrator, so the create/activate/retire controls are absent.
    await installFxRouter(page);
    await signIn(page, AUDITOR);
    // Governance defaults collapsed (Stream F); open it before reading the rail.
    await page.click('[data-nav-group="Governance"]');
    const ids = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    expect(ids).toContain('fx-rates');
    expect(await page.evaluate(() => viewAllowed('fx-rates'))).toBe(true);
    await page.goto('/#fx-rates');
    await settleScreen(page);
    const host = page.locator('#content .scr-host[data-mounted="1"]');
    await expect(host).toContainText('USD');
    await expect(host.getByRole('button', { name: /new exchange rate|record/i })).toHaveCount(0);
  });

  test('the test lookup shows the coded refusal verbatim, never a substitute rate', async ({ page }) => {
    await installFxRouter(page);
    await signIn(page);
    await page.goto('/#fx-rates');
    await settleScreen(page);
    await page.fill('#fxLookupSource', 'USD');
    await page.fill('#fxLookupDate', '2026-08-06');
    await page.click('#fxLookupSource >> xpath=ancestor::form//button[@type="submit"]');
    const result = page.locator('.fx-lookup-result');
    await expect(result).toContainText('FX_RATE_UNAVAILABLE');
    await expect(result).toContainText('2026-08-06');
  });

  test('axe-core: Exchange Rates has no violations', async ({ page }) => {
    await installFxRouter(page);
    await signIn(page);
    await page.goto('/#fx-rates');
    await settleScreen(page);
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
});
