// tests/vrt/closure.spec.js
//
// WAVE 7 — THE SIX CLOSURE SCREENS, AS ROUTES OF THE SHIPPED SPA SHELL.
//
//   SCR-11  Budget Revision Request        #closure-budget-revision
//   SCR-12  Budget Transfer Screen         #closure-budget-transfer
//   SCR-14  Purchase Request Control View  #closure-pr-control
//   SCR-20  Project Completion Review      #closure-completion-review
//   SCR-21  Capitalisation Workbench       #closure-capitalisation
//   SCR-22  Asset Allocation Screen        #closure-asset-allocation
//
// WHY THIS FILE EXISTS, AND THE DEFECT IT MUST NOT REPEAT
// ------------------------------------------------------
// The six screens were written, reviewed and committed in Wave 7 and then
// imported by nothing: no manifest, no entry in router.js's SCREENS, no row in
// app.js's SCR_ROUTES, no spec. They were unreachable in a running browser for
// three waves and no test said so, because no test referred to them.
//
// That was not an accident of omission alone. integration.spec.js,
// analytics.spec.js and mapping.spec.js each installed their OWN routes into
// the running page with `page.addInitScript` — pushing rows into SCR_ROUTES and
// overriding `V` — so every one of them went green against a build that existed
// only inside the test. The suites proved that a manifest WOULD work if anybody
// applied it, and were read as proving that somebody had.
//
// SO THIS FILE INSTALLS NOTHING. It pushes no route, overrides no view, and
// defines no `V` entry. Every navigation below is a real hash against the
// shipped wiring — `app.js`'s SCR_ROUTES rows and `router.js`'s SCREENS spread
// — and `closureRouteCount()` READS what the application declares rather than
// writing it first. If a screen does not render here, the screen does not
// render for a user, and that is the result this file is for.
//
// WHAT THIS FILE PROVES THAT NOTHING ELSE DOES
//
//  1. All six deep-link from a COLD LOAD and reach `data-mounted="1"`, under
//     the shipped router — the exact claim that was false for three waves.
//  2. Six render states on every screen, with UNAVAILABLE distinct from EMPTY
//     in wording and in mechanism. A screen that says "no rows" when the route
//     is not mounted is a screen telling a controller there is nothing to
//     capitalise when nobody asked.
//  3. Money is integer paise all the way to the DOM. Asserted twice: on a
//     value that LOSES ITS PAISA under `paise / 100`, and structurally, over
//     the feature's own source, which may not contain money arithmetic at all.
//  4. X-01 on SCR-21: the posting note renders above the balance AND the
//     approval result restates it, so no green success message stands alone
//     next to a rupee figure.
//  5. SCR-22: a write-off is a POSITIVE amount with a badge and a reason. The
//     amount column carries no minus sign and no parentheses, ever.
//  6. SCR-12: both legs of every transfer, always.
//
// Conventions follow tests/vrt/mapping.spec.js and tests/vrt/integration.spec.js:
// sign in through the real backend (session, permissions and shell chrome are
// part of what is under test and must not be faked), then intercept ONLY the
// closure endpoints with `page.route()`. No static fake-data file, and no fixed
// timeout anywhere — the one test that must observe the LOADING state holds the
// response open on a promise it releases itself, rather than racing a sleep.
//
// NO SCREENSHOT ASSERTION AND NO BASELINE PNG. This suite adds none, so it
// adds no `closure.spec.js-snapshots` directory: a baseline captured to make a
// new test pass proves only that the screen renders the way it rendered.
//
// This file owns no configuration. package.json and playwright.config.js are
// not declared or modified here.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

/* ---------------- identities ----------------
   Demo credentials are user_id + '!demo', provisioned idempotently by
   auth.provision_dev_identities.

   THE TWO CHOSEN HERE HAVE GENUINELY DIFFERENT GRANTS, verified against
   app/backend/auth.py's PERMISSIONS map rather than assumed:

     U-AUD  roles ['Auditor']                       — holds connector.read and
            audit.read; holds NEITHER capitalisation.approve NOR
            capitalisation.allocate NOR approval.read NOR connector.manage.
     U-CFO  roles ['FinanceApprover',
                   'CapitalisationApprover']        — holds
            capitalisation.approve, capitalisation.allocate, revision.approve
            and approval.read; holds NEITHER connector.read NOR
            connector.manage NOR audit.read.

   They therefore differ on six permissions and agree on the one that gates
   these screens. That is not a weakness of the choice, it is the finding:
   `budget.read` is declared as `ROLES` — the whole tuple — so EVERY seeded
   identity holds it and no demo principal exists that the closure gate would
   refuse. The negative control below is consequently taken against a route
   whose permission these principals really do lack, which exercises the SAME
   `viewAllowed()` code path on the SAME SCR_ROUTES table. See
   'the permission gate is live, and is refusing something'. */
const AUDITOR = { user: 'U-AUD', password: 'U-AUD!demo' };
const CAPITALISER = { user: 'U-CFO', password: 'U-CFO!demo' };
const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };

/* ---------------- the six screens under test ----------------

   `title` is C8's `name_verbatim`, which is what the page h1 must read. Note
   SCR-12 and SCR-22: the manifest's short `label` is 'Budget Transfer' and
   'Asset Allocation', and the closure-list card inside SCR-12 is headed
   'Budget Transfer' too. The CONTRACT name carries the word 'Screen', and it
   is the contract name that has to reach `#pageTitle`. */
const SCREENS = [
  {
    hash: 'closure-budget-revision',
    scr: 'SCR-11',
    title: 'Budget Revision Request',
    status: '#budgetRevisionStatus',
    what: 'Budget revision requests',
    empty: 'No budget revision matches this filter.',
    template: '/api/control/budget-revisions',
    api: (url) => url.pathname === '/api/control/budget-revisions',
  },
  {
    hash: 'closure-budget-transfer',
    scr: 'SCR-12',
    title: 'Budget Transfer Screen',
    status: '#budgetTransferStatus',
    what: 'Budget transfers',
    empty: 'No budget transfer matches this filter.',
    template: '/api/control/budget-transfers',
    api: (url) => url.pathname === '/api/control/budget-transfers',
  },
  {
    hash: 'closure-pr-control',
    scr: 'SCR-14',
    title: 'Purchase Request Control View',
    status: '#purchaseRequestControlStatus',
    what: 'Purchase requests',
    empty: 'No purchase request matches this filter.',
    template: '/api/control/purchase-requests',
    api: (url) => url.pathname === '/api/control/purchase-requests',
  },
  {
    hash: 'closure-completion-review',
    scr: 'SCR-20',
    title: 'Project Completion Review',
    status: '#completionReviewListStatus',
    what: 'Project completion reviews',
    empty: 'No completion review has been raised for this filter.',
    template: '/api/closure/reviews',
    api: (url) => url.pathname === '/api/closure/reviews',
  },
  {
    hash: 'closure-capitalisation',
    scr: 'SCR-21',
    title: 'Capitalisation Workbench',
    status: '#capitalisationRequestsStatus',
    what: 'Capitalisation requests',
    empty: 'No capitalisation request matches this filter.',
    template: '/api/closure/requests',
    api: (url) => url.pathname === '/api/closure/requests',
  },
  {
    hash: 'closure-asset-allocation',
    scr: 'SCR-22',
    title: 'Asset Allocation Screen',
    status: '#assetAllocationListStatus',
    what: 'Asset allocations',
    empty: 'Nothing has been allocated against this capitalisation request yet.',
    template: '/api/closure/requests/{cap_id}/allocations',
    api: (url) => /^\/api\/closure\/requests\/[^/]+\/allocations$/.test(url.pathname),
  },
];

/**
 * The `.integration-loader` that owns one screen's status host.
 *
 * THREE OF THE SIX CARRY TWO LOADERS — a position panel and a list panel, each
 * naming its own route, because a build that mounts the review routes but not
 * the position route must not show "unavailable" over a working review list.
 * An assertion written against `.integration-unavailable` unqualified lands on
 * whichever panel comes first in the document, which on SCR-20, SCR-21 and
 * SCR-22 is the OTHER one — and the test then passes or fails for a reason
 * that has nothing to do with what it says it checks. This scopes every
 * per-state assertion to the panel it is about.
 */
function loaderFor(screen) {
  return `#content .integration-loader:has(${screen.status})`;
}

const PROJECT = 'PRJ-DM-01';
const CAP_ID = 'CAP-2026-0007';

/* The query string that makes the three project-scoped screens load on a cold
   deep link. The shell routes on the HASH, so the search string survives —
   which is what `integration-kit.js`'s `queryParam()` reads. */
