// tests/vrt/analytics.spec.js
//
// WAVE 7 / A3 — THE ELEVEN ANALYTICS SCREENS AS ROUTES OF THE SPA SHELL.
//
//   SCR-01  Executive CAPEX Dashboard        #analytics-executive
//   SCR-02  Project Controller Workbench     #analytics-controller
//   SCR-04  CAPEX Project List               #analytics-project-list
//   SCR-05  CAPEX Project Object Page        #analytics-project-object
//   SCR-06  WBS Hierarchy Explorer           #analytics-wbs-explorer
//   SCR-07  WBS Tree Table                   #analytics-wbs-tree
//   SCR-08  WBS Element Detail Page          #analytics-wbs-element
//   SCR-19  CWIP Ledger                      #analytics-cwip-ledger
//   SCR-23  Open Commitment Ageing           #analytics-commitment-ageing
//   SCR-24  CWIP Ageing                      #analytics-cwip-ageing
//   SCR-25  Exception and Overrun Monitor    #analytics-exceptions
//
// All eleven are named verbatim in research/30_contracts/C8_screens.json and
// all eleven carry their real number. No SCR was invented here.
//
// THIS SUITE TAKES NO SCREENSHOTS, DELIBERATELY
// ---------------------------------------------
// `toHaveScreenshot` is absent from this file on purpose. Every existing PNG
// under tests/vrt/ is an approved baseline and MUST NOT be regenerated; adding
// eleven new ones would also be a change to that directory, and a baseline
// captured on the same run that wrote the code proves only that the code
// renders what it renders. What is asserted instead is BEHAVIOUR: that each
// screen deep-links on a cold load, mounts, names its source or says what is
// missing, distinguishes its states, keeps its drill-down summing back, and is
// axe-clean. Those are the properties a pixel diff cannot see.
//
// MOST OF THIS RUNS AGAINST THE REAL BACKEND, WITH NO STUB
// --------------------------------------------------------
// Today's build mounts no /api/reports/* and no /api/exports/*. That is not a
// gap to be simulated — it is the state under test. So the deep-link, source,
// unavailable, axe and drill-down tests intercept NOTHING: the screens probe
// the application's own /openapi.json, find the reporting routes absent, fall
// through to the ledger routes that ARE mounted, and render what really
// happens. A fallback proven against a stub proves only that the stub
// answered.
//
// Stubs appear only where a state cannot be produced from the real server:
// a declared scope refusal, a declared staleness, a server fault, a mounted
// reporting service, and a deliberately inconsistent card/row pair.
//
// HOW THESE SCREENS ARE REACHABLE WITHOUT THIS AGENT EDITING app.js OR router.js
// ------------------------------------------------------------------------------
// Route registration and the navigation rail belong to the LEAD. This agent
// owns app/frontend/src/features/analytics/** and neither of those two files,
// so it ships the screens plus manifest.js — which declares, in one place, the
// eleven route entries router.js spreads into SCREENS and the eleven gate rows
// app.js pastes into SCR_ROUTES.
//
// `installRoutes()` performs EXACTLY those two splices in the running page,
// from that manifest, before the shell's first render. So this suite is not
// testing a mock of the wiring: it is testing the wiring, applied from the
// same declaration the lead will apply. `PASTE_ROWS` is the literal
// transcription of the app.js half, and one test asserts it still deep-equals
// the manifest — so a manifest change nobody carried into app.js fails the
// suite rather than shipping.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

/* ---------------- identities ----------------
   Demo credentials are user_id + '!demo', provisioned idempotently by
   auth.provision_dev_identities.

   auth.PERMISSIONS: `budget.read` is held by every role; `budget.check` by
   every role except CapitalisationApprover. Administrator and Auditor are the
   two identities exercised here, and both are POSITIVE controls on a different
   part of the gate — an Auditor is read-only and pinned by
   test_aud_c_006_auditor_is_read_only to a four-permission allow-list, so
   proving an Auditor reaches all eleven proves these are read surfaces and not
   accidentally gated on something administrative. */
const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };
const AUDITOR = { user: 'U-AUD', password: 'U-AUD!demo' };

/* ---------------- the eleven screens under test ---------------- */

const SCREENS = [
  { hash: 'analytics-executive', scr: 'SCR-01', title: 'Executive CAPEX Dashboard', need: 'budget.read' },
  { hash: 'analytics-controller', scr: 'SCR-02', title: 'Project Controller Workbench', need: 'budget.check' },
  { hash: 'analytics-project-list', scr: 'SCR-04', title: 'CAPEX Project List', need: 'budget.read' },
  { hash: 'analytics-project-object', scr: 'SCR-05', title: 'CAPEX Project Object Page', need: 'budget.read' },
  { hash: 'analytics-wbs-explorer', scr: 'SCR-06', title: 'WBS Hierarchy Explorer', need: 'budget.read' },
  { hash: 'analytics-wbs-tree', scr: 'SCR-07', title: 'WBS Tree Table', need: 'budget.read' },
  { hash: 'analytics-wbs-element', scr: 'SCR-08', title: 'WBS Element Detail Page', need: 'budget.read' },
  { hash: 'analytics-cwip-ledger', scr: 'SCR-19', title: 'CWIP Ledger', need: 'budget.read' },
  { hash: 'analytics-commitment-ageing', scr: 'SCR-23', title: 'Open Commitment Ageing', need: 'budget.read' },
  { hash: 'analytics-cwip-ageing', scr: 'SCR-24', title: 'CWIP Ageing', need: 'budget.read' },
  { hash: 'analytics-exceptions', scr: 'SCR-25', title: 'Exception and Overrun Monitor', need: 'budget.check' },
];

/** Screens that need a project id before they can ask for anything. */
const NEEDS_PROJECT = new Set([
  'analytics-project-object', 'analytics-wbs-explorer', 'analytics-wbs-tree',
  'analytics-wbs-element',
]);

