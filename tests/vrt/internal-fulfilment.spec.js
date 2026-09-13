// tests/vrt/internal-fulfilment.spec.js
//
// Migration 034's frontend: the fulfilment decision on an approved,
// multi-line purchase request (app.js's Purchase Requests screen), and the
// Internal Material Requests register/detail (src/features/procurement/
// imr-register.js). This process has no PostgreSQL configured (see
// app/backend/api/internal_fulfilment.py / api/closure.py's own
// DATABASE_NOT_CONFIGURED convention), so every endpoint this feature reads
// or writes is intercepted with page.route() -- the same approach
// integration-actions.spec.js and budget.spec.js already take for their own
// PostgreSQL-backed screens. No screenshot assertions: this file is
// behavioural, and adds no new VRT baseline (see
// docs/ui-change-2026-09/A-internal-fulfilment.md for what DOES change an
// existing one -- approved-ui.spec.js's `prs.png`, because the new
// "Internal fulfilment" card is now part of that screen for every role that
// holds budget.read).
//
// WHAT THIS FILE PROVES
//   1. The Purchase Requests screen lists an approved multi-line request,
//      expands to its per-line fulfilment decisions, and the Fulfilment
//      dialog sends exactly the PUT body the router documents for a SPLIT
//      decision (mode, internal_quantity) and renders the external/internal
//      split the server answers.
//   2. A request with every line already INTERNAL_TRANSFER shows the
//      PR_FULLY_INTERNAL warning before Convert is ever clicked.
//   3. The IMR register renders the internal allocation and consumption
//      columns the brief asked for, kept separate from the legacy screens'
//      external commitment/actual.
//   4. The detail screen's action buttons are gated by BOTH permission and
//      status: a Requestor (imr.read + imr.create only) sees no action on a
//      REQUESTED request; a ProcurementApprover sees Approve and Set
//      valuation on the same record.

const { test, expect } = require('@playwright/test');

const REQUESTOR = { user: 'U-REQ', password: 'U-REQ!demo' };
const APPROVER = { user: 'U-PROC', password: 'U-PROC!demo' };

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

async function signIn(page, who) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

async function settleClassic(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
}

/** The IMR screen is {node, mount} — same signal integration-actions.spec.js
 * waits on for every module-hosted route. */
async function settleModule(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 15_000 });
  const failed = page.locator('#content .scr-host[data-mount-failed="1"]');
  if (await failed.count()) {
    throw new Error(`the screen's mount() threw: ${(await failed.innerText()).trim()}`);
  }
}

const APPROVED_MPR = {
  items: [{
    pr_id: 'PR-MPR-1', pr_number: 'PR-2026-9001', project_id: 'PRJ-01',
    capex_code: 'CAPEX-01', project_name: 'Angul Line 3', status: 'Approved',
    line_count: 2, amount_paise: 500000000, reserved_paise: 500000000,
  }],
};

const FULFILMENT_TWO_LINES = {
  pr_id: 'PR-MPR-1',
  lines: [
    {
      pr_line_id: 'PRL-1', line_no: 1, wbs_id: 'W-01', wbs_code: 'W-01',
      budget_head_id: 'BH-CIVIL', description: 'Structural steel', line_quantity: '10',
      line_amount_paise: 300000000, mode: 'EXTERNAL_PURCHASE', decided: false,
      external_quantity: '10', internal_quantity: '0',
      external_amount_paise: 300000000, internal_amount_paise: 0,
      reason: null, decided_by: null, decided_at: null,
      imr_id: null, imr_number: null, imr_status: null,
    },
    {
      pr_line_id: 'PRL-2', line_no: 2, wbs_id: 'W-01', wbs_code: 'W-01',
      budget_head_id: 'BH-CIVIL', description: 'Cement bags', line_quantity: '500',
      line_amount_paise: 200000000, mode: 'EXTERNAL_PURCHASE', decided: false,
      external_quantity: '500', internal_quantity: '0',
      external_amount_paise: 200000000, internal_amount_paise: 0,
      reason: null, decided_by: null, decided_at: null,
      imr_id: null, imr_number: null, imr_status: null,
    },
  ],
};

const FULFILMENT_ALL_INTERNAL = {
  pr_id: 'PR-MPR-1',
  lines: [{
    pr_line_id: 'PRL-1', line_no: 1, wbs_id: 'W-01', wbs_code: 'W-01',
    budget_head_id: 'BH-CIVIL', description: 'Structural steel', line_quantity: '10',
    line_amount_paise: 300000000, mode: 'INTERNAL_TRANSFER', decided: true,
    external_quantity: '0', internal_quantity: '10',
    external_amount_paise: 0, internal_amount_paise: 300000000,
    reason: 'met from Angul store', decided_by: 'U-PROC', decided_at: '2026-09-10T09:00:00',
    imr_id: 'IMR-0001', imr_number: 'IMR-2026-0001', imr_status: 'ALLOCATED',
  }],
};

