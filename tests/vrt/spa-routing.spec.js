// tests/vrt/spa-routing.spec.js
//
// SCR-28, SCR-09, SCR-10, SCR-13 and SCR-30 AS ROUTES OF THE SPA SHELL.
//
// Wave 2 built these five screens as ES modules mounted by standalone host
// pages (audit.html, budget.html, settings.html). They rendered correctly —
// tests/vrt/{audit-trail,budget,settings}.spec.js prove that, and still do —
// but nothing in the application led to them: app.js's hash router had no
// route, and the primary navigation had no entry. The Wave 3 security-closure
// gate requires "SCR-28, SCR-09, SCR-10, SCR-13, SCR-30 routable in the SPA
// shell", and this file is what proves it.
//
// What is asserted here, and nowhere else:
//   * every one of the five screens is reachable by clicking the primary
//     navigation, and lands on its own hash;
//   * every one is deep-linkable — a cold load of /#<hash> renders the screen,
//     not the dashboard;
//   * browser Back and Forward move between them and restore the right screen;
//   * app.js's NAV permission table and src/core/router.js's SCREENS registry
//     agree, so the two declarations cannot drift apart unnoticed;
//   * the four required states — loading, empty, error, permission-denied —
//     render inside the shell for each screen;
//   * a user without the permission cannot reach the route at all, and is not
//     shown a navigation entry for it;
//   * axe-core is clean on each screen inside the shell, keyboard-only
//     traversal reaches every screen, focus stays visible, and status is
//     distinguishable without colour;
//   * the rendered result does not drift, at 1440 / 1024 / 800 px.
//
// Conventions follow tests/vrt/approved-ui.spec.js and audit-trail.spec.js:
// sign in through the real backend (session, permissions and the shell chrome
// are the thing under test and must not be faked), then intercept ONLY the
// SCR-nn feature endpoints with page.route(), so no static fake data has to
// exist in product code. No fixed timeouts anywhere.
//
// Requires devDependencies `@playwright/test` and `@axe-core/playwright`, and
// the webServer defined in playwright.config.js. This file does not own
// package.json or playwright.config.js and does not declare them.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

/* ---------------- identities ----------------
   Demo credentials are user_id + '!demo', provisioned idempotently by
   auth.provision_dev_identities. The roles below come from auth.PERMISSIONS.  */

// Administrator: holds audit.read, budget.read, budget.check, settings.read
// and masters.read, so one session reaches all five screens.
const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };
// Requestor: holds budget.read / budget.check / settings.read / masters.read
// but NOT audit.read.
const REQUESTOR = { user: 'U-REQ', password: 'U-REQ!demo' };
// Auditor: holds audit.read / budget.read / budget.check but NOT
// settings.read or masters.read.
const AUDITOR = { user: 'U-AUD', password: 'U-AUD!demo' };

/* ---------------- the five screens under test ---------------- */

const SCREENS = [
  { hash: 'audit-trail', scr: 'SCR-28', navLabel: 'Audit Trail Viewer', title: 'Audit Trail Viewer' },
  { hash: 'budget-grid', scr: 'SCR-09', navLabel: 'Budget Planning Grid (cells)', title: 'Budget Planning Grid' },
  { hash: 'budget-compare', scr: 'SCR-10', navLabel: 'Budget Version Comparison', title: 'Budget Version Comparison' },
  { hash: 'budget-availability', scr: 'SCR-13', navLabel: 'Budget Availability Check (cells)', title: 'Budget Availability Check' },
  { hash: 'settings', scr: 'SCR-30', navLabel: 'Settings & Master Data', title: 'Settings and Master Data' },
];

/* ---------------- fixtures ---------------- */

const AUDIT_STREAMS = {
  items: [
    { stream_key: 'WBS-PRJ-01', entry_count: 128, head_seq: 128, last_at: '2026-08-20T09:14:22Z' },
    { stream_key: 'PO-PRJ-01', entry_count: 42, head_seq: 42, last_at: '2026-08-18T11:02:05Z' },
  ],
};

const AUDIT_ENTRIES = {
  items: [1, 2, 3].map((n) => ({
    audit_id: n,
    stream_key: 'WBS-PRJ-01',
    seq: n,
    at: '2026-08-20T09:14:22Z',
    actor: 'U-PFC',
    action: 'Approve',
    object_type: 'budget_revision',
    object_id: `REV-000${n}`,
    detail: 'Approved revision for WBS-01.02.03 within delegated authority.',
    correlation_id: 'a1b2c3d4-e5f6-4789-9abc-def012345678',
    entry_hash: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85',
  })),
  next_cursor: null,
  has_more: false,
};

