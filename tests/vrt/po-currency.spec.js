// tests/vrt/po-currency.spec.js
//
// The Raise Purchase Order screen (Fable 5.1, migration 029): registered in
// src/core/router.js as the SPA hash #purchase-order under Procurement &
// Actuals, gated on po.amend, with no SCR number and no rail row (it is
// reached from the Commitments header). This is the spec the registry guard
// in spa-routing.spec.js requires every routable screen to bring: it holds
// the registry row, the way in from Commitments, and the three behaviours
// the currency selector adds --
//
//   * INR (the default) sends the base payload and NOT ONE currency field;
//   * a JPY order is entered in whole yen (exponent 0), the rate-book lookup
//     answers the ACTIVE rate for the document date, and the request body
//     carries currency / document_date / exchange_rate (the exact decimal
//     string) / rate_source / fx_rate_id with source_amount_minor per line
//     and no amount_paise;
//   * a lookup that refuses shows FX_RATE_UNAVAILABLE verbatim, disables
//     submit and links #fx-rates.
//
// Every API call the screen makes is stubbed: the screen is the subject, not
// the server. Sign-in is real, as in fx-rates.spec.js.
const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const APPROVER = { user: 'U-PROC', password: 'U-PROC!demo' };

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
      active: true, status: 'ACTIVE', activated_at: '2026-08-07T09:00:00+05:30', activated_by: 'U-FIN',
      created_at: '2026-08-07T08:00:00+05:30', created_by: 'U-FIN', version_no: 2,
    },
  ],
  next_cursor: null,
  has_more: false,
};

const JPY_LOOKUP = {
  identity: false, source: 'JPY', target: 'INR', date: '2026-08-07',
  rate: '0.56100000', fx_rate_id: 'FXR-2', rate_source: 'BANK_ADVICE',
  source_minor_exponent: 0, target_minor_exponent: 2,
  row: RATES.items[1],
};

const WBS = {
  tree: [
    {
      wbs_id: 'W-01', wbs_code: 'C-01', description: 'Plant expansion', allow_procurement: false,
      children: [
        { wbs_id: 'W-01-01', wbs_code: 'C-01.01', description: 'Civil works', allow_procurement: true, children: [] },
        { wbs_id: 'W-01-02', wbs_code: 'C-01.02', description: 'Design (no procurement)', allow_procurement: false, children: [] },
      ],
    },
  ],
  totals: { budget: 0, exposure: 0, available: 0, utilisation_pct: 0, band: 'ok' },
};

function fulfillJson(route, status, body) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

/**
 * Stub every API call the screen makes. `lookup` is the rate-book answer:
 * a 200 document, or {status, body} for a refusal.
 * @returns {{posted: Array<Object>}} the bodies POSTed to the PO route.
 */
async function installRoutes(page, { lookup = JPY_LOOKUP } = {}) {
  const posted = [];
  await page.route('**/api/fx/rates**', (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === '/api/fx/rates/lookup') {
      if (lookup && lookup.status) return fulfillJson(route, lookup.status, lookup.body);
      return fulfillJson(route, 200, lookup);
    }
    if (pathname === '/api/fx/rates') return fulfillJson(route, 200, RATES);
    return fulfillJson(route, 404, { detail: `unhandled fx route in test: ${pathname}` });
  });
  await page.route('**/api/projects/*/wbs', (route) => fulfillJson(route, 200, WBS));
  await page.route('**/api/procurement/purchase-orders', (route) => {
    const body = route.request().postDataJSON();
    posted.push(body);
    const foreign = body.currency && body.currency !== 'INR';
    const lines = body.lines.map((l, i) => ({
      po_line_id: `POL-${i + 1}`, line_no: i + 1, wbs_id: l.wbs_id, wbs_code: 'C-01.01',
      budget_head_id: l.budget_head_id, quantity: l.quantity,
      source_amount_minor: foreign ? l.source_amount_minor : null,
      amount_paise: foreign ? 280500 : l.amount_paise,
    }));
    return fulfillJson(route, 201, {
      po_id: 'PO-TEST-1', po_number: body.po_number || 'PO-2026-0007', project_id: body.project_id,
      vendor_name: body.vendor_name, status: 'Draft', version_no: 1,
      currency: body.currency || 'INR',
      exchange_rate: foreign ? body.exchange_rate : '1.00000000',
      rate_source: foreign ? body.rate_source : null,
      fx_rate_id: foreign ? body.fx_rate_id : null,
      fx_rate_date: foreign ? body.document_date : null,
      source_amount_minor: foreign ? lines.reduce((a, l) => a + l.source_amount_minor, 0) : null,
      amount_paise: lines.reduce((a, l) => a + l.amount_paise, 0),
      line_count: lines.length, lines, verdicts: [],
    });
  });
  return { posted };
}

async function signIn(page, who = APPROVER) {
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
  // The form is built after the bootstrap, rate book and WBS reads resolve.
  await page.waitForSelector('#poLine1Amount', { state: 'attached', timeout: 15_000 });
}

