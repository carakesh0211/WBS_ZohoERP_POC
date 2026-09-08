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
// This build MOUNTS /api/reports/* and /api/exports/*, and this harness gives
// them no database. `run.py::check_postgres_schema` builds the PostgreSQL
// runtime only when CAPEX_DB_URL or CAPEX_DB_HOST is set, and the webServer
// block in playwright.config.js sets neither — it sets CAPEX_DB_PATH, SQLite.
// So every reporting route that touches a database answers
//
//     503 {"detail": {"code": "DATABASE_NOT_CONFIGURED",
//                     "state": "unavailable", "message": "…"}}
//
// which is MOUNTED-BUT-CANNOT-ANSWER, not absence and not a fault. The screens
// fall through to the ledger routes and name both what answered and why the
// reporting service did not. `/api/reports/dimensions` needs no database and
// answers for real, which is how the ageing screens learn — rather than assume
// — that this build defines no age-band dimension.
//
// That is the state under test, so the deep-link, source, unavailable, axe and
// drill-down tests intercept NOTHING. A fallback proven against a stub proves
// only that the stub answered.
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

/**
 * The reporting and export surfaces, as api/reports.py and api/exports.py
 * actually publish them.
 *
 * These templates are matched against /openapi.json character for character,
 * so a wrong one makes a MOUNTED route read as absent. That is not a
 * hypothetical: the screens shipped probing `/api/reports/{report_id}`,
 * `/api/reports/saved-views`, `/api/reports/export` and
 * `/api/exports/{job_id}` — a guess written before the backend existed, and
 * not one of the four is in the build. Every screen therefore declared itself
 * unavailable while the service that answers it was mounted.
 */
const REPORT_PATHS = [
  '/api/reports/metrics',
  '/api/reports/drill-down',
  '/api/reports/dimensions',
  '/api/reports/freshness',
  '/api/reports/views',
  '/api/reports/views/{view_id}',
  '/api/exports',
  '/api/exports/{export_job_id}',
  '/api/exports/datasets',
  '/api/dashboard',
  '/api/projects/{project_id}/wbs',
  '/api/reconciliation',
  '/api/bills',
];

/**
 * A `/api/reports/metrics` payload in the shape `reporting.aggregate` returns:
 * grouped rows carrying `key` and `labels` side-maps plus flat integer-paise
 * metrics, a `totals` block computed over the WHOLE filtered population, and
 * the `freshness` block `reporting.freshness()` produces.
 *
 * Written out rather than hand-waved because the row shape is exactly what
 * this wave got wrong: the screens expected `project_id` and `capex_code` on
 * the row, and the service puts them in `key.project` and `labels.project`.
 */
function metricsPayload({ rows, totals, freshness = null, state = 'ok' }) {
  return {
    state,
    detail: null,
    group_by: ['project'],
    next_cursor: null,
    has_more: false,
    rows,
    totals,
    freshness: freshness || {
      state: 'local',
      source_label: 'capex-control-hub',
      last_sync_at: null,
      high_water_mark: null,
      detail: 'Every figure on this report is ours.',
    },
  };
}

/** One grouped row: the metrics flat, the identity in `key` and `labels`. */
function metricsRow(projectId, capexCode, measures) {
  return {
    key: { project: projectId },
    labels: { project: capexCode },
    ...measures,
  };
}

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

/**
 * Deep-link on a COLD LOAD: a full page.goto to the hash, not an in-app click.
 *
 * `about:blank` FIRST, AND IT IS NOT DEFENSIVE PADDING. Navigating to a URL
 * that differs from the current one only in its fragment is a SAME-DOCUMENT
 * navigation: the browser moves the hash and fetches nothing. So a test that
 * changed a route stub and called this again with the same hash re-asserted
 * against the page it already had — no reload, no new requests, the previous
 * stub's DOM still on screen.
 *
 * That is exactly how the bare-503 check failed: phase one served a declared
 * unavailability and fell through to the ledger, phase two flipped the stub to
 * a bare 503 and re-visited the same URL, nothing was re-fetched, and the
 * assertion read "a bare 503 was swallowed" about a request that was never
 * made. Every call here now starts from a blank document, which is what "cold
 * load" claims in the first place.
 */