const DEEP = `?project_id=${PROJECT}&cap_id=${CAP_ID}`;

/* ---------------- fixtures ----------------
   Every one is a `page.route()` body. There is no fixture FILE: a JSON file on
   disk drifts from the response shape silently, and the shapes below are read
   straight off app/backend/api/closure.py's response construction. */

/* A PAISA THAT A FLOAT WOULD EAT.
   100000000000001 paise is ₹10,00,00,00,00,000.01. Divided by 100 in IEEE-754
   it is 1000000000000.00001, and `.toFixed(2)` on that is
   "1000000000000.00" — the paisa is gone, silently, with no error anywhere.
   formatINR does integer division and a modulo, so it keeps it. This value is
   used wherever a money assertion can carry it. */
const HUGE_PAISE = 100000000000001;
const HUGE_RENDERED = '₹10,00,00,00,00,000.01';

const BUDGET_REVISIONS = {
  items: [
    {
      revision_id: 'REV-2026-0031',
      capex_code: 'CX-DM-01',
      wbs_code: 'WBS-A-CIVIL',
      budget_head: 'Civil works',
      delta_paise: HUGE_PAISE,
      effective_from: '2026-04-01',
      status: 'APPROVED',
      created_by: 'U-PM',
      decided_by: 'U-FIN',
      justification: 'Foundation redesign after the soil report.',
    },
    {
      // A SURRENDER. Signed, and rendered signed — formatINR puts a negative in
      // parentheses, never in colour alone (C6 financial.rule).
      revision_id: 'REV-2026-0032',
      capex_code: 'CX-DM-01',
      wbs_code: 'WBS-A-ELEC',
      budget_head: 'Electrical',
      delta_paise: -250000,
      effective_from: '2026-05-01',
      status: 'SUBMITTED',
      created_by: 'U-PM',
      decided_by: null,
      justification: 'Scope moved to the EPC package.',
    },
  ],
};

const BUDGET_TRANSFERS = {
  items: [{
    transfer_id: 'TRF-2026-0009',
    from_wbs_code: 'WBS-A-CIVIL',
    from_head: 'Civil works',
    from_project_id: PROJECT,
    to_wbs_code: 'WBS-A-ELEC',
    to_head: 'Electrical',
    to_project_id: PROJECT,
    amount_paise: 12345678,
    effective_from: '2026-06-01',
    status: 'APPROVED',
    created_by: 'U-PM',
    decided_by: 'U-FIN',
    justification: 'Rebalancing after the civil surrender.',
  }],
};

const PURCHASE_REQUESTS = {
  items: [
    {
      // THE ROW THIS SCREEN EXISTS FOR: a commitment let through OVER a budget
      // refusal, with the reason somebody gave. A control view that only ever
      // rendered WITHIN_BUDGET rows would pass every other assertion here.
      pr_number: 'PR-2026-0104',
      capex_code: 'CX-DM-01',
      status: 'Approved',
      check_result: 'EXCEEDS_BUDGET',
      amount_paise: 900000000,
      reserved_paise: 900000000,
      line_count: 3,
      requested_by: 'U-REQ',
      approver: 'U-FIN',
      exception_reason: 'Plant shutdown window closes on 30 June; approved by the CFO.',
    },
    {
      pr_number: 'PR-2026-0105',
      capex_code: 'CX-DM-01',
      status: 'Submitted',
      check_result: 'WITHIN_BUDGET',
      amount_paise: 4500000,
      reserved_paise: 0,
      line_count: 1,
      requested_by: 'U-REQ',
      approver: null,
      exception_reason: null,
    },
  ],
};

/* The closure position. `posting_status` and `posting_note` are the SERVER's,
   pinned by migration 018's `ck_capitalisation_request_not_posted`; the screens
   render the field rather than a literal, so this fixture is what X-01 is
   asserted against. */
const POSITION = {
  project_id: PROJECT,
  project_status: 'Execution',
  accepted_review_id: null,
  posting_status: 'NOT POSTED',
  posting_note: 'NOT POSTED — local approval only; no ERP/GL or fixed-asset posting '
    + 'exists in this build.',
  cwip_balance_paise: HUGE_PAISE,
  open_commitment_paise: 900000000,
  received_not_billed_paise: 125000,
  pr_reserved_paise: 900000000,
  allocated_paise: 100000000000000,
  unallocated_paise: 1,
  open_exceptions: { unattributed_paise: 7500000 },
  blockers: [
    'Open commitment of ₹90,00,000.00 remains against this project.',
    'No completion review has been accepted.',
  ],
};

const REVIEWS = {
  items: [{
    review_id: 'PCR-2026-0003',
    capex_code: 'CX-DM-01',
    status: 'Submitted',
    completion_date: '2026-08-30',
    submitted_by: 'U-PM',
    decided_by: null,
    decided_at: null,
    summary: 'Civil and electrical complete; commissioning outstanding.',
    decision_note: null,
  }],
};

const REQUESTS = {
  posting_status: 'NOT POSTED',
  posting_note: 'NOT POSTED — local approval only; no ERP/GL or fixed-asset posting '
    + 'exists in this build.',
  items: [{
    cap_id: CAP_ID,
    cap_number: 'CAP-2026-0007',
    capex_code: 'CX-DM-01',
    status: 'Submitted',
    cwip_balance_paise: HUGE_PAISE,
    allocated_paise: 100000000000000,
    posting_status: 'NOT POSTED',
    requested_by: 'U-PM',
    approver: null,
    approved_at: null,
    decision_note: null,
  }],
};

/* A CAPITALISATION AND A WRITE-OFF, BOTH POSITIVE.
   The write-off carries `is_writeoff: true` and a reason, and its
   `amount_paise` is POSITIVE — which is the whole rule. A fixture with a
   negative write-off would be testing that the screen renders a defect
   faithfully. */
const ALLOCATIONS = {
  items: [
    {
      allocation_id: 'ALC-2026-0011',
      asset_name: 'Substation transformer, 33/11 kV',
      asset_category: 'Plant & machinery',
      wbs_code: 'WBS-A-ELEC',
      amount_paise: 100000000000000,
      is_writeoff: false,
      writeoff_reason: null,
      created_by: 'U-PFC',
    },
    {
      allocation_id: 'ALC-2026-0012',
      asset_name: 'Abandoned trial pit works',
      asset_category: null,
      wbs_code: 'WBS-A-CIVIL',
      amount_paise: 5500000,
      is_writeoff: true,
      writeoff_reason: 'Trial pits abandoned after the revised soil report; no asset results.',
      created_by: 'U-PFC',
    },
  ],
};

/* ---------------- stubs ---------------- */

function json(route, body, status = 200) {
  return route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  });
}

/** The path templates this build really mounts, as closure-api.js names them. */
const REAL_PATHS = [
  '/api/health',
  '/api/bootstrap',
  '/api/closure/projects/{project_id}/position',
  '/api/closure/reviews',
  '/api/closure/reviews/{review_id}/submit',
  '/api/closure/reviews/{review_id}/decide',
  '/api/closure/requests',
  '/api/closure/requests/{cap_id}',
  '/api/closure/requests/{cap_id}/submit',
  '/api/closure/requests/{cap_id}/approve',
  '/api/closure/requests/{cap_id}/reject',
  '/api/closure/requests/{cap_id}/allocations',
  '/api/control/budget-revisions',
  '/api/control/budget-transfers',
  '/api/control/purchase-requests',
];

/**
 * Declare which paths this build "mounts".
 *
 * closure-api.js reads /openapi.json to tell an UNMOUNTED route from an EMPTY
 * result, so the schema IS the switch that selects available from unavailable.
 * Stubbing it is stubbing the thing the code actually reads — and one test
 * below deliberately does NOT stub it, so that the templates above are checked
 * against the schema the running FastAPI app really serves.
 */
async function routeOpenApi(page, paths) {
  const schema = { openapi: '3.1.0', info: { title: 'stub', version: '0' }, paths: {} };
  for (const p of paths) schema.paths[p] = { get: {}, post: {} };
  await page.route('**/openapi.json', (route) => json(route, schema));
}

/**
 * ONE HANDLER PER FAMILY, BRANCHING ON THE PATH — not a stack of globs.
 *
 * Playwright matches routes in REVERSE registration order, so a broad glob
 * registered after a narrow one silently shadows it: `**\/api/closure/requests`
 * and `**\/api/closure/requests/*\/allocations` overlap exactly that way, and
 * the shadowing failure is the worst kind — the suite goes green having
 * asserted against the wrong fixture. A handler that reads the URL cannot have
 * that bug. Overrides registered by an individual test come AFTER this call and
 * therefore win, which is the one place the ordering is wanted.
 */