/**
 * THE app.js PASTE, transcribed.
 *
 * manifest.js tells the lead to paste exactly these rows into `SCR_ROUTES`.
 * `installRoutes()` applies it to the running page the same way app.js will,
 * and "the app.js paste still matches the manifest" asserts the two have not
 * drifted.
 */
const PASTE_ROWS = [
  { id: 'analytics-executive', ico: '▣', label: 'Executive CAPEX Dashboard', need: ['budget.read'] },
  { id: 'analytics-controller', ico: '◎', label: 'Project Controller Workbench', need: ['budget.check'] },
  { id: 'analytics-project-list', ico: '▤', label: 'CAPEX Project List', need: ['budget.read'] },
  { id: 'analytics-project-object', ico: '▥', label: 'CAPEX Project Object Page', need: ['budget.read'] },
  { id: 'analytics-wbs-explorer', ico: '⌗', label: 'WBS Hierarchy Explorer', need: ['budget.read'] },
  { id: 'analytics-wbs-tree', ico: '▦', label: 'WBS Tree Table', need: ['budget.read'] },
  { id: 'analytics-wbs-element', ico: '⊡', label: 'WBS Element Detail', need: ['budget.read'] },
  { id: 'analytics-cwip-ledger', ico: '₹', label: 'CWIP Ledger', need: ['budget.read'] },
  { id: 'analytics-commitment-ageing', ico: '◷', label: 'Open Commitment Ageing', need: ['budget.read'] },
  { id: 'analytics-cwip-ageing', ico: '◔', label: 'CWIP Ageing', need: ['budget.read'] },
  { id: 'analytics-exceptions', ico: '⚠', label: 'Exception & Overrun Monitor', need: ['budget.check'] },
];

/**
 * Apply the manifest's two splices to the running page, before the first
 * render.
 *
 * TIMING. `addInitScript` runs before any page script, when neither SCR_ROUTES
 * nor V exists yet, so the work is deferred to DOMContentLoaded — which fires
 * after app.js's top-level code and before its bootstrap finishes its first
 * `await api('/health')`. The routes are therefore in place before `render()`
 * ever reads them.
 *
 * The SYNCHRONOUS half is the gate rows: `viewAllowed()` runs synchronously
 * inside render(), and a route missing from SCR_ROUTES at that moment is
 * corrected to home. The build is deferred behind a dynamic import of the
 * manifest, exactly as router.js defers its own feature modules.
 */
async function installRoutes(page) {
  await page.addInitScript((rows) => {
    document.addEventListener('DOMContentLoaded', () => {
      for (const row of rows) {
        // eslint-disable-next-line no-undef
        if (!SCR_ROUTES.some((r) => r.id === row.id)) SCR_ROUTES.push(row);
        // eslint-disable-next-line no-undef
        V[row.id] = async () => {
          const mod = await import('/static/src/features/analytics/manifest.js');
          const screen = mod.analyticsScreenById(row.id);
          // eslint-disable-next-line no-undef
          setHeader(screen.title, screen.crumbs, []);
          return screen.build();
        };
      }
      window.__analyticsRoutesInstalled = rows.length;
    });
  }, PASTE_ROWS);
}

/* ---------------- helpers ---------------- */

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

/**
 * Declare which paths this build "mounts".
 *
 * The screens read /openapi.json to tell an unmounted route from an empty
 * result, so the schema IS the switch that selects available / unavailable /
 * fallback behaviour. Stubbing it is stubbing the thing the code reads, not a
 * proxy for it.
 */
async function routeOpenApi(page, paths) {
  const schema = { openapi: '3.1.0', info: { title: 'stub', version: '0' }, paths: {} };
  for (const p of paths) schema.paths[p] = { get: {}, post: {} };
  await routeJson(page, '**/openapi.json', schema);
}

/** The reporting and export surfaces, as A1 and A2 will publish them. */
const REPORT_PATHS = [
  '/api/reports/{report_id}',
  '/api/reports/saved-views',
  '/api/reports/export',
  '/api/exports',
  '/api/exports/{job_id}',
  '/api/dashboard',
  '/api/projects/{project_id}/wbs',
  '/api/reconciliation',
  '/api/bills',
];

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
  await page.waitForFunction(() => window.__analyticsRoutesInstalled === 11, null, { timeout: 15_000 });
}

async function settleShell(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 20_000 });
}

/**
 * Wait for a screen to be MOUNTED, not merely present.
 *
 * app.js appends the host node BEFORE it awaits mount(), so `.scr-host`
 * appears while the screen is still empty. `data-mounted` is the signal, and
 * `data-mount-failed` ends the wait too: a load failure diagnosed as a load
 * failure is worth far more than the same failure arriving as a twenty-second
 * timeout.
 */
async function settleScreen(page) {
  await settleShell(page);
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 20_000 });
  const failed = page.locator('#content .scr-host[data-mount-failed="1"]');
  if (await failed.count()) {
    throw new Error(`the screen's mount() threw, so it never rendered: ${(await failed.innerText()).trim()}`);
  }
  await page.waitForFunction(() => {
    const host = document.querySelector('#content .scr-host[data-mounted="1"]');
    if (!host) return false;
    if (host.querySelector('.audit-skel-row')) return false;
    if (host.querySelector('.loading')) return false;
    // Every loader on the screen must have left `loading`.
    return [...host.querySelectorAll('.analytics-loader')]
      .every((el) => el.getAttribute('data-analytics-state') !== 'loading');
  }, null, { timeout: 20_000 });
  await page.waitForLoadState('networkidle');
}

/** Deep-link on a COLD LOAD: a full page.goto to the hash, not an in-app click. */
async function gotoScreen(page, hash, search = '') {
  await page.goto(`/${search ? `?${search}` : ''}#${hash}`);
  await page.waitForFunction(() => window.__analyticsRoutesInstalled === 11, null, { timeout: 15_000 });
  await settleScreen(page);
}

function pageTitle(page) { return page.locator('#pageTitle'); }