async function gotoScreen(page, hash, search = '') {
  await page.goto('about:blank');
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

/**
 * A project id from the real seeded data, for the four project-scoped screens.
 *
 * THE `X-Session` HEADER IS NOT OPTIONAL HERE, and leaving it off is why every
 * test that called this helper failed. `/api/dashboard` is
 * `Depends(principal)`, which reads `Authorization` or `X-Session`; a bare
 * `fetch` inside `page.evaluate` sends neither, because it does not go through
 * `core/api-client.js`, which is the only thing that attaches the header. The
 * route answered 401, `r.ok` was false, this returned null, and the assertion
 * below reported it as "the seeded demo database returned no project" — a
 * message about the fixture for what was really a missing header.
 *
 * The session id is read from where the shell puts it: `sessionStorage`, under
 * `capex.session_id` (`core/api-client.js::SESSION_KEY`).
 */
async function seededProject(page) {
  const data = await page.evaluate(async () => {
    const headers = { Accept: 'application/json' };
    let sid = '';
    try { sid = sessionStorage.getItem('capex.session_id') || ''; } catch { sid = ''; }
    if (sid) headers['X-Session'] = sid;
    const r = await fetch('/api/dashboard', { headers });
    if (!r.ok) return { __status: r.status };
    return r.json();
  });
  expect(data && data.__status,
    `/api/dashboard refused this probe with HTTP ${data && data.__status} — the helper is not `
    + 'authenticated, which is a fault in the test and not in the seeded data')
    .toBeFalsy();
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
        /* UNSTARTED COUNTS AS EXPLAINED, and SCR-08 is why. The WBS Element
           Detail page needs a project AND an element; given only a project it
           renders an explicit prompt naming what is still to be chosen, and
           settles its loader in `unstarted`. That is not a screen showing
           nothing and explaining nothing — it is a query nobody has asked yet,
           which is a state this feature models deliberately, and it has no
           source to name because it made no request. */
        const explained = evidence.sources.length > 0
          || evidence.unavailable > 0
          || evidence.denied > 0
          || evidence.unstarted > 0;
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

    // ONE HANDLER, SWITCHED BY A FLAG — not two handlers and an unroute.
    // `page.unroute()` followed by a fresh `page.route()` on the same pattern
    // left the FIRST handler serving: phase two received the declared-503 body
    // again, fell through to the ledger, settled `ready`, and the assertion
    // reported "a bare 503 was swallowed" about a bare 503 that was never
    // sent. A flag cannot half-apply.
    let phase = 'declared';
    await page.route('**/api/reports/**', (route) => (phase === 'declared'
      ? route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: { unavailable: true, missing: 'No reporting database is configured.' },
        }),
      })
      : route.fulfill({
        status: 503, contentType: 'text/plain', body: 'upstream unavailable',
      })));

    // 1. Declared unavailable — falls through to the ledger, which answers.
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');
    let states = await loaderStates(page);
    expect(states, 'a declared unavailability should have fallen through to the ledger')
      .not.toContain('error');
    await expect(page.locator('#content .analytics-source[data-source="ledger"]')).toBeAttached();

    // 2. Bare 503 — a real fault, rendered as one.
    phase = 'bare';
    await gotoScreen(page, 'analytics-executive');
    states = await loaderStates(page);
    expect(states, 'a bare 503 was swallowed as "not built yet"').toContain('error');
  });

  test('a mounted reporting service is used, and named as the reporting service',
    async ({ page }) => {
      await routeOpenApi(page, REPORT_PATHS);
      const measures = {
        budget: 500000, commitment: 100000, actual: 200000, available: 200000,
        exposure: 300000, received_not_billed: 0, pr_reserved: 0,
        ordered: 100000, received: 0, original: 500000, revisions: 0,
      };
      await routeJson(page, '**/api/reports/metrics**', metricsPayload({
        rows: [metricsRow('P-1', 'CX-1', measures)],
        totals: measures,
        freshness: {
          state: 'synced',
          source_label: 'capex-control-hub',
          last_sync_at: '2026-09-08T06:00:00Z',
          high_water_mark: '2026-09-08T05:59:00Z',
          detail: null,
        },
      }));
      await signIn(page);
      await gotoScreen(page, 'analytics-executive');

      await expect(page.locator('#content .analytics-source[data-source="reports"]')).toBeAttached();
      const line = await page.locator('#content .analytics-source').first().innerText();
      expect(line).toContain('/api/reports/metrics');

      // And the freshness the report declared is rendered, not the fetch time.
      await expect(page.locator('#content .analytics-freshness[data-freshness="fresh"]'))
        .toBeAttached();

      // The grouped row's identity survived the read: `labels.project` became
      // the capex code the table renders. The screens expected `capex_code` on
      // the row and the service sends it in a side-map, which is the exact
      // mismatch that made this wave's contract wrong.
      expect(await page.locator('#content .scr-host').innerText()).toContain('CX-1');
    });

  test('a reporting 503 that DECLARES it cannot answer falls through, and says why',
    async ({ page }) => {
      // The state this harness is really in: /api/reports/metrics is mounted
      // and has no database. Nothing is intercepted — the real 503 is the
      // thing under test.
      await signIn(page);
      await gotoScreen(page, 'analytics-executive');

      // It fell through rather than rendering a fault...
      const states = await loaderStates(page);
      expect(states, 'a declared "cannot answer" was rendered as a system failure')
        .not.toContain('error');
      await expect(page.locator('#content .analytics-source[data-source="ledger"]')).toBeAttached();

      // ...and the reader is told WHY the reporting service stood aside, in the
      // server's own words. "not mounted" and "mounted with no database" are
      // different problems with different remedies, and this module knows only
      // that some source declined.
      const declined = page.locator('#content .analytics-declined');
      await expect(declined).toBeAttached();
      expect(await declined.first().innerText()).toContain('no database is configured');
    });

  test('a ledger fallback names the filters it could NOT apply', async ({ page }) => {
    // A figure narrowed by three of eighteen filters, presented as though it
    // had been narrowed by eighteen, is a wrong number wearing a right
    // number's clothes. The dropped dimensions are named individually.
    //
    // The filters here are ones the REPORTING service accepts — budget head
    // and category are real FilterSet fields — so the fall-through to the
    // ledger is caused by the 503, not by a refusal. A vendor filter would
    // take the other path entirely; that is the next test.
    await signIn(page);
    await gotoScreen(page, 'analytics-executive', 'head=BH-1&category=C-1');

    await expect(page.locator('#content .analytics-source[data-source="ledger"]')).toBeAttached();
    const warning = page.locator('#content .analytics-unapplied');
    await expect(warning).toBeVisible();
    const text = await warning.innerText();
    expect(text).toContain('Budget head');
    expect(text).toContain('Category');
    // The chips mark the same filters as dropped, so the warning and the chip
    // row cannot disagree.
    await expect(page.locator('#content .analytics-chip-dropped').first()).toBeVisible();
  });

  test('a REFUSED filter is UNAVAILABLE naming the field — not validation, not a fallback',
    async ({ page }) => {
      // `reporting.UNSUPPORTED_FILTERS` refuses vendor_ids with a 422 carrying
      // state "unavailable": purchase_order holds a vendor NAME while bill
      // holds a vendor id, so the filter would narrow the actuals and leave
      // the commitments across the whole estate.
      //
      // THREE THINGS MUST NOT HAPPEN, and each has its own assertion below.
      // It must not render as a form-validation error ("correct the
      // highlighted fields" is advice the reader cannot take — nothing they
      // typed is malformed). It must not fall through to the ledger, which
      // honours fewer filters still and would answer a question nobody asked.
      // And it must not show a figure.
      await routeOpenApi(page, REPORT_PATHS);
      // NESTED UNDER `detail`, because that is the envelope FastAPI produces:
      // `api/reports.py` raises HTTPException(status_code=422, detail={...}),
      // and FastAPI serialises the detail object under a `detail` key. A stub
      // that puts the problem at the top level would be testing a shape the
      // server never sends.
      await routeJson(page, '**/api/reports/metrics**', {
        detail: {
          type: 'about:blank',
          title: 'Vendor Dimension Incomplete',
          status: 422,
          code: 'VENDOR_DIMENSION_INCOMPLETE',
          state: 'unavailable',
          detail: 'Filtering by vendor is not available.',
          unsupported_filters: [{
            field: 'vendor_ids',
            code: 'VENDOR_DIMENSION_INCOMPLETE',
            detail: 'purchase_order still carries only vendor_name as text.',
          }],
        },
      }, 422);
      await signIn(page);
      await gotoScreen(page, 'analytics-executive', 'vendor=V-1');

      expect(await loaderStates(page)).toContain('unavailable');
      const block = page.locator('#content .analytics-filter-unavailable');
      await expect(block).toBeVisible();
      const text = await block.innerText();
      expect(text, 'the refused field was not named').toContain('Vendor');
      expect(text).toContain('VENDOR_DIMENSION_INCOMPLETE');

      // Not validation wording, and not a ledger answer standing in for it.
      const host = await page.locator('#content .scr-host').innerText();
      expect(host).not.toContain('Correct the highlighted fields');
      await expect(page.locator('#content .analytics-source[data-source="ledger"]'))
        .toHaveCount(0);
    });

  test('NEVER SYNCED is stale, and is not rendered like data nothing syncs', async ({ page }) => {
    // `reporting.freshness()` returns a null last_sync_at for BOTH `local`
    // (nothing syncs this, which is complete provenance) and `never_synced` (a
    // connector is configured and has never completed a poll, so mirrored
    // documents are absent rather than out of date). Reading only the
    // timestamp renders the second exactly like the first — a calm screen over
    // a connector that has never delivered a row.
    await routeOpenApi(page, REPORT_PATHS);
    const measures = {
      budget: 500000, commitment: 0, actual: 0, available: 500000, exposure: 0,
      received_not_billed: 0, pr_reserved: 0, ordered: 0, received: 0,
      original: 500000, revisions: 0,
    };
    await routeJson(page, '**/api/reports/metrics**', metricsPayload({
      rows: [metricsRow('P-1', 'CX-NEVER', measures)],
      totals: measures,
      freshness: {
        state: 'never_synced',
        source_label: 'capex-control-hub',
        last_sync_at: null,
        high_water_mark: null,
        detail: 'A connector is configured for this data but has never completed a poll.',
      },
    }));
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');

    expect(await loaderStates(page)).toContain('stale');
    await expect(page.locator('#content .analytics-freshness[data-freshness="stale"]'))
      .toBeAttached();
    await expect(
      page.locator('#content .analytics-freshness[data-freshness-state="never_synced"]'),
    ).toBeAttached();
    // The figures are still shown — a labelled old figure beats a blank panel.
    expect(await page.locator('#content .scr-host').innerText()).toContain('CX-NEVER');
  });

  test('LOCAL freshness is complete provenance, not a missing timestamp', async ({ page }) => {
    await routeOpenApi(page, REPORT_PATHS);
    const measures = {
      budget: 400000, commitment: 0, actual: 0, available: 400000, exposure: 0,
      received_not_billed: 0, pr_reserved: 0, ordered: 0, received: 0,
      original: 400000, revisions: 0,
    };
    await routeJson(page, '**/api/reports/metrics**', metricsPayload({
      rows: [metricsRow('P-1', 'CX-LOCAL', measures)],
      totals: measures,
    }));
    await signIn(page);
    await gotoScreen(page, 'analytics-executive');

    // Reported, not "unknown": a figure computed from documents this
    // application raised has no sync time BECAUSE nothing syncs it, and
    // saying "freshness not reported" of it would be false.
    const line = page.locator('#content .analytics-freshness[data-freshness-state="local"]');
    await expect(line).toBeAttached();
    const text = await line.first().innerText();
    expect(text).toContain('capex-control-hub');
    expect(text).not.toContain('Freshness not reported');
    expect(await loaderStates(page)).not.toContain('stale');
  });
});