async function stubAll(page, overrides = {}) {
  await routeOpenApi(page, overrides.paths || REAL_PATHS);

  await page.route('**/api/control/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/budget-revisions')) {
      return json(route, overrides.revisions || BUDGET_REVISIONS);
    }
    if (path.endsWith('/budget-transfers')) {
      return json(route, overrides.transfers || BUDGET_TRANSFERS);
    }
    if (path.endsWith('/purchase-requests')) {
      return json(route, overrides.purchaseRequests || PURCHASE_REQUESTS);
    }
    return json(route, { items: [] });
  });

  await page.route('**/api/closure/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/position')) return json(route, overrides.position || POSITION);
    if (path.endsWith('/allocations')) return json(route, overrides.allocations || ALLOCATIONS);
    if (path.endsWith('/approve')) return json(route, overrides.approve || APPROVE_RESULT);
    if (path.endsWith('/reviews')) return json(route, overrides.reviews || REVIEWS);
    if (path.endsWith('/requests')) return json(route, overrides.requests || REQUESTS);
    return json(route, { items: [] });
  });
}

/* The approval response. `posting_note` is the SERVER's sentence and the
   screen renders it rather than a constant of its own — which is what makes
   X-01 survive a backend that changed its mind. */
const APPROVE_RESULT = {
  cap_id: CAP_ID,
  cap_number: 'CAP-2026-0007',
  status: 'Approved',
  capitalised_paise: HUGE_PAISE,
  posting_status: 'NOT POSTED',
  posting_note: 'NOT POSTED — local approval only; no ERP/GL or fixed-asset posting '
    + 'exists in this build.',
};

/* ---------------- helpers ---------------- */

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

/**
 * How many closure routes the SHIPPED application declares.
 *
 * READ, never written. `SCR_ROUTES` is app.js's own top-level `const`; a
 * top-level binding in a classic script lives in the global lexical
 * environment, which is on the scope chain of anything evaluated in the same
 * realm, so it resolves here as a free identifier. Nothing is pushed into it.
 */
function closureRouteCount(page) {
  // eslint-disable-next-line no-undef
  return page.evaluate(() => SCR_ROUTES.filter((r) => r.id.startsWith('closure-')).length);
}

async function settleShell(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
}

/**
 * Wait for a screen to be MOUNTED, not merely present.
 *
 * app.js appends the host node BEFORE it awaits mount(), so `.scr-host` appears
 * while the screen is still empty. `data-mounted` is the signal — and
 * `data-mount-failed` ends the wait too, because a diagnosed failure carrying
 * the module's own message is worth far more than the same failure arriving as
 * a fifteen-second timeout with nothing in it.
 */
async function settleScreen(page) {
  await settleShell(page);
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 15_000 });
  const failed = page.locator('#content .scr-host[data-mount-failed="1"]');
  if (await failed.count()) {
    throw new Error(`the screen's mount() threw, so it never rendered: ${(await failed.innerText()).trim()}`);
  }
  await page.waitForFunction(() => {
    const host = document.querySelector('#content .scr-host[data-mounted="1"]');
    return !!host && !host.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForLoadState('networkidle');
}

/** Navigate by REAL HASH against the shipped wiring, and settle. */
async function gotoScreen(page, hash, search = '') {
  await page.goto(`/${search}#${hash}`);
  await settleScreen(page);
}

function pageTitle(page) { return page.locator('#pageTitle'); }

/** The whole rendered screen as text, for a substring assertion. */
function screenText(page) {
  return page.locator('#content').innerText();
}

/** What the live region is currently announcing. */
function announced(page) {
  return page.locator('#closureLiveRegion');
}

/* ================================================ routes, not rail ======== */

test.describe('The six closure screens are reachable in the shipped build', () => {
  test('app.js declares all six SCR_ROUTES rows, and this file did not put them there',
    async ({ page }) => {
      // The assertion the previous three suites could not make, because they
      // had written the rows they then read back. Nothing here installs
      // anything: `closureRouteCount()` only reads.
      await stubAll(page);
      await signIn(page);
      expect(await closureRouteCount(page),
        'app.js does not declare the six closure routes, so the screens are unreachable')
        .toBe(6);
    });

  test('router.js resolves all six, with C8\'s verbatim names', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    const declared = await page.evaluate(async () => {
      const mod = await import('/static/src/core/router.js');
      return mod.SCREENS.map((s) => ({ id: s.id, scr: s.scr, title: s.title }));
    });
    const closure = declared.filter((s) => s.id && s.id.startsWith('closure-'));
    expect(closure.map((s) => s.scr))
      .toEqual(['SCR-11', 'SCR-12', 'SCR-14', 'SCR-20', 'SCR-21', 'SCR-22']);
    for (const s of SCREENS) {
      const found = closure.find((c) => c.id === s.hash);
      expect(found, `${s.scr} is not in router.js's SCREENS`).toBeTruthy();
      expect(found.title, `${s.scr}'s title is not C8's verbatim name`).toBe(s.title);
    }
  });

  test('the app.js rows and the manifest agree, both read from the running page',
    async ({ page }) => {
      // Not a transcription in this file compared against itself — both halves
      // come off the application. A manifest edit nobody carried into app.js
      // fails here rather than shipping.
      await stubAll(page);
      await signIn(page);
      const { rows, manifest } = await page.evaluate(async () => {
        const mod = await import('/static/src/features/closure/manifest.js');
        return {
          // eslint-disable-next-line no-undef
          rows: SCR_ROUTES.filter((r) => r.id.startsWith('closure-'))
            .map(({ id, ico, label, need }) => ({ id, ico, label, need })),
          manifest: mod.CLOSURE_NAV_ROWS,
        };
      });
      expect(rows).toEqual(manifest);
    });

  for (const s of SCREENS) {
    test(`${s.scr} resolves from a COLD LOAD of #${s.hash} and mounts`, async ({ page }) => {
      // Deep-linkability is not "the hash works once the shell is up". It is
      // "typing the URL into a fresh tab lands on the screen", which is a
      // different code path: viewAllowed() runs synchronously inside render(),
      // before any dynamic import could have resolved. Signing in and THEN
      // clicking would never exercise it.
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, s.hash, DEEP);
      await expect(pageTitle(page), `${s.scr} did not render its own title`)
        .toHaveText(s.title);
      await expect(page.locator('#content .scr-host[data-mounted="1"]')).toBeAttached();
      expect(page.url()).toContain(`#${s.hash}`);
    });
  }

  test('none of the six appears in the primary navigation', async ({ page }) => {
    // The rail already overflows at laptop-1024 when a stream adds a block of
    // entries; the manifest says so and this measures it.
    await stubAll(page);
    await signIn(page);
    const rail = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    for (const s of SCREENS) {
      expect(rail, `${s.scr} added a rail entry`).not.toContain(s.hash);
    }
  });

  test('the path templates closure-api.js uses are the ones FastAPI really serves',
    async ({ page }) => {
      /* /openapi.json is DELIBERATELY NOT STUBBED here. closure-api.js decides
         availability by STRING EQUALITY against the schema, so a route serving
         perfectly well under `{cap_id}` is reported ABSENT to a screen asking
         for `{capId}` — and the screen then renders an unavailable state over
         a working endpoint. Only the live schema can catch that. */
      await signIn(page);
      const { templates, mounted } = await page.evaluate(async () => {
        const mod = await import('/static/src/features/closure/closure-api.js');
        const schema = await (await fetch('/openapi.json')).json();
        return { templates: mod.TEMPLATES, mounted: Object.keys(schema.paths) };
      });
      const missing = Object.entries(templates)
        .filter(([, path]) => !mounted.includes(path))
        .map(([name, path]) => `${name} -> ${path}`);
      expect(missing, 'closure-api.js names a path template this build does not mount; '
        + 'every screen reading it will render UNAVAILABLE over a working route').toEqual([]);
    });
});

/* ================================================== the six render states === */

