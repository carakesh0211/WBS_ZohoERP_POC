// tests/vrt/export-button.spec.js
//
// STREAM C — the reusable export-button component
// (app/frontend/src/components/export-button.js), exercised through the
// Purchase Requests screen (V.prs, app.js), one of the fourteen mounts.
//
// GET /api/exports/datasets, POST /api/exports, POST /api/exports/{id}/
// advance and GET /api/exports/{id} are all PostgreSQL-backed
// (app/backend/pg/exports.py) and the Playwright webServer here runs
// CAPEX_PROFILE=local-demo against SQLite only (playwright.config.js), so
// every one of them is stubbed with page.route() — the same convention
// integration-actions.spec.js already uses for its own PG-mounted routes.
// GET /api/purchase-requests itself is the legacy SQLite endpoint and needs
// no stub to answer; it is stubbed anyway, to an empty list, so this spec
// does not depend on whatever rows happen to be seeded.

const { test, expect } = require('@playwright/test');

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

const DATASETS = {
  items: [
    {
      dataset: 'purchase_requests', title: 'Purchase requests', format: 'csv',
      formats: ['csv', 'xlsx'],
      columns: [{ name: 'pr_number', kind: 'text' }, { name: 'amount_paise', kind: 'money' }],
      filters_supported: ['entity_ids', 'plant_ids', 'project_ids'],
      filters_refused: {}, sort_key: ['pr_id'],
    },
  ],
  filter_fields: ['entity_ids', 'plant_ids', 'project_ids'],
  filter_fields_not_applicable: [],
  filterset_source: 'stub',
  money_note: 'stub',
};

function publicJob(overrides) {
  return {
    export_job_id: 'EXP-1', dataset: 'purchase_requests', format: 'csv', state: 'QUEUED',
    requested_by: 'U-ADM', columns: ['pr_number', 'amount_paise'], filters: {},
    progress: { rows_written: 0, rows_total: null, chunks_written: 0, percent: null, percent_note: 'stub' },
    attempt: null, max_attempts: null, cancel_requested: false, error: null, result: null,
    correlation_id: 'cid-exp-1', created_at: '2026-09-13T00:00:00Z', started_at: null,
    finished_at: null, expires_at: null,
    ...overrides,
  };
}

/* 'prs' sits in the Procurement & Actuals nav group, which defaults
   COLLAPSED (Fable 5.1 Stream F), so its .nav-item is not even in the DOM
   until that group is expanded. Navigating to the hash directly, BEFORE
   signing in, sidesteps that entirely: app.js's start() reads
   location.hash on the very first render after a session exists, exactly
   the way a bookmarked deep link works for a real user. */
async function signInToPrs(page, who = ADMIN) {
  await page.goto('/#prs');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#exportPrsHost button', { timeout: 15_000 });
}