/* ================================================ the drill-down round trip = */

test.describe('Wave 7 analytics screens — every metric drills down, and sums back', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('every card is a link, and its href carries the same FilterSet plus the dimension',
    async ({ page }) => {
      await signIn(page);
      // PL-01, WHICH IS A REAL SEEDED PLANT. This read is unstubbed, so it
      // falls through to /api/dashboard, which DOES apply plant_id — and the
      // id here was `PL-1`, which matches nothing. Every project was filtered
      // out, the screen entered its empty state, `onState` cleared the tiles,
      // and the assertion below reported "the executive dashboard rendered no
      // metric cards" for what was a typo in a fixture id.
      await gotoScreen(page, 'analytics-executive', 'plant=PL-01&from=2026-04-01');

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
          .toContain('plant=PL-01');
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

  test('the export goes to /api/exports as {dataset, filters}, carrying the same FilterSet',
    async ({ page }) => {
      // THE EXPORT ROUTE IS /api/exports AND THE BODY IS {dataset, filters}.
      // The screens shipped posting {report_id, format, filters} to
      // /api/reports/export — a route that does not exist — and
      // `CreateExportRequest` declares extra="forbid", so even against the
      // right path the old body would have been a 422 rather than a silently
      // ignored field.
      await routeOpenApi(page, REPORT_PATHS);
      const posted = [];
      await page.route('**/api/exports', async (route) => {
        const request = route.request();
        if (request.method() !== 'POST') return route.continue();
        posted.push(JSON.parse(request.postData() || '{}'));
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            export_job_id: 'EX-1', dataset: 'budget_ledger_cells', format: 'csv',
            state: 'QUEUED', requested_by: 'admin', columns: [], filters: {},
            progress: { rows_written: 0, rows_total: null, chunks_written: 0, percent: null },
          }),
        });
      });

      await signIn(page);
      await gotoScreen(page, 'analytics-project-list', 'plant=PL-1&limit=25&sort=budget');
      const button = page.locator('#content .analytics-export');
      await expect(button).toBeEnabled();
      expect(await button.getAttribute('data-export')).toBe('available');
      await button.click();
      await expect.poll(() => posted.length, { timeout: 10_000 }).toBeGreaterThan(0);

      const body = posted[0];
      expect(body.dataset, 'the export named no dataset').toBe('budget_ledger_cells');
      expect(body.report_id, 'the export still sends the invented report_id').toBeUndefined();
      expect(body.format, 'the export sends a format the dataset already fixes').toBeUndefined();

      // THE SAME FilterSet the screen is showing...
      expect(body.filters.plant_ids).toEqual(['PL-1']);
      // ...minus the four fields an export refuses outright. These are the
      // screen's paging and shape, not narrowing filters, and
      // `FILTER_FIELDS_NOT_APPLICABLE_TO_AN_EXPORT` rejects all four: an
      // export delivers the whole result set in a captured column and row
      // order, so a `limit` would truncate the file while the reader believed
      // they had the portfolio.
      for (const shape of ['cursor', 'limit', 'sort', 'group_by']) {
        expect(body.filters[shape], `the export sent ${shape}, which it refuses`).toBeUndefined();
      }
    });

  test('the export control is disabled, with the reason on it, when /api/exports is absent',
    async ({ page }) => {
      // A button that looks live and does nothing teaches an operator that the
      // application is unreliable. The openapi stub omits /api/exports.
      await routeOpenApi(page, REPORT_PATHS.filter((p) => !p.startsWith('/api/exports')));
      await signIn(page);
      await gotoScreen(page, 'analytics-project-list');
      const button = page.locator('#content .analytics-export');
      await expect(button).toBeVisible();
      await expect(button).toBeDisabled();
      expect(await button.getAttribute('data-export')).toBe('unavailable');
      expect(await button.getAttribute('title')).toContain('No export endpoint is mounted');
      // And it names the dataset, so an operator can check the catalogue at
      // /api/exports/datasets for the columns they would have got.
      expect(await button.getAttribute('title')).toContain('budget_ledger_cells');
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

  test('a list filter reaches the wire as REPEATED KEYS, never comma-joined', async ({ page }) => {
    // THE FAILURE THIS PINS IS SILENT AND TOTAL.
    // `core/api-client.js::buildQuery` builds parameters with
    // URLSearchParams.set, and `set` stringifies an array by joining it with
    // commas — set('entity_ids', ['E1','E2']) emits entity_ids=E1%2CE2, ONE
    // value, the eight-character string "E1,E2". api/reports.py declares every
    // list filter as `list[str] | None = Query()`, so FastAPI would read that
    // as a single entity id nobody has, match no row, and answer state
    // "empty". The screen would then say "no data matches these filters" —
    // confident, wrong, and unfalsifiable — about a question it never asked.
    //
    // So the assertion is on the URL the browser actually requests, not on a
    // helper's return value: the helper could be right and the call site still
    // hand the array to buildQuery.
    await routeOpenApi(page, REPORT_PATHS);
    const urls = [];
    await page.route('**/api/reports/metrics**', (route) => {
      urls.push(route.request().url());
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(metricsPayload({ rows: [], totals: {}, state: 'empty' })),
      });
    });
    await signIn(page);
    await gotoScreen(page, 'analytics-executive', 'entity=E1,E2&plant=P1,P2');

    expect(urls.length, 'the reporting service was never called').toBeGreaterThan(0);
    const query = new URL(urls[0]).searchParams;
    expect(query.getAll('entity_ids'), 'the entity list was comma-joined into one value')
      .toEqual(['E1', 'E2']);
    expect(query.getAll('plant_ids')).toEqual(['P1', 'P2']);
    expect(urls[0], 'a comma survived into the query string').not.toContain('%2C');
  });

  test('the drill-down carries the SAME FilterSet plus the clicked dimension, and sums back',
    async ({ page }) => {
      // THE CONTRACT: "a drill-down that does not sum back to the figure
      // clicked is a defect". This exercises the real client path — the same
      // FilterSet object goes to the card and to the drill-down — and then
      // does the arithmetic the contract names, in integers.
      await routeOpenApi(page, REPORT_PATHS);

      // The card: one grand total over two projects.
      const totals = {
        budget: 900000, commitment: 150000, actual: 250000, available: 500000,
        exposure: 400000, received_not_billed: 0, pr_reserved: 0,
        ordered: 150000, received: 0, original: 900000, revisions: 0,
      };
      await routeJson(page, '**/api/reports/metrics**', metricsPayload({
        rows: [
          metricsRow('P-1', 'CX-1', {
            budget: 500000, commitment: 100000, actual: 200000, available: 200000,
            exposure: 300000, received_not_billed: 0, pr_reserved: 0,
            ordered: 100000, received: 0, original: 500000, revisions: 0,
          }),
          metricsRow('P-2', 'CX-2', {
            budget: 400000, commitment: 50000, actual: 50000, available: 300000,
            exposure: 100000, received_not_billed: 0, pr_reserved: 0,
            ordered: 50000, received: 0, original: 400000, revisions: 0,
          }),
        ],
        totals,
      }));

      // The drill-down: the rows behind the clicked project, at the finer
      // grain. Their budgets sum to that project's own figure, exactly.
      const drillUrls = [];
      await page.route('**/api/reports/drill-down**', (route) => {
        drillUrls.push(route.request().url());
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            state: 'ok',
            detail: null,
            group_by: ['project', 'wbs', 'budget_head'],
            next_cursor: null,
            has_more: false,
            drill_of: { dimension: 'project', key: 'P-1' },
            rows: [
              {
                key: { project: 'P-1', wbs: 'W-1', budget_head: 'BH-1' },
                labels: { project: 'CX-1', wbs: 'W.1', budget_head: 'Civil' },
                budget: 300000, commitment: 60000, actual: 120000, available: 120000,
                exposure: 180000, received_not_billed: 0, pr_reserved: 0,
                ordered: 60000, received: 0, original: 300000, revisions: 0,
              },
              {
                key: { project: 'P-1', wbs: 'W-2', budget_head: 'BH-2' },
                labels: { project: 'CX-1', wbs: 'W.2', budget_head: 'Plant' },
                budget: 200000, commitment: 40000, actual: 80000, available: 80000,
                exposure: 120000, received_not_billed: 0, pr_reserved: 0,
                ordered: 40000, received: 0, original: 200000, revisions: 0,
              },
            ],
            totals: {
              budget: 500000, commitment: 100000, actual: 200000, available: 200000,
              exposure: 300000, received_not_billed: 0, pr_reserved: 0,
              ordered: 100000, received: 0, original: 500000, revisions: 0,
            },
            freshness: {
              state: 'local', source_label: 'capex-control-hub',
              last_sync_at: null, high_water_mark: null, detail: null,
            },
          }),
        });
      });

      await signIn(page);
      await gotoScreen(page, 'analytics-executive', 'entity=E1&plant=PL-1&from=2026-04-01');

      const outcome = await page.evaluate(async () => {
        const api = await import('/static/src/features/analytics/analytics-api.js');
        const filtersMod = await import('/static/src/features/analytics/analytics-filters.js');
        const metrics = await import('/static/src/features/analytics/analytics-metrics.js');

        // ONE FilterSet, read from the URL exactly as the screen read it.
        const filters = filtersMod.readFilters(window.location.search);

        const card = await api.getPortfolio(filters, api.SCREENS.executive);
        const clicked = card.data.rows.find((r) => r.project_id === 'P-1');

        // The SAME object, plus the clicked dimension and key.
        const drill = await api.getDrilldown(filters, 'project', 'P-1', ['wbs', 'budget_head']);

        return {
          cardRowBudget: clicked.budget,
          drillRows: drill.data.rows.length,
          // The contract's arithmetic, in integers, over the real payloads.
          sumsBack: metrics.assertSumsBack(clicked.budget, drill.data.rows, 'budget'),
          exposureSumsBack: metrics.assertSumsBack(
            clicked.exposure, drill.data.rows, 'exposure',
          ),
        };
      });

      expect(outcome.drillRows, 'the drill-down returned no rows').toBe(2);
      expect(outcome.sumsBack.checked, 'the round trip could not be checked').toBe(true);
      expect(outcome.sumsBack.ok, outcome.sumsBack.message).toBe(true);
      expect(outcome.sumsBack.sum).toBe(outcome.cardRowBudget);
      expect(outcome.exposureSumsBack.ok, outcome.exposureSumsBack.message).toBe(true);

      // And the request really did carry the card's whole FilterSet plus the
      // click — not a fresh query, and not a subset.
      expect(drillUrls.length, 'the drill-down endpoint was never called').toBeGreaterThan(0);
      const q = new URL(drillUrls[0]).searchParams;
      expect(q.getAll('entity_ids')).toEqual(['E1']);
      expect(q.getAll('plant_ids')).toEqual(['PL-1']);
      expect(q.get('date_from')).toBe('2026-04-01');
      expect(q.get('dimension')).toBe('project');
      expect(q.get('key')).toBe('P-1');
      expect(q.getAll('grain')).toEqual(['wbs', 'budget_head']);
      // A drill-down starts at the first page of its OWN result: inheriting
      // the card screen's cursor would page into the middle of a different
      // result set and return rows that cannot sum back to anything.
      expect(q.get('cursor')).toBeNull();
    });

  test('DENIED SCOPE from the reporting service is state "denied", never state "empty"',
    async ({ page }) => {
      // `reporting.aggregate` short-circuits a denied scope BEFORE the query
      // and labels it, rather than inferring "denied" from an empty result —
      // which it could not do, because a denied scope and a quiet month both
      // return no rows. The client must not undo that on the way in.
      await routeOpenApi(page, REPORT_PATHS);
      await routeJson(page, '**/api/reports/metrics**', {
        state: 'denied',
        detail: 'You have no grant that reaches any of this data. This is not an empty report; '
          + 'it is a report you cannot see.',
        rows: [],
        totals: {},
        group_by: ['project'],
        next_cursor: null,
        has_more: false,
      });
      await signIn(page);
      await gotoScreen(page, 'analytics-exceptions');

      expect(await loaderStates(page)).toContain('denied');
      const text = await page.locator('#content .scr-host').innerText();
      expect(text).toContain('access scope reaches none of it');
      expect(text).not.toContain('measured "none"');
      // And it did NOT quietly answer from the ledger instead: falling through
      // would answer a scope question from a source applying a different
      // scope, which is how a screen comes to show a number the caller was
      // not entitled to.
      await expect(page.locator('#content .analytics-source[data-source="ledger"]'))
        .toHaveCount(0);
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
      /* THE NODE, NOT JUST THE COUNT. A summary of "color-contrast x1" names
         a rule and leaves the element to be hunted for; the selector and the
         failure text turn the same failure into a one-line fix. */
      const summary = results.violations.map(
        (v) => `${v.id} (${v.impact}) x${v.nodes.length}: ${v.help} — `
          + v.nodes.slice(0, 3).map(
            (n) => `${(n.target || []).join(' ')} [${(n.failureSummary || '').replace(/\s+/g, ' ').trim()}]`,
          ).join(' | '),
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
