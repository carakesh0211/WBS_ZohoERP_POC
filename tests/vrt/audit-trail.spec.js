// tests/vrt/audit-trail.spec.js
//
// SCR-28 Audit Trail Viewer — behavioural and accessibility coverage for the
// screen built in app/frontend/src/features/audit/audit-trail.js and its
// host page app/frontend/audit.html.
//
// Every response below is served through Playwright's page.route(). The
// module itself carries no static fake data (see the header comment in
// audit-trail.js) — every row that ever reaches the screen came back from a
// fetch() call — so these tests prove the screen is network-driven rather
// than fixture-driven, and let each of the four required states (loading,
// empty, error, permission) be exercised deterministically without a real
// backend.
//
// Follows the conventions of tests/vrt/approved-ui.spec.js: no fixed
// timeouts; wait for the loading indicator to clear.
//
// Requires devDependencies `@playwright/test` and `@axe-core/playwright`,
// and a playwright.config.js with a webServer that serves this repo's
// app/frontend directory at /static (exactly as app/backend/main.py's
// `app.mount("/static", StaticFiles(directory=FRONTEND))` does) plus
// app/frontend/audit.html reachable at /static/audit.html. This file does
// not own package.json or playwright.config.js and does not declare them.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const VIEWPORTS = [
  { name: 'desktop-1440', width: 1440, height: 900 },
  { name: 'laptop-1024', width: 1024, height: 900 },
  { name: 'tablet-800', width: 800, height: 900 },
];

const STREAMS_FIXTURE = {
  items: [
    { stream_key: 'WBS-PRJ-01', entry_count: 128, head_seq: 128, last_at: '2026-08-20T09:14:22Z' },
    { stream_key: 'PO-PRJ-01', entry_count: 42, head_seq: 42, last_at: '2026-08-18T11:02:05Z' },
  ],
};

function makeEntry(overrides = {}) {
  return {
    audit_id: 1,
    stream_key: 'WBS-PRJ-01',
    seq: 1,
    at: '2026-08-20T09:14:22Z',
    actor: 'U-PFC',
    action: 'Approve',
    object_type: 'budget_revision',
    object_id: 'REV-0007',
    detail: 'Approved revision for WBS-01.02.03 within delegated authority.',
    correlation_id: 'a1b2c3d4-e5f6-4789-9abc-def012345678',
    entry_hash: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85',
    ...overrides,
  };
}

const ENTRIES_FIXTURE = {
  items: [1, 2, 3].map((n) => makeEntry({ audit_id: n, seq: n })),
  next_cursor: null,
  has_more: false,
};

async function routeJson(page, urlPattern, body, status = 200) {
  await page.route(urlPattern, (route) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  }));
}

async function gotoAudit(page) {
  await page.goto('/static/audit.html');
}

/** Wait for the initial fetch cycle to clear rather than for a fixed timeout. */
async function settle(page) {
  await page.waitForFunction(() => {
    const root = document.getElementById('audit-trail-root');
    return !!root && !root.querySelector('.audit-skel-row') && !root.querySelector('.loading');
  });
  await page.waitForLoadState('networkidle');
}

test.describe('SCR-28 Audit Trail Viewer — rendering', () => {
  for (const vp of VIEWPORTS) {
    test(`renders at ${vp.width}px (${vp.name})`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
      await routeJson(page, '**/api/audit/entries**', ENTRIES_FIXTURE);
      await gotoAudit(page);
      await settle(page);

      await expect(page.locator('#audit-trail-root table')).toBeVisible();
      // Each data row has a sibling detail row (hidden until "Details" is
      // pressed) for the expandable correlation-id / full-hash panel, so
      // only the visible rows are counted here.
      await expect(page.locator('#audit-trail-root tbody tr:not([hidden])')).toHaveCount(3);

      // AUD-M-004 in the existing app: the document itself never scrolls
      // horizontally, even on the narrowest supported viewport.
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      );
      expect(overflows, `audit trail viewer scrolls horizontally at ${vp.width}px`).toBe(false);
    });
  }
});

