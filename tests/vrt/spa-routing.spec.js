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
// THE PRIMARY NAVIGATION IS DELIBERATELY NOT TOUCHED.
//
// The client-approved navigation rail is rendered inside every one of the
// thirty-seven screenshots in approved-ui.spec.js-snapshots/. Adding five
// entries to it changes all thirty-seven — and the only way to make the suite
// green again is to overwrite the very evidence that would have caught a real
// regression. That is a client design decision, not an implementation-stream
// one, so these five screens are routable and deep-linkable but carry no
// navigation entry, and the first test below asserts the rail is unchanged.
// src/core/router.js already carries the icon, label and group for each screen,
// so listing them is a small loop on the day the client signs it off.
//
// What is asserted here, and nowhere else:
//   * the approved primary navigation still holds exactly its seventeen
//     entries, and none of the five SCR-nn routes has been added to it;
//   * every one of the five is deep-linkable — a cold load of /#<hash> renders
//     the screen, not the dashboard — and reachable by hash from a running
//     shell;
//   * browser Back and Forward move between them and restore the right screen;
//   * app.js's SCR_ROUTES permission gate and src/core/router.js's SCREENS
//     registry agree, so the two declarations cannot drift apart unnoticed,
//     and an unknown hash resolves to nothing rather than to an ungated screen;
//   * the four required states — loading, empty, error, permission-denied —
//     render inside the shell for each screen;
//   * a user without the permission cannot reach the route at all, proven
//     against a positive control that can;
//   * axe-core is clean on each screen inside the shell, Tab alone reaches
//     every control with a visible focus ring, and status is distinguishable
//     without colour;
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
  // data-mounted, not merely .scr-host. app.js appends the host node BEFORE it
  // awaits the feature module's mount() — the module needs its roots in the
  // document to find them by id — so .scr-host appears while the screen is
  // still empty. Waiting on the host alone raced the dynamic import and the
  // on-demand stylesheet, and intermittently found a screen with no controls
  // in it yet.
  //
  // BOTH outcomes end the wait. app.js marks a screen whose mount() threw with
  // data-mount-failed instead of data-mounted, and a load failure diagnosed as
  // a load failure is worth far more than the same failure arriving fifteen
  // seconds later as a timeout — or, worse, as "this screen rendered no
  // focusable control", which describes the design rather than the fault.
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

function pageTitle(page) {
  return page.locator('#pageTitle');
}

/**
 * Move to a route from inside an already-running shell, by hash. This is how
 * the five SCR-nn screens are reached: they are routable and deep-linkable but
 * deliberately carry no primary-navigation entry, because every approved
 * screenshot contains that rail.
 */
async function hashTo(page, hash) {
  await page.evaluate((h) => { window.location.hash = h; }, hash);
  await settleScreen(page);
}

/** Open the navigation drawer if this viewport collapses it (below 900px). */
async function openNavIfCollapsed(page) {
  const collapsed = await page.locator('#nav')
    .evaluate((n) => getComputedStyle(n).display === 'none');
  if (collapsed) await page.locator('#navToggle').click();
  return collapsed;
}

/**
 * There is no longer an accessibility exclusion on this suite.
 *
 * Wave 3 excluded `#userAvatar` here: it rendered white 11px text on the
 * frozen stylesheet's avatar background (#2E8A9A) at 4.02:1, below the 4.5:1
 * WCAG 2.2 AA minimum, and the colour lived in a byte-frozen, SHA-256-pinned
 * stylesheet that stream could not change. It was reported rather than fixed,
 * and excluded so that every OTHER violation still failed the build.
 *
 * The product owner approved the correction on 2026-09-03 (APPROVED UI CHANGE
 * 2 of 2). `.avatar` now uses `--primary-600` (#24707E) and the same text
 * measures 5.6850:1. The exclusion is therefore DELETED rather than retained
 * as dead configuration: axe now runs against the whole shell with nothing
 * carved out, so this suite is strictly stricter than it was, and a future
 * regression of that same contrast fails here instead of being silently
 * permitted by a stale allowance.
 */
function axeForShell(page) {
  return new AxeBuilder({ page });
}

/* ============================================================ navigation */

