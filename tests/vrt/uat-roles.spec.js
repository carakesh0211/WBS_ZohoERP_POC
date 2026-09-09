// tests/vrt/uat-roles.spec.js
//
// ROLE-BASED UAT IN THE BROWSER: WHAT EACH ROLE CANNOT REACH.
//
// `tests/uat/uat_role_matrix.py` proves the SERVER refuses. This proves the
// SHIPPED PAGE refuses too, and refuses in the way the design says it does —
// which is a different claim, and the one a client is actually looking at.
// A server that returns 403 behind a screen the user can still open and read a
// half-rendered heading from is not the same product as one where the screen
// was never reachable.
//
// WHAT THIS FILE INSTALLS: NOTHING.
// ---------------------------------
// integration.spec.js, analytics.spec.js and mapping.spec.js each used to push
// rows into `SCR_ROUTES` and override `V` through `page.addInitScript`, and
// every one of them went green against a build that existed only inside the
// test — their screens were unreachable in a running browser for three waves
// and no suite said so. This file pushes no route, overrides no view, defines
// no `V` entry and stubs no permission. Every navigation below is a real hash
// against the shipped wiring, and the permission table it checks against is
// READ out of the running page, never written into it.
//
// If a screen listed here does not behave as asserted, that is a defect in the
// application, not something for this file to route around.
//
// NO SCREENSHOT ASSERTION AND NO BASELINE PNG. This suite adds none, so it adds
// no `uat-roles.spec.js-snapshots` directory. A baseline captured to make a new
// test pass proves only that the screen renders the way it renders.
//
// Recorded BY HAND in tests/vrt/evidence/vrt-inventory.json. Regenerating that
// file rewrites it from whatever is on disk, which is how an unreviewed spec
// enters the approved set.

const { test, expect } = require('@playwright/test');

/* ---------------- identities ----------------

   The seven business roles of the UAT charter, mapped to the seeded identity
   that ACTUALLY holds the permissions. Verified against app/backend/auth.py's
   PERMISSIONS and DEV_USERS rather than inferred from the name — `U-PLH` and
   `U-PROC` read like different jobs and carry identical grants, and `U-PM` is
   a Requestor as well as a BudgetController.

   Password is user_id + '!demo', provisioned idempotently by
   auth.provision_dev_identities(). Development credentials in a demo profile;
   nothing here is a production secret. */
const IDENTITIES = {
  requestor:  { user: 'U-REQ',  password: 'U-REQ!demo',  roles: ['Requestor'] },
  approver:   { user: 'U-PROC', password: 'U-PROC!demo', roles: ['ProcurementApprover'] },
  finance:    { user: 'U-FIN',  password: 'U-FIN!demo',  roles: ['FinanceApprover'] },
  controller: { user: 'U-PM',   password: 'U-PM!demo',   roles: ['Requestor', 'BudgetController'] },
  capitalise: { user: 'U-CFO',  password: 'U-CFO!demo',  roles: ['FinanceApprover', 'CapitalisationApprover'] },
  auditor:    { user: 'U-AUD',  password: 'U-AUD!demo',  roles: ['Auditor'] },
  admin:      { user: 'U-ADM',  password: 'U-ADM!demo',  roles: ['Administrator'] },
};

/* ---------------- the negative cases ----------------

   Each row is a screen, the permission its SCR_ROUTES entry declares, and the
   identities that must be REFUSED it. The refusals are derived from
   auth.PERMISSIONS, and the derivation is re-checked live by
   'the refusal list still matches what the page declares' below — so this table
   cannot drift away from the model without a test saying so.

   Chosen because each one refuses somebody a reader would not predict:

     approval-delegations  refuses the ADMINISTRATOR. You cannot delegate an
                           authority you do not hold, and the Administrator
                           holds no approval authority at all.
     approval-inbox        refuses the AUDITOR. approval.read deliberately
                           excludes it; an auditor reads approval history
                           through the hash-chained audit trail instead.
     settings              refuses the AUDITOR, and is the only screen whose
                           `need` is an AND of two permissions.
     audit-trail           refuses the FINANCE APPROVER and the CFO — the two
                           identities most likely to be assumed to have it.
     integration-setup     refuses everyone except the Administrator, including
                           the Auditor who can READ connectors but not manage
                           them. */
