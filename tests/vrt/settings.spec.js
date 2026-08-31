// tests/vrt/settings.spec.js
//
// Settings and Master Data — SCR-30 Master Data Configuration plus the
// organisation-hierarchy admin screens, built in
// app/frontend/src/features/settings/{org-hierarchy,masters,settings-app}.js
// and their host page app/frontend/settings.html.
//
// Every response below is served through Playwright's page.route() against
// the exact pathname the screen calls, per docs/WAVE2_CONTRACTS.md's
// "API contract — Settings and masters". Neither feature module carries any
// static fake data — every row on screen came back from a fetch() call —
// so these tests prove the screens are network-driven rather than
// fixture-driven, and let each of the four required states (loading, empty,
// error, permission) plus the settings-specific requirements (ZOHO
// read-only, masked identity, 409 conflict) be exercised deterministically
// without a real backend.
//
// Follows the conventions of tests/vrt/audit-trail.spec.js: no fixed
// timeouts; wait for the loading indicator to clear. Requires
// devDependencies `@playwright/test` and `@axe-core/playwright`, and the
// webServer defined in playwright.config.js, which serves this repo's
// app/frontend directory at /static exactly as app/backend/main.py's
// `app.mount("/static", StaticFiles(directory=FRONTEND))` does. This file
// does not own package.json or playwright.config.js and does not declare
// them.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const VIEWPORTS = [
  { name: 'desktop-1440', width: 1440, height: 900 },
  { name: 'laptop-1024', width: 1024, height: 900 },
  { name: 'tablet-800', width: 800, height: 1100 },
];

/* ---------------- fixtures ---------------- */

function makeOrgRow(overrides = {}) {
  return {
    organisation_id: 'ORG-1',
    code: 'ATHA',
    name: 'Atha Group',
    is_active: true,
    created_at: '2026-01-05T10:00:00Z',
    created_by: 'U-ADMIN',
    updated_at: '2026-06-10T09:30:00Z',
    updated_by: 'U-ADMIN',
    version_no: 3,
    ...overrides,
  };
}

function makeItemRow(overrides = {}) {
  return {
    item_id: 'ITM-1',
    code: 'STL-001',
    name: 'Structural Steel Section',
    source: 'LOCAL',
    external_source: null,
    external_id: null,
    external_last_modified: null,
    source_of_truth_status: 'ERP',
    duplicate_of: null,
    mapping_status: 'MAPPED',
    is_active: true,
    version_no: 1,
    created_at: '2026-02-01T08:00:00Z',
    created_by: 'U-PFC',
    updated_at: '2026-02-01T08:00:00Z',
    updated_by: 'U-PFC',
    ...overrides,
  };
}

function makeVendorRow(overrides = {}) {
  return {
    vendor_id: 'VEN-1',
    code: 'VEN-001',
    name: 'Bharat Steel Traders',
    source: 'LOCAL',
    external_source: null,
    external_id: null,
    external_last_modified: null,
    source_of_truth_status: 'ERP',
    duplicate_of: null,
    mapping_status: 'MAPPED',
    is_active: true,
    version_no: 2,
    gst_no: '27ABCDE****1Z5',
    pan_no: 'ABCDE****F',
    gst_treatment: 'Registered Business - Regular',
    place_of_contact: '27-Maharashtra',
    created_at: '2026-02-02T08:00:00Z',
    created_by: 'U-PFC',
    updated_at: '2026-02-02T08:00:00Z',
    updated_by: 'U-PFC',
    ...overrides,
  };
}

/* ---------------- routing helpers ---------------- */

function jsonRoute(status, body) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/** Route an exact pathname (ignoring query string), dispatching by HTTP method. */
async function routeExact(page, pathname, methodHandlers) {
  await page.route((url) => url.pathname === pathname, async (route) => {
    const method = route.request().method();
    const entry = methodHandlers[method];
    if (!entry) {
      await route.fulfill(jsonRoute(405, { detail: `No test handler for ${method} ${pathname}` }));
      return;
    }
    if (typeof entry === 'function') { await entry(route); return; }
    await route.fulfill(jsonRoute(entry.status || 200, entry.body));
  });
}

async function routeDuplicatesEmpty(page, kind) {
  await routeExact(page, `/api/masters/${kind}/duplicates`, {
    GET: { status: 200, body: { items: [] } },
  });
}

async function gotoSettings(page) {
  await page.goto('/static/settings.html');
}

/** Wait for the hierarchy panel's initial fetch cycle to clear. */
async function settleHierarchy(page) {
  await page.waitForFunction(() => {
    const root = document.getElementById('settingsHierarchyPanel');
    return !!root && !root.querySelector('.audit-skel-row') && !root.querySelector('.loading');
  });
  await page.waitForLoadState('networkidle');
}