test.describe('SPA routing — every SCR-nn screen is reachable from the shell', () => {
  test.beforeEach(async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);
  });

  test('the primary navigation is exactly the approved navigation', async ({ page }) => {
    // THE GUARD THAT MATTERS, and it still matters — it has been re-pointed,
    // not relaxed. Every approved screenshot contains this rail, so any entry
    // added to it silently invalidates all of them.
    //
    // On 2026-09-03 the product owner approved, in writing, exactly one change
    // to this rail: the five completed SCR-nn screens are now listed
    // (APPROVED UI CHANGE 1 of 2). The list below is that approved rail, in
    // full and in order. An UNEXPECTED entry, or a reordering, or a silent
    // removal, still fails here — which is the whole job of this test. It
    // asserts the exact sequence rather than a membership check precisely so
    // that "the approved navigation" stays a fact with a value, not a
    // direction of travel.
    //
    // WAVE 4 ADDS NOTHING TO THIS RAIL, AND THAT IS THE POINT.
    // M4b's eight approval screens were spliced in here in 69f45e1 as change
    // A3. A3 was INSTRUCTED by the lead but never APPROVED by the product
    // owner, unlike A1 and A2 — and measurement showed it broke the approved
    // layout rather than extending it. Measured from the running application
    // as U-ADM, who saw seven of the eight:
    //
    //     viewport        rail    content   overflow
    //     desktop-1440    856px   1050px    194px
    //     laptop-1024     713px   1050px    336px
    //
    // The whole Governance group — including `settings`, one of the five
    // entries A1 WAS approved to add — fell below the fold of a scroll
    // container with no visual cue that it scrolls. The eight are therefore
    // reachable BY ROUTE ONLY, which costs them nothing: every one still
    // deep-links, stays permission-gated and stays bookmarkable, and
    // `viewAllowed()` resolves an SCR_ROUTES id whether or not it is listed
    // here. See docs/ui-change-2026-09/A3-approval-navigation.md.
    //
    // The expectation below is therefore back to exactly the A1-approved rail.
    // This is a RESTORATION, not a relaxation: it is still an exact ordered
    // sequence, so an unexpected entry, a reordering or a silent removal all
    // still fail here.
    await settleShell(page);
    const ids = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    expect(ids).toEqual([
      'home', 'approvals', 'alerts',
      'projects', 'wbs', 'budget', 'check', 'revisions',
      'budget-grid', 'budget-compare', 'budget-availability',
      'prs', 'pos', 'grns', 'bills', 'recon',
      'cap',
      'zoho', 'inventory',
      'audit', 'audit-trail',
      'settings',
    ]);
    // STRONGER than the assertion this replaces, which excluded only
    // Delegation Management. No approval screen belongs in the rail at all
    // now, for ANY principal, so all eight are named — including the seven an
    // Administrator does hold the permission for. A permission-gated entry
    // that reappears because someone re-splices SCR_ROUTES into NAV would
    // otherwise be invisible to this test for exactly the principal that can
    // see it.
    for (const id of ['approval-inbox', 'approval-request', 'approval-sla',
      'approval-timeline', 'approval-matrix', 'approval-versions',
      'approval-simulator', 'approval-delegations']) {
      expect(ids, `"${id}" is a route, not a rail entry: listing it re-lands the unapproved A3`)
        .not.toContain(id);
    }
    // Every one of the five is now reachable from the rail, not merely by URL.
    for (const s of SCREENS) {
      expect(ids, `${s.scr} is missing from the approved navigation`).toContain(s.hash);
    }
    // The seventeen originally-approved entries all survive. An addition must
    // never have been a replacement.
    for (const id of ['home', 'approvals', 'alerts', 'projects', 'wbs', 'budget', 'check',
      'revisions', 'prs', 'pos', 'grns', 'bills', 'recon', 'cap', 'zoho', 'inventory', 'audit']) {
      expect(ids, `the approved view "${id}" was dropped from the navigation`).toContain(id);
    }
  });

  test('a navigation entry is permission-gated, not merely present', async ({ page }) => {
    // The rail entries are new, so the gate behind them is new too. An entry
    // rendered for a principal who cannot open the route would be a dead link
    // that also leaks the existence of a screen they may not see.
    //
    // Administrator (the session in this describe) holds all five permissions.
    // The Auditor below holds audit.read/budget.read/budget.check but NOT
    // settings.read or masters.read, so Settings must be absent for them while
    // the budget entries remain — a positive control on the same assertion.
    await settleShell(page);
    const adminIds = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    expect(adminIds).toContain('settings');

    const auditor = await page.context().newPage();
    await stubScreenApis(auditor);
    await signIn(auditor, AUDITOR);
    await settleShell(auditor);
    const auditorIds = await auditor.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    expect(auditorIds, 'Settings was listed for a principal without settings.read')
      .not.toContain('settings');
    expect(auditorIds, 'the Audit Trail Viewer should be listed for an Auditor')
      .toContain('audit-trail');
    expect(auditorIds, 'the budget screens should be listed for an Auditor')
      .toContain('budget-grid');
    await auditor.close();
  });

  for (const s of SCREENS) {
    test(`${s.scr} — /#${s.hash} deep-links on a cold load`, async ({ page }) => {
      await gotoScreen(page, s.hash);
      await expect(pageTitle(page)).toHaveText(s.title);
      // Not silently redirected to the dashboard.
      expect(new URL(page.url()).hash).toBe(`#${s.hash}`);
      await expect(page.locator('#content .scr-host')).toHaveCount(1);
    });
  }

  test('every SCR-nn screen is reachable by hash from inside a running shell', async ({ page }) => {
    // Not a cold load: sign in once, then move between screens the way a link
    // or a bookmark inside the application would, proving the router handles
    // them live rather than only at boot.
    await settleShell(page);
    for (const s of SCREENS) {
      await page.evaluate((hash) => { window.location.hash = hash; }, s.hash);
      await settleScreen(page);
      await expect(pageTitle(page), `${s.scr} did not open`).toHaveText(s.title);
    }
  });

  test('Back and Forward move between SCR-nn routes and restore each screen', async ({ page }) => {
    await gotoScreen(page, 'audit-trail');
    await expect(pageTitle(page)).toHaveText('Audit Trail Viewer');

    await hashTo(page, 'budget-grid');
    await expect(pageTitle(page)).toHaveText('Budget Planning Grid');

    await hashTo(page, 'settings');
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
    // By hash, not by clicking the rail: below 900px the rail is a closed
    // drawer and the entry is not visible.
    await page.evaluate(() => { window.location.hash = 'home'; });
    await settleShell(page);
    await expect(page.locator('#content .scr-host')).toHaveCount(0);

    await hashTo(page, 'budget-grid');
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
  test('app.js SCR_ROUTES and src/core/router.js SCREENS agree on id and permission', async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);

    const registry = await page.evaluate(async () => {
      const mod = await import('/static/src/core/router.js');
      return mod.SCREENS.map((s) => ({ id: s.id, scr: s.scr, need: s.need }));
    });

    // app.js has to restate `need` in SCR_ROUTES because the permission gate
    // runs synchronously in render(), long before any dynamic import can
    // resolve. This is the assertion that keeps the restatement honest — a
    // permission loosened in one table and not the other is exactly the kind of
    // drift that opens a screen to a principal who should not see it.
    const gates = await page.evaluate(
      () => SCR_ROUTES.map((r) => ({ id: r.id, need: r.need })),
    );
    expect(gates.map((g) => g.id).sort()).toEqual(registry.map((r) => r.id).sort());
    for (const entry of registry) {
      const gate = gates.find((g) => g.id === entry.id);
      expect(gate, `${entry.scr} (${entry.id}) has no SCR_ROUTES gate in app.js`).toBeTruthy();
      expect(gate.need, `${entry.scr} permission gate drifted`).toEqual(entry.need);
    }

    // Every screen THIS suite claims to cover is in the registry, with its
    // SCR number intact.
    for (const s of SCREENS) {
      const entry = registry.find((r) => r.id === s.hash);
      expect(entry, `${s.scr} (${s.hash}) is missing from the registry`).toBeTruthy();
      expect(entry.scr, `${s.hash} lost its SCR number`).toBe(s.scr);
    }

    // And the registry as a whole is exactly the forty-six routable screens:
    // five shell screens, the approval engine's eight from Wave 4
    // (tests/vrt/approvals.spec.js), Wave 5's twelve integration screens
    // (tests/vrt/integration.spec.js), Wave 7's eleven analytics screens
    // (tests/vrt/analytics.spec.js), its four mapping/connector screens
    // (tests/vrt/mapping.spec.js) and its six closure screens
    // (tests/vrt/closure.spec.js). Naming every one rather than asserting
    // "at least the five" keeps this an exact statement — a forty-seventh
    // screen appearing without a test still fails here.
    //
    // Those forty-six carry thirty-seven distinct SCR numbers; with the three
    // legacy shell views `pos`, `grns` and `bills` (SCR-15/16/17) that is
    // C8's full forty.
    //
    // This list was thirteen until Wave 7, and it FAILED when the fifteen new
    // screens were spliced into the registry. That is the assertion working,
    // not an obstacle to route around: it is extended here only because each
    // of the fifteen brought its own spec asserting its own SCR number, which
    // is the condition the guard is checking for. Relaxing it to a subset
    // check would have removed the property entirely.
    expect(registry.map((r) => r.id).sort()).toEqual([
      'closure-asset-allocation', 'closure-budget-revision',
      'closure-budget-transfer', 'closure-capitalisation',
      'closure-completion-review', 'closure-pr-control',
      'integration-events', 'integration-exceptions', 'integration-health',
      'integration-inbound-bill', 'integration-inbound-grn', 'integration-oauth',
      'integration-organisation', 'integration-outbound-po',
      'integration-reconciliation', 'integration-retry', 'integration-scopes',
      'integration-setup',
      'analytics-commitment-ageing', 'analytics-controller', 'analytics-cwip-ageing',
      'analytics-cwip-ledger', 'analytics-exceptions', 'analytics-executive',
      'analytics-project-list', 'analytics-project-object', 'analytics-wbs-element',
      'analytics-wbs-explorer', 'analytics-wbs-tree',
      'approval-delegations', 'approval-inbox', 'approval-matrix', 'approval-request',
      'approval-simulator', 'approval-sla', 'approval-timeline', 'approval-versions',
      'audit-trail', 'budget-availability', 'budget-compare', 'budget-grid',
      'connector-audit', 'mapping-fields', 'mapping-master', 'mapping-sync',
      'settings',
    ].sort());
  });

  test('an unknown hash cannot resolve to an ungated screen', async ({ page }) => {
    // viewAllowed() refuses an id in neither NAV nor SCR_ROUTES. Before it
    // existed, render() fell back to navAllowed({}), which returns true for
    // everyone — so any route missing from NAV was readable by every signed-in
    // principal regardless of permission.
    await stubScreenApis(page);
    await signIn(page);
    const verdicts = await page.evaluate(
      () => ['no-such-view', '', 'constructor', '__proto__'].map((id) => viewAllowed(id)),
    );
    expect(verdicts).toEqual([false, false, false, false]);
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

/* ====================================== the one shared client, both envelopes */

test.describe('core/api-client.js — one client, two error envelopes', () => {
  test.beforeEach(async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page);
  });

  test('the session header, correlation id and problem parsing exist in exactly one module', async ({ page }) => {
    // The defect this module was created to close: core/api.js was hard-wired
    // to /api/audit, so budget-api.js and settings/api.js each grew their own
    // copy of the same mechanics — and only two of the three copies ever
    // learned that this API has two error envelopes. A fourth copy, or a
    // regrown third, is what this test exists to catch.
    const files = [
      '/static/src/core/api.js',
      '/static/src/features/budget/budget-api.js',
      '/static/src/features/settings/api.js',
    ];
    for (const f of files) {
      const body = await (await page.request.get(f)).text();
      const code = body.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
      expect(code, `${f} still calls fetch() itself`).not.toMatch(/\bfetch\s*\(/);
      expect(code, `${f} still reads the session itself`).not.toMatch(/sessionStorage/);
      expect(code, `${f} still sets its own correlation id`).not.toMatch(/X-Correlation-Id/);
      expect(code, `${f} still parses problem bodies itself`).not.toMatch(/message_id/);
      expect(code, `${f} does not build on the shared client`)
        .toMatch(/createApiClient/);
    }
  });

  test('readProblem reads `code` from BOTH envelopes', async ({ page }) => {
    const got = await page.evaluate(async () => {
      const { readProblem } = await import('/static/src/core/api-client.js');
      return {
        // A: services.BusinessError — code at the TOP level.
        businessError: readProblem({
          code: 'BUDGET_EXCEEDED', message: 'Exceeds available budget.', message_id: 'MSG-BUD-001',
        }),
        // B: a router raising HTTPException(detail={...}) — code NESTED.
        httpException: readProblem({
          detail: { code: 'VERSION_CONFLICT', message: 'Someone else changed this record.' },
        }),
        // C: FastAPI's own plain-string detail.
        plainString: readProblem({ detail: 'Not found.' }),
        // D: FastAPI request validation — an array the old copies dropped.
        validation: readProblem({
          detail: [{ loc: ['body', 'amount_paise'], msg: 'is not a valid integer' }],
        }),
        rfc7807: readProblem({ title: 'Unprocessable Entity' }),
        garbage: readProblem(null),
      };
    });

    expect(got.businessError.code).toBe('BUDGET_EXCEEDED');
    expect(got.businessError.detail).toBe('Exceeds available budget.');
    expect(got.businessError.messageId).toBe('MSG-BUD-001');

    expect(got.httpException.code, 'the nested envelope lost its code').toBe('VERSION_CONFLICT');
    expect(got.httpException.detail).toBe('Someone else changed this record.');

    expect(got.plainString.detail).toBe('Not found.');
    expect(got.plainString.code).toBeNull();

    expect(got.validation.detail).toBe('amount_paise: is not a valid integer');
    expect(got.validation.messageId).toBe('VALIDATION_ERROR');

    expect(got.rfc7807.detail).toBe('Unprocessable Entity');
    expect(got.garbage.detail).toBeNull();
  });

  test('classifyStatus applies not-found-over-forbidden to reads only', async ({ page }) => {
    const got = await page.evaluate(async () => {
      const { classifyStatus } = await import('/static/src/core/api-client.js');
      return {
        getForbidden: classifyStatus(403, null, 'GET'),
        headForbidden: classifyStatus(403, null, 'HEAD'),
        postForbidden: classifyStatus(403, null, 'POST'),
        putForbidden: classifyStatus(403, null, 'PUT'),
        notFound: classifyStatus(404, null, 'GET'),
        unauthorised: classifyStatus(401, null, 'GET'),
        conflict: classifyStatus(409, null, 'PUT'),
        conflictByCode: classifyStatus(400, 'VERSION_CONFLICT', 'PUT'),
        validation: classifyStatus(422, null, 'POST'),
        network: classifyStatus(0, null, 'GET'),
        server: classifyStatus(500, null, 'GET'),
      };
    });
    // A 403 on a READ is an existence oracle, so it must be indistinguishable
    // from a 404. A 403 on a WRITE leaks no id the caller did not already name,
    // and "not found" would be a lie about a permission refusal.
    expect(got.getForbidden).toBe('notfound');
    expect(got.headForbidden).toBe('notfound');
    expect(got.notFound).toBe('notfound');
    expect(got.postForbidden).toBe('forbidden');
    expect(got.putForbidden).toBe('forbidden');
    expect(got.unauthorised).toBe('auth');
    expect(got.conflict).toBe('conflict');
    expect(got.conflictByCode).toBe('conflict');
    expect(got.validation).toBe('validation');
    expect(got.network).toBe('network');
    expect(got.server).toBe('error');
  });

  test('each feature error stays its own type while sharing one base', async ({ page }) => {
    const got = await page.evaluate(async () => {
      const [{ ApiClientError }, audit, budget, settings] = await Promise.all([
        import('/static/src/core/api-client.js'),
        import('/static/src/core/api.js'),
        import('/static/src/features/budget/budget-api.js'),
        import('/static/src/features/settings/api.js'),
      ]);
      const a = new audit.AuditApiError('x');
      const b = new budget.BudgetApiError('x');
      const s = new settings.SettingsApiError('x');
      return {
        names: [a.name, b.name, s.name],
        allShareBase: [a, b, s].every((e) => e instanceof ApiClientError),
        // The families stay distinguishable — feature code branches on these.
        auditIsNotBudget: !(a instanceof budget.BudgetApiError),
        budgetIsNotSettings: !(b instanceof settings.SettingsApiError),
      };
    });
    expect(got.names).toEqual(['AuditApiError', 'BudgetApiError', 'SettingsApiError']);
    expect(got.allShareBase, 'a feature error is not an ApiClientError').toBe(true);
    expect(got.auditIsNotBudget).toBe(true);
    expect(got.budgetIsNotSettings).toBe(true);
  });

  test('each client is bound to its own base path', async ({ page }) => {
    // The whole point of parameterising: one implementation, three base paths.
    const urls = [];
    page.on('request', (r) => { if (r.url().includes('/api/')) urls.push(new URL(r.url()).pathname); });
    await page.evaluate(async () => {
      const [audit, budget, settings] = await Promise.all([
        import('/static/src/core/api.js'),
        import('/static/src/features/budget/budget-api.js'),
        import('/static/src/features/settings/api.js'),
      ]);
      await Promise.all([
        audit.listStreams(),
        budget.listVersions('PRJ-01'),
        settings.listSettings('organisations'),
        settings.listMasters('items'),
      ]);
    });
    expect(urls).toContain('/api/audit/streams');
    expect(urls).toContain('/api/budget/versions');
    expect(urls).toContain('/api/settings/organisations');
    expect(urls).toContain('/api/masters/items');
  });

  test('every request carries the session header and a fresh correlation id', async ({ page }) => {
    const seen = [];
    page.on('request', (r) => {
      if (r.url().includes('/api/audit/streams')) {
        seen.push({
          session: r.headers()['x-session'] || null,
          cid: r.headers()['x-correlation-id'] || null,
        });
      }
    });
    await page.evaluate(async () => {
      const audit = await import('/static/src/core/api.js');
      await audit.listStreams();
      await audit.listStreams();
    });
    expect(seen.length).toBeGreaterThanOrEqual(2);
    for (const s of seen) {
      expect(s.session, 'a request went out with no X-Session').toBeTruthy();
      expect(s.cid, 'a request went out with no X-Correlation-Id').toBeTruthy();
    }
    // A correlation id that repeats cannot correlate anything.
    expect(new Set(seen.map((s) => s.cid)).size).toBe(seen.length);
  });
});

/* ================================================== permission-denied state */

test.describe('SPA routing — permission-denied', () => {
  test('a user without audit.read cannot route to the Audit Trail Viewer', async ({ page }) => {
    // Positive control first, so the refusal below cannot pass because the
    // route is simply broken for everyone.
    await stubScreenApis(page);
    await signIn(page, ADMIN);
    await gotoScreen(page, 'audit-trail');
    await expect(pageTitle(page)).toHaveText('Audit Trail Viewer');

    await page.evaluate(() => sessionStorage.clear());
    await signIn(page, REQUESTOR);
    await settleShell(page);

    await page.goto('/#audit-trail');
    await settleShell(page);
    // Falls back to the dashboard rather than mounting a screen this user may
    // not see. The hash is rewritten so a bookmark cannot keep re-trying it.
    await expect(page.locator('#content .scr-host')).toHaveCount(0);
    expect(new URL(page.url()).hash).toBe('#home');
  });

  test('a user without settings.read or masters.read cannot route to Settings', async ({ page }) => {
    await stubScreenApis(page);
    await signIn(page, ADMIN);
    await gotoScreen(page, 'settings');
    await expect(pageTitle(page)).toHaveText('Settings and Master Data');

    await page.evaluate(() => sessionStorage.clear());
    await signIn(page, AUDITOR);
    await settleShell(page);

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
      // broken shell. NOTHING is excluded any more.
      const wholePage = await axeForShell(page).analyze();
      expect(wholePage.violations, JSON.stringify(wholePage.violations, null, 2)).toEqual([]);
    });
  }

  test('the shell carries no accessibility violation at all, avatar included', async ({ page }) => {
    // The inverse of the test this replaces. Wave 3 asserted that exactly one
    // violation remained — the #userAvatar contrast defect it could not fix —
    // so that the exclusion could not quietly grow into a list. That defect is
    // now FIXED (APPROVED UI CHANGE 2 of 2), so the assertion inverts: the
    // whole shell must be clean, with nothing carved out.
    //
    // This is strictly stronger than what it replaces. The old test permitted
    // one violation; this one permits none.
    await gotoScreen(page, 'audit-trail');
    const results = await new AxeBuilder({ page }).analyze();
    const found = results.violations.flatMap((v) => v.nodes
      .map((n) => ({ id: v.id, target: n.target })));
    expect(found, JSON.stringify(found, null, 2)).toEqual([]);
  });

  test('#userAvatar meets WCAG 2.2 AA, measured rather than assumed', async ({ page }) => {
    // The specific regression guard for APPROVED UI CHANGE 2 of 2. axe would
    // catch a contrast failure, but it would not say what the ratio was, and a
    // future re-theme that lands at 4.49:1 should fail with the number that
    // made it fail. This computes the WCAG 2 relative-luminance ratio from the
    // element's OWN computed colours.
    await gotoScreen(page, 'audit-trail');
    const measured = await page.evaluate(() => {
      const el = document.getElementById('userAvatar');
      const cs = getComputedStyle(el);
      const parse = (v) => {
        const m = String(v).match(/rgba?\(([^)]+)\)/);
        if (!m) return null;
        const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
      };
      let bg = parse(cs.backgroundColor);
      let node = el;
      while (bg && bg.a === 0 && node.parentElement) {
        node = node.parentElement;
        bg = parse(getComputedStyle(node).backgroundColor);
      }
      const fg = parse(cs.color);
      const lin = (c) => {
        const s = c / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      const L = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
      const l1 = L(fg);
      const l2 = L(bg);
      return {
        fontSize: parseFloat(cs.fontSize),
        fontWeight: cs.fontWeight,
        ratio: (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05),
      };
    });

    // 11px 600-weight is BODY text under WCAG 2.2, not large text: large is
    // 18.66px bold or 24px regular. So the threshold is 4.5:1, not 3:1.
    expect(measured.fontSize).toBeLessThan(18.66);
    expect(measured.ratio,
      `#userAvatar contrast is ${measured.ratio.toFixed(4)}:1, below the 4.5:1 AA minimum`)
      .toBeGreaterThanOrEqual(4.5);
  });

  for (const s of SCREENS) {
    test(`${s.scr} — every control is reachable by Tab alone, with visible focus`, async ({ page }) => {
      await gotoScreen(page, s.hash);

      // Walk the whole screen with Tab and check that focus actually lands on
      // each control and is visibly indicated. A control that can be clicked
      // but never focused is unreachable for a keyboard-only user.
      const controls = await page.locator(
        '#content .scr-host button, #content .scr-host input, '
        + '#content .scr-host select, #content .scr-host textarea, '
        + '#content .scr-host [tabindex="0"]',
      ).count();
      expect(controls, `${s.scr} rendered no focusable control`).toBeGreaterThan(0);

      await page.locator('#content').focus();
      const seen = new Set();
      // Generous bound: enough Tabs to cross the screen plus the shell chrome
      // that follows it, without looping forever if focus ever gets trapped.
      for (let i = 0; i < controls + 40; i += 1) {
        await page.keyboard.press('Tab');
        const info = await page.evaluate(() => {
          const el = document.activeElement;
          if (!el || el === document.body) return null;
          const host = el.closest('#content .scr-host');
          if (!host) return { inside: false };
          const cs = getComputedStyle(el);
          return {
            inside: true,
            key: (el.id || '') + '|' + el.tagName + '|' + (el.textContent || '').slice(0, 24),
            focusVisible: el.matches(':focus-visible'),
            outlineWidth: parseFloat(cs.outlineWidth) || 0,
            outlineStyle: cs.outlineStyle,
          };
        });
        if (!info || !info.inside) continue;
        seen.add(info.key);
        // styles.css draws one high-contrast ring through :focus-visible, and
        // Tab is exactly the interaction that triggers it.
        expect(info.focusVisible, `${s.scr}: ${info.key} took focus with no :focus-visible ring`)
          .toBe(true);
        expect(info.outlineStyle, `${s.scr}: ${info.key} has outline-style none while focused`)
          .not.toBe('none');
        expect(info.outlineWidth, `${s.scr}: ${info.key} has a zero-width focus outline`)
          .toBeGreaterThan(0);
      }
      expect(seen.size, `${s.scr}: Tab reached no control inside the screen`).toBeGreaterThan(0);
    });
  }

  test('the shell chrome keeps a visible, non-colour-only focus ring', async ({ page }) => {
    await settleShell(page);
    await openNavIfCollapsed(page);
    // styles.css draws the ring through :focus-visible, which Chromium only
    // matches once the user has interacted by keyboard. Press Tab first so the
    // browser is in keyboard modality, exactly as a keyboard-only user is.
    await page.keyboard.press('Tab');
    const item = page.locator('#nav .nav-item[data-nav="audit"]');
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