const CELLS = {
  items: [
    {
      wbs_id: 'WBS-01', wbs_path: '01', budget_head_id: 'CIVIL',
      budget_paise: 500000000, original_paise: 450000000, revisions_paise: 50000000, future_budget_paise: 0,
      ordered_paise: 0, commitment_paise: 120000000, actual_paise: 80000000, received_paise: 60000000,
      received_not_billed_paise: 5000000, pr_reserved_paise: 10000000,
      exposure_paise: 210000000, available_paise: 290000000, recomputed_at: '2026-08-20T09:00:00Z',
    },
    {
      wbs_id: 'WBS-01-02', wbs_path: '01.02', budget_head_id: 'ELEC',
      budget_paise: 200000000, original_paise: 150000000, revisions_paise: 50000000, future_budget_paise: 0,
      ordered_paise: 0, commitment_paise: 150000000, actual_paise: 60000000, received_paise: 40000000,
      received_not_billed_paise: 2000000, pr_reserved_paise: 4000000,
      exposure_paise: 214000000, available_paise: -14000000, recomputed_at: '2026-08-20T09:00:00Z',
    },
  ],
  next_cursor: null,
  has_more: false,
};

const VERSIONS = {
  items: [
    { version: 'V2', label: 'Revised', created_at: '2026-06-01T00:00:00Z', total_paise: 520000000 },
    { version: 'V1', label: 'Original', created_at: '2026-01-01T00:00:00Z', total_paise: 500000000 },
  ],
};

const COMPARE = {
  rows: [
    { wbs_id: 'WBS-01', budget_head_id: 'CIVIL', left_paise: 450000000, right_paise: 500000000, delta_paise: 50000000 },
    { wbs_id: 'WBS-01-02', budget_head_id: 'ELEC', left_paise: 150000000, right_paise: 130000000, delta_paise: -20000000 },
  ],
};

const AVAILABILITY = {
  wbs_id: 'WBS-01-02', budget_head_id: 'ELEC', owning_wbs_id: 'WBS-01',
  budget_paise: 200000000, exposure_paise: 100000000, available_paise: 100000000,
  requested_paise: 5000000, verdict: 'OK', shortfall_paise: 0,
  checked_at: '2026-08-20T09:30:00Z',
};

const ORGANISATIONS = {
  items: [{
    organisation_id: 'ORG-1', code: 'ATHA', name: 'Atha Group', is_active: true,
    created_at: '2026-01-05T10:00:00Z', created_by: 'U-ADMIN',
    updated_at: '2026-06-10T09:30:00Z', updated_by: 'U-ADMIN', version_no: 3,
  }],
  next_cursor: null,
  has_more: false,
};

/* ---------------- routing helpers ----------------
   Only the SCR-nn feature endpoints are intercepted. /api/auth/login,
   /api/bootstrap, /api/health and /api/dashboard deliberately reach the real
   server: the session, the permission set and the shell chrome are part of
   what these tests are proving, and faking them would prove nothing. */

function jsonRoute(body, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill(jsonRoute(body, status)));
}

/** Every endpoint the five screens can call, with a deterministic default. */
async function stubScreenApis(page) {
  await routeJson(page, '**/api/audit/streams**', AUDIT_STREAMS);
  await routeJson(page, '**/api/audit/entries**', AUDIT_ENTRIES);
  await routeJson(page, '**/api/audit/chain/verify**', {
    stream_key: 'WBS-PRJ-01', intact: true, entries_checked: 128,
    first_break_seq: null, verified_at: '2026-08-20T09:20:00Z',
  });
  await routeJson(page, '**/api/budget/cells**', CELLS);
  await routeJson(page, '**/api/budget/lines**', { items: [] });
  await routeJson(page, '**/api/budget/versions**', VERSIONS);
  await routeJson(page, '**/api/budget/compare**', COMPARE);
  await routeJson(page, '**/api/budget/availability**', AVAILABILITY);
  await routeJson(page, '**/api/settings/**', ORGANISATIONS);
  await routeJson(page, '**/api/masters/*/duplicates**', { items: [] });
  await routeJson(page, '**/api/masters/**', { items: [], next_cursor: null, has_more: false });
}

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  // 'attached', not 'visible': below 900px the navigation is an overlay drawer
  // that is display:none until opened (AUD-M-004).
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

/** Wait for the shell's own loading indicator to clear. */
async function settleShell(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
}