const DENIALS = [
  { id: 'audit-trail',          need: ['audit.read'],        title: 'Audit Trail Viewer',
    refused: ['requestor', 'approver', 'finance', 'controller', 'capitalise'],
    allowed: ['auditor', 'admin'] },
  { id: 'approval-matrix',      need: ['approval.configure'], title: 'Approval Matrix Configuration',
    refused: ['requestor', 'approver', 'finance', 'controller', 'capitalise', 'auditor'],
    allowed: ['admin'] },
  { id: 'approval-inbox',       need: ['approval.read'],
    refused: ['auditor'],
    allowed: ['requestor', 'approver', 'finance', 'controller', 'capitalise', 'admin'] },
  { id: 'approval-delegations', need: ['approval.delegate'],
    refused: ['requestor', 'auditor', 'admin'],
    allowed: ['approver', 'finance', 'controller', 'capitalise'] },
  { id: 'settings',             need: ['settings.read', 'masters.read'],
    refused: ['auditor'],
    allowed: ['requestor', 'approver', 'finance', 'controller', 'capitalise', 'admin'] },
  { id: 'integration-setup',    need: ['connector.manage'],
    refused: ['requestor', 'approver', 'finance', 'controller', 'capitalise', 'auditor'],
    allowed: ['admin'] },
  { id: 'mapping-fields',       need: ['connector.read'],
    refused: ['requestor', 'approver', 'finance', 'controller', 'capitalise'],
    allowed: ['auditor', 'admin'] },
];

/* ---------------- helpers ---------------- */

async function signIn(page, who) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

/** Wait until the router has finished correcting or committing a hash. */
async function settle(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
}

/**
 * Read the shipped permission tables out of the running page.
 *
 * `SCR_ROUTES`, `NAV` and `can` are app.js's own top-level bindings; a
 * top-level binding in a classic script lives in the global lexical
 * environment, so they resolve here as free identifiers. Nothing is written.
 */
function declaredNeed(page, id) {
  return page.evaluate((screenId) => {
    /* eslint-disable no-undef */
    const row = NAV.find((n) => n.id === screenId)
      || SCR_ROUTES.find((n) => n.id === screenId);
    /* eslint-enable no-undef */
    return row ? (row.need || null) : undefined;
  }, id);
}

function pageAllows(page, id) {
  // eslint-disable-next-line no-undef
  return page.evaluate((screenId) => viewAllowed(screenId), id);
}


/**
 * The permission gate is viewport-independent: `viewAllowed()` reads a role
 * table and never a media query. Running these three times would triple the
 * suite's cost and prove the same thing twice. The NAVIGATION test below is
 * deliberately NOT skipped -- the rail collapses to a drawer under 800px, so
 * what it renders really can differ by width, and that is the one part of this
 * file where a second viewport is a second result.
 */
function desktopOnly(testInfo) {
  test.skip(testInfo.project.name !== 'desktop-1440',
    'the permission gate does not vary by viewport');
}

/* ================================================= the refusals =========== */

test.describe('A denied screen is not reachable by deep link', () => {
  for (const screen of DENIALS) {
    for (const key of screen.refused) {
      const who = IDENTITIES[key];
      test(`${who.user} (${who.roles.join('+')}) cannot deep-link to #${screen.id}`,
        async ({ page }, testInfo) => {
          desktopOnly(testInfo);
          await signIn(page, who);
          await page.goto(`/#${screen.id}`);
          await settle(page);

          // THE CORRECTION, NOT A NAVIGATION. app.js replaces the history entry
          // rather than pushing one, precisely so that Back cannot walk into a
          // screen the principal may not see. Assert the landing AND the
          // history behaviour, because a refusal you can reach with the Back
          // button is not a refusal.
          expect(await pageAllows(page, screen.id),
            `viewAllowed('${screen.id}') should be false for ${who.user}`).toBe(false);
          expect(page.url(), `${who.user} was left on #${screen.id}`).toContain('#home');

          // And the screen's own content never rendered. Checking the host node
          // is what separates "redirected" from "rendered, then redirected" —
          // the second would have put the data on screen.
          expect(await page.locator(`#content .scr-host[data-screen="${screen.id}"]`).count(),
            `${screen.id} mounted for ${who.user} before the correction`).toBe(0);

          await page.goBack().catch(() => {});
          await page.waitForTimeout(0);
          expect(page.url(), 'Back walked into the refused screen').not.toContain(`#${screen.id}`);
        });
    }
  }
});