test.describe('The six render states, on every screen', () => {
  for (const s of SCREENS) {
    test(`${s.scr} renders SUCCESS with the server's rows`, async ({ page }) => {
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, s.hash, DEEP);
      await expect(page.locator(`${s.status}`)).toBeHidden();
      await expect(page.locator('#content .closure-table').first()).toBeVisible();
    });

    test(`${s.scr} renders EMPTY for a successful empty response`, async ({ page }) => {
      await stubAll(page);
      await page.route(s.api, (route) => json(route, { items: [] }));
      await signIn(page);
      await gotoScreen(page, s.hash, DEEP);
      const host = page.locator(s.status);
      await expect(host).toBeVisible();
      await expect(host.locator('.empty')).toBeVisible();
      await expect(host).toContainText(s.empty);
      // EMPTY is not UNAVAILABLE. The distinction is the point of this suite.
      await expect(host).not.toContainText('is not available in this build');
    });

    test(`${s.scr} renders UNAVAILABLE, naming the route, when it is NOT MOUNTED`,
      async ({ page }) => {
        /* The mechanism is different from EMPTY's, not just the wording: the
           route is absent from /openapi.json, so closure-api.js never issues
           the call at all and raises EndpointUnavailableError. A screen that
           answered "no rows" here would be telling a controller there is
           nothing to capitalise when nobody asked. */
        await stubAll(page, { paths: ['/api/health', '/api/bootstrap'] });
        await signIn(page);
        await gotoScreen(page, s.hash, DEEP);
        // Scoped to THIS panel. On SCR-20, SCR-21 and SCR-22 the position
        // panel renders its own unavailable block first, naming a different
        // route; an unscoped `.first()` would assert against that one.
        const block = page.locator(`${loaderFor(s)} .integration-unavailable`);
        await expect(block).toBeVisible();
        await expect(block).toContainText('is not available in this build');
        await expect(block, 'the unavailable state does not name the missing route')
          .toContainText(s.template);
        await expect(block, 'an absent endpoint was described as an empty result')
          .toContainText('not because the queue is empty');
        // And it is NOT the empty state's sentence.
        expect(await screenText(page)).not.toContain(s.empty);
      });

    test(`${s.scr} renders ERROR, with a retry, for a real server failure`,
      async ({ page }) => {
        await stubAll(page);
        await page.route(s.api, (route) => json(route, { detail: 'boom' }, 500));
        await signIn(page);
        await gotoScreen(page, s.hash, DEEP);
        const host = page.locator(s.status);
        /* THE RETRY IS WHAT MAKES THIS STATE DISTINCT from the other five.
           Empty, unavailable and permission all say "there is nothing here"
           and none of them offers to try again, because for none of them would
           trying again help. Asserting on the wording would assert on whatever
           sentence the server happened to send. */
        await expect(host.locator('.msg-error')).toBeVisible();
        await expect(host.locator('button:has-text("Retry")')).toBeVisible();
        await expect(host).not.toContainText(s.empty);
        await expect(host).not.toContainText('is not available in this build');
      });

    test(`${s.scr} renders PERMISSION-DENIED from a REFUSAL, not from an empty result`,
      async ({ page }) => {
        /* The not-found-over-forbidden rule makes a refused READ arrive as
           kind 'notfound', which state-host.js renders in the SHAPE of the
           empty state and never with the words "forbidden", "denied" or "403"
           — a 403 worded differently from a 404 is an existence oracle. So the
           assertion is on the MECHANISM and on what must NOT appear: the
           screen's own empty sentence, which would tell an operator their
           filters are wrong when their permissions are. */
        await stubAll(page);
        await page.route(s.api, (route) => json(route, {
          detail: { code: 'NOT_FOUND', message: 'No closure records were found for these filters.' },
        }, 404));
        await signIn(page);
        await gotoScreen(page, s.hash, DEEP);
        const host = page.locator(s.status);
        await expect(host).toBeVisible();
        await expect(host.locator('.empty')).toBeVisible();
        await expect(host, 'a refusal was rendered with the screen\'s own empty-filter wording')
          .not.toContainText(s.empty);
        await expect(host).not.toContainText('403');
        await expect(host).not.toContainText('orbidden');
        await expect(host).not.toContainText('enied');
        // Not the error state either: a refusal is not something to retry.
        await expect(host.locator('button:has-text("Retry")')).toHaveCount(0);
      });

    test(`${s.scr} renders LOADING while the response is outstanding`, async ({ page }) => {
      /* NO FIXED TIMEOUT. The response is held open on a promise this test
         resolves itself, so the loading state is observed deterministically
         rather than raced against a sleep. Registered AFTER stubAll so it wins
         Playwright's reverse-order match. */
      await stubAll(page);
      let release;
      const held = new Promise((resolve) => { release = resolve; });
      await page.route(s.api, async (route) => {
        await held;
        return json(route, { items: [] });
      });
      await signIn(page);
      await page.goto(`/${DEEP}#${s.hash}`);
      await expect(page.locator(`${s.status} .loading`)).toBeVisible();
      release();
      await settleScreen(page);
      await expect(page.locator(`${s.status} .loading`)).toHaveCount(0);
    });
  }
});

/* ========================================================== money ========== */