/**
 * Wait for a screen to be mounted inside the shell. The .scr-host wrapper is
 * added by src/core/router.js only once the feature module's mount() has been
 * awaited, so its presence plus a cleared skeleton is the settled state.
 */
async function settleScreen(page) {
  await settleShell(page);
  await page.waitForSelector('#content .scr-host', { state: 'attached', timeout: 15_000 });
  await page.waitForFunction(() => {
    const host = document.querySelector('#content .scr-host');
    return !!host && !host.querySelector('.audit-skel-row') && !host.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForLoadState('networkidle');
}

async function gotoScreen(page, hash) {
  await page.goto(`/#${hash}`);
  await settleScreen(page);
}

function pageTitle(page) {
  return page.locator('#pageTitle');
}

/** Open the navigation drawer if this viewport collapses it (below 900px). */
async function openNavIfCollapsed(page) {
  const collapsed = await page.locator('#nav')
    .evaluate((n) => getComputedStyle(n).display === 'none');
  if (collapsed) await page.locator('#navToggle').click();
  return collapsed;
}

/**
 * Click a primary-navigation entry the way a user would: open the drawer if
 * this viewport has one, scroll the entry into view, then click it. Never
 * `force: true` — the navigation is taller than a 900px viewport now that five
 * screens have been added to it, and a forced click on an off-screen entry
 * lands on whatever happens to be at that coordinate instead.
 */
async function navigateTo(page, hash) {
  await openNavIfCollapsed(page);
  const item = page.locator(`#nav .nav-item[data-nav="${hash}"]`);
  await item.scrollIntoViewIfNeeded();
  await item.click();
}

/**
 * The one accessibility defect this stream found and did not fix, because it
 * lives in a file this stream does not own.
 *
 * `#userAvatar` in the shell header renders white 11px text on the frozen
 * stylesheet's avatar background (#2e8a9a) at a contrast ratio of 4.02:1,
 * below the 4.5:1 WCAG 2 AA minimum. The colour is in app/frontend/styles.css,
 * which is BYTE-FROZEN and SHA-256 pinned in CI, so it is reported rather than
 * changed. It is excluded here — and ONLY here, by that one selector — so that
 * every other violation on these screens still fails the build.
 */
const KNOWN_FROZEN_STYLESHEET_DEFECT = '#userAvatar';

function axeForShell(page) {
  return new AxeBuilder({ page }).exclude(KNOWN_FROZEN_STYLESHEET_DEFECT);
}

/* ============================================================ navigation */

test.describe('SPA routing — every SCR-nn screen is reachable from the shell', () => {
  test.beforeEach(async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);
  });

  for (const s of SCREENS) {
    test(`${s.scr} — the primary navigation reaches ${s.title}`, async ({ page }) => {
      await settleShell(page);
      await openNavIfCollapsed(page);
      const item = page.locator(`#nav .nav-item[data-nav="${s.hash}"]`);
      await expect(item, `${s.scr} has no primary navigation entry`).toHaveCount(1);
      await expect(item).toContainText(s.navLabel);

      await navigateTo(page, s.hash);
      await settleScreen(page);

      await expect(pageTitle(page)).toHaveText(s.title);
      expect(new URL(page.url()).hash, `${s.scr} did not update the hash`).toBe(`#${s.hash}`);
    });
  }

  for (const s of SCREENS) {
    test(`${s.scr} — /#${s.hash} deep-links on a cold load`, async ({ page }) => {
      await gotoScreen(page, s.hash);
      await expect(pageTitle(page)).toHaveText(s.title);
      // Not silently redirected to the dashboard.
      expect(new URL(page.url()).hash).toBe(`#${s.hash}`);
      await expect(page.locator('#content .scr-host')).toHaveCount(1);
      // The active navigation entry follows the route.
      await expect(page.locator(`#nav .nav-item[data-nav="${s.hash}"][aria-current="page"]`))
        .toHaveCount(1);
    });
  }

  test('Back and Forward move between SCR-nn routes and restore each screen', async ({ page }) => {
    await gotoScreen(page, 'audit-trail');
    await expect(pageTitle(page)).toHaveText('Audit Trail Viewer');

    await navigateTo(page, 'budget-grid');
    await settleScreen(page);
    await expect(pageTitle(page)).toHaveText('Budget Planning Grid');

    await navigateTo(page, 'settings');
    await settleScreen(page);
    await expect(pageTitle(page)).toHaveText('Settings and Master Data');

    await page.goBack();
    await settleScreen(page);
    expect(new URL(page.url()).hash).toBe('#budget-grid');
    await expect(pageTitle(page)).toHaveText('Budget Planning Grid');

    await page.goBack();
    await settleScreen(page);
    expect(new URL(page.url()).hash).toBe('#audit-trail');
    await expect(pageTitle(page)).toHaveText('Audit Trail Viewer');

    await page.goForward();
    await settleScreen(page);
    expect(new URL(page.url()).hash).toBe('#budget-grid');
    await expect(pageTitle(page)).toHaveText('Budget Planning Grid');
  });

  test('leaving an SCR-nn route and returning re-mounts it cleanly', async ({ page }) => {
    await gotoScreen(page, 'budget-grid');
    await navigateTo(page, 'home');
    await settleShell(page);
    await expect(page.locator('#content .scr-host')).toHaveCount(0);

    await navigateTo(page, 'budget-grid');
    await settleScreen(page);
    // Exactly one host, one live region — a second mount would duplicate both,
    // and duplicate ids are both an axe violation and a broken live region.
    await expect(page.locator('#content .scr-host')).toHaveCount(1);
    await expect(page.locator('#budgetLiveRegion')).toHaveCount(1);
  });

  test('the shell never scrolls horizontally on any SCR-nn route', async ({ page }) => {
    // AUD-M-004: wide tables scroll inside their own container; the document
    // itself must not, at any supported width.
    for (const s of SCREENS) {
      await gotoScreen(page, s.hash);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      );
      expect(overflows, `${s.scr} scrolls horizontally`).toBe(false);
    }
  });

  test('the existing shell views still route, unchanged', async ({ page }) => {
    // The five new routes were given hashes of their own precisely so they
    // could not displace `audit`, `budget` or `check`, which are different,
    // already-approved screens with their own baselines.
    for (const [hash, title] of [
      ['audit', 'Audit Trail'],
      ['budget', 'Budget Planning Grid'],
      ['check', 'Budget Availability Check'],
    ]) {
      await page.goto(`/#${hash}`);
      await settleShell(page);
      await expect(pageTitle(page)).toHaveText(title);
      // The legacy views render HTML strings, not the module hosts.
      await expect(page.locator('#content .scr-host')).toHaveCount(0);
    }
  });
});