test.describe('SCR-28 Audit Trail Viewer — required states', () => {
  test('loading state renders a table skeleton, never a blank panel', async ({ page }) => {
    let releaseEntries;
    const held = new Promise((resolve) => { releaseEntries = resolve; });
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await page.route('**/api/audit/entries**', async (route) => {
      await held;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ENTRIES_FIXTURE) });
    });

    await gotoAudit(page);
    await expect(page.locator('.audit-skel-row').first()).toBeVisible();
    const panelText = (await page.locator('#audit-trail-root').innerText()).trim();
    expect(panelText.length, 'loading state rendered a blank panel').toBeGreaterThan(0);

    releaseEntries();
    await settle(page);
    await expect(page.locator('.audit-skel-row')).toHaveCount(0);
  });

  test('empty state explains what would appear and why it is empty', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', { items: [] });
    await routeJson(page, '**/api/audit/entries**', { items: [], next_cursor: null, has_more: false });
    await gotoAudit(page);
    await settle(page);

    // Scoped to the shared status host: the (hidden) table also carries its
    // own generic .empty fallback in its own tbody, which is not the one a
    // user actually sees.
    const empty = page.locator('#auditStatusHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/no audit entries/i);
    await expect(empty).toContainText(/appear here/i);
  });

  test('error state shows the server detail, actionable, never a raw traceback', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', {
      type: 'about:blank',
      title: 'Internal Server Error',
      status: 500,
      detail: 'The audit index is temporarily unavailable. Try again in a moment.',
      code: 'AUDIT_INDEX_UNAVAILABLE',
    }, 500);
    await gotoAudit(page);
    await settle(page);

    const banner = page.locator('#audit-trail-root .msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The audit index is temporarily unavailable');
    await expect(banner).not.toContainText(/traceback/i);
    await expect(banner).not.toContainText(/\bat \S+:\d+:\d+/); // no stack-frame-shaped text
    await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('permission state renders a 404 as not-found, never as forbidden', async ({ page }) => {
    // The contract requires an out-of-scope read to come back 404, not 403 —
    // a 403 on an id is an existence oracle. The screen must render this
    // identically to a genuinely missing record: no "forbidden" wording.
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', {
      type: 'about:blank',
      title: 'Not Found',
      status: 404,
      detail: 'No audit trail was found for these filters.',
    }, 404);
    await gotoAudit(page);
    await settle(page);

    // Scoped to the shared status host: the (hidden) table also carries its
    // own generic .empty fallback in its own tbody, which is not the one a
    // user actually sees.
    const empty = page.locator('#auditStatusHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText('No audit trail was found');
    await expect(page.locator('#audit-trail-root')).not.toContainText(/forbidden|access denied|\b403\b/i);
  });
});

test.describe('SCR-28 Audit Trail Viewer — chain verification', () => {
  test('a broken chain renders the exact break sequence, never softened', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', ENTRIES_FIXTURE);
    await routeJson(page, '**/api/audit/chain/verify**', {
      stream_key: 'WBS-PRJ-01', intact: false, entries_checked: 46,
      first_break_seq: 47, verified_at: '2026-08-20T09:20:00Z',
    });
    await gotoAudit(page);
    await settle(page);

    await page.selectOption('#auditStreamSelect', 'WBS-PRJ-01');
    await page.getByRole('button', { name: 'Apply filters' }).click();

    const banner = page.locator('#auditChainArea .msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('sequence 47');
    await expect(banner).toContainText('46');
    // Must never read as merely "pending" — an unverified chain is a defect,
    // not a state waiting to resolve itself.
    await expect(banner).not.toContainText(/\bpending\b/i);
  });

  test('an intact chain states the entries checked, honestly and specifically', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', ENTRIES_FIXTURE);
    await routeJson(page, '**/api/audit/chain/verify**', {
      stream_key: 'WBS-PRJ-01', intact: true, entries_checked: 128,
      first_break_seq: null, verified_at: '2026-08-20T09:20:00Z',
    });
    await gotoAudit(page);
    await settle(page);

    await page.selectOption('#auditStreamSelect', 'WBS-PRJ-01');
    await page.getByRole('button', { name: 'Apply filters' }).click();

    const banner = page.locator('#auditChainArea .msg-success');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('Chain intact');
    await expect(banner).toContainText('128');
  });

  test('no stream selected shows guidance, not a false verification result', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', ENTRIES_FIXTURE);
    await gotoAudit(page);
    await settle(page);

    const hint = page.locator('#auditChainArea .msg-info');
    await expect(hint).toBeVisible();
    await expect(hint).toContainText(/select a single audit stream/i);
    await expect(page.locator('#auditChainArea .msg-success, #auditChainArea .msg-error')).toHaveCount(0);
  });
});