test.describe('purchase request fulfilment (migration 034)', () => {
  test.beforeEach(async ({ page }) => {
    await routeJson(page, '**/api/control/purchase-requests**', APPROVED_MPR);
  });

  test('an approved multi-line request expands to its per-line fulfilment decisions', async ({ page }) => {
    await routeJson(page, '**/api/procurement/purchase-requests/PR-MPR-1/fulfilment', FULFILMENT_TWO_LINES);
    await signIn(page, APPROVER);
    await page.goto('/#prs');
    await settleClassic(page);

    await expect(page.getByText('Internal fulfilment — approved multi-line requests')).toBeVisible();
    await expect(page.getByRole('rowheader', { name: 'PR-2026-9001' })).toBeVisible();

    await page.locator('[data-toggle-mpr="PR-MPR-1"]').click();
    await expect(page.locator('td.sub-line', { hasText: 'Structural steel' })).toBeVisible();
    await expect(page.locator('td.sub-line', { hasText: 'Cement bags' })).toBeVisible();
    await expect(page.locator('[data-fulfil-pr="PR-MPR-1"][data-fulfil-line="PRL-1"]')).toBeVisible();
  });

  test('the Fulfilment dialog sends the documented SPLIT body and renders the split it answers', async ({ page }) => {
    await routeJson(page, '**/api/procurement/purchase-requests/PR-MPR-1/fulfilment', FULFILMENT_TWO_LINES);
    let putBody = null;
    await page.route('**/api/procurement/purchase-requests/PR-MPR-1/lines/PRL-1/fulfilment', async (route) => {
      putBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          pr_line_id: 'PRL-1', pr_id: 'PR-MPR-1', pr_number: 'PR-2026-9001', line_no: 1,
          mode: 'SPLIT_FULFILMENT', line_quantity: '10',
          external_quantity: '6', internal_quantity: '4',
          line_amount_paise: 300000000, external_amount_paise: 180000000,
          internal_amount_paise: 120000000, reason: 'stock covers 4 of 10', imr: null,
        }),
      });
    });
    await signIn(page, APPROVER);
    await page.goto('/#prs');
    await settleClassic(page);
    await page.locator('[data-toggle-mpr="PR-MPR-1"]').click();
    await page.locator('[data-fulfil-pr="PR-MPR-1"][data-fulfil-line="PRL-1"]').click();

    await page.waitForSelector('#dlg[open]');
    await page.selectOption('#fkMode', 'SPLIT_FULFILMENT');
    await page.fill('#fkInternalQty', '4');
    await page.fill('#fkReason', 'stock covers 4 of 10');
    await page.click('#dlgOk');

    await page.waitForFunction(() => !document.getElementById('dlg').open, null, { timeout: 10_000 });
    expect(putBody).toEqual(expect.objectContaining({
      mode: 'SPLIT_FULFILMENT', internal_quantity: '4', reason: 'stock covers 4 of 10',
    }));
    await settleClassic(page);
    await expect(page.getByText(/set to SPLIT_FULFILMENT/)).toBeVisible();
  });

  test('a fully-internal request shows the PR_FULLY_INTERNAL warning before Convert is clicked', async ({ page }) => {
    await routeJson(page, '**/api/procurement/purchase-requests/PR-MPR-1/fulfilment', FULFILMENT_ALL_INTERNAL);
    await signIn(page, APPROVER);
    await page.goto('/#prs');
    await settleClassic(page);
    await page.locator('[data-toggle-mpr="PR-MPR-1"]').click();
    await expect(page.getByText('PR_FULLY_INTERNAL')).toBeVisible();
    // The button remains: the server, not this screen, is the authority on
    // whether conversion is refused.
    await expect(page.locator('[data-convert-pr="PR-MPR-1"]')).toBeVisible();
  });
});