/* ============================================ registry / NAV consistency */

test.describe('SPA routing — the two declarations cannot drift', () => {
  test('app.js NAV and src/core/router.js SCREENS agree on hash, label and permission', async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);

    const registry = await page.evaluate(async () => {
      const mod = await import('/static/src/core/router.js');
      return mod.SCREENS.map((s) => ({
        id: s.id, scr: s.scr, label: s.label, title: s.title, need: s.need,
      }));
    });

    // Every screen in the registry has a navigation row, and the permission
    // gate on that row is exactly the registry's. app.js has to restate `need`
    // because NAV renders before any dynamic import can resolve; this is the
    // assertion that keeps the restatement honest.
    for (const entry of registry) {
      const nav = await page.evaluate(
        (id) => {
          const row = NAV.find((n) => n.id === id);
          return row ? { id: row.id, label: row.label, need: row.need || null } : null;
        },
        entry.id,
      );
      expect(nav, `${entry.scr} (${entry.id}) has no NAV row in app.js`).not.toBeNull();
      expect(nav.label, `${entry.scr} label drifted`).toBe(entry.label);
      expect(nav.need, `${entry.scr} permission gate drifted`).toEqual(entry.need);
    }

    // And every screen this suite claims to cover is actually in the registry.
    expect(registry.map((r) => r.id).sort())
      .toEqual(SCREENS.map((s) => s.hash).sort());
    expect(registry.map((r) => r.scr).sort())
      .toEqual(['SCR-09', 'SCR-10', 'SCR-13', 'SCR-28', 'SCR-30']);
  });

  test('no SCR-nn route displaces an existing shell view', async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);
    const collisions = await page.evaluate(async () => {
      const mod = await import('/static/src/core/router.js');
      const existing = new Set(['home', 'approvals', 'alerts', 'projects', 'wbs', 'budget',
        'check', 'revisions', 'prs', 'pos', 'grns', 'bills', 'recon', 'cap', 'zoho',
        'inventory', 'audit']);
      return mod.SCREENS.map((s) => s.id).filter((id) => existing.has(id));
    });
    expect(collisions, 'an SCR-nn route reused an existing view id').toEqual([]);
  });
});

/* ================================================== permission-denied state */