/** The states every loader on the screen settled into. */
function loaderStates(page) {
  return page.evaluate(() => [...document.querySelectorAll('#content .analytics-loader')]
    .map((el) => el.getAttribute('data-analytics-state')));
}

/** A project id from the real seeded data, for the four project-scoped screens. */
async function seededProject(page) {
  const data = await page.evaluate(async () => {
    const r = await fetch('/api/dashboard', { headers: { Accept: 'application/json' } });
    if (!r.ok) return null;
    return r.json();
  });
  expect(data && Array.isArray(data.projects) && data.projects.length,
    'the seeded demo database returned no project, so the project-scoped screens cannot be exercised')
    .toBeTruthy();
  return data.projects[0];
}

/* ====================================================== routes and mounting == */

test.describe('Wave 7 analytics screens — routes, and nothing in the rail', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await signIn(page);
  });

  test('the app.js paste still matches the manifest', async ({ page }) => {
    // A manifest edit nobody carried into app.js fails HERE rather than
    // shipping as a route that deep-links to a screen the gate refuses.
    const fromManifest = await page.evaluate(async () => {
      const mod = await import('/static/src/features/analytics/manifest.js');
      return mod.ANALYTICS_NAV_ROWS;
    });
    expect(fromManifest).toEqual(PASTE_ROWS);
  });

  test('every screen carries a real C8 number and none was invented', async ({ page }) => {
    const declared = await page.evaluate(async () => {
      const mod = await import('/static/src/features/analytics/manifest.js');
      return mod.ANALYTICS_SCREENS.map((s) => ({ id: s.id, scr: s.scr, title: s.title }));
    });
    expect(declared.map((d) => d.id)).toEqual(SCREENS.map((s) => s.hash));
    for (const d of declared) {
      expect(d.scr, `${d.id} carries no C8 screen id`).toMatch(/^SCR-\d{2}$/);
      // C8_screens.json is frozen at forty. A number above it would be
      // manufactured traceability, which is the specific mistake this catches.
      expect(Number(d.scr.slice(4))).toBeLessThanOrEqual(40);
    }
    expect(new Set(declared.map((d) => d.scr)).size, 'two screens claim the same C8 id')
      .toBe(declared.length);
  });

  test('not one of the eleven appears in the primary navigation', async ({ page }) => {
    // The rail is inside every client-approved screenshot. Eleven rows is
    // 330px against a rail that already overflows by 127px at laptop-1024, so
    // nothing here asks for one — and this measures that rather than asserting
    // it in prose.
    await settleShell(page);
    const ids = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    for (const s of SCREENS) {
      expect(ids, `${s.scr} is a route, not a rail entry`).not.toContain(s.hash);
    }
    expect(ids[0]).toBe('home');
  });

  for (const screen of SCREENS) {
    test(`${screen.scr} ${screen.hash} — deep-links on a cold load and mounts`, async ({ page }) => {
      // A COLD LOAD: a full navigation to the hash, not an in-app click. This
      // is the property a bookmark, a pasted link and an email link all
      // depend on, and the one a screen that keeps its state in a module
      // variable silently loses.
      await gotoScreen(page, screen.hash);
      await expect(pageTitle(page)).toHaveText(screen.title);
      await expect(page.locator('#content .scr-host[data-mounted="1"]')).toBeAttached();
      // The live region every announcer binds to must exist BEFORE mount, or
      // announce() silently does nothing for the life of the screen.
      await expect(page.locator('#analyticsLiveRegion')).toBeAttached();
    });
  }
});

/* ================================================ source, freshness, states == */