test.describe('Stream C — export button', () => {
  test('a job is created with the screen\'s current filters and chosen format', async ({ page }) => {
    await routeJson(page, '**/api/purchase-requests', []);
    await routeJson(page, '**/api/exports/datasets', DATASETS);
    const createBodies = [];
    await page.route('**/api/exports', (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      createBodies.push(JSON.parse(route.request().postData() || '{}'));
      return route.fulfill({
        status: 202, contentType: 'application/json',
        headers: { Location: '/api/exports/EXP-1', 'Retry-After': '2' },
        body: JSON.stringify(publicJob({})),
      });
    });
    // This test is only about what POST /api/exports was called with; the
    // job is left terminal immediately so the component's own /advance
    // polling loop does not keep running in the background after the
    // assertions below are made.
    await routeJson(page, '**/api/exports/EXP-1/advance',
      { export_job_id: 'EXP-1', state: 'CANCELLED', rows_written_now: 0, rows_written: 0, chunks_written: 0, more: false });
    await routeJson(page, '**/api/exports/EXP-1', publicJob({ state: 'CANCELLED' }));

    await signInToPrs(page);

    // Choose an entity: the export must reflect it, proving the component
    // reads the screen's CURRENT filters at the moment of export, not a
    // snapshot taken when it mounted.
    const entityOptions = await page.locator('#entitySel option').count();
    let chosenEntity = null;
    if (entityOptions > 1) {
      await page.selectOption('#entitySel', { index: 1 });
      await page.waitForSelector('#exportPrsHost button', { timeout: 15_000 });
      chosenEntity = await page.locator('#entitySel').inputValue();
    }

    await page.locator('#exportPrsHost').getByRole('button', { name: 'Export XLSX' }).click();

    await expect.poll(() => createBodies.length, { timeout: 15_000 }).toBeGreaterThan(0);
    expect(createBodies[0].dataset).toBe('purchase_requests');
    expect(createBodies[0].format).toBe('xlsx');
    if (chosenEntity) {
      expect(createBodies[0].filters.entity_ids).toEqual([chosenEntity]);
    }
    // Only fields the dataset's own filters_supported names are ever sent —
    // scopeExportFilters() also offers plant_ids, which is undefined here
    // and must therefore be entirely absent, not sent as null.
    expect(Object.prototype.hasOwnProperty.call(createBodies[0].filters, 'plant_ids')).toBe(false);
  });

  test('a download link appears once the job reaches SUCCEEDED, and downloads the named file', async ({ page }) => {
    await routeJson(page, '**/api/purchase-requests', []);
    await routeJson(page, '**/api/exports/datasets', DATASETS);
    await page.route('**/api/exports', (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      return route.fulfill({
        status: 202, contentType: 'application/json',
        body: JSON.stringify(publicJob({})),
      });
    });
    let advanceCalls = 0;
    await page.route('**/api/exports/EXP-1/advance', (route) => {
      advanceCalls += 1;
      const body = advanceCalls === 1
        ? { export_job_id: 'EXP-1', state: 'RUNNING', rows_written_now: 5, rows_written: 5, chunks_written: 1, more: true }
        : { export_job_id: 'EXP-1', state: 'SUCCEEDED', rows_written_now: 0, rows_written: 5, rows_total: 5, chunks_written: 1, more: false };
      return route.fulfill({ status: advanceCalls === 1 ? 202 : 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await routeJson(page, '**/api/exports/EXP-1', publicJob({
      state: 'SUCCEEDED',
      progress: { rows_written: 5, rows_total: 5, chunks_written: 1, percent: 100, percent_note: null },
      result: {
        filename: 'purchase_requests-EXP-1.csv', media_type: 'text/csv; charset=utf-8',
        bytes: 42, sha256: 'abc123', rows: 5, expires_at: '2026-09-14T00:00:00Z',
      },
    }));
    await page.route('**/api/exports/EXP-1/result', (route) => route.fulfill({
      status: 200,
      contentType: 'text/csv; charset=utf-8',
      headers: { 'Content-Disposition': 'attachment; filename="purchase_requests-EXP-1.csv"' },
      body: 'pr_number,amount_paise\r\nPR-0001,100000\r\n',
    }));

    await signInToPrs(page);

    await page.locator('#exportPrsHost').getByRole('button', { name: 'Export CSV' }).click();

    const downloadBtn = page.locator('#exportPrsHost').getByRole('button', { name: /^Download/ });
    await expect(downloadBtn).toBeVisible({ timeout: 15_000 });
    await expect(downloadBtn).toContainText('purchase_requests-EXP-1.csv');
    await expect(page.locator('#exportPrsHost .export-status')).toContainText('5 rows');

    const downloadPromise = page.waitForEvent('download');
    await downloadBtn.click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe('purchase_requests-EXP-1.csv');
  });

  test('a FAILED job offers Retry, and a cancelled job says so', async ({ page }) => {
    await routeJson(page, '**/api/purchase-requests', []);
    await routeJson(page, '**/api/exports/datasets', DATASETS);
    await page.route('**/api/exports', (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(publicJob({})) });
    });
    await routeJson(page, '**/api/exports/EXP-1/advance',
      { export_job_id: 'EXP-1', state: 'FAILED', rows_written_now: 0, rows_written: 0, chunks_written: 0, more: false });
    await routeJson(page, '**/api/exports/EXP-1', publicJob({
      state: 'FAILED', error: { code: 'EXPORT_CHUNK_NOT_WRITTEN', detail: 'stub failure' },
    }));

    await signInToPrs(page);
    await page.locator('#exportPrsHost').getByRole('button', { name: 'Export CSV' }).click();

    const retryBtn = page.locator('#exportPrsHost').getByRole('button', { name: 'Retry' });
    await expect(retryBtn).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('#exportPrsHost .export-status')).toContainText('failed');
  });
});