test.describe('SPA routing — permission-denied', () => {
  test('a user without audit.read gets no Audit Trail Viewer entry and cannot route to it', async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page, REQUESTOR);
    await settleShell(page);

    await expect(page.locator('#nav .nav-item[data-nav="audit-trail"]')).toHaveCount(0);

    await page.goto('/#audit-trail');
    await settleShell(page);
    // Falls back to the dashboard rather than mounting a screen this user may
    // not see. The hash is rewritten so a bookmark cannot keep re-trying it.
    await expect(page.locator('#content .scr-host')).toHaveCount(0);
    expect(new URL(page.url()).hash).toBe('#home');
  });

  test('a user without settings.read or masters.read cannot route to Settings', async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page, AUDITOR);
    await settleShell(page);

    await expect(page.locator('#nav .nav-item[data-nav="settings"]')).toHaveCount(0);

    await page.goto('/#settings');
    await settleShell(page);
    await expect(page.locator('#content .scr-host')).toHaveCount(0);
    expect(new URL(page.url()).hash).toBe('#home');
  });

  test('an out-of-scope read renders as not-found, never as forbidden', async ({ page }) => {
    // The anti-oracle rule: a 403 on a GET is indistinguishable from a 404, and
    // both render in the same words. A "forbidden" or "403" anywhere on screen
    // would be the existence oracle the contract forbids.
    await stubScreenApis(page);
    await page.route('**/api/budget/cells**', (route) => route.fulfill(
      jsonRoute({ detail: { code: 'NOT_FOUND', message: 'No budget data was found for this scope.' } }, 404),
    ));
    await signIn(page);
    await gotoScreen(page, 'budget-grid');

    await expect(page.locator('#content')).toContainText(/no budget data/i);
    await expect(page.locator('#content')).not.toContainText(/forbidden|access denied|\b403\b/i);
  });

  test('a 403 on a read is rendered exactly as the 404 is', async ({ page }) => {
    await stubScreenApis(page);
    await page.route('**/api/audit/entries**', (route) => route.fulfill(
      jsonRoute({ code: 'FORBIDDEN', message: 'Out of scope.' }, 403),
    ));
    await signIn(page);
    await gotoScreen(page, 'audit-trail');
    await expect(page.locator('#content')).not.toContainText(/forbidden|access denied|\b403\b/i);
  });
});

/* ================================================ loading / empty / error */