test.describe('Wave 7 analytics screens — no data without its provenance', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await signIn(page);
  });

  for (const screen of SCREENS) {
    test(`${screen.scr} — names a source, or says what is missing, never a bare empty state`,
      async ({ page }) => {
        // Against the REAL server, with nothing intercepted. Today that means
        // the reporting routes are genuinely absent and the ledger routes are
        // genuinely mounted, which is the state under test rather than a
        // simulation of it.
        let search = '';
        if (NEEDS_PROJECT.has(screen.hash)) {
          // The four project-scoped screens are UNSTARTED without one, which
          // is a real state and is tested elsewhere. Here the question is
          // whether a screen that HAS been asked something names its source.
          const project = await seededProject(page);
          search = `project=${encodeURIComponent(project.project_id)}`;
        }
        await gotoScreen(page, screen.hash, search);

        const evidence = await page.evaluate(() => {
          const host = document.querySelector('#content .scr-host');
          return {
            sources: [...host.querySelectorAll('.analytics-source')].map((el) => el.dataset.source),
            freshness: [...host.querySelectorAll('.analytics-freshness')]
              .map((el) => el.dataset.freshness),
            unavailable: host.querySelectorAll('.analytics-unavailable').length,
            denied: host.querySelectorAll('.analytics-scope-denied').length,
            unstarted: [...host.querySelectorAll('[data-state="unstarted"]')].length,
            bareEmpty: [...host.querySelectorAll('.empty')].length,
            states: [...host.querySelectorAll('.analytics-loader')]
              .map((el) => el.getAttribute('data-analytics-state')),
          };
        });

        // EITHER a named source, OR an explicit unavailable / denied block.
        // Never neither: a screen showing nothing and explaining nothing is
        // the exact failure this suite exists to prevent.
        const explained = evidence.sources.length > 0
          || evidence.unavailable > 0
          || evidence.denied > 0;
        expect(explained,
          `${screen.scr} rendered neither a named source nor an explicit unavailable state`)
          .toBe(true);

        // Every named source is one this build actually has. An unrecognised
        // source name would render no line at all, so this also proves the
        // source vocabulary and the renderer agree.
        for (const source of evidence.sources) {
          expect(['reports', 'ledger', 'exports']).toContain(source);
        }

        // Freshness is rendered wherever a source is. "not reported" is an
        // acceptable answer; silence is not.
        if (evidence.sources.length) {
          expect(evidence.freshness.length,
            `${screen.scr} named a source and rendered no freshness line`)
            .toBeGreaterThan(0);
          for (const f of evidence.freshness) {
            expect(['fresh', 'stale', 'unknown']).toContain(f);
          }
        }

        // No loader is left in `loading`, and none is in a state outside the
        // declared set.
        const declared = await page.evaluate(async () => {
          const mod = await import('/static/src/features/analytics/analytics-screen.js');
          return mod.STATES;
        });
        for (const state of evidence.states) {
          expect([...declared, 'unstarted'],
            `${screen.scr} settled into an undeclared state "${state}"`).toContain(state);
          expect(state, `${screen.scr} is still loading after it settled`).not.toBe('loading');
        }
      });
  }

  test('SCR-23 and SCR-24 show a measured total AND name the missing ageing report',
    async ({ page }) => {
      // The shape this build must produce: one loader ready with a real
      // server-computed figure, one loader unavailable naming the route. If
      // either collapsed into the other the screen would be dishonest in one
      // of the two directions the contract names.
      for (const hash of ['analytics-commitment-ageing', 'analytics-cwip-ageing']) {
        await gotoScreen(page, hash);
        const states = await loaderStates(page);
        expect(states.length, `${hash} should carry two independent loaders`).toBe(2);
        expect(states, `${hash} lost its unavailable state for the ageing distribution`)
          .toContain('unavailable');

        const named = await page.locator('#content .analytics-unavailable code').first().innerText();
        expect(named, `${hash} did not name the missing route`).toContain('/api/reports/');

        // And it does NOT offer a retry: retrying cannot succeed until A1
        // mounts the endpoint, and a button that invites it is a lie about
        // what the operator can do.
        const retryInUnavailable = await page.evaluate(() => {
          const block = document.querySelector('#content .analytics-unavailable');
          return block ? block.querySelectorAll('button').length : -1;
        });
        expect(retryInUnavailable, 'the unavailable block offered a retry').toBe(0);
      }
    });

  test('an unavailable state is never rendered as an empty one', async ({ page }) => {
    await gotoScreen(page, 'analytics-cwip-ageing');
    const text = await page.locator('#content .scr-host').innerText();
    // The empty wording would be a claim about the data. The unavailable
    // wording is a claim about the build. Only one of them is true today.
    expect(text).toContain('not available in this build');
    expect(text).not.toContain('The report returned no ageing bucket');
  });
});

/* ============================================================== permissions == */

test.describe('Wave 7 analytics screens — permission gates', () => {
  test('an Administrator reaches all eleven, and they render', async ({ page }) => {
    await installRoutes(page);
    await signIn(page, ADMIN);
    const verdicts = await page.evaluate((all) => Object.fromEntries(
      // eslint-disable-next-line no-undef
      all.map((hash) => [hash, viewAllowed(hash)]),
    ), SCREENS.map((s) => s.hash));
    for (const s of SCREENS) {
      expect(verdicts[s.hash], `an Administrator must reach ${s.scr}`).toBe(true);
    }
    await gotoScreen(page, 'analytics-controller');
    await expect(pageTitle(page)).toHaveText('Project Controller Workbench');
  });

  test('an Auditor reaches all eleven — these are read surfaces, not administrative ones',
    async ({ page }) => {
      // THE SECOND ROLE, AND A POSITIVE CONTROL ON A DIFFERENT PROPERTY.
      // test_aud_c_006_auditor_is_read_only pins Auditor to a four-permission
      // allow-list. An Auditor reaching all eleven proves these screens are
      // gated on the financial read permissions they claim, and not on
      // anything administrative that crept in.
      await installRoutes(page);
      await signIn(page, AUDITOR);
      const verdicts = await page.evaluate((all) => Object.fromEntries(
        // eslint-disable-next-line no-undef
        all.map((hash) => [hash, viewAllowed(hash)]),
      ), SCREENS.map((s) => s.hash));
      for (const s of SCREENS) {
        expect(verdicts[s.hash], `an Auditor holds ${s.need} and must reach ${s.scr}`).toBe(true);
      }

      // And the SAME gate genuinely refuses something this identity lacks, so
      // the eleven trues above are not eleven trues from a gate that says yes
      // to everything. `approval.configure` is Administrator-only and an
      // Auditor does not hold it.
      const refused = await page.evaluate(() => ({
        // eslint-disable-next-line no-undef
        matrix: viewAllowed('approval-matrix'),
        // eslint-disable-next-line no-undef
        nonsense: viewAllowed('analytics-does-not-exist'),
      }));
      expect(refused.matrix, 'the gate admitted a screen this identity may not see').toBe(false);
      expect(refused.nonsense, 'the gate admitted an unknown hash').toBe(false);

      await page.goto('/#approval-matrix');
      await settleShell(page);
      await expect(pageTitle(page)).not.toHaveText('Approval Matrix Configuration');
    });

  test('the two budget.check screens are actually gated on budget.check', async ({ page }) => {
    // A REAL NEGATIVE ON THE REAL GATE.
    //
    // No seeded identity lacks `budget.check`: auth.DEV_USERS has no
    // CapitalisationApprover-only user, and every other role holds it. So the
    // negative cannot be produced by signing in as somebody — it is produced
    // by removing the permission from the shell's own permission set and
    // asking the SAME `viewAllowed()` the same question about the SAME route
    // rows. Nothing about the gate is simulated; only the permission set is
    // narrowed, and it is restored afterwards and re-verified.
    //
    // This is reported as a limitation of the seeded identity set rather than
    // hidden: a CapitalisationApprover-only demo user would let this be an
    // ordinary sign-in test.
    await installRoutes(page);
    await signIn(page, ADMIN);

    const result = await page.evaluate((hashes) => {
      // eslint-disable-next-line no-undef
      const had = S.perms.has('budget.check');
      // eslint-disable-next-line no-undef
      S.perms.delete('budget.check');
      // eslint-disable-next-line no-undef
      const without = Object.fromEntries(hashes.map((h) => [h, viewAllowed(h)]));
      // eslint-disable-next-line no-undef
      if (had) S.perms.add('budget.check');
      // eslint-disable-next-line no-undef
      const restored = Object.fromEntries(hashes.map((h) => [h, viewAllowed(h)]));
      return { had, without, restored };
    }, SCREENS.map((s) => s.hash));

    expect(result.had, 'U-ADM should hold budget.check').toBe(true);
    for (const s of SCREENS) {
      if (s.need === 'budget.check') {
        expect(result.without[s.hash], `${s.scr} claims budget.check but was admitted without it`)
          .toBe(false);
      } else {
        expect(result.without[s.hash], `${s.scr} is gated on budget.read and must survive`)
          .toBe(true);
      }
      expect(result.restored[s.hash], `${s.scr} did not recover when the permission returned`)
        .toBe(true);
    }
  });
});