test.describe('Money is integer paise, and no float touches it', () => {
  test('a paisa that float division would eat survives to the DOM', async ({ page }) => {
    /* 100000000000001 / 100 is 1000000000000.00001 in IEEE-754, and
       `.toFixed(2)` on that is "1000000000000.00" — the paisa vanishes with no
       error anywhere. formatINR divides and modulos as integers, so this
       assertion FAILS the moment anybody "simplifies" it into a float. */
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-budget-revision', DEEP);
    expect(await screenText(page)).toContain(HUGE_RENDERED);
  });

  test('a negative delta renders in parentheses, not by colour alone', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-budget-revision', DEEP);
    const text = await screenText(page);
    expect(text, 'a surrender rendered without its sign, or with a bare minus')
      .toContain('(₹2,500.00)');
  });

  test('the feature source contains no money arithmetic at all', async ({ page }) => {
    /* The structural half. A rendering assertion proves TODAY's numbers are
       right; this proves the class of bug cannot be introduced. Every one of
       these is a canonical way to lose a paisa in a browser, and the feature's
       own headers promise none of them is present. */
    await signIn(page);
    const FILES = [
      'closure-api.js', 'closure-kit.js', 'closure-list.js', 'manifest.js',
      'budget-revision-request.js', 'budget-transfer.js', 'purchase-request-control.js',
      'completion-review.js', 'capitalisation-workbench.js', 'asset-allocation.js',
    ];
    const offences = [];
    for (const file of FILES) {
      const src = await page.evaluate(async (f) => {
        const r = await fetch(`/static/src/features/closure/${f}`);
        return r.text();
      }, file);
      // COMMENTS FIRST. Every one of these files documents the rule it keeps,
      // in prose that necessarily contains the words — a check that matched
      // those would fail on a file that is correct precisely because it
      // explains itself. The rule is about CODE, so the test reads code.
      const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
      for (const [name, re] of [
        ['parseFloat', /\bparseFloat\s*\(/],
        ['toFixed', /\.toFixed\s*\(/],
        ['division by 100', /\/\s*100\b/],
        ['multiplication by 100', /\*\s*100\b/],
        // Arithmetic applied directly to a paise-bearing field. `money(x)` and
        // `figure(l, x)` are fine; `a_paise + b_paise` is a total nobody can
        // reconcile against the ledger.
        ['arithmetic on a _paise field', /_paise\s*[+\-*/]\s*[A-Za-z0-9_.[]/],
      ]) {
        if (re.test(code)) offences.push(`${file}: ${name}`);
      }
    }
    expect(offences, 'money arithmetic entered the closure feature; every figure must be an '
      + 'integer paise value the server sent, formatted by core/format.js').toEqual([]);
  });

  test('an ABSENT total says so, and is never rendered as nil', async ({ page }) => {
    /* Zero and absent are different facts. A CWIP tile reading ₹0.00 says the
       balance was computed and came to nothing; a decision taken against that
       when nobody had computed it is the failure this product exists to
       prevent. */
    await stubAll(page, {
      position: { ...POSITION, cwip_balance_paise: null },
    });
    await signIn(page);
    await gotoScreen(page, 'closure-capitalisation', DEEP);
    const missing = page.locator('#content .closure-figure-missing[data-state="unavailable"]')
      .first();
    await expect(missing).toBeVisible();
    await expect(missing).toContainText('It is NOT nil.');
    await expect(missing, 'an absent CWIP balance rendered as a number').not.toContainText('₹0.00');
  });
});

/* ===================================== SCR-21 — X-01, said before the money === */

test.describe('SCR-21 — no green success stands next to a rupee figure', () => {
  test('the posting note renders ABOVE the balance, from the server\'s own fields',
    async ({ page }) => {
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, 'closure-capitalisation', DEEP);
      const note = page.locator('#content .closure-posting-note').first();
      await expect(note).toBeVisible();
      await expect(note).toHaveAttribute('data-posting-status', 'NOT POSTED');
      await expect(note, 'the X-01 exclusion is not stated on the screen that most needs it')
        .toContainText('no ERP/GL or fixed-asset posting exists in this build');
      // ABOVE the money, not below it: a reader who has already seen a balance
      // and an Approve button has already drawn the inference.
      const order = await page.evaluate(() => {
        const host = document.querySelector('#content .scr-host');
        const n = host.querySelector('.closure-posting-note');
        const f = host.querySelector('.closure-figure');
        return !!(n && f) && (n.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
      });
      expect(order, 'the posting note renders after the CWIP figure').toBe(true);
    });

  test('every request row carries its own posting status, read off the row', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-capitalisation', DEEP);
    const headers = await page.evaluate(
      () => [...document.querySelectorAll('#content .closure-table th')].map((th) => th.textContent.trim()),
    );
    expect(headers).toContain('Posting');
    expect(await screenText(page)).toContain('NOT POSTED');
  });

  test('a build whose server said something else would SHOW what it said', async ({ page }) => {
    // The note is a rendered FIELD, not a literal in the file. This proves it.
    await stubAll(page, {
      position: { ...POSITION, posting_status: 'POSTED', posting_note: 'Posted to GL on 2026-08-01.' },
    });
    await signIn(page);
    await gotoScreen(page, 'closure-capitalisation', DEEP);
    const note = page.locator('#content .closure-posting-note').first();
    await expect(note).toHaveAttribute('data-posting-status', 'POSTED');
    await expect(note).toContainText('Posted to GL on 2026-08-01.');
  });

  test('APPROVING restates the posting note; the success message never stands alone',
    async ({ page }) => {
      /* The single most misreadable moment in the application. A green
         "Capitalisation approved: ₹10,00,00,00,00,000.01" with nothing beside
         it tells a finance controller that money moved. */
      await stubAll(page);
      await signIn(page, CAPITALISER);
      await gotoScreen(page, 'closure-capitalisation', DEEP);
      await page.click('#content button:has-text("Approve capitalisation")');
      const result = page.locator('#capitalisationActionResult');
      await expect(result).toBeVisible();
      await expect(result).toContainText('Capitalisation approved');
      await expect(result).toContainText(HUGE_RENDERED);
      await expect(result, 'the approval reported success and said nothing about X-01')
        .toContainText('no ERP/GL or fixed-asset posting exists in this build');
    });

  test('a refusal is rendered in the SERVER\'s words, with its blockers', async ({ page }) => {
    await stubAll(page);
    await page.route('**/api/closure/requests/*/approve', (route) => json(route, {
      detail: {
        code: 'BLOCKED',
        message: 'This project cannot be capitalised: two blockers remain.',
        blockers: ['Open commitment remains.', 'No completion review has been accepted.'],
      },
    }, 409));
    await signIn(page, CAPITALISER);
    await gotoScreen(page, 'closure-capitalisation', DEEP);
    await page.click('#content button:has-text("Approve capitalisation")');
    const result = page.locator('#capitalisationActionResult');
    await expect(result).toBeVisible();
    await expect(result).toContainText('was refused');
    await expect(result.locator('.closure-blockers li')).toHaveCount(2);
  });

  test('the blocker list comes from the server\'s array, not from split prose',
    async ({ page }) => {
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, 'closure-capitalisation', DEEP);
      const list = page.locator('#capitalisationBlockers .closure-blockers');
      await expect(list).toHaveAttribute('data-blockers', '2');
      await expect(list.locator('li')).toHaveCount(2);
    });

  test('an unattributed receipt is shown at FULL VALUE, never spread pro-rata',
    async ({ page }) => {
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, 'closure-capitalisation', DEEP);
      const text = await screenText(page);
      expect(text).toContain('₹75,000.00');
      expect(text).toContain('never spread pro-rata');
    });
});

/* ============================ SCR-22 — a write-off is not a negative number === */

test.describe('SCR-22 — a write-off is a POSITIVE amount with a flag and a reason', () => {
  test('the write-off row carries a badge and its reason, and its amount is positive',
    async ({ page }) => {
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, 'closure-asset-allocation', DEEP);
      const badge = page.locator('#content .closure-writeoff');
      await expect(badge).toHaveCount(1);
      await expect(badge).toHaveText('WRITE-OFF');
      await expect(page.locator('#content .closure-table'))
        .toContainText('Trial pits abandoned after the revised soil report');
      expect(await screenText(page)).toContain('₹55,000.00');
    });

  test('NO amount cell carries a minus sign or parentheses', async ({ page }) => {
    /* The rule, asserted where it would actually break: over the rendered
       column. A sign is not a category — a reader scanning this column has to
       see WHICH rupees became an asset and which did not, and a negative
       number says neither. */
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-asset-allocation', DEEP);
    const amounts = await page.evaluate(() => {
      const table = document.querySelector('#content .closure-table');
      const heads = [...table.querySelectorAll('th')].map((th) => th.textContent.trim());
      const i = heads.indexOf('Amount');
      return [...table.querySelectorAll('tbody tr')]
        .map((tr) => tr.children[i].textContent.trim());
    });
    expect(amounts.length).toBeGreaterThan(0);
    for (const a of amounts) {
      expect(a, `an allocation amount rendered as a negative: ${a}`).not.toMatch(/[(-]/);
    }
  });

  test('the capitalised row is labelled Capitalised, so the badge is a real distinction',
    async ({ page }) => {
      // The positive control. Without it, a screen that badged every row would
      // pass the assertion above.
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, 'closure-asset-allocation', DEEP);
      await expect(page.locator('#content .closure-table')).toContainText('Capitalised');
    });

  test('the form offers a flag and a reason, and no rupee field', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-asset-allocation', DEEP);
    await expect(page.locator('#assetAllocationWriteoff')).toHaveAttribute('type', 'checkbox');
    await expect(page.locator('#assetAllocationReason')).toBeAttached();
    await expect(page.locator('#assetAllocationAmount'))
      .toHaveAttribute('inputmode', 'numeric');
    expect(await screenText(page)).toContain('integer paise');
  });

  test('a negative amount is REFUSED here, before it can reach the server',
    async ({ page }) => {
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, 'closure-asset-allocation', DEEP);
      await page.fill('#assetAllocationAmount', '-5500000');
      await page.click('#content button:has-text("Record allocation")');
      const result = page.locator('#assetAllocationResult');
      await expect(result).toBeVisible();
      await expect(result).toContainText('positive whole number of paise');
      await expect(result, 'the refusal does not say what a write-off actually is')
        .toContainText('POSITIVE amount with the write-off flag set');
    });

  test('a fractional amount is REFUSED rather than rounded', async ({ page }) => {
    // `parseInt('12.9')` is 12, silently. That is the rounding this surface
    // refuses, and it refuses it on the client as well as on the server.
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-asset-allocation', DEEP);
    await page.fill('#assetAllocationAmount', '12.9');
    await page.click('#content button:has-text("Record allocation")');
    await expect(page.locator('#assetAllocationResult'))
      .toContainText('positive whole number of paise');
  });
});

/* ================================= SCR-12 — both legs, or the row is absent === */

test.describe('SCR-12 — both legs of a transfer, always', () => {
  test('every row renders a From cell AND a To cell', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-budget-transfer', DEEP);
    const headers = await page.evaluate(
      () => [...document.querySelectorAll('#content .closure-table th')].map((th) => th.textContent.trim()),
    );
    expect(headers).toContain('From cell');
    expect(headers).toContain('To cell');
    const text = await screenText(page);
    expect(text, 'the source leg is missing').toContain('WBS-A-CIVIL');
    expect(text, 'the destination leg is missing').toContain('WBS-A-ELEC');
    expect(text).toContain('Civil works');
    expect(text).toContain('Electrical');
  });

  test('the screen states that a half-scoped transfer is not listed at all',
    async ({ page }) => {
      // Not an error and not half a row. The server applies the scope
      // predicate to BOTH legs and returns the row only if both pass.
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, 'closure-budget-transfer', DEEP);
      expect(await screenText(page)).toContain('half a transfer is worse than none');
    });

  test('the availability refusal is NOT predicted in the browser', async ({ page }) => {
    // MSG-BUD-006 runs on the server at the moment of the write. A second
    // implementation here would be wrong as soon as a commitment lands
    // between the read and the write, and the screen says so.
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-budget-transfer', DEEP);
    const text = await screenText(page);
    expect(text).toContain('MSG-BUD-006');
    expect(text).toContain('runs on the server');
  });

  test('SCR-11 and SCR-12 offer no write control, and say why', async ({ page }) => {
    /* A DISABLED button would read as a permission problem, which it is not.
       Both screens are read surfaces over routers this stream does not own. */
    await stubAll(page);
    await signIn(page);
    for (const [hash, noteId] of [
      ['closure-budget-revision', '#budgetRevisionReadOnly'],
      ['closure-budget-transfer', '#budgetTransferReadOnly'],
    ]) {
      await gotoScreen(page, hash, DEEP);
      await expect(page.locator(noteId)).toBeVisible();
      const disabled = await page.evaluate(
        () => [...document.querySelectorAll('#content button')].filter((b) => b.disabled).length,
      );
      expect(disabled, 'a disabled control on a read-only screen reads as a permission problem')
        .toBe(0);
    }
  });
});