test.describe('SCR-28 Audit Trail Viewer — accessibility', () => {
  test('keyboard-only traversal reaches every control, with visible focus', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', ENTRIES_FIXTURE);
    await routeJson(page, '**/api/audit/chain/verify**', {
      stream_key: 'WBS-PRJ-01', intact: true, entries_checked: 128,
      first_break_seq: null, verified_at: '2026-08-20T09:20:00Z',
    });
    await gotoAudit(page);
    await settle(page);
    // A stream must be selected for "Re-verify chain" to be an active control
    // at all (verifying no stream is a no-op by design) — select one with the
    // mouse first so every control below is genuinely reachable, then do the
    // traversal itself with the keyboard only.
    await page.selectOption('#auditStreamSelect', 'WBS-PRJ-01');
    await page.getByRole('button', { name: 'Apply filters' }).click();
    await page.waitForSelector('#auditChainArea .msg-success');

    const requiredIds = ['auditStreamSelect', 'auditObjectType', 'auditObjectId'];
    const requiredLabels = ['Apply filters', 'Clear filters', 'Table', 'Timeline', 'Re-verify chain', 'Details'];
    const reachedIds = new Set();
    const reachedLabels = new Set();

    await page.evaluate(() => document.body.focus());
    for (let i = 0; i < 60; i += 1) {
      await page.keyboard.press('Tab');
      const info = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el || el === document.body) return null;
        return { id: el.id || null, text: (el.textContent || '').trim().slice(0, 40) };
      });
      if (!info) continue;
      if (info.id) reachedIds.add(info.id);
      if (info.text) reachedLabels.add(info.text);
    }

    for (const id of requiredIds) {
      expect(reachedIds.has(id), `#${id} was never reached by keyboard Tab traversal`).toBe(true);
    }
    for (const label of requiredLabels) {
      expect(reachedLabels.has(label), `control labelled "${label}" was never reached by keyboard Tab traversal`).toBe(true);
    }

    // Visible focus throughout: whatever is currently focused must not have
    // been stripped of an outline.
    const outlineStyle = await page.evaluate(() => {
      const el = document.activeElement;
      return el ? getComputedStyle(el).outlineStyle : null;
    });
    expect(outlineStyle).not.toBe('none');
  });

  test('axe-core: initial view has no violations', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', ENTRIES_FIXTURE);
    await gotoAudit(page);
    await settle(page);

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('axe-core: verified-chain view has no violations', async ({ page }) => {
    await routeJson(page, '**/api/audit/streams', STREAMS_FIXTURE);
    await routeJson(page, '**/api/audit/entries**', ENTRIES_FIXTURE);
    await routeJson(page, '**/api/audit/chain/verify**', {
      stream_key: 'WBS-PRJ-01', intact: true, entries_checked: 128,
      first_break_seq: null, verified_at: '2026-08-20T09:20:00Z',
    });
    await gotoAudit(page);
    await settle(page);
    await page.selectOption('#auditStreamSelect', 'WBS-PRJ-01');
    await page.getByRole('button', { name: 'Apply filters' }).click();
    await page.waitForSelector('#auditChainArea .msg-success');

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
});