/* ===================================================== the four extra states = */

test.describe('Wave 7 analytics screens — four states that never collapse', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('DENIED SCOPE is not rendered as NO DATA', async ({ page }) => {
    // THE MOST DANGEROUS COLLAPSE IN THE APPLICATION. An empty exception
    // monitor reads as "there are no overruns". If the reason it is empty is
    // that the caller's scope reaches none of them, that reading is the
    // opposite of the truth.
    await routeJson(page, '**/api/dashboard**', {
      scope_denied: true,
      missing_grant: 'You hold no scope grant for any entity in this filter.',
      projects: [],
      totals: null,
      alerts: {},
    });
    await signIn(page);
    await gotoScreen(page, 'analytics-exceptions');

    expect(await loaderStates(page)).toContain('denied');
    const host = page.locator('#content .scr-host');
    await expect(host.locator('.analytics-scope-denied')).toBeVisible();
    const text = await host.innerText();
    expect(text).toContain('access scope reaches none of it');
    // It must NOT say the measured-none sentence, which is the empty state.
    expect(text).not.toContain('measured "none"');
    // And it must not imply zero.
    expect(text).toContain('no figure of zero should be inferred');
  });

  test('NO DATA says it is a measurement', async ({ page }) => {
    await routeJson(page, '**/api/dashboard**', {
      projects: [],
      totals: null,
      alerts: {
        budget_exceptions: [],
        over_billed_lines: [],
        received_not_billed_lines: [],
        pending_revisions: [],
        awaiting_capitalisation: [],
        reconciliation_exceptions: [],
      },
    });
    await signIn(page);
    await gotoScreen(page, 'analytics-exceptions');

    expect(await loaderStates(page)).toContain('empty');
    const text = await page.locator('#content .scr-host').innerText();
    expect(text).toContain('measured "none"');
    expect(text).not.toContain('access scope reaches none of it');
  });

  test('STALE data is shown AND marked', async ({ page }) => {
    // Hiding a labelled old figure helps nobody; showing it unlabelled is the
    // defect. So it is both rendered and flagged, and the state is its own.
    await routeJson(page, '**/api/dashboard**', {
      meta: {
        as_of: '2026-09-01T00:00:00Z',
        stale: true,
        stale_reason: 'The nightly rollup did not complete.',
      },
      projects: [{
        project_id: 'P-STALE', capex_code: 'CX-STALE', name: 'Stale project',
        entity: 'E', plant: 'PL', status: 'RELEASED',
        budget: 100000, commitment: 20000, actual: 30000, available: 50000,
        exposure: 50000, received_not_billed: 0, pr_reserved: 0,
        ordered: 20000, received: 0, original: 100000, revisions: 0,
      }],
      totals: {
        budget: 100000, commitment: 20000, actual: 30000, available: 50000,
        exposure: 50000, received_not_billed: 0, pr_reserved: 0,
        ordered: 20000, received: 0, original: 100000, revisions: 0,
      },
      alerts: {},
    });
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');

    expect(await loaderStates(page)).toContain('stale');
    const host = page.locator('#content .scr-host');
    await expect(host.locator('.analytics-stale')).toBeVisible();
    await expect(host.locator('.analytics-freshness[data-freshness="stale"]')).toBeAttached();
    // The data is still there. A stale screen that hid its figures would be a
    // different failure, not a fix.
    expect(await host.innerText()).toContain('CX-STALE');
  });

  test('SYSTEM FAILURE is an error with a retry, not an empty table', async ({ page }) => {
    await page.route('**/api/dashboard**', (route) => route.fulfill({
      status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'boom' }),
    }));
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');

    expect(await loaderStates(page)).toContain('error');
    const host = page.locator('#content .scr-host');
    await expect(host.locator('.msg-error')).toBeVisible();
    await expect(host.locator('.msg-error button', { hasText: 'Retry' })).toBeVisible();
    const text = await host.innerText();
    expect(text).not.toContain('measured "none"');
  });

  test('a BARE 503 is a fault; a 503 declaring unavailable falls through', async ({ page }) => {
    // The seam rule, exercised in both directions on one screen. Treating
    // every 503 as "not built yet" would give a broken reporting service a
    // permanently calm face; treating a declared unavailability as a fault
    // would put a red error on a build that is simply incomplete.
    await routeOpenApi(page, REPORT_PATHS);

    // 1. Declared unavailable — falls through to the ledger, which answers.
    await page.route('**/api/reports/**', (route) => route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: { unavailable: true, missing: 'No reporting database is configured.' },
      }),
    }));
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');
    let states = await loaderStates(page);
    expect(states, 'a declared unavailability should have fallen through to the ledger')
      .not.toContain('error');
    await expect(page.locator('#content .analytics-source[data-source="ledger"]')).toBeAttached();

    // 2. Bare 503 — a real fault, rendered as one.
    await page.unroute('**/api/reports/**');
    await page.route('**/api/reports/**', (route) => route.fulfill({
      status: 503, contentType: 'text/plain', body: 'upstream unavailable',
    }));
    await gotoScreen(page, 'analytics-executive');
    states = await loaderStates(page);
    expect(states, 'a bare 503 was swallowed as "not built yet"').toContain('error');
  });

  test('a mounted reporting service is used, and named as the reporting service',
    async ({ page }) => {
      await routeOpenApi(page, REPORT_PATHS);
      await routeJson(page, '**/api/reports/portfolio-summary**', {
        meta: { as_of: '2026-09-08T06:00:00Z' },
        rows: [{
          project_id: 'P-1', capex_code: 'CX-1', name: 'One', entity: 'E', plant: 'PL',
          status: 'RELEASED',
          budget: 500000, commitment: 100000, actual: 200000, available: 200000,
          exposure: 300000, received_not_billed: 0, pr_reserved: 0,
          ordered: 100000, received: 0, original: 500000, revisions: 0,
        }],
        totals: {
          budget: 500000, commitment: 100000, actual: 200000, available: 200000,
          exposure: 300000, received_not_billed: 0, pr_reserved: 0,
          ordered: 100000, received: 0, original: 500000, revisions: 0,
        },
      });
      await signIn(page);
      await gotoScreen(page, 'analytics-executive');

      await expect(page.locator('#content .analytics-source[data-source="reports"]')).toBeAttached();
      const line = await page.locator('#content .analytics-source').first().innerText();
      expect(line).toContain('/api/reports/portfolio-summary');
      // And the freshness the report declared is rendered, not the fetch time.
      await expect(page.locator('#content .analytics-freshness[data-freshness="fresh"]'))
        .toBeAttached();
    });

  test('a ledger fallback names the filters it could NOT apply', async ({ page }) => {
    // A figure narrowed by three of nine filters, presented as though it had
    // been narrowed by nine, is a wrong number wearing a right number's
    // clothes. The dropped dimensions are named individually.
    await signIn(page);
    await gotoScreen(page, 'analytics-executive', 'vendor=V-1&head=BH-1&category=C-1');

    await expect(page.locator('#content .analytics-source[data-source="ledger"]')).toBeAttached();
    const warning = page.locator('#content .analytics-unapplied');
    await expect(warning).toBeVisible();
    const text = await warning.innerText();
    expect(text).toContain('Vendor');
    expect(text).toContain('Budget head');
    // The chips mark the same filters as dropped, so the warning and the chip
    // row cannot disagree.
    await expect(page.locator('#content .analytics-chip-dropped').first()).toBeVisible();
  });
});