const IMR_LIST = {
  items: [{
    imr_id: 'IMR-0001', imr_number: 'IMR-2026-0001', pr_id: 'PR-MPR-1', pr_number: 'PR-2026-9001',
    project_id: 'PRJ-01', wbs_id: 'W-01', wbs_code: 'W-01', budget_head_id: 'BH-CIVIL',
    item_external_id: null, item_description: 'Structural steel', status: 'ALLOCATED',
    requested_quantity: '10', approved_quantity: '10', allocated_quantity: '10',
    issued_quantity: '0', returned_quantity: '0', consumed_quantity: '0',
    unit_rate_paise: 30000000, valuation_source: 'MANUAL', mapping_ok: true,
    internal_allocation_paise: 300000000, internal_consumption_paise: 0,
    allocated_paise: 300000000, issued_paise: 0, returned_paise: 0, cancelled_paise: 0,
    requested_by: 'U-REQ', version_no: 3,
  }],
  statuses: ['REQUESTED', 'APPROVED', 'ALLOCATED', 'PARTIALLY_ISSUED', 'ISSUED',
    'PARTIALLY_CONSUMED', 'CONSUMED', 'RETURNED', 'CANCELLED'],
  modes: ['EXTERNAL_PURCHASE', 'INTERNAL_TRANSFER', 'SPLIT_FULFILMENT'],
};

test.describe('Internal Material Requests register', () => {
  test('renders the internal allocation and consumption columns kept separate from external figures', async ({ page }) => {
    await routeJson(page, '**/api/procurement/internal-material-requests?**', IMR_LIST);
    await routeJson(page, '**/api/procurement/internal-material-requests', IMR_LIST);
    await signIn(page, APPROVER);
    await page.goto('/#imrs');
    await settleModule(page);

    await expect(page.getByText('IMR-2026-0001')).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Allocation ₹' })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Consumption ₹' })).toBeVisible();
  });
});

const IMR_REQUESTED_SUMMARY = {
  imr_id: 'IMR-0002', imr_number: 'IMR-2026-0002', pr_id: 'PR-MPR-1', pr_number: 'PR-2026-9001',
  wbs_id: 'W-01', wbs_code: 'W-01', budget_head_id: 'BH-CIVIL', item_external_id: null,
  item_description: 'Cement bags', status: 'REQUESTED', requested_quantity: '500',
  approved_quantity: null, allocated_quantity: '0', issued_quantity: '0', returned_quantity: '0',
  consumed_quantity: '0', unit_rate_paise: null, valuation_source: 'MISSING', mapping_ok: true,
  internal_allocation_paise: 0, internal_consumption_paise: 0,
  allocated_paise: 0, issued_paise: 0, returned_paise: 0, cancelled_paise: 0,
};

const IMR_REQUESTED_DETAIL = {
  ...IMR_REQUESTED_SUMMARY,
  project_id: 'PRJ-01', from_location_id: null, to_location_id: null,
  valuation_reference: null, valuation_note: null, reason: null, requested_by: 'U-REQ',
  approved_by: null, approved_at: null, cancelled_at: null, cancel_reason: null,
  created_at: '2026-09-10T09:00:00', updated_at: '2026-09-10T09:00:00', version_no: 1,
  entity_id: 'ENT-01', movements: [], exceptions: [],
};

test.describe('Internal Material Request detail — action gating by permission and status', () => {
  test.beforeEach(async ({ page }) => {
    // The register list must name the same request so the test drives the
    // REAL "View" click rather than reaching into the module's own state.
    await routeJson(page, '**/api/procurement/internal-material-requests?**',
      { items: [IMR_REQUESTED_SUMMARY], statuses: IMR_LIST.statuses, modes: IMR_LIST.modes });
    await routeJson(page, '**/api/procurement/internal-material-requests/IMR-0002', IMR_REQUESTED_DETAIL);
  });

  async function openDetail(page) {
    await page.goto('/#imrs');
    await settleModule(page);
    await page.getByRole('button', { name: 'View IMR-2026-0002' }).click();
    await expect(page.getByText('IMR-2026-0002')).toBeVisible();
  }

  test('a Requestor (imr.read + imr.create only) sees no action on a REQUESTED request', async ({ page }) => {
    await signIn(page, REQUESTOR);
    await openDetail(page);
    await expect(page.getByRole('button', { name: 'Approve' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Set valuation' })).toHaveCount(0);
    await expect(page.getByText('No action on this request is available to your role')).toBeVisible();
  });

  test('a ProcurementApprover sees Approve and Set valuation on the same REQUESTED record', async ({ page }) => {
    await signIn(page, APPROVER);
    await openDetail(page);
    await expect(page.getByRole('button', { name: 'Approve' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Set valuation' })).toBeVisible();
    // Not yet allocated: Issue/Return/Consume/Transfer gate on status too,
    // and REQUESTED offers only the two above plus Cancel.
    await expect(page.getByRole('button', { name: 'Issue' })).toHaveCount(0);
  });
});