/* ============================== SCR-14 — the cell a controller opens this for === */

test.describe('SCR-14 — an exception-approved commitment is named, with its reason', () => {
  test('the exception reason is rendered, and emphasised', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-pr-control', DEEP);
    const text = await screenText(page);
    expect(text).toContain('EXCEEDS_BUDGET');
    expect(text).toContain('Plant shutdown window closes on 30 June');
    await expect(page.locator('#purchaseRequestExceptions')).toBeVisible();
    await expect(page.locator('#purchaseRequestExceptions'))
      .toContainText('approved over a budget refusal');
  });

  test('a list with no exception shows no exception banner', async ({ page }) => {
    // The positive control: a banner that always appeared would prove nothing.
    await stubAll(page, {
      purchaseRequests: { items: [PURCHASE_REQUESTS.items[1]] },
    });
    await signIn(page);
    await gotoScreen(page, 'closure-pr-control', DEEP);
    await expect(page.locator('#purchaseRequestExceptions')).toHaveCount(0);
  });

  test('a request with no recorded check does not read as one that passed',
    async ({ page }) => {
      await stubAll(page, {
        purchaseRequests: {
          items: [{ ...PURCHASE_REQUESTS.items[1], check_result: null }],
        },
      });
      await signIn(page);
      await gotoScreen(page, 'closure-pr-control', DEEP);
      const text = await screenText(page);
      expect(text).toContain('not recorded');
      expect(text).not.toContain('WITHIN_BUDGET');
    });

  test('a live hold of zero is a real figure, not an absent one', async ({ page }) => {
    // A request that reserves nothing HOLDS nothing — that is a computed zero
    // from the server, and it renders as a number rather than as an em dash.
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-pr-control', DEEP);
    const holds = await page.evaluate(() => {
      const table = document.querySelector('#content .closure-table');
      const heads = [...table.querySelectorAll('th')].map((th) => th.textContent.trim());
      const i = heads.indexOf('Live hold');
      return [...table.querySelectorAll('tbody tr')].map((tr) => tr.children[i].textContent.trim());
    });
    expect(holds).toContain('₹0.00');
  });
});

/* ============================================== SCR-20 — two panels, two loads === */

test.describe('SCR-20 — the position and the review list fail independently', () => {
  test('a broken position does not make a working review list look broken',
    async ({ page }) => {
      /* Driving both from one loader would mean a failure in either renders
         the other as broken — and, worse, a build that mounts the review
         routes but not the position route would show "unavailable" over a
         working review list. */
      await stubAll(page);
      await page.route('**/api/closure/projects/*/position',
        (route) => json(route, { detail: 'boom' }, 500));
      await signIn(page);
      await gotoScreen(page, 'closure-completion-review', DEEP);
      await expect(page.locator('#completionReviewPositionStatus .msg-error')).toBeVisible();
      await expect(page.locator('#completionReviewListStatus')).toBeHidden();
      await expect(page.locator('#content .closure-table')).toBeVisible();
    });

  test('both controls are offered to everyone; the server decides', async ({ page }) => {
    // A control hidden on a client-side permission guess makes a refusal look
    // like a missing feature, and the client does not hold the answer.
    await stubAll(page);
    await signIn(page, AUDITOR);
    await gotoScreen(page, 'closure-completion-review', DEEP);
    for (const label of ['Raise review', 'Assert completion', 'Accept', 'Reject']) {
      const button = page.locator(`#content button:has-text("${label}")`).first();
      await expect(button, `${label} is not offered`).toBeVisible();
      await expect(button, `${label} was hidden behind a client-side permission guess`)
        .toBeEnabled();
    }
  });

  test('the completion date is typed, never defaulted', async ({ page }) => {
    // Nothing in this system knows when the last bolt was tightened.
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-completion-review', DEEP);
    await expect(page.locator('#completionReviewDate')).toHaveValue('');
    expect(await screenText(page)).toContain('nothing in this system knows it');
  });

  /* THE TWO PANELS ARE ASSERTED IN SEPARATE TESTS, not in one test that calls
     stubAll() twice. Two stubAll() calls in one test leave two handlers
     registered for the same glob and the winner is a property of registration
     order rather than of anything the test states — which is exactly the
     shadowing bug stubAll()'s own comment warns about, reproduced inside a
     test. One fixture per test, and the pair is the control. */
  test('a project with blockers shows the blocker panel and no all-clear', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-completion-review', DEEP);
    await expect(page.locator('#completionReviewBlockers')).toBeVisible();
    await expect(page.locator('#completionReviewClear')).toHaveCount(0);
  });

  test('a project with none shows the all-clear, and it still says nothing is posted',
    async ({ page }) => {
      await stubAll(page, { position: { ...POSITION, blockers: [] } });
      await signIn(page);
      await gotoScreen(page, 'closure-completion-review', DEEP);
      await expect(page.locator('#completionReviewClear')).toBeVisible();
      await expect(page.locator('#completionReviewClear'),
        'the all-clear panel reads as permission to capitalise without restating X-01')
        .toContainText('posted nowhere');
      await expect(page.locator('#completionReviewBlockers')).toHaveCount(0);
    });
});

/* ========================================================== permissions ==== */