/* ================================================ the drill-down round trip = */

test.describe('Wave 7 analytics screens — every metric drills down, and sums back', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('every card is a link, and its href carries the same FilterSet plus the dimension',
    async ({ page }) => {
      await signIn(page);
      await gotoScreen(page, 'analytics-executive', 'plant=PL-1&from=2026-04-01');

      const cards = await page.evaluate(() => [...document.querySelectorAll('#content .analytics-tile')]
        .map((tile) => {
          const link = tile.querySelector('a.analytics-drill');
          return {
            metric: tile.dataset.metric,
            href: link ? link.getAttribute('href') : null,
            noDrill: !!tile.querySelector('.analytics-no-drill'),
          };
        }));

      expect(cards.length, 'the executive dashboard rendered no metric cards').toBeGreaterThan(0);
      for (const card of cards) {
        expect(card.noDrill, `${card.metric} rendered no drill-down at all`).toBe(false);
        expect(card.href, `${card.metric} has no drill-down href`).toBeTruthy();
        // THE SAME FILTERS. Not a subset, not a fresh query.
        expect(card.href, `${card.metric} dropped the plant filter on the way down`)
          .toContain('plant=PL-1');
        expect(card.href, `${card.metric} dropped the date filter on the way down`)
          .toContain('from=2026-04-01');
        // Plus the clicked dimension, so the target knows what was clicked.
        expect(card.href, `${card.metric} carries no metric identity`).toContain('metric=');
      }
    });

  test('the card figure equals the sum of the rows behind it, and the check is rendered',
    async ({ page }) => {
      await signIn(page);
      await gotoScreen(page, 'analytics-executive');
      // Against the REAL ledger: /api/dashboard's totals are the sum of its
      // own project rows, so this is a genuine round trip and not a stub
      // agreeing with itself.
      const reconciled = page.locator('#content .analytics-reconciled[data-reconciled="true"]');
      await expect(reconciled).toBeVisible();
      expect(await reconciled.innerText()).toContain('exactly the sum');
      // No data-quality failure anywhere on a correct source.
      await expect(page.locator('#content .analytics-data-quality[data-reconciled="false"]'))
        .toHaveCount(0);
    });

  test('a card that does NOT equal its rows raises a data-quality state', async ({ page }) => {
    // The check has to be able to FAIL, or it is decoration. The total here is
    // deliberately 1,00,000 paise larger than the single row beneath it.
    await routeJson(page, '**/api/dashboard**', {
      projects: [{
        project_id: 'P-1', capex_code: 'CX-1', name: 'One', entity: 'E', plant: 'PL',
        status: 'RELEASED',
        budget: 400000, commitment: 0, actual: 0, available: 400000, exposure: 0,
        received_not_billed: 0, pr_reserved: 0, ordered: 0, received: 0,
        original: 400000, revisions: 0,
      }],
      totals: {
        budget: 500000, commitment: 0, actual: 0, available: 400000, exposure: 0,
        received_not_billed: 0, pr_reserved: 0, ordered: 0, received: 0,
        original: 500000, revisions: 0,
      },
      alerts: {},
    });
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');

    const block = page.locator('#content .analytics-data-quality[data-reconciled="false"]');
    await expect(block.first()).toBeVisible();
    const text = await block.first().innerText();
    expect(text).toContain('does not equal the rows behind it');
    expect(text).toContain('a difference of');
    // AND THE CARD STILL SHOWS THE SERVER'S FIGURE. Quietly redrawing it as
    // the sum of the rows would erase the evidence and leave a plausible
    // number in its place.
    const budgetCard = page.locator('#content .analytics-tile[data-metric="budget"]');
    expect(await budgetCard.innerText()).toContain('5,000.00');
  });

  test('a check that could not run is reported as unchecked, never as a pass', async ({ page }) => {
    await routeJson(page, '**/api/dashboard**', {
      projects: [{
        project_id: 'P-1', capex_code: 'CX-1', name: 'One', entity: 'E', plant: 'PL',
        status: 'RELEASED',
      }],
      totals: null,
      alerts: {},
    });
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');

    // Totals absent -> every card is "not reported" and every check is
    // unchecked. Neither is a zero and neither is a pass.
    await expect(page.locator('#content .analytics-tile[data-reported="false"]').first())
      .toBeVisible();
    const notReported = await page.locator('#content .analytics-not-reported').first().innerText();
    expect(notReported).toContain('not reported');
    const host = await page.locator('#content .scr-host').innerText();
    expect(host).not.toContain('₹0.00');
    await expect(page.locator('#content .analytics-data-quality[data-reconciled="unchecked"]').first())
      .toBeVisible();
  });

  test('a WBS total is checked against the ROOT rows, not against every row', async ({ page }) => {
    // Summing every row of a tree double counts every ancestor and would fail
    // on a perfectly correct hierarchy. This asserts the check passes on the
    // real seeded tree, which is the only way to tell the two apart.
    await signIn(page);
    const project = await seededProject(page);
    await gotoScreen(page, 'analytics-wbs-tree', `project=${encodeURIComponent(project.project_id)}`);

    await expect(page.locator('#content .analytics-tree-row').first()).toBeVisible();
    const reconciled = page.locator('#content .analytics-reconciled[data-reconciled="true"]');
    await expect(reconciled).toBeVisible();
    expect(await reconciled.innerText()).toContain('root element');

    // And the tree renders NO column total, because one would double count.
    const footers = await page.locator('#content .analytics-tree-row').count();
    expect(footers).toBeGreaterThan(0);
    await expect(page.locator('#content table tfoot')).toHaveCount(0);
  });
});

