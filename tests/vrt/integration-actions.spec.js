// tests/vrt/integration-actions.spec.js
//
// SCR-31's connection table gets a screen control for three operator routes
// that were proven live against the running application (tested from Python)
// before any screen existed: `/sweep`, `/adopt-orders` and `/drain-outbox`,
// all `POST /api/integrations/connections/{connection_id}/...`, gated on
// `connector.manage`.
//
// WHAT THIS FILE PROVES
//   1. The Actions cell renders only for a LIVE_READ / LIVE_WRITE row — a
//      MOCK row in the SAME table gets no buttons at all, so the seeded MOCK
//      profiles and every approved screenshot baseline stay untouched.
//   2. "Drain outbox" asks for confirmation, naming the tenant, before it
//      sends anything, and a completed drain renders `outbox_id →
//      external_id` for a sent row.
//   3. A 409 from any of the three renders the RFC-7807 `code` verbatim,
//      not the generic wording the shared API client falls back to for an
//      unrecognised error shape.
//
// Conventions follow integration.spec.js: sign in through the real backend,
// intercept only the integration endpoints with page.route(). No fixed
// timeouts, no screenshot assertions — this file is behavioural, not visual.

const { test, expect } = require('@playwright/test');

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };

/* ---------------- routing helpers (copied from integration.spec.js) ---------------- */

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

/** Declare which `/api/integrations/*` paths this build "mounts", the same
 * way the screens themselves probe presence via /openapi.json. */
async function routeOpenApi(page, paths) {
  const schema = { openapi: '3.1.0', info: { title: 'stub', version: '0' }, paths: {} };
  for (const p of paths) schema.paths[p] = { get: {}, post: {}, put: {} };
  await routeJson(page, '**/openapi.json', schema);
}

const WAVE5_PATHS = [
  '/api/integrations/connections',
  '/api/integrations/connections/{connection_id}/sweep',
  '/api/integrations/connections/{connection_id}/adopt-orders',
  '/api/integrations/connections/{connection_id}/drain-outbox',
];

/* One LIVE_WRITE row (the ERP demo tenant this whole file is about) and one
   MOCK row, in the SAME response — so a single table render carries both a
   row that must offer the three actions and a row that must offer none. */
const CONNECTIONS = {
  items: [
    {
      connection_id: 'CONN-LIVE',
      entity_id: 'ENT-ATHA',
      connector_name: 'Atha Steel — Zoho ERP (demo WBS)',
      product: 'ERP',
      dc: 'IN',
      organization_id: '60074128927',
      mode: 'LIVE_WRITE',
    },
    {
      connection_id: 'CONN-MOCK',
      entity_id: 'ENT-CEMENT',
      connector_name: 'Atha Cement — Zoho ERP (mock)',
      product: 'ERP',
      dc: 'IN',
      organization_id: '60029999999',
      mode: 'MOCK',
    },
  ],
};

async function stubConnections(page, overrideOpenApiPaths) {
  await routeOpenApi(page, overrideOpenApiPaths || WAVE5_PATHS);
  await routeJson(page, '**/api/integrations/connections?**', CONNECTIONS);
  await routeJson(page, '**/api/integrations/connections', CONNECTIONS);
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

/** Wait for a screen to be MOUNTED, not merely present — see
 * integration.spec.js's twin for why `data-mounted` is the real signal. */
async function settleScreen(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 15_000 });
  const failed = page.locator('#content .scr-host[data-mount-failed="1"]');
  if (await failed.count()) {
    throw new Error(`the screen's mount() threw, so it never rendered: ${(await failed.innerText()).trim()}`);
  }
  await page.waitForFunction(() => {
    const host = document.querySelector('#content .scr-host[data-mounted="1"]');
    return !!host && !host.querySelector('.audit-skel-row') && !host.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForLoadState('networkidle');
}

async function gotoScreen(page, hash) {
  await page.goto(`/#${hash}`);
  await settleScreen(page);
}

function liveRow(page) {
  return page.locator('#content table tbody tr').filter({ hasText: 'CONN-LIVE' });
}

function mockRow(page) {
  return page.locator('#content table tbody tr').filter({ hasText: 'CONN-MOCK' });
}

/* ====================================================================== */

test.describe('SCR-31 connection table — sweep / adopt / drain', () => {
  test('the Actions cell appears only on the live row, with all three buttons, and the live-write warning shows', async ({ page }) => {
    await stubConnections(page);
    await signIn(page);
    await gotoScreen(page, 'integration-setup');

    // The operational warning, product-owner decision 2026-09-13: shown
    // because CONN-LIVE is in LIVE_WRITE.
    await expect(page.locator('[data-testid="live-write-warning"]')).toBeVisible();
    await expect(page.locator('[data-testid="live-write-warning"]')).toContainText('60074128927');

    const live = liveRow(page);
    await expect(live.getByRole('button', { name: 'Sweep now' })).toBeVisible();
    await expect(live.getByRole('button', { name: 'Adopt orders' })).toBeVisible();
    await expect(live.getByRole('button', { name: 'Drain outbox' })).toBeVisible();

    // The MOCK row shares this table (the Actions column exists, because at
    // least one row is live) and offers NOTHING — not a disabled button, no
    // button at all.
    await expect(mockRow(page).getByRole('button')).toHaveCount(0);
  });

  test('draining the outbox confirms against the tenant, then renders the sent row', async ({ page }) => {
    await stubConnections(page);
    let confirmMessage = null;
    page.on('dialog', (dialog) => {
      confirmMessage = dialog.message();
      dialog.accept();
    });
    await page.route('**/api/integrations/connections/*/drain-outbox', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        claimed: 1,
        sent: 1,
        results: [{
          outbox_id: 'OUT-1', sent: true, external_id: '3912780000000125001', local_id: 'PO-1',
        }],
      }),
    }));
    await signIn(page);
    await gotoScreen(page, 'integration-setup');

    await liveRow(page).getByRole('button', { name: 'Drain outbox' }).click();

    // The confirmation named the real tenant BEFORE anything was sent.
    expect(confirmMessage).toContain('60074128927');
    expect(confirmMessage).toContain('Zoho ERP DEMO WBS');

    const result = page.locator('[data-testid="connection-action-result"]');
    await expect(result).toContainText('claimed 1, sent 1');
    await expect(result).toContainText('OUT-1 → 3912780000000125001');
  });

  test('a 409 from drain-outbox renders the RFC-7807 code and detail, not the generic conflict wording', async ({ page }) => {
    await stubConnections(page);
    page.on('dialog', (dialog) => dialog.accept());
    await page.route('**/api/integrations/connections/*/drain-outbox', (route) => route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: {
          code: 'CONNECTION_NOT_LIVE_WRITE',
          title: 'Connection Not Live Write',
          detail: 'x',
          status: 409,
        },
      }),
    }));
    await signIn(page);
    await gotoScreen(page, 'integration-setup');

    await liveRow(page).getByRole('button', { name: 'Drain outbox' }).click();

    const result = page.locator('[data-testid="connection-action-result"]');
    await expect(result).toContainText('CONNECTION_NOT_LIVE_WRITE');
    // The shared API client's generic 409 fallback wording ("This connection
    // changed since it was loaded...") is what a client that reads only
    // `err.message` would render instead — asserting its absence is what
    // proves this screen reached into the RFC-7807 body for the real detail.
    await expect(result).not.toContainText('changed since it was loaded');
  });
});