async function activateMasters(page) {
  await page.click('#settingsTabMasters');
  await page.waitForFunction(() => {
    const root = document.getElementById('settingsMastersPanel');
    return !!root && !root.querySelector('.audit-skel-row') && !root.querySelector('.loading');
  });
  await page.waitForLoadState('networkidle');
}

/* ---------------- rendering ---------------- */

test.describe('Settings and Master Data — rendering', () => {
  for (const vp of VIEWPORTS) {
    test(`renders at ${vp.width}px (${vp.name})`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await routeExact(page, '/api/settings/organisations', {
        GET: { status: 200, body: { items: [makeOrgRow()], next_cursor: null, has_more: false } },
      });
      await gotoSettings(page);
      await settleHierarchy(page);

      await expect(page.locator('#settingsHierarchyPanel table')).toBeVisible();
      await expect(page.locator('#settingsHierarchyPanel tbody tr:not([hidden])')).toHaveCount(1);

      // AUD-M-004 in the existing app: the document itself never scrolls
      // horizontally, even on the narrowest supported viewport.
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      );
      expect(overflows, `settings screen scrolls horizontally at ${vp.width}px`).toBe(false);
    });
  }
});

/* ---------------- required states (organisation hierarchy) ---------------- */

test.describe('Settings — organisation hierarchy — required states', () => {
  test('loading state renders a table skeleton, never a blank panel', async ({ page }) => {
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route((url) => url.pathname === '/api/settings/organisations', async (route) => {
      if (route.request().method() !== 'GET') { await route.fulfill(jsonRoute(405, {})); return; }
      await held;
      await route.fulfill(jsonRoute(200, { items: [makeOrgRow()], next_cursor: null, has_more: false }));
    });

    await gotoSettings(page);
    await expect(page.locator('#settingsHierarchyPanel .audit-skel-row').first()).toBeVisible();
    const panelText = (await page.locator('#settingsHierarchyPanel').innerText()).trim();
    expect(panelText.length, 'loading state rendered a blank panel').toBeGreaterThan(0);

    release();
    await settleHierarchy(page);
    await expect(page.locator('#settingsHierarchyPanel .audit-skel-row')).toHaveCount(0);
  });

  test('empty state explains what would appear and how to create one', async ({ page }) => {
    await routeExact(page, '/api/settings/organisations', {
      GET: { status: 200, body: { items: [], next_cursor: null, has_more: false } },
    });
    await gotoSettings(page);
    await settleHierarchy(page);

    const empty = page.locator('#orgStatusHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/no organisations match/i);
    await expect(empty).toContainText(/new organisation/i);
  });

  test('error state shows the server detail, actionable, never a raw traceback', async ({ page }) => {
    await routeExact(page, '/api/settings/organisations', {
      GET: {
        status: 500,
        body: {
          type: 'about:blank',
          title: 'Internal Server Error',
          status: 500,
          detail: 'The settings index is temporarily unavailable. Try again in a moment.',
          code: 'SETTINGS_INDEX_UNAVAILABLE',
        },
      },
    });
    await gotoSettings(page);
    await settleHierarchy(page);

    const banner = page.locator('#settingsHierarchyPanel .msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The settings index is temporarily unavailable');
    await expect(banner).not.toContainText(/traceback/i);
    await expect(banner).not.toContainText(/\bat \S+:\d+:\d+/);
    await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('permission state renders a 404 as not-found, never as forbidden', async ({ page }) => {
    // The contract requires an out-of-scope read to come back 404 (or 403,
    // which this screen treats identically for a GET) rather than a
    // distinctly worded "forbidden" — a differently worded message would
    // itself be the existence oracle the UI contract forbids.
    await routeExact(page, '/api/settings/organisations', {
      GET: {
        status: 404,
        body: {
          type: 'about:blank', title: 'Not Found', status: 404,
          detail: 'No organisations were found for these filters.',
        },
      },
    });
    await gotoSettings(page);
    await settleHierarchy(page);

    const empty = page.locator('#orgStatusHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText('No organisations were found');
    await expect(page.locator('#settingsHierarchyPanel')).not.toContainText(/forbidden|access denied|\b403\b/i);
  });
});

/* ---------------- ZOHO read-only ---------------- */

test.describe('Settings — SCR-30 Master Data Configuration — ZOHO read-only', () => {
  test('a ZOHO-sourced item is not editable and states why', async ({ page }) => {
    await routeExact(page, '/api/settings/organisations', {
      GET: { status: 200, body: { items: [makeOrgRow()], next_cursor: null, has_more: false } },
    });
    const zohoItem = makeItemRow({
      item_id: 'ITM-9', code: 'ZI-9001', name: 'Zoho-synced Cable Tray', source: 'ZOHO',
      external_source: 'Zoho Inventory', external_id: 'ZI-9981', external_last_modified: '2026-08-20T04:00:00Z',
    });
    await routeExact(page, '/api/masters/items', {
      GET: { status: 200, body: { items: [zohoItem], next_cursor: null, has_more: false } },
    });
    await routeDuplicatesEmpty(page, 'items');

    await gotoSettings(page);
    await settleHierarchy(page);
    await activateMasters(page);

    await expect(page.locator('#settingsMastersPanel .source-badge-group')).toContainText('ZOHO');
    // Honesty indicator: a Zoho-sourced row is never presented as verified.
    await expect(page.locator('#settingsMastersPanel')).toContainText('MOCK · NOT VERIFIED');

    await page.locator('#settingsMastersPanel tbody tr:not([hidden])').first()
      .getByRole('button', { name: 'Edit' }).click();

    const codeInput = page.locator('#settingsField_code');
    const nameInput = page.locator('#settingsField_name');
    await expect(codeInput).toBeVisible();
    await expect(codeInput).toBeDisabled();
    await expect(nameInput).toBeDisabled();

    // The reason must be visible text, not only a title/tooltip.
    const dialog = page.locator('dialog[open]');
    await expect(dialog).toContainText(/synchronised from zoho inventory/i);
    await expect(dialog).toContainText(/ZI-9981/);
  });
});

/* ---------------- masked identifiers ---------------- */

test.describe('Settings — SCR-30 Master Data Configuration — masked tax identity', () => {
  test('gst_no and pan_no render masked, with no reveal control available', async ({ page }) => {
    await routeExact(page, '/api/settings/organisations', {
      GET: { status: 200, body: { items: [makeOrgRow()], next_cursor: null, has_more: false } },
    });
    await routeExact(page, '/api/masters/items', {
      GET: { status: 200, body: { items: [], next_cursor: null, has_more: false } },
    });
    await routeDuplicatesEmpty(page, 'items');
    const vendor = makeVendorRow();
    await routeExact(page, '/api/masters/vendors', {
      GET: { status: 200, body: { items: [vendor], next_cursor: null, has_more: false } },
    });
    await routeDuplicatesEmpty(page, 'vendors');

    await gotoSettings(page);
    await settleHierarchy(page);
    await activateMasters(page);
    await page.getByRole('button', { name: 'Vendors' }).click();
    await page.waitForFunction(() => {
      const root = document.getElementById('settingsMastersPanel');
      return !!root && !root.querySelector('.audit-skel-row') && !root.querySelector('.loading');
    });

    const panelText = await page.locator('#settingsMastersPanel').innerText();
    expect(panelText).toContain('27ABCDE****1Z5');
    expect(panelText).toContain('ABCDE****F');

    // Never an unmasked value anywhere in the DOM: the fixture's own value
    // is already masked, and the DOM must not contain any wider, less-masked
    // rendering of it (e.g. reconstructing extra digits client-side).
    const html = await page.content();
    expect(html).not.toMatch(/27ABCDE\d{4}1Z5/); // a fully-digit-filled GSTIN would mean reconstruction
    expect(html).not.toMatch(/ABCDE\d{4}F/);

    // The reveal control, where offered, must be inert: this screen has no
    // reveal permission signal from the frozen API contract to act on.
    const revealButtons = page.locator('.mask-reveal-btn');
    const count = await revealButtons.count();
    expect(count).toBeGreaterThan(0);
    for (let i = 0; i < count; i += 1) {
      await expect(revealButtons.nth(i)).toBeDisabled();
    }
  });
});

/* ---------------- optimistic concurrency ---------------- */

test.describe('Settings — organisation hierarchy — optimistic concurrency', () => {
  test('a 409 on save renders an actionable conflict message, never a silent overwrite', async ({ page }) => {
    const row = makeOrgRow();
    await routeExact(page, '/api/settings/organisations', {
      GET: { status: 200, body: { items: [row], next_cursor: null, has_more: false } },
    });
    await gotoSettings(page);
    await settleHierarchy(page);

    await page.locator('#settingsHierarchyPanel tbody tr:not([hidden])').first()
      .getByRole('button', { name: 'Edit' }).click();
    const dialog = page.locator('dialog[open]');
    await expect(dialog).toBeVisible();

    await routeExact(page, '/api/settings/organisations/ORG-1', {
      PUT: {
        status: 409,
        body: {
          type: 'about:blank', title: 'Conflict', status: 409,
          detail: 'This organisation was changed by another user after it was loaded. Reload to see the '
            + 'latest version before saving again.',
          code: 'VERSION_CONFLICT',
        },
      },
    });

    await page.locator('#settingsField_name').fill('Atha Group (renamed)');
    await dialog.getByRole('button', { name: 'Save' }).click();

    const conflictBanner = dialog.locator('.msg-warning');
    await expect(conflictBanner).toBeVisible();
    await expect(conflictBanner).toContainText(/someone else changed this record/i);
    await expect(conflictBanner).toContainText(/changed by another user/i);
    await expect(conflictBanner.getByRole('button', { name: 'Reload latest values' })).toBeVisible();

    // Never a silent overwrite: the dialog must still be open, with the
    // user's typed value intact rather than discarded.
    await expect(dialog).toBeVisible();
    await expect(page.locator('#settingsField_name')).toHaveValue('Atha Group (renamed)');
  });
});

/* ---------------- accessibility ---------------- */

test.describe('Settings and Master Data — accessibility', () => {
  test('keyboard-only traversal reaches every control, with visible focus', async ({ page }) => {
    await routeExact(page, '/api/settings/organisations', {
      GET: { status: 200, body: { items: [makeOrgRow()], next_cursor: null, has_more: false } },
    });
    await gotoSettings(page);
    await settleHierarchy(page);

    // settingsTabMasters is deliberately NOT in this list: the two section
    // tabs use the standard WAI-ARIA tabs roving-tabindex pattern (only the
    // selected tab is in the Tab order; ArrowRight/ArrowLeft move between
    // tabs), verified separately below.
    const requiredIds = ['settingsTabHierarchy', 'orgCollectionSelect', 'orgSearchInput', 'orgActiveSelect'];
    const requiredLabels = ['Apply filters', 'Clear filters', 'Edit', 'Deactivate'];
    const reachedIds = new Set();
    const reachedLabels = new Set();
    // Checked at every stop along the traversal (not only the last one an
    // arbitrary tab count happens to land on), so this genuinely proves
    // focus stays visible throughout rather than at one sampled point.
    const noOutlineAt = [];

    await page.evaluate(() => document.body.focus());
    for (let i = 0; i < 60; i += 1) {
      await page.keyboard.press('Tab');
      const info = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el || el === document.body) return null;
        return {
          id: el.id || null,
          text: (el.textContent || '').trim().slice(0, 40),
          outlineStyle: getComputedStyle(el).outlineStyle,
        };
      });
      if (!info) continue;
      if (info.id) reachedIds.add(info.id);
      if (info.text) reachedLabels.add(info.text);
      if (info.outlineStyle === 'none') noOutlineAt.push(info.id || info.text);
    }

    for (const id of requiredIds) {
      expect(reachedIds.has(id), `#${id} was never reached by keyboard Tab traversal`).toBe(true);
    }
    for (const label of requiredLabels) {
      expect(reachedLabels.has(label), `control labelled "${label}" was never reached by keyboard Tab traversal`).toBe(true);
    }
    expect(noOutlineAt, `these controls had no visible focus ring: ${noOutlineAt.join(', ')}`).toEqual([]);

    // Roving tabindex: from the hierarchy tab, ArrowRight moves to and
    // activates the masters tab, and its focus ring is visible too.
    await page.locator('#settingsTabHierarchy').focus();
    await page.keyboard.press('ArrowRight');
    await expect(page.locator('#settingsTabMasters')).toBeFocused();
    await expect(page.locator('#settingsTabMasters')).toHaveAttribute('aria-selected', 'true');
    const masterTabOutline = await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle);
    expect(masterTabOutline).not.toBe('none');
  });

  test('axe-core: organisation hierarchy initial view has no violations', async ({ page }) => {
    await routeExact(page, '/api/settings/organisations', {
      GET: { status: 200, body: { items: [makeOrgRow(), makeOrgRow({ organisation_id: 'ORG-2', code: 'ATHA-2', name: 'Atha Infra', is_active: false })], next_cursor: null, has_more: false } },
    });
    await gotoSettings(page);
    await settleHierarchy(page);

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('axe-core: SCR-30 masters view has no violations', async ({ page }) => {
    await routeExact(page, '/api/settings/organisations', {
      GET: { status: 200, body: { items: [makeOrgRow()], next_cursor: null, has_more: false } },
    });
    await routeExact(page, '/api/masters/items', {
      GET: { status: 200, body: { items: [makeItemRow(), makeItemRow({ item_id: 'ITM-9', code: 'ZI-9001', source: 'ZOHO', external_source: 'Zoho Inventory', external_id: 'ZI-9981', external_last_modified: '2026-08-20T04:00:00Z' })], next_cursor: null, has_more: false } },
    });
    await routeDuplicatesEmpty(page, 'items');

    await gotoSettings(page);
    await settleHierarchy(page);
    await activateMasters(page);

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
});