/* ================================================================== money === */

test.describe('Wave 7 analytics screens — money is integer paise', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('the metrics module refuses a fractional or unsafe value', async ({ page }) => {
    await signIn(page);
    const result = await page.evaluate(async () => {
      const m = await import('/static/src/features/analytics/analytics-metrics.js');
      const out = {};
      try { m.paise(12345.67); out.fractional = 'accepted'; } catch (e) { out.fractional = e.name; }
      try { m.paise(Number.MAX_SAFE_INTEGER + 2); out.unsafe = 'accepted'; } catch (e) { out.unsafe = e.name; }
      out.nullStaysNull = m.paise(null) === null;
      out.zeroIsZero = m.paise(0) === 0;
      // Exactness above 2^53 is why the accumulator is BigInt.
      out.bigSum = m.sumPaise([4503599627370496, 1, 1]).total;
      // Integer basis points, not a float quotient.
      out.bp = m.basisPoints(1, 3);
      // A width assembled from integer digits.
      out.width = m.bpWidth(9999);
      // The classic float failure, which must not be reachable from paise.
      out.formatted = m.inr(30);
      return out;
    });
    expect(result.fractional).toBe('TypeError');
    expect(result.unsafe).toBe('TypeError');
    expect(result.nullStaysNull).toBe(true);
    expect(result.zeroIsZero).toBe(true);
    expect(result.bigSum).toBe(4503599627370498);
    expect(result.bp).toBe(3333);          // truncated, not rounded to 3333.33
    expect(result.width).toBe('99.99%');
    expect(result.formatted).toBe('₹0.30');
  });

  test('no rendered figure is NaN, Infinity or a bare float', async ({ page }) => {
    await signIn(page);
    for (const hash of ['analytics-executive', 'analytics-project-list', 'analytics-cwip-ledger']) {
      await gotoScreen(page, hash);
      const text = await page.locator('#content .scr-host').innerText();
      expect(text, `${hash} rendered NaN`).not.toContain('NaN');
      expect(text, `${hash} rendered Infinity`).not.toContain('Infinity');
      expect(text, `${hash} rendered undefined`).not.toContain('undefined');
      // Every rupee figure carries exactly two decimals — a float leaking
      // through would show more, or fewer.
      const amounts = text.match(/₹[\d,]+\.\d+/g) || [];
      for (const amount of amounts) {
        expect(amount, `${hash} rendered ${amount}, which is not two decimal places`)
          .toMatch(/₹[\d,]+\.\d{2}$/);
      }
    }
  });
});

/* ============================================================ filter cascade = */