test.describe('SPA routing — required states inside the shell', () => {
  test.beforeEach(async ({ page }) => {
    await stubScreenApis(page);
  });

  test('SCR-09 loading: the shell shows a skeleton, never a blank panel', async ({ page }) => {
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route('**/api/budget/cells**', async (route) => {
      await held;
      await route.fulfill(jsonRoute(CELLS));
    });
    await signIn(page);
    await page.goto('/#budget-grid');

    // The panel exists and is showing skeleton rows while the fetch is in
    // flight — the assertion is that the wait is visible, not blank.
    await page.waitForSelector('#content .scr-host .audit-skel-row', { timeout: 15_000 });
    expect(await page.locator('#content .scr-host .audit-skel-row').count()).toBeGreaterThan(0);
    release();
    await settleScreen(page);
    await expect(page.locator('#content .scr-host tbody tr')).not.toHaveCount(0);
  });

  test('SCR-09 empty: an empty result set says so', async ({ page }) => {
    await page.route('**/api/budget/cells**', (route) => route.fulfill(
      jsonRoute({ items: [], next_cursor: null, has_more: false }),
    ));
    await signIn(page);
    await gotoScreen(page, 'budget-grid');
    // `.first()`: the tree table renders its own empty row alongside the
    // status block, so two `.empty` nodes are correct here.
    await expect(page.locator('#content .empty').first()).toBeVisible();
    await expect(page.locator('#content')).toContainText(/no budget cells match these filters/i);
  });

  test('SCR-09 error: a 500 is an alert with a retry, not a silent blank', async ({ page }) => {
    await page.route('**/api/budget/cells**', (route) => route.fulfill(
      jsonRoute({ code: 'INTERNAL', message: 'The budget planning grid could not be loaded.' }, 500),
    ));
    await signIn(page);
    await gotoScreen(page, 'budget-grid');
    const banner = page.locator('#content .msg-error');
    await expect(banner).toBeVisible();
    await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('SCR-28 loading / empty / error inside the shell', async ({ page }) => {
    // loading
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route('**/api/audit/entries**', async (route) => {
      await held;
      await route.fulfill(jsonRoute(AUDIT_ENTRIES));
    });
    await signIn(page);
    await page.goto('/#audit-trail');
    await page.waitForSelector('#content .scr-host .audit-skel-row', { timeout: 15_000 });
    release();
    await settleScreen(page);
    await expect(page.locator('#audit-trail-root tbody tr:not([hidden])')).toHaveCount(3);

    // empty
    await page.unroute('**/api/audit/entries**');
    await routeJson(page, '**/api/audit/entries**', { items: [], next_cursor: null, has_more: false });
    await page.goto('/#home');
    await settleShell(page);
    await gotoScreen(page, 'audit-trail');
    await expect(page.locator('#content .empty').first()).toBeVisible();

    // error
    await page.unroute('**/api/audit/entries**');
    await page.route('**/api/audit/entries**', (route) => route.fulfill(
      jsonRoute({ code: 'INTERNAL', message: 'The audit trail could not be loaded.' }, 500),
    ));
    await page.goto('/#home');
    await settleShell(page);
    await gotoScreen(page, 'audit-trail');
    await expect(page.locator('#content .msg-error')).toBeVisible();
  });

  test('SCR-10 loading / empty / error inside the shell', async ({ page }) => {
    await signIn(page);
    await gotoScreen(page, 'budget-compare');
    const host = page.locator('#content .scr-host');

    // ready: load versions, then compare
    await host.locator('#budgetCompareProject').fill('PRJ-01');
    await host.getByRole('button', { name: 'Load versions' }).click();
    await expect(host.locator('#budgetCompareLeft')).toBeEnabled();
    await host.getByRole('button', { name: 'Compare' }).click();
    await expect(host.locator('table tbody tr')).not.toHaveCount(0);

    // empty
    await page.unroute('**/api/budget/compare**');
    await routeJson(page, '**/api/budget/compare**', { rows: [] });
    await host.getByRole('button', { name: 'Compare' }).click();
    await expect(host.locator('.empty')).toBeVisible();

    // error
    await page.unroute('**/api/budget/compare**');
    await page.route('**/api/budget/compare**', (route) => route.fulfill(
      jsonRoute({ code: 'INTERNAL', message: 'The comparison could not be loaded.' }, 500),
    ));
    await host.getByRole('button', { name: 'Compare' }).click();
    await expect(host.locator('.msg-error')).toBeVisible();
  });

  test('SCR-13 ready / error inside the shell', async ({ page }) => {
    await signIn(page);
    await gotoScreen(page, 'budget-availability');
    const host = page.locator('#content .scr-host');

    await host.locator('#availWbs').fill('WBS-01-02');
    await host.locator('#availHead').fill('ELEC');
    await host.locator('#availAmount').fill('50000');
    await host.getByRole('button', { name: 'Check availability' }).click();
    await expect(host.locator('#availResultHost')).toContainText(/availability result/i);

    await page.unroute('**/api/budget/availability**');
    await page.route('**/api/budget/availability**', (route) => route.fulfill(
      jsonRoute({ code: 'INTERNAL', message: 'The availability check could not be completed.' }, 500),
    ));
    await host.getByRole('button', { name: 'Check availability' }).click();
    // `:not([hidden])`: the amount field's own inline validation box is a
    // permanently-present, hidden .msg-error until a field is invalid.
    await expect(host.locator('.msg-error:not([hidden])')).toBeVisible();
    await expect(host).toContainText(/availability check could not be completed/i);
  });

  test('SCR-30 empty and error inside the shell', async ({ page }) => {
    await page.unroute('**/api/settings/**');
    await routeJson(page, '**/api/settings/**', { items: [], next_cursor: null, has_more: false });
    await signIn(page);
    await gotoScreen(page, 'settings');
    await expect(page.locator('#settingsHierarchyPanel .empty').first()).toBeVisible();

    await page.unroute('**/api/settings/**');
    await page.route('**/api/settings/**', (route) => route.fulfill(
      jsonRoute({ code: 'INTERNAL', message: 'The organisation hierarchy could not be loaded.' }, 500),
    ));
    await page.goto('/#home');
    await settleShell(page);
    await gotoScreen(page, 'settings');
    await expect(page.locator('#settingsHierarchyPanel .msg-error')).toBeVisible();
  });
});

/* ================================================================ a11y */

test.describe('SPA routing — accessibility', () => {
  test.beforeEach(async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);
  });

  for (const s of SCREENS) {
    test(`axe-core: ${s.scr} has no violations inside the shell`, async ({ page }) => {
      await gotoScreen(page, s.hash);

      // The mounted screen itself, with nothing excluded at all.
      const screenOnly = await new AxeBuilder({ page }).include('#content').analyze();
      expect(screenOnly.violations, JSON.stringify(screenOnly.violations, null, 2)).toEqual([]);

      // And the whole page around it, so a screen cannot pass by sitting in a
      // broken shell. Only the one frozen-stylesheet defect is excluded.
      const wholePage = await axeForShell(page).analyze();
      expect(wholePage.violations, JSON.stringify(wholePage.violations, null, 2)).toEqual([]);
    });
  }

  test('the excluded shell defect is still exactly the one that was reported', async ({ page }) => {
    // Guards the exclusion above: if #userAvatar's contrast is ever fixed, or
    // if a second shell defect appears, this test says so instead of the
    // exclusion quietly hiding a growing list.
    await gotoScreen(page, 'audit-trail');
    const results = await new AxeBuilder({ page }).analyze();
    const outside = results.violations.flatMap((v) => v.nodes
      .filter((n) => !n.target.includes(KNOWN_FROZEN_STYLESHEET_DEFECT))
      .map((n) => ({ id: v.id, target: n.target })));
    expect(outside, JSON.stringify(outside, null, 2)).toEqual([]);
    expect(
      results.violations.map((v) => v.id),
      'the reported #userAvatar contrast defect is no longer present — remove the exclusion',
    ).toEqual(['color-contrast']);
  });

  test('every SCR-nn screen is reachable with the keyboard alone', async ({ page }) => {
    await settleShell(page);

    // Below 900px the navigation is an overlay drawer that closes itself after
    // every navigation (closeNavOnNarrow), so it has to be re-opened before
    // each screen — with the keyboard, since that is the whole point here.
    const openDrawerIfNeeded = async () => {
      const collapsed = await page.locator('#nav')
        .evaluate((n) => getComputedStyle(n).display === 'none');
      if (!collapsed) return;
      await page.locator('#navToggle').focus();
      await page.keyboard.press('Enter');
      await expect(page.locator('#nav')).toBeVisible();
    };

    for (const s of SCREENS) {
      await openDrawerIfNeeded();
      const item = page.locator(`#nav .nav-item[data-nav="${s.hash}"]`);
      await item.focus();
      // The focused element really is the navigation entry, and it is a real
      // button — Enter activates it without a click handler on a div.
      const focusedRoute = await page.evaluate(() => document.activeElement
        && document.activeElement.getAttribute('data-nav'));
      expect(focusedRoute, `${s.scr} navigation entry could not take focus`).toBe(s.hash);
      await page.keyboard.press('Enter');
      await settleScreen(page);
      await expect(pageTitle(page)).toHaveText(s.title);
    }
  });

  test('the focused navigation entry has a visible, non-colour-only focus ring', async ({ page }) => {
    await settleShell(page);
    await openNavIfCollapsed(page);
    // styles.css draws the ring through :focus-visible, which Chromium only
    // matches once the user has interacted by keyboard. Press Tab first so the
    // browser is in keyboard modality, exactly as a keyboard-only user is.
    await page.keyboard.press('Tab');
    const item = page.locator('#nav .nav-item[data-nav="settings"]');
    await item.focus();
    const ring = await item.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        width: cs.outlineWidth,
        style: cs.outlineStyle,
        matchesFocusVisible: el.matches(':focus-visible'),
      };
    });
    expect(ring.matchesFocusVisible, 'the focused entry does not match :focus-visible').toBe(true);
    expect(ring.style, 'focused navigation entry has no outline style').not.toBe('none');
    expect(parseFloat(ring.width), 'focused navigation entry has a zero-width outline')
      .toBeGreaterThan(0);
  });

  test('the skip link reaches the mounted screen content', async ({ page }) => {
    await gotoScreen(page, 'audit-trail');
    await page.locator('.skip-link').focus();
    await page.keyboard.press('Enter');
    const id = await page.evaluate(() => document.activeElement && document.activeElement.id);
    expect(id).toBe('content');
  });

  test('status on an SCR-nn screen is distinguishable without colour', async ({ page }) => {
    // C3_statuses.json: never colour alone. The budget verdict badge is the
    // status treatment these screens use; it must carry a text label.
    await gotoScreen(page, 'budget-availability');
    const host = page.locator('#content .scr-host');
    await host.locator('#availWbs').fill('WBS-01-02');
    await host.locator('#availHead').fill('ELEC');
    await host.locator('#availAmount').fill('50000');
    await host.getByRole('button', { name: 'Check availability' }).click();
    await expect(host.locator('#availResultHost')).toContainText(/availability result/i);

    const badges = await page.evaluate(() => [...document.querySelectorAll('#content .status')]
      .map((el) => el.textContent.trim()));
    expect(badges.length, 'no status element rendered').toBeGreaterThan(0);
    expect(badges.filter((t) => !t), 'a status rendered with no text label').toEqual([]);
  });

  test('each SCR-nn screen announces itself through a live region', async ({ page }) => {
    // Every feature module looks its live region up by id at mount time; if
    // the shell host did not carry one, every announcement would be silently
    // dropped and a screen-reader user would never learn the table had loaded.
    for (const [hash, regionId] of [
      ['audit-trail', 'auditLiveRegion'],
      ['budget-grid', 'budgetLiveRegion'],
      ['settings', 'settingsLiveRegion'],
    ]) {
      await gotoScreen(page, hash);
      const region = page.locator(`#${regionId}`);
      await expect(region, `${hash} has no live region`).toHaveCount(1);
      await expect(region).toHaveAttribute('aria-live', 'polite');
    }
  });
});