test.describe('Permission gating', () => {
  test('the permission gate is live, and is refusing something', async ({ page }) => {
    /* THE HONEST NEGATIVE CONTROL.

       `auth.PERMISSIONS['budget.read']` is declared as `ROLES` — the whole
       tuple — so every one of the nine seeded identities holds it and NO demo
       principal exists that the closure gate would refuse. Asserting that the
       six screens are reachable therefore proves nothing on its own: a build
       with the gate deleted would pass it too.

       So the negative is taken on the SAME `viewAllowed()` call, over the SAME
       SCR_ROUTES table, at a route whose permission these principals really do
       lack. If that refusal works, the mechanism gating the closure rows works;
       what remains is a statement about the grants, which is asserted
       separately below. Reported to the lead as a coverage gap: a role holding
       no `budget.read` would be needed to close it. */
    await stubAll(page);
    await signIn(page, CAPITALISER);
    const verdicts = await page.evaluate((ids) => {
      // eslint-disable-next-line no-undef
      const out = Object.fromEntries(ids.map((id) => [id, viewAllowed(id)]));
      // eslint-disable-next-line no-undef
      out['integration-setup'] = viewAllowed('integration-setup');
      // eslint-disable-next-line no-undef
      out['connector-audit'] = viewAllowed('connector-audit');
      return out;
    }, SCREENS.map((s) => s.hash));

    for (const s of SCREENS) {
      expect(verdicts[s.hash], `${s.scr} is unreachable for a principal holding budget.read`)
        .toBe(true);
    }
    expect(verdicts['integration-setup'],
      'U-CFO holds no connector.manage, so the gate must refuse SCR-31 — it did not, '
      + 'which means the gate is not deciding anything').toBe(false);
    expect(verdicts['connector-audit'],
      'U-CFO holds no audit.read, so the gate must refuse SCR-40').toBe(false);
  });

  test('two principals with genuinely different grants both hold budget.read',
    async ({ page }) => {
      /* Verified against auth.PERMISSIONS rather than assumed: U-AUD and U-CFO
         differ on six permissions and agree on the one that gates these six
         screens. The differences are asserted, so this cannot silently become
         a comparison of one identity with itself. */
      await stubAll(page);

      await signIn(page, AUDITOR);
      // eslint-disable-next-line no-undef
      const auditor = await page.evaluate(() => [...S.perms].sort());

      /* A REAL SIGN-OUT, through the shell's own control. The session is a
         server-issued id held in sessionStorage and sent as X-Session, so
         clearing localStorage or reloading changes nothing — the second
         signIn() would find the shell already up and time out waiting for a
         login form that is hidden. That is how this test first failed. */
      await page.click('#signOutBtn');
      await page.waitForSelector('#loginForm', { state: 'visible', timeout: 15_000 });

      await signIn(page, CAPITALISER);
      // eslint-disable-next-line no-undef
      const capitaliser = await page.evaluate(() => [...S.perms].sort());

      expect(auditor, 'U-AUD does not hold budget.read').toContain('budget.read');
      expect(capitaliser, 'U-CFO does not hold budget.read').toContain('budget.read');

      expect(auditor).toContain('connector.read');
      expect(capitaliser).not.toContain('connector.read');
      expect(capitaliser).toContain('capitalisation.approve');
      expect(auditor).not.toContain('capitalisation.approve');
      expect(auditor).not.toContain('capitalisation.allocate');
    });

  test('the six screens are gated on budget.read and on nothing narrower', async ({ page }) => {
    /* Every closure read route sits behind `require_closure_access`, which
       requires `budget.read` before any route-specific permission is
       considered. Declaring anything narrower in the manifest would hide a
       screen from someone the server would have answered; anything wider would
       show a shell that can only ever render a refusal. */
    await stubAll(page);
    await signIn(page);
    const needs = await page.evaluate(async () => {
      const mod = await import('/static/src/features/closure/manifest.js');
      return mod.CLOSURE_SCREENS.map((s) => ({ id: s.id, need: s.need }));
    });
    for (const n of needs) {
      expect(n.need, `${n.id} declares a permission other than budget.read`)
        .toEqual(['budget.read']);
    }
  });

  test('the server refuses an Auditor on no closure route, on permission grounds',
    async ({ page }) => {
      /* THE SERVER HALF. Not a claim about the client's gate: real requests
         through the real router, unstubbed.

         THE EXPECTED STATUS IS NOT 200. This harness runs the `local-demo`
         profile over SQLite and sets no `CAPEX_DB_URL`, and every route on
         this router takes `Depends(_get_database)` — which, with no PostgreSQL
         configured, answers a coded 503 carrying `unavailable: true` BEFORE
         any route body runs. That is the designed degradation, and it is the
         reason the six screens render UNAVAILABLE against this build (asserted
         end-to-end in 'against the REAL backend' below).

         What is asserted here is therefore the thing that is actually about
         permissions: an Auditor is never refused. 401 would mean the session
         did not reach the router, 403 that `require_closure_access` rejected a
         principal who holds `budget.read`, and 404 — under the
         not-found-over-forbidden rule — that a read was refused and collapsed.
         None of the three may appear. */
      await signIn(page, AUDITOR);
      const statuses = await page.evaluate(async (paths) => {
        /* THE SESSION IS A HEADER, NOT A COOKIE. app.js issues every call with
           `X-Session: <server-issued id>` out of sessionStorage; a bare fetch()
           carries no session at all and is answered 401 by every route,
           including ones the principal may perfectly well read. A test that
           forgot this would report a permission refusal that is really a
           missing header — which is how this one first failed. */
        const sid = sessionStorage.getItem('capex.session_id') || '';
        const out = {};
        for (const p of paths) {
          const r = await fetch(p, {
            headers: { Accept: 'application/json', 'X-Session': sid },
          });
          let body = null;
          try { body = await r.json(); } catch { body = null; }
          const detail = body && body.detail;
          out[p] = {
            status: r.status,
            unavailable: !!(detail && typeof detail === 'object' && detail.unavailable),
          };
        }
        return out;
      }, ['/api/control/budget-revisions', '/api/control/budget-transfers',
        '/api/control/purchase-requests', '/api/closure/reviews', '/api/closure/requests']);

      for (const [path, result] of Object.entries(statuses)) {
        expect([401, 403, 404],
          `${path} answered ${result.status} to an Auditor holding budget.read — a refusal, `
          + 'not a degradation').not.toContain(result.status);
        if (result.status !== 200) {
          expect(result.status,
            `${path} failed with ${result.status}, which is neither a result nor the coded `
            + 'unavailable degradation').toBe(503);
          expect(result.unavailable,
            `${path} answered 503 WITHOUT the unavailable envelope, so every screen reading it `
            + 'renders a red fault banner for a build that is simply not configured for this')
            .toBe(true);
        }
      }
    });
});

/* ============================== the real backend, with nothing intercepted === */

test.describe('Against the REAL backend, with no route intercepted', () => {
  for (const s of SCREENS) {
    test(`${s.scr} renders UNAVAILABLE, not EMPTY, when the store cannot answer`,
      async ({ page }) => {
        /* THE END-TO-END PROOF OF THE RULE THIS WHOLE FEATURE IS SHAPED BY.
           Nothing is stubbed here — not /openapi.json, not one endpoint. This
           harness runs `local-demo` over SQLite with no `CAPEX_DB_URL`, so the
           closure router is MOUNTED (its templates are in the live schema, as
           'the path templates ... FastAPI really serves' asserts) and answers
           a coded 503 with `unavailable: true`.

           So this is the exact case the design exists for: a route that is
           present and cannot answer. It must read as "this build cannot serve
           this", naming the route — and NEVER as "no records were found",
           which on a capitalisation screen tells a controller there is nothing
           to capitalise when nobody asked. */
        await signIn(page);
        await gotoScreen(page, s.hash, DEEP);

        const block = page.locator(`${loaderFor(s)} .integration-unavailable`);
        await expect(block,
          `${s.scr} did not render an unavailable state against a 503 it cannot answer`)
          .toBeVisible();
        await expect(block).toContainText('is not available in this build');
        await expect(block, 'the unavailable state does not name the route')
          .toContainText(s.template);

        const text = await screenText(page);
        expect(text, `${s.scr} rendered an EMPTY result over a route that refused to answer`)
          .not.toContain(s.empty);
        // The server's own remedy, rendered verbatim rather than paraphrased —
        // it knows which half is missing and this screen does not.
        expect(text).toContain('the closure API is mounted');
      });
  }
});

/* ======================================================== accessibility ==== */