test.describe('Wave 7 analytics screens — one FilterSet, cascading', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('filters survive a cold load and are stated on screen', async ({ page }) => {
    await signIn(page);
    await gotoScreen(page, 'analytics-project-list', 'plant=PL-1&entity=E-1&from=2026-04-01');
    const chips = await page.locator('#content .analytics-chips').innerText();
    expect(chips).toContain('Plant: PL-1');
    expect(chips).toContain('Entity: E-1');
    expect(chips).toContain('From: 2026-04-01');
    // And the inputs are populated from the URL, so Apply does not silently
    // widen the query by re-reading empty boxes.
    expect(await page.locator('#plist-plant').inputValue()).toBe('PL-1');
    expect(await page.locator('#plist-entity').inputValue()).toBe('E-1');
  });

  test('the export carries the same FilterSet, or is disabled and says why', async ({ page }) => {
    await signIn(page);
    await gotoScreen(page, 'analytics-project-list', 'plant=PL-1');
    // No export route is mounted in this build, so the control must be
    // disabled with the reason ON it rather than failing when pressed.
    const button = page.locator('#content .analytics-export');
    await expect(button).toBeVisible();
    await expect(button).toBeDisabled();
    expect(await button.getAttribute('data-export')).toBe('unavailable');
    expect(await button.getAttribute('title')).toContain('No export endpoint is mounted');
  });

  test('the export control becomes live when an export route is mounted', async ({ page }) => {
    await routeOpenApi(page, REPORT_PATHS);
    await signIn(page);
    await gotoScreen(page, 'analytics-project-list');
    const button = page.locator('#content .analytics-export');
    await expect(button).toBeEnabled();
    expect(await button.getAttribute('data-export')).toBe('available');
  });

  test('the filter round trip is lossless and deterministic', async ({ page }) => {
    await signIn(page);
    const result = await page.evaluate(async () => {
      const f = await import('/static/src/features/analytics/analytics-filters.js');
      const search = 'entity=E1,E2&plant=P1&project=PR1&wbs=W.1&head=BH1&category=C1'
        + '&vendor=V1&item=I1&doctype=PO,BILL&lifecycle=RELEASED&approval=APPROVED'
        + '&from=2026-04-01&to=2026-09-30&period=FY27-Q1&group=plant,vendor';
      const parsed = f.readFilters(search);
      const written = f.writeFilters(parsed);
      const reparsed = f.readFilters(written);
      return {
        lossless: JSON.stringify(parsed) === JSON.stringify(reparsed),
        stable: written === f.writeFilters(reparsed),
        entity: parsed.entity_ids,
        doctypes: parsed.document_types,
        // A drill-down carries the same set plus the clicked dimension, and
        // deliberately drops the cursor: paging into the middle of a different
        // result set produces rows that cannot sum back to anything.
        drill: f.drilldownHref('analytics-cwip-ledger', { ...parsed, cursor: 'abc' }, {
          metric: 'actual', dimension: 'vendor', narrowKey: 'vendor_ids', narrowValue: 'V9',
        }),
      };
    });
    expect(result.lossless, 'a filter was lost in the URL round trip').toBe(true);
    expect(result.stable, 'the same FilterSet serialised two different ways').toBe(true);
    expect(result.entity).toEqual(['E1', 'E2']);
    expect(result.doctypes).toEqual(['PO', 'BILL']);
    expect(result.drill).toContain('vendor=V1%2CV9');
    expect(result.drill).toContain('metric=actual');
    expect(result.drill).not.toContain('cursor=');
  });
});

/* ============================================================ accessibility = */

test.describe('Wave 7 analytics screens — axe-core clean', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await signIn(page);
  });

  for (const screen of SCREENS) {
    test(`${screen.scr} ${screen.hash} — axe-core reports no violation`, async ({ page }) => {
      const search = NEEDS_PROJECT.has(screen.hash)
        ? `project=${encodeURIComponent((await seededProject(page)).project_id)}`
        : '';
      await gotoScreen(page, screen.hash, search);
      const results = await new AxeBuilder({ page })
        .include('#content')
        .analyze();
      const summary = results.violations.map(
        (v) => `${v.id} (${v.impact}) x${v.nodes.length}: ${v.help}`,
      );
      expect(summary, `${screen.scr} has accessibility violations`).toEqual([]);
    });
  }

  test('the WBS tree exposes its hierarchy to assistive technology', async ({ page }) => {
    // Indentation is visual. aria-level is what a screen reader reads, and a
    // tree whose depth exists only in pixels is a flat list to anyone not
    // looking at it.
    const project = await seededProject(page);
    await gotoScreen(page, 'analytics-wbs-tree', `project=${encodeURIComponent(project.project_id)}`);
    const rows = await page.evaluate(() => [...document.querySelectorAll('#content .analytics-tree-row')]
      .map((tr) => ({
        level: tr.getAttribute('aria-level'),
        posinset: tr.getAttribute('aria-posinset'),
        setsize: tr.getAttribute('aria-setsize'),
      })));
    expect(rows.length, 'the tree rendered no rows').toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.level, 'a tree row carries no aria-level').toBeTruthy();
      expect(row.posinset, 'a tree row carries no aria-posinset').toBeTruthy();
      expect(row.setsize, 'a tree row carries no aria-setsize').toBeTruthy();
    }
  });

  test('expand and collapse are real buttons, reachable by keyboard', async ({ page }) => {
    const project = await seededProject(page);
    await gotoScreen(page, 'analytics-wbs-explorer', `project=${encodeURIComponent(project.project_id)}`);
    const toggle = page.locator('#content .tree-toggle:not(.leaf)').first();
    if (await toggle.count()) {
      await expect(toggle).toHaveAttribute('aria-expanded', /true|false/);
      const before = await toggle.getAttribute('aria-expanded');
      await toggle.focus();
      await page.keyboard.press('Enter');
      await expect(toggle).not.toHaveAttribute('aria-expanded', before);
    }
  });

  test('no screen emits a style attribute — the CSP forbids it', async ({ page }) => {
    // `style-src 'self'` blocks a markup style attribute outright, and
    // core/dom.js's h() throws on one. Geometry goes through the CSSOM, which
    // CSP does not govern, so the computed widths ARE present as inline
    // cssText while no `style="…"` was ever written into markup. What is
    // asserted is the observable consequence: nothing failed to lay out, and
    // no CSP violation was reported.
    const violations = [];
    page.on('console', (msg) => {
      if (/Content Security Policy/i.test(msg.text())) violations.push(msg.text());
    });
    const project = await seededProject(page);
    await gotoScreen(page, 'analytics-wbs-tree', `project=${encodeURIComponent(project.project_id)}`);
    // The indentation really was applied, through the CSSOM.
    const widths = await page.evaluate(
      () => [...document.querySelectorAll('#content .analytics-tree-indent')]
        .map((el) => el.style.width),
    );
    expect(widths.length).toBeGreaterThan(0);
    expect(widths.some((w) => w && w !== '0px'), 'no tree row was indented').toBe(true);
    expect(violations, 'a Content-Security-Policy violation was reported').toEqual([]);
  });
});