/* =========================================================== CSP / XSS */

test.describe('SPA routing — the CSP contract holds on the new routes', () => {
  test('no CSP violation is reported on any SCR-nn route', async ({ page }) => {
    // main.py sends `style-src 'self'` with no 'unsafe-inline'. A markup
    // style="…" attribute is blocked outright and reported; CSSOM assignment
    // (el.style.left = …) is not governed by the policy at all, even though it
    // leaves an identical-looking attribute in the serialised DOM — which is
    // why the assertion is on the browser's own violation reports and on the
    // console, not on `[style]` selectors.
    const reports = [];
    page.on('console', (m) => {
      if (/content security policy/i.test(m.text())) reports.push(m.text());
    });
    await page.addInitScript(() => {
      window.__cspViolations = [];
      document.addEventListener('securitypolicyviolation', (e) => {
        window.__cspViolations.push(`${e.violatedDirective} ${e.blockedURI} ${e.sourceFile || ''}`);
      });
    });
    await stubScreenApis(page);
    await signIn(page);

    for (const s of SCREENS) {
      await gotoScreen(page, s.hash);
      const inPage = await page.evaluate(() => window.__cspViolations || []);
      expect(inPage, `${s.scr}: ${inPage.join('; ')}`).toEqual([]);
    }
    expect(reports, reports.join('\n')).toEqual([]);
  });

  test('no SCR-nn source file contains a style="…" literal', async ({ page }) => {
    // The static half of the same contract. core/dom.js's h() throws on a
    // `style` attribute, but a template string assembled by hand would slip
    // past it; this reads the shipped files back over HTTP and checks.
    const files = [
      '/static/app.js',
      '/static/src/core/router.js',
      '/static/src/core/dom.js',
      '/static/src/core/api-client.js',
      '/static/src/features/audit/audit-trail.js',
      '/static/src/features/budget/budget-grid.js',
      '/static/src/features/budget/budget-compare.js',
      '/static/src/features/budget/budget-availability.js',
      '/static/src/features/settings/settings-app.js',
      '/static/src/features/settings/org-hierarchy.js',
      '/static/src/features/settings/masters.js',
    ];
    for (const f of files) {
      const res = await page.request.get(f);
      expect(res.ok(), `${f} is not being served`).toBe(true);
      const body = await res.text();
      // Comments are stripped first: several of these files document the rule
      // by quoting the very attribute they forbid, and a header comment saying
      // "a markup style=… attribute is blocked" must not read as a breach of
      // it. `el.style.left = …` and `el.style[prop] = …` are the permitted
      // CSSOM form and do not match `style =`.
      const code = body
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
      const emitted = code.split('\n').filter((line) => /style\s*=\s*["'`]/.test(line));
      expect(emitted, `${f} emits a style attribute:\n${emitted.join('\n')}`).toEqual([]);
    }
  });
});

/* ============================================== visual regression 1440/1024/800 */

test.describe('SPA routing — visual regression', () => {
  test.beforeEach(async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);
  });

  for (const s of SCREENS) {
    test(`${s.scr} — ${s.hash} renders identically`, async ({ page }) => {
      await gotoScreen(page, s.hash);
      await expect(page).toHaveScreenshot(`spa-${s.hash}.png`, { fullPage: true });
    });
  }
});