test.describe('Accessibility', () => {
  test('the live region exists on every screen, before mount() runs', async ({ page }) => {
    /* `announce()` looks `closureLiveRegion` up by id at MOUNT time. If it is
       not in the document by then the lookup returns null and every
       announcement is written into nothing — SILENTLY. A screen-reader user
       would get no feedback on load, empty, error, unavailable or refusal, and
       nothing anywhere would fail. */
    await stubAll(page);
    await signIn(page);
    for (const s of SCREENS) {
      await gotoScreen(page, s.hash, DEEP);
      const region = announced(page);
      await expect(region, `${s.scr} has no live region`).toBeAttached();
      await expect(region).toHaveAttribute('role', 'status');
      await expect(region).toHaveAttribute('aria-live', 'polite');
      await expect(region).toHaveClass(/sr-only/);
    }
  });

  test('EMPTY is announced', async ({ page }) => {
    await stubAll(page);
    await page.route('**/api/control/budget-revisions*', (route) => json(route, { items: [] }));
    await signIn(page);
    await gotoScreen(page, 'closure-budget-revision', DEEP);
    await expect(announced(page)).toHaveText('No budget revision matches this filter.');
  });

  test('UNAVAILABLE is announced, and does not announce an empty result',
    async ({ page }) => {
      await stubAll(page, { paths: ['/api/health', '/api/bootstrap'] });
      await signIn(page);
      await gotoScreen(page, 'closure-budget-revision', DEEP);
      await expect(announced(page))
        .toHaveText('Budget revision requests is not available in this build.');
    });

  test('ERROR is announced', async ({ page }) => {
    await stubAll(page);
    await page.route('**/api/control/budget-revisions*',
      (route) => json(route, { detail: 'boom' }, 500));
    await signIn(page);
    await gotoScreen(page, 'closure-budget-revision', DEEP);
    await expect(announced(page)).toHaveText('Budget revision requests could not be loaded.');
  });

  test('an ACTION result reaches the live region', async ({ page }) => {
    /* THE OUTCOME OF A DECISION IS THE ONE THING THIS REGION MUST CARRY. A
       screen-reader user who pressed Approve has to hear what happened.

       THE REFRESH IS HELD OPEN WHILE THIS IS OBSERVED, and that is not a
       convenience — it is the shape of a DEFECT FOUND BY THIS TEST and
       REPORTED, not papered over. `run()` announces the result and then fires
       `loadPosition()` and `loadRequests()` without awaiting them; each of
       those reaches `ready` and integration-screen.js:137 then speaks a
       generic `${what} is shown.`, replacing the decision in a region that
       holds exactly one message. The guard meant to prevent that
       (`screenSpoke`) reads `announce.calls`, a counter that
       `analytics-kit.js`'s announcer keeps and `integration-kit.js`'s — the
       one every closure screen uses — does not. So the guard is inert here and
       the approval outcome is always talked over.

       Fixing it means editing integration-kit.js or the closure screens.
       Neither is this file's to change, so this test asserts the property that
       IS true — the outcome is announced when it happens — and the clobbering
       is reported to the lead rather than pinned here as though it were
       correct. */
    await stubAll(page);
    let holding = false;
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route(
      (url) => url.pathname === '/api/closure/requests' || url.pathname.endsWith('/position'),
      async (route) => {
        if (holding) await held;
        const path = new URL(route.request().url()).pathname;
        return json(route, path.endsWith('/position') ? POSITION : REQUESTS);
      });

    await signIn(page, CAPITALISER);
    await gotoScreen(page, 'closure-capitalisation', DEEP);

    holding = true;
    await page.click('#content button:has-text("Approve capitalisation")');
    await expect(announced(page)).toContainText('Capitalisation approved');
    release();
    await settleScreen(page);
    // The visible result is NOT transient, whatever the live region ends up
    // saying — which is why the defect above is an accessibility defect and
    // not a data one.
    await expect(page.locator('#capitalisationActionResult'))
      .toContainText('no ERP/GL or fixed-asset posting exists in this build');
  });

  for (const s of SCREENS) {
    test(`${s.scr} is keyboard reachable — every enabled control takes focus`,
      async ({ page }) => {
        await stubAll(page);
        await signIn(page);
        await gotoScreen(page, s.hash, DEEP);
        const unreachable = await page.evaluate(() => [...document.querySelectorAll(
          '#content button, #content select, #content input, #content textarea, #content a[href]')]
          .filter((el) => !el.disabled && el.tabIndex < 0)
          .map((el) => el.id || el.tagName));
        expect(unreachable, 'an enabled control cannot be reached by keyboard').toEqual([]);
      });

    test(`${s.scr} can be traversed with Tab, and focus lands inside the screen`,
      async ({ page }) => {
        await stubAll(page);
        await signIn(page);
        await gotoScreen(page, s.hash, DEEP);
        const first = await page.evaluate(() => {
          const el = document.querySelector('#content input, #content button');
          if (!el) return null;
          el.focus();
          return document.activeElement === el ? (el.id || el.tagName) : null;
        });
        expect(first, `${s.scr} has no focusable control at all`).toBeTruthy();
        await page.keyboard.press('Tab');
        const stillInside = await page.evaluate(
          () => !!document.activeElement && !!document.activeElement.closest('#content'),
        );
        expect(stillInside, 'Tab left the screen after one step').toBe(true);
      });

    test(`${s.scr} does not scroll the document sideways`, async ({ page }) => {
      // AUD-M-004, measured at whichever viewport this project runs.
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, s.hash, DEEP);
      const overflow = await page.evaluate(() => {
        const d = document.documentElement;
        return d.scrollWidth - d.clientWidth;
      });
      expect(overflow, `${s.scr} overflows horizontally by ${overflow}px`)
        .toBeLessThanOrEqual(0);
    });

    test(`${s.scr} has no serious or critical axe violation`, async ({ page }) => {
      await stubAll(page);
      await signIn(page);
      await gotoScreen(page, s.hash, DEEP);
      const results = await new AxeBuilder({ page }).include('#content').analyze();

      /* STRUCTURAL RULES: ZERO, with no allowance. Heading order, form labels,
         list semantics, ARIA validity, duplicate ids, name-role-value — every
         one is inside this feature's control. The failure NAMES THE ELEMENT,
         because a rule id alone costs an hour to act on. */
      const structural = results.violations
        .filter((v) => v.id !== 'color-contrast')
        .flatMap((v) => v.nodes.map((n) => `${v.id} :: ${n.target.join(' ')}`));
      expect(structural).toEqual([]);

      /* COLOUR CONTRAST: pre-existing defects in the byte-frozen,
         client-approved styles.css, which is SHA-256 pinned in
         tests/test_contracts.py and which this stream may not edit. The exact
         COLOUR PAIRS are pinned rather than the rule, exactly as
         tests/vrt/mapping.spec.js and tests/vrt/integration.spec.js pin them —
         so a contrast failure in any OTHER colour, one introduced by
         closure.css for instance, still fails here. */
      const FROZEN_FOREGROUNDS = ['#a66a00', '#917139', '#6b7280'];
      const FROZEN_BACKGROUNDS = ['#ffffff', '#f7f8f9', '#eff1f3', '#eaf4f6', '#fdf3e2'];
      const contrast = results.violations
        .filter((v) => v.id === 'color-contrast')
        .flatMap((v) => v.nodes.map((n) => {
          const data = (n.any && n.any[0] && n.any[0].data) || {};
          return {
            fg: String(data.fgColor || '').toLowerCase(),
            bg: String(data.bgColor || '').toLowerCase(),
            target: n.target.join(' '),
            ratio: data.contrastRatio,
          };
        }));
      for (const c of contrast) {
        expect(FROZEN_FOREGROUNDS,
          `${c.target} fails contrast at ${c.ratio}:1 with foreground ${c.fg}, which is NOT one `
          + 'of the known frozen-stylesheet colours — this is a new defect')
          .toContain(c.fg);
        expect(FROZEN_BACKGROUNDS,
          `${c.target} fails contrast at ${c.ratio}:1 over background ${c.bg}, which is not a `
          + 'frozen :root token — this feature introduced a background it should not have')
          .toContain(c.bg);
      }
    });
  }

  test('every table announces what it is', async ({ page }) => {
    await stubAll(page);
    await signIn(page);
    await gotoScreen(page, 'closure-capitalisation', DEEP);
    const captions = await page.evaluate(
      () => [...document.querySelectorAll('#content .closure-table caption')]
        .map((c) => c.textContent.trim()),
    );
    expect(captions.length).toBeGreaterThan(0);
    for (const c of captions) expect(c.length).toBeGreaterThan(10);
  });
});

/* ================================================ the byte-frozen stylesheet === */

test.describe('The approved stylesheet is untouched', () => {
  test('closure.css declares no custom property and no raw hex colour', async ({ page }) => {
    // The gate in tests/test_contracts.py reads styles.css only. This is the
    // same discipline applied to the file this feature adds: a colour
    // introduced here would be off-token and invisible to that gate.
    await signIn(page);
    const css = await page.evaluate(async () => {
      const r = await fetch('/static/src/features/closure/closure.css');
      return r.text();
    });
    const code = css.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(code.match(/#[0-9A-Fa-f]{6}\b/g) || [], 'a raw hex colour entered closure.css')
      .toEqual([]);
    expect(/^\s*--[a-z0-9-]+\s*:/m.test(code),
      'closure.css declares a custom property; tokens belong in C6 and styles.css').toBe(false);
    expect(code.includes(':root'), 'closure.css opens a :root block').toBe(false);

    /* Every var() it names must already be declared by the frozen stylesheet,
       or it resolves to nothing and the rule silently does not apply — the
       failure mode a token typo produces. */
    const declared = new Set((await page.evaluate(async () => {
      const r = await fetch('/static/styles.css');
      return r.text();
    })).match(/--[a-z0-9-]+(?=\s*:)/g) || []);
    for (const used of code.match(/var\(\s*(--[a-z0-9-]+)/g) || []) {
      const token = used.replace(/var\(\s*/, '');
      expect([...declared], `closure.css uses ${token}, which styles.css does not declare`)
        .toContain(token);
    }
  });

  test('every selector in closure.css is scoped to a closure- class', async ({ page }) => {
    // settings.css once shipped `input:disabled` unscoped and restyled every
    // disabled control in the application.
    await signIn(page);
    const css = await page.evaluate(async () => {
      const r = await fetch('/static/src/features/closure/closure.css');
      return r.text();
    });
    const selectors = css.replace(/\/\*[\s\S]*?\*\//g, '')
      .split('}')
      .map((block) => block.split('{')[0].trim())
      .filter((sel) => sel && !sel.startsWith('@'));
    const unscoped = selectors.filter(
      (sel) => !sel.split(',').every((part) => part.includes('.closure-')),
    );
    expect(unscoped, 'an unscoped selector would restyle approved shell views').toEqual([]);
  });
});