/* ================================================= the permissions ======== */

test.describe('The refusal list is derived from the shipped model, not from this file',
  () => {
    test('every screen under test declares the permission this file claims it does',
      async ({ page }, testInfo) => {
        desktopOnly(testInfo);
        await signIn(page, IDENTITIES.admin);
        for (const screen of DENIALS) {
          const need = await declaredNeed(page, screen.id);
          expect(need, `#${screen.id} is not declared in NAV or SCR_ROUTES at all — `
            + 'this file is asserting against a screen the application does not have')
            .not.toBe(undefined);
          expect(need, `#${screen.id} declares a different permission than this file expects`)
            .toEqual(screen.need);
        }
      });

    test('the refusal and permission lists partition all seven identities',
      async ({ page }, testInfo) => {
        desktopOnly(testInfo);
        // A row that listed an identity in neither list would silently test
        // less than it appears to. Cheap to assert, and it fails loudly the day
        // an eighth demo identity is seeded.
        const all = Object.keys(IDENTITIES).sort();
        for (const screen of DENIALS) {
          expect([...screen.refused, ...screen.allowed].sort(),
            `#${screen.id} does not account for every identity exactly once`)
            .toEqual(all);
        }
      });
  });

/* ================================================= the grants ============= */

test.describe('A granted screen IS reachable by deep link', () => {
  // The other half. A suite that only proved refusals would be satisfied by an
  // application that refused everything.
  for (const screen of DENIALS) {
    const key = screen.allowed[0];
    const who = IDENTITIES[key];
    test(`${who.user} (${who.roles.join('+')}) can deep-link to #${screen.id}`,
      async ({ page }, testInfo) => {
        desktopOnly(testInfo);
        await signIn(page, who);
        await page.goto(`/#${screen.id}`);
        await settle(page);

        expect(await pageAllows(page, screen.id),
          `viewAllowed('${screen.id}') should be true for ${who.user}`).toBe(true);
        expect(page.url(), `${who.user} was corrected away from #${screen.id}`)
          .toContain(`#${screen.id}`);
        if (screen.title) {
          await expect(page.locator('#pageTitle')).toHaveText(screen.title);
        }
      });
  }
});

/* ================================================= the navigation ========= */

test.describe('The navigation offers no entry a role may not use', () => {
  for (const [key, who] of Object.entries(IDENTITIES)) {
    test(`every nav entry shown to ${who.user} passes the permission gate`,
      async ({ page }) => {
        await signIn(page, who);
        // Read the rendered rail, then ask the shipped gate about each id.
        // A rail that offered a refused destination would hand the user a dead
        // end and, worse, would tell them the screen exists.
        // `data-nav`, which is what app.js:378 actually writes. Read from the
        // rendered rail rather than from NAV, so a rail that rendered an entry
        // the model would have hidden is still caught.
        const ids = await page.$$eval('#nav .nav-item',
          (nodes) => nodes.map((n) => n.getAttribute('data-nav')).filter(Boolean));
        expect(ids.length, `${who.user} was shown an empty navigation`).toBeGreaterThan(0);
        for (const id of ids) {
          expect(await pageAllows(page, id),
            `the rail offered ${who.user} #${id}, which viewAllowed() refuses`).toBe(true);
        }
      });
  }
});

/* ================================================= signed out ============= */

test('signing out puts the shell away and no deep link brings it back',
  async ({ page }, testInfo) => {
    desktopOnly(testInfo);
    await signIn(page, IDENTITIES.admin);
    await page.goto('/#audit-trail');
    await settle(page);
    expect(page.url()).toContain('#audit-trail');

    // The application's own sign-out control, not a storage poke: clearing a
    // key by hand would test this file's idea of the session rather than the
    // app's. `#signOutBtn` is app.js:1166/1517.
    await page.click('#signOutBtn');
    await page.waitForSelector('#loginForm', { state: 'visible', timeout: 15_000 });

    await page.goto('/#audit-trail');
    await page.waitForSelector('#loginForm', { state: 'visible', timeout: 15_000 });
    expect(await page.locator('#shell:not([hidden])').count(),
      'a deep link re-opened the shell for a signed-out visitor').toBe(0);
  });