async function openScreen(page, opts) {
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(err));
  const routes = await installRoutes(page, opts);
  await signIn(page);
  await page.goto('/#purchase-order');
  await settleScreen(page);
  return { pageErrors, posted: routes.posted };
}

test.describe('Raise Purchase Order — registry, way in, currency at origin', () => {
  test('the registry row: #purchase-order, Procurement & Actuals, po.amend, no SCR number, no rail row', async ({ page }) => {
    const { pageErrors } = await openScreen(page);
    const row = await page.evaluate(async () => {
      const mod = await import('/static/src/core/router.js');
      const s = mod.SCREENS.find((x) => x.id === 'purchase-order');
      return s && { id: s.id, scr: s.scr, group: s.group, need: s.need, label: s.label };
    });
    expect(row).toEqual({ id: 'purchase-order', scr: null, group: 'Procurement & Actuals', need: ['po.amend'], label: 'Raise Purchase Order' });
    const ids = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    expect(ids).not.toContain('purchase-order');
    expect(await page.evaluate(() => viewAllowed('purchase-order'))).toBe(true);
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });

  test('the Commitments header offers "Raise purchase order" to the po.amend holder and it opens the screen', async ({ page }) => {
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err));
    await installRoutes(page);
    await signIn(page);
    await page.goto('/#pos');
    const btn = page.locator('#pageActions button[data-nav="purchase-order"]');
    await expect(btn).toHaveText('Raise purchase order');
    await btn.click();
    await settleScreen(page);
    await expect(page).toHaveURL(/#purchase-order$/);
    await expect(page.locator('#pageTitle')).toHaveText('Raise Purchase Order');
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });

  test('INR is the default and an INR order sends the base payload with no currency field at all', async ({ page }) => {
    const { pageErrors, posted } = await openScreen(page);
    await expect(page.locator('#poCurrency')).toHaveValue('INR');
    await expect(page.locator('#poRatePanel')).toBeHidden();
    await expect(page.locator('#poAmountHeader')).toHaveText('Amount (INR)');

    await page.fill('#poVendor', 'Larsen Civil Works');
    await page.fill('#poLine1Desc', 'Foundation concrete');
    await page.fill('#poLine1Qty', '2');
    await page.fill('#poLine1Amount', '1,000.50');
    await expect(page.locator('#poLines tbody tr[data-line="1"] .po-inr')).toHaveText('₹1,000.50');
    await expect(page.locator('#poTotalInr')).toHaveText('₹1,000.50');

    const projectId = await page.locator('#poProject').inputValue();
    const headId = await page.locator('#poLine1Head').inputValue();
    await expect(page.locator('#poSubmit')).toBeEnabled();
    await page.click('#poSubmit');
    await expect.poll(() => posted.length).toBe(1);
    expect(posted[0]).toEqual({
      project_id: projectId,
      vendor_name: 'Larsen Civil Works',
      lines: [{ wbs_id: 'W-01-01', budget_head_id: headId, quantity: 2, description: 'Foundation concrete', amount_paise: 100050 }],
    });
    for (const key of ['currency', 'currency_code', 'document_date', 'exchange_rate', 'rate_source', 'fx_rate_id']) {
      expect(posted[0]).not.toHaveProperty(key);
    }
    expect(posted[0].lines[0]).not.toHaveProperty('source_amount_minor');
    await expect(page.locator('#poResultHost')).toContainText('PO-2026-0007 raised');
    await expect(page.locator('#poResultHost')).toContainText('₹1,000.50');
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });

  test('JPY: whole yen at exponent 0, the rate for the document date shown with source and entry, and the exact body', async ({ page }) => {
    const { pageErrors, posted } = await openScreen(page);
    // The selector offers INR first, then the rate book's currencies.
    const options = await page.locator('#poCurrency option').allTextContents();
    expect(options).toEqual(['INR', 'JPY', 'USD']);

    await page.fill('#poDate', '2026-08-07');
    await page.selectOption('#poCurrency', 'JPY');
    const panel = page.locator('#poRatePanel');
    await expect(panel).toBeVisible();
    await expect(page.locator('#poRateValue')).toHaveText('0.56100000');
    await expect(page.locator('#poRateSource')).toHaveText('BANK_ADVICE');
    await expect(page.locator('#poRateId')).toHaveText('FXR-2');
    await expect(panel).toContainText('Minor-unit exponent JPY 0');
    await expect(page.locator('#poAmountHeader')).toHaveText('Amount (JPY, whole units)');

    await page.fill('#poVendor', 'Komatsu Kōgyō');
    await page.fill('#poLine1Desc', 'Excavator lease');
    // A yen has no minor unit: a fraction is refused at the field, not rounded.
    await page.fill('#poLine1Amount', '5000.5');
    await page.click('#poSubmit');
    await expect(page.locator('#poLines tbody tr[data-line="1"] .field-err').last()).toContainText('whole number of JPY');
    expect(posted.length).toBe(0);

    await page.fill('#poLine1Amount', '5000');
    // 5000 yen x 0.561 = 2805.00 rupees, translated once and shown to the paisa.
    await expect(page.locator('#poLines tbody tr[data-line="1"] .po-inr')).toHaveText('₹2,805.00');
    await expect(page.locator('#poTotalSource')).toHaveText('JPY 5,000');
    await expect(page.locator('#poTotalInr')).toHaveText('₹2,805.00');

    const projectId = await page.locator('#poProject').inputValue();
    const headId = await page.locator('#poLine1Head').inputValue();
    await expect(page.locator('#poSubmit')).toBeEnabled();
    await page.click('#poSubmit');
    await expect.poll(() => posted.length).toBe(1);
    expect(posted[0]).toEqual({
      project_id: projectId,
      vendor_name: 'Komatsu Kōgyō',
      currency: 'JPY',
      document_date: '2026-08-07',
      exchange_rate: '0.56100000',
      rate_source: 'BANK_ADVICE',
      fx_rate_id: 'FXR-2',
      lines: [{ wbs_id: 'W-01-01', budget_head_id: headId, quantity: 1, description: 'Excavator lease', source_amount_minor: 5000 }],
    });
    expect(typeof posted[0].exchange_rate).toBe('string');
    expect(Number.isInteger(posted[0].lines[0].source_amount_minor)).toBe(true);
    expect(posted[0].lines[0]).not.toHaveProperty('amount_paise');
    await expect(page.locator('#poResultHost')).toContainText('PO-2026-0007 raised');
    await expect(page.locator('#poResultHost')).toContainText('JPY 5,000 translated at 0.56100000');
    await expect(page.locator('#poResultHost')).toContainText('₹2,805.00');
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });

  test('a refused lookup shows FX_RATE_UNAVAILABLE verbatim, disables submit and links Exchange Rates', async ({ page }) => {
    const { pageErrors, posted } = await openScreen(page, {
      lookup: {
        status: 404,
        body: {
          detail: {
            type: 'about:blank', title: 'Fx Rate Unavailable', status: 404,
            code: 'FX_RATE_UNAVAILABLE',
            detail: 'No ACTIVE JPY/INR rate is on file for 2026-08-06. A document in this currency dated that day is REFUSED, not translated at another day\'s rate or at face value. Record the rate for that date and activate it.',
          },
        },
      },
    });
    await page.fill('#poDate', '2026-08-06');
    await page.selectOption('#poCurrency', 'JPY');
    const panel = page.locator('#poRatePanel');
    await expect(panel).toContainText('FX_RATE_UNAVAILABLE');
    await expect(panel).toContainText('No ACTIVE JPY/INR rate is on file for 2026-08-06');
    await expect(panel.locator('a[href="#fx-rates"]')).toHaveText('Exchange Rates');
    await expect(page.locator('#poSubmit')).toBeDisabled();
    await page.fill('#poVendor', 'Komatsu Kōgyō');
    await page.fill('#poLine1Amount', '5000');
    await expect(page.locator('#poSubmit')).toBeDisabled();
    expect(posted.length).toBe(0);
    // No substitute figure: without a rate there is no INR translation to show.
    await expect(page.locator('#poLines tbody tr[data-line="1"] .po-inr')).toHaveText('—');
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });

  test('a server refusal on the write is rendered verbatim in the error host', async ({ page }) => {
    const { pageErrors } = await openScreen(page);
    await page.unroute('**/api/procurement/purchase-orders');
    await page.route('**/api/procurement/purchase-orders', (route) => fulfillJson(route, 409, {
      detail: {
        type: 'about:blank', title: 'Po Exceeds Budget', status: 409, code: 'PO_EXCEEDS_BUDGET',
        detail: 'The order has NOT been created. C-01.01 / Civil: short by 1,000.50.',
      },
    }));
    await page.fill('#poVendor', 'Larsen Civil Works');
    await page.fill('#poLine1Amount', '1000.50');
    await page.click('#poSubmit');
    const host = page.locator('#poErrorHost [role="alert"]');
    await expect(host).toContainText('PO_EXCEEDS_BUDGET');
    await expect(host).toContainText('The order has NOT been created. C-01.01 / Civil: short by 1,000.50.');
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });

  test('axe-core: Raise Purchase Order has no violations, in INR and with a JPY rate shown', async ({ page }) => {
    const { pageErrors } = await openScreen(page);
    const inr = await new AxeBuilder({ page }).analyze();
    expect(inr.violations, JSON.stringify(inr.violations, null, 2)).toEqual([]);
    await page.fill('#poDate', '2026-08-07');
    await page.selectOption('#poCurrency', 'JPY');
    await expect(page.locator('#poRateValue')).toHaveText('0.56100000');
    await page.fill('#poLine1Amount', '5000');
    const jpy = await new AxeBuilder({ page }).analyze();
    expect(jpy.violations, JSON.stringify(jpy.violations, null, 2)).toEqual([]);
    expect(pageErrors, pageErrors.map(String).join('\n')).toEqual([]);
  });
});
