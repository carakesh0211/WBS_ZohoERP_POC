// tests/vrt/budget-setup.spec.js
//
// "Budget Setup" and "Budget Categories" — the frontend for
// app/backend/api/budgets_original.py (migration 026), routed by
// src/core/router.js as the SPA hashes #budget-setup and #budget-categories.
// Neither screen carries an SCR number (router.js: "C8_screens.json is
// frozen at forty and names nothing for original-budget creation or its
// category master"), so this file names them the way router.js itself does.
//
// Conventions follow tests/vrt/spa-routing.spec.js (these are SPA routes, not
// standalone host pages: sign in through the real #loginForm so the session,
// permission set and shell chrome are the real thing, then intercept ONLY
// the four endpoint families named in the assignment with page.route()) and
// tests/vrt/budget.spec.js (routeJson() fixtures, waiting for `.loading` /
// `.audit-skel-row` to clear rather than a fixed timeout, axe-core, three
// viewports). No static fake data lives in product code — every figure on
// screen came back from a fetch() call.
//
// Endpoints intercepted: /api/budget/originals*, /api/budget/categories*,
// /api/budget/selectors*, /api/budget/custom-fields. Every other request
// (login, bootstrap, dashboard, the shell chrome) reaches the real local-demo
// SQLite server the shared webServer starts, exactly as spa-routing.spec.js
// and nav-rail-budget.spec.js already do.
//
// This file does not own package.json or playwright.config.js and does not
// declare them.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

/* ---------------- identities (auth.PERMISSIONS) ----------------
   Administrator: budget.read, budget.create, budget.category.manage,
   settings.read — reaches every control on both screens.
   Requestor: budget.read, budget.create, settings.read, but NOT
   budget.category.manage — views Budget Categories with every write
   control absent. */
const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };
const REQUESTOR = { user: 'U-REQ', password: 'U-REQ!demo' };

/* ---------------- fixtures ---------------- */

const ORIGINALS_LIST = {
  items: [
    {
      budget_id: 'BUD-0001', budget_number: 'ORB-2026-0001', capex_code: 'CAPEX-01',
      project_name: 'Atha Group WBS Rollout', fiscal_year: 'FY26', title: 'Phase 1 civil works',
      status: 'DRAFT', total_paise: 125000000, line_count: 1, created_by: 'U-REQ', version_no: 1,
    },
    {
      budget_id: 'BUD-0002', budget_number: 'ORB-2026-0002', capex_code: 'CAPEX-01',
      project_name: 'Atha Group WBS Rollout', fiscal_year: 'FY26', title: 'Electrical rollout',
      status: 'SUBMITTED', total_paise: 98000000, line_count: 2, created_by: 'U-REQ', version_no: 2,
    },
  ],
  next_cursor: null,
};

const CUSTOM_FIELDS = {
  applies_to: 'BUDGET',
  items: [
    {
      code: 'region', label: 'Region', data_type: 'SELECT',
      select_options: ['North', 'South', 'East', 'West'], is_required: true,
    },
    { code: 'remarks', label: 'Remarks', data_type: 'TEXT', is_required: false },
  ],
};

const CATEGORIES_LIST = {
  items: [
    {
      category_id: 'CAT-01', code: 'STRUCT', name: 'Structural', description: 'Structural works',
      parent_category_id: null, display_order: 100, active: true, entity_id: null,
      effective_from: '2026-01-01', effective_to: null, version_no: 1,
    },
    {
      category_id: 'CAT-02', code: 'ELEC', name: 'Electrical', description: '',
      parent_category_id: 'CAT-01', display_order: 200, active: false, entity_id: null,
      effective_from: '2026-01-01', effective_to: '2026-06-01', version_no: 2,
    },
  ],
};

/**
 * FIXED DEFECT: app/frontend/src/features/budget/budget-setup.js's
 * addLine(prefill) used to call governed-select's presetSelection() on each
 * line field BEFORE linesContainer.appendChild(card) connected the card to
 * the document. components/budget/governed-select.js only builds its
 * internal <input> in connectedCallback(), so presetSelection() on a line
 * with any id field set (wbs_id, budget_head_id, ...) threw
 * "TypeError: Cannot set properties of undefined (setting 'value')" —
 * confirmed live via a page.on('pageerror') capture while authoring this
 * suite. This crashed applyDocToForm() for ANY document that already had a
 * line, which meant: opening an existing budget with lines, and the
 * doc-refresh that runs right after a successful Save/Submit/Import, all
 * broke in the real application whenever the returned document was not
 * empty. addLine() now pushes the entry and appends the card BEFORE applying
 * any prefill, so this no longer crashes (see the dedicated "reopening a
 * saved budget with an existing line" test in the list describe block
 * below, which deliberately returns a populated line to prove it). The
 * default fixture here still returns NO lines, purely so tests that are not
 * specifically about this scenario stay focused on what they are actually
 * asserting; the REQUEST body a test cares about is asserted from what was
 * actually posted, not from what the response echoes back.
 */
function defaultOriginalDoc(over = {}) {
  return {
    budget_id: 'BUD-0001',
    budget_number: 'ORB-2026-0001',
    capex_code: 'CAPEX-01',
    project_id: 'PRJ-01',
    project_name: 'Atha Group WBS Rollout',
    fiscal_year: 'FY26',
    period_id: null,
    title: 'Phase 1 civil works',
    justification: '',
    status: 'DRAFT',
    version_no: 1,
    total_paise: 125000000,
    custom_fields: {},
    lines: [],
    ...over,
  };
}

/* ---------------- small helpers ---------------- */

function fulfillJson(route, status, body) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => fulfillJson(route, status, body));
}

/** GET /api/budget/originals?..., POST /originals, GET/PUT /originals/{id},
 * POST /originals/{id}/(submit|cancel), GET /originals/{id}/audit, and the
 * three import routes — one dispatcher so tests never fight Playwright's
 * "most-recently-registered route wins" rule across overlapping globs. A
 * test overrides one path by registering '**\/api/budget/originals**' AGAIN
 * afterwards and calling route.fallback() for anything it does not own. */
async function installOriginalsRouter(page) {
  await page.route('**/api/budget/originals**', (route) => {
    const req = route.request();
    const { pathname } = new URL(req.url());
    const method = req.method();
    const readBody = () => { try { return JSON.parse(req.postData() || '{}'); } catch { return {}; } };

    if (pathname === '/api/budget/originals/import/template') {
      return route.fulfill({
        status: 200, contentType: 'text/csv',
        body: 'wbs_id,budget_head_id,budget_category_id,amount_rupees,justification\n',
      });
    }
    if (pathname === '/api/budget/originals/import/preview' && method === 'POST') {
      return fulfillJson(route, 200, { rows: 1, problems: [], valid: true, total_paise: 10000000 });
    }
    if (pathname === '/api/budget/originals/import' && method === 'POST') {
      return fulfillJson(route, 201, defaultOriginalDoc({ budget_id: 'BUD-IMP-1', budget_number: 'ORB-2026-0100' }));
    }
    if (pathname === '/api/budget/originals' && method === 'GET') {
      return fulfillJson(route, 200, ORIGINALS_LIST);
    }
    if (pathname === '/api/budget/originals' && method === 'POST') {
      const body = readBody();
      const lines = body.lines || [];
      // lines: [] in the response, not the posted lines — see
      // defaultOriginalDoc()'s doc comment on the addLine()/presetSelection
      // defect a populated response line would otherwise trigger.
      return fulfillJson(route, 201, defaultOriginalDoc({
        total_paise: lines.reduce((s, l) => s + (l.amount_paise || 0), 0),
      }));
    }
    const m = pathname.match(/^\/api\/budget\/originals\/([^/]+)(\/(submit|cancel|audit))?$/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      const sub = m[3];
      if (!sub && method === 'GET') return fulfillJson(route, 200, defaultOriginalDoc({ budget_id: id }));
      if (!sub && method === 'PUT') {
        const body = readBody();
        const { lines: _ignoredLines, ...rest } = body;
        return fulfillJson(route, 200, defaultOriginalDoc({
          budget_id: id, ...rest, version_no: (body.expected_version || 0) + 1,
        }));
      }
      if (sub === 'submit' && method === 'POST') {
        return fulfillJson(route, 200, { status: 'SUBMITTED', approval_instance_id: 'AR-0007' });
      }
      if (sub === 'cancel' && method === 'POST') return fulfillJson(route, 200, { status: 'CANCELLED' });
      if (sub === 'audit' && method === 'GET') return fulfillJson(route, 200, { entries: [] });
    }
    return route.fulfill({
      status: 404, contentType: 'application/json',
      body: JSON.stringify({ detail: `unhandled originals route in test: ${method} ${pathname}` }),
    });
  });
}

/** GET /api/budget/categories?..., POST /categories, PUT /categories/{id},
 * POST /categories/{id}/deactivate — same one-dispatcher shape as above. */
async function installCategoriesRouter(page) {
  await page.route('**/api/budget/categories**', (route) => {
    const req = route.request();
    const { pathname } = new URL(req.url());
    const method = req.method();
    const readBody = () => { try { return JSON.parse(req.postData() || '{}'); } catch { return {}; } };

    if (pathname === '/api/budget/categories' && method === 'GET') return fulfillJson(route, 200, CATEGORIES_LIST);
    if (pathname === '/api/budget/categories' && method === 'POST') {
      const b = readBody();
      return fulfillJson(route, 201, { category_id: 'CAT-NEW', code: b.code, name: b.name, active: true, version_no: 1 });
    }
    const m = pathname.match(/^\/api\/budget\/categories\/([^/]+)(\/deactivate)?$/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      if (!m[2] && method === 'PUT') {
        const b = readBody();
        return fulfillJson(route, 200, { category_id: id, ...b, version_no: (b.expected_version || 0) + 1, active: true });
      }
      if (m[2] && method === 'POST') return fulfillJson(route, 200, { category_id: id, active: false });
    }
    return route.fulfill({
      status: 404, contentType: 'application/json',
      body: JSON.stringify({ detail: `unhandled categories route in test: ${method} ${pathname}` }),
    });
  });
}

/** GET /api/budget/selectors?kind=&q=&project_id= — every governed-select on
 * both screens goes through this one endpoint. `wbs` REQUIRES project_id per
 * budget-api.js's own contract comment and app/backend/pg/original_budget.py
 * (SELECTOR_NEEDS_PROJECT); this fixture enforces that the same way the real
 * service does, so a caller that bypasses the client-side disabled state
 * still meets a real refusal rather than silent fake data. */
function selectorItemsFor(kind) {
  const MAP = {
    project: [{ id: 'PRJ-01', label: 'CAPEX-01 — Atha Group WBS Rollout' }],
    fiscal_year: [{ id: 'FY26', label: 'FY26' }],
    period: [{ id: 'PER-Q1', label: 'Apr–Jun 2026' }],
    wbs: [{ id: 'WBS-01', label: '01 — Civil works' }],
    budget_head: [{ id: 'CIVIL', label: 'Civil' }],
    budget_category: [{ id: 'CAT-01', label: 'CAT-01 — Structural' }],
  };
  return MAP[kind] || [];
}
async function installSelectorsRouter(page) {
  await page.route('**/api/budget/selectors**', (route) => {
    const url = new URL(route.request().url());
    const kind = url.searchParams.get('kind');
    const projectId = url.searchParams.get('project_id');
    if (kind === 'wbs' && !projectId) {
      return fulfillJson(route, 422, {
        type: 'about:blank', title: 'Unprocessable Entity', status: 422,
        code: 'SELECTOR_NEEDS_PROJECT',
        detail: 'Choose a project before choosing a WBS element.',
      });
    }
    return fulfillJson(route, 200, { kind, items: selectorItemsFor(kind) });
  });
}

async function installCustomFieldsRoute(page) {
  await routeJson(page, '**/api/budget/custom-fields', CUSTOM_FIELDS);
}

async function installAllRouters(page) {
  await installSelectorsRouter(page);
  await installCustomFieldsRoute(page);
  await installOriginalsRouter(page);
  await installCategoriesRouter(page);
}

/* ---------------- sign-in / SPA routing (mirrors spa-routing.spec.js) ---------------- */

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

async function settleShell(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
}

async function settleScreen(page) {
  await settleShell(page);
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 15_000 },
  );
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

/** Attached, not settled — for the "loading never renders blank" test, whose
 * held route would otherwise deadlock settleScreen's own wait for the
 * skeleton to clear. */
async function gotoScreenAttachedOnly(page, hash) {
  await page.goto(`/#${hash}`);
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 15_000 },
  );
}

async function openNewBudget(page) {
  await page.getByRole('button', { name: '+ New Budget' }).click();
  await expect(page.locator('#budgetSetupEditorView')).toBeVisible();
}

/** Pick an option in a <governed-select> combobox by keyboard, exactly the
 * ArrowDown+Enter path the WAI-ARIA combobox-with-list-autocomplete pattern
 * requires — this is the sequence a real keyboard user takes. Waits for the
 * option to render rather than any fixed delay, per this suite's own
 * "no fixed timeouts" rule. */
async function chooseGovernedOption(combo) {
  const listboxId = await combo.getAttribute('aria-controls');
  const listbox = combo.page().locator(`#${listboxId}`);
  await expect(listbox.locator('.gov-select-option').first()).toBeVisible();
  await combo.press('ArrowDown');
  await combo.press('Enter');
}

/** Type a query and choose the resulting option. Waits for the response to
 * THIS EXACT query before touching the keyboard — governed-select.js fires an
 * extra, undebounced empty-query search on focus (when the field is empty),
 * so racing straight into ArrowDown/Enter risks a second render (the real,
 * debounced query landing late) resetting the highlighted index between the
 * two key presses. Waiting for the query's own network response first makes
 * the interaction deterministic without any fixed timeout. */
async function typeAndChoose(combo, query) {
  const page = combo.page();
  const waitForOwnResponse = page.waitForResponse((res) => {
    if (!res.url().includes('/api/budget/selectors')) return false;
    return new URL(res.url()).searchParams.get('q') === query;
  });
  await combo.click();
  await combo.fill(query);
  await waitForOwnResponse;
  await chooseGovernedOption(combo);
}

/** Preset a governed-select's value directly (its own public API — see
 * components/budget/governed-select.js::presetSelection), used only where the
 * keyboard interaction itself is not what the test is proving (e.g. the
 * amount/idempotency-key test below cares about the payload, not about how
 * each selector got its value — that path is covered by the dedicated
 * selectors test). presetSelection dispatches the same 'change' event a real
 * choice does, so the project->WBS project-id wiring still fires. */
async function presetGovernedSelect(page, cssSelector, id, label) {
  await page.evaluate(({ sel, id: i, label: l }) => {
    const el = document.querySelector(sel);
    if (!el) throw new Error(`presetGovernedSelect: no element for ${sel}`);
    el.presetSelection(i, l);
  }, { sel: cssSelector, id, label });
}

/* ==================================================================
   1. #budget-setup — list rendering and the four required states
   ================================================================== */

test.describe('Budget Setup — list', () => {
  test.beforeEach(async ({ page }) => {
    await installAllRouters(page);
    await signIn(page, ADMIN);
  });

  test('list renders fixture rows with a status chip carrying a non-colour indicator, and New Budget is visible for budget.create', async ({ page }) => {
    await gotoScreen(page, 'budget-setup');
    const rows = page.locator('#budgetSetupPanelBudgets table tbody tr');
    await expect(rows).toHaveCount(2);
    // Every figure came from the routed fetch, not static markup.
    await expect(rows.nth(0)).toContainText('ORB-2026-0001');
    await expect(rows.nth(1)).toContainText('ORB-2026-0002');
    // Non-colour indicator: each status chip carries its own `.sym` glyph,
    // not merely a background colour — DRAFT and SUBMITTED render distinct
    // symbols (statusChip() throws if a caller ever omits the text label).
    const draftChip = rows.nth(0).locator('.status');
    const submittedChip = rows.nth(1).locator('.status');
    // statusChip() displays row.status verbatim (no re-casing), so the fixture's
    // own 'DRAFT'/'SUBMITTED' values are exactly what renders.
    await expect(draftChip).toContainText('DRAFT');
    await expect(draftChip.locator('.sym')).toBeVisible();
    await expect(submittedChip).toContainText('SUBMITTED');
    await expect(submittedChip.locator('.sym')).toBeVisible();
    expect(await draftChip.locator('.sym').innerText()).not.toBe(await submittedChip.locator('.sym').innerText());

    await expect(page.getByRole('button', { name: '+ New Budget' })).toBeVisible();
  });

  test('reopening a saved budget with an existing line does not throw a page error, and the line prefills', async ({ page }) => {
    // FIXED DEFECT: budget-setup.js's addLine(prefill) used to call
    // governed-select's presetSelection() on each line field BEFORE
    // linesContainer.appendChild(card) connected the card to the document.
    // components/budget/governed-select.js only builds its internal <input>
    // in connectedCallback(), so presetSelection() on a line with any id
    // field set (wbs_id, budget_head_id, ...) threw "TypeError: Cannot set
    // properties of undefined (setting 'value')" -- this crashed
    // applyDocToForm() for ANY document that already had a line, breaking
    // both "reopen an existing budget" and the doc-refresh that runs right
    // after a successful Save/Submit/Import. addLine() now pushes the entry
    // and appends the card to linesContainer BEFORE applying any prefill, so
    // presetSelection() always runs against a connected element. Unlike this
    // file's other fixtures (see defaultOriginalDoc()'s comment), this test
    // deliberately returns a document WITH a populated line, to prove the
    // regression stays fixed; page.on('pageerror') is kept so any future
    // regression here is loud rather than silently swallowed.
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err));

    await page.route('**/api/budget/originals**', (route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (pathname === '/api/budget/originals/BUD-0001' && req.method() === 'GET') {
        return fulfillJson(route, 200, defaultOriginalDoc({
          lines: [{
            wbs_id: 'WBS-01', wbs_code: '01', wbs_description: 'Civil works',
            budget_head_id: 'CIVIL', budget_head_name: 'Civil',
            budget_category_id: 'CAT-01', budget_category_name: 'Structural', budget_category_code: 'CAT-01',
            amount_paise: 125000000, justification: 'Phase 1 civil works line',
            custom_fields: {},
          }],
        }));
      }
      return route.fallback();
    });

    await gotoScreen(page, 'budget-setup');
    await page.getByRole('button', { name: 'ORB-2026-0001' }).click();
    await expect(page.locator('#budgetSetupEditorView')).toBeVisible();

    const line = page.locator('#budgetSetupEditorView .budget-line-card').first();
    await expect(line.getByRole('combobox', { name: 'WBS element *' })).toHaveValue('01 — Civil works');
    await expect(line.getByRole('combobox', { name: 'Budget head *' })).toHaveValue('Civil');
    await expect(line.getByRole('combobox', { name: 'Budget category *' })).toHaveValue('CAT-01 — Structural');

    expect(pageErrors, `unexpected page error(s): ${pageErrors.map((e) => e.message).join('; ')}`).toEqual([]);
  });

  test('loading state renders a table skeleton, never a blank panel', async ({ page }) => {
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route('**/api/budget/originals**', async (route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (pathname === '/api/budget/originals' && req.method() === 'GET') {
        await held;
        return fulfillJson(route, 200, ORIGINALS_LIST);
      }
      return route.fallback();
    });

    await gotoScreenAttachedOnly(page, 'budget-setup');
    await expect(page.locator('#budgetSetupPanelBudgets .audit-skel-row').first()).toBeVisible();
    const panelText = (await page.locator('#budgetSetupPanelBudgets').innerText()).trim();
    expect(panelText.length, 'loading state rendered a blank panel').toBeGreaterThan(0);

    release();
    await expect(page.locator('#budgetSetupPanelBudgets .audit-skel-row')).toHaveCount(0);
    await expect(page.locator('#budgetSetupPanelBudgets table tbody tr')).toHaveCount(2);
  });

  test('empty state explains what would appear and offers New Budget', async ({ page }) => {
    await page.route('**/api/budget/originals**', (route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (pathname === '/api/budget/originals' && req.method() === 'GET') {
        return fulfillJson(route, 200, { items: [], next_cursor: null });
      }
      return route.fallback();
    });
    await gotoScreen(page, 'budget-setup');
    const empty = page.locator('#budgetSetupListStatus .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/no original budgets match these filters/i);
    await expect(empty).toContainText(/New Budget/);
  });

  test('error state shows the server detail, actionable, never a raw traceback', async ({ page }) => {
    await page.route('**/api/budget/originals**', (route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (pathname === '/api/budget/originals' && req.method() === 'GET') {
        return fulfillJson(route, 500, {
          type: 'about:blank', title: 'Internal Server Error', status: 500,
          detail: 'The budget service is temporarily unavailable. Try again in a moment.',
          code: 'BUDGET_ENGINE_UNAVAILABLE',
        });
      }
      return route.fallback();
    });
    await gotoScreen(page, 'budget-setup');
    const banner = page.locator('#budgetSetupListStatus .msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The budget service is temporarily unavailable');
    await expect(banner).not.toContainText(/traceback/i);
    await expect(banner).not.toContainText(/\bat \S+:\d+:\d+/);
    await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('unavailable state (503 DATABASE_NOT_CONFIGURED) renders the same non-blank error state', async ({ page }) => {
    await page.route('**/api/budget/originals**', (route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (pathname === '/api/budget/originals' && req.method() === 'GET') {
        return fulfillJson(route, 503, {
          type: 'about:blank', title: 'Service Unavailable', status: 503,
          detail: 'The budget database is not configured. Contact an administrator.',
          code: 'DATABASE_NOT_CONFIGURED',
        });
      }
      return route.fallback();
    });
    await gotoScreen(page, 'budget-setup');
    const banner = page.locator('#budgetSetupListStatus .msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The budget database is not configured');
    await expect(banner).not.toContainText(/traceback/i);
  });
});

/* ==================================================================
   2. #budget-setup — the document editor
   ================================================================== */

test.describe('Budget Setup — editor', () => {
  test.beforeEach(async ({ page }) => {
    await installAllRouters(page);
    await signIn(page, ADMIN);
    await gotoScreen(page, 'budget-setup');
    await openNewBudget(page);
  });

  test('shows project, fiscal year, period, title and header custom fields, with a required SELECT marked', async ({ page }) => {
    const editor = page.locator('#budgetSetupEditorView');
    await expect(editor.getByRole('combobox', { name: 'Project *' })).toBeVisible();
    await expect(editor.getByRole('combobox', { name: 'Fiscal year *' })).toBeVisible();
    await expect(editor.getByRole('combobox', { name: 'Accounting period' })).toBeVisible();
    await expect(page.locator('#budgetSetupTitle')).toBeVisible();

    const regionField = editor.locator('#budgetSetupHeaderCustomFields .field', { hasText: 'Region' });
    await expect(regionField.locator('label')).toHaveText('Region *');
    const regionSelect = regionField.locator('select');
    await expect(regionSelect).toBeVisible();
    await expect(regionSelect.locator('option')).toHaveCount(5); // the blank "—" plus four options
    await expect(regionSelect.locator('option', { hasText: 'North' })).toHaveCount(1);
  });

  test('a line carries SEPARATE Budget head and Budget category governed selectors; typing queries /api/budget/selectors and ArrowDown+Enter picks an option', async ({ page }) => {
    const editor = page.locator('#budgetSetupEditorView');
    const line = editor.locator('.budget-line-card').first();

    const headCombo = line.getByRole('combobox', { name: 'Budget head *' });
    const catCombo = line.getByRole('combobox', { name: 'Budget category *' });
    await expect(headCombo).toHaveCount(1);
    await expect(catCombo).toHaveCount(1);
    expect(await headCombo.getAttribute('id')).not.toBe(await catCombo.getAttribute('id'));

    const selectorRequests = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/budget/selectors')) selectorRequests.push(req.url());
    });

    // Choose the project first (header field) — this is the field the WBS
    // selector below is gated on.
    const projCombo = editor.getByRole('combobox', { name: 'Project *' });
    await typeAndChoose(projCombo, 'Atha');
    await expect(projCombo).toHaveValue('CAPEX-01 — Atha Group WBS Rollout');

    const wbsCombo = line.getByRole('combobox', { name: 'WBS element *' });
    await expect(wbsCombo).toBeEnabled();
    await typeAndChoose(wbsCombo, 'civil');
    await expect(wbsCombo).toHaveValue('01 — Civil works');
    expect(selectorRequests.some((u) => u.includes('kind=wbs') && u.includes('project_id=PRJ-01'))).toBe(true);

    await typeAndChoose(headCombo, 'civil');
    await expect(headCombo).toHaveValue('Civil');
    expect(selectorRequests.some((u) => u.includes('kind=budget_head'))).toBe(true);

    await typeAndChoose(catCombo, 'struct');
    await expect(catCombo).toHaveValue('CAT-01 — Structural');
    expect(selectorRequests.some((u) => u.includes('kind=budget_category'))).toBe(true);
  });

  test('choosing WBS before a project is refused with the SELECTOR_NEEDS_PROJECT message', async ({ page }) => {
    const editor = page.locator('#budgetSetupEditorView');
    const wbsCombo = editor.locator('.budget-line-card').first().getByRole('combobox', { name: 'WBS element *' });

    // Client-side refusal: the field is disabled before any network call is
    // even attempted.
    //
    // FIXED DEFECT: components/budget/governed-select.js's
    // connectedCallback() used to call this._syncDisabled() — which sets the
    // input's placeholder to "Choose a project first" when the WBS kind has
    // no project-id — and then unconditionally call this._syncPlaceholder()
    // right after, which overwrote it straight back to the generic "Search
    // wbs element…" placeholder from the `placeholder` attribute. So the
    // explanatory placeholder text never actually reached the screen on
    // initial connection (confirmed live: the input WAS correctly disabled,
    // but its placeholder read "Search wbs element…"). connectedCallback()
    // now relies solely on _syncDisabled() (which already calls
    // _syncPlaceholder() itself once it knows the enabled state), so the
    // explanatory placeholder survives.
    await expect(wbsCombo).toBeDisabled();
    await expect(wbsCombo).toHaveAttribute('placeholder', 'Choose a project first');

    // Defense in depth: even a caller that bypasses the disabled state (the
    // governed-select's OWN `disabled` attribute is untouched by the
    // needs-a-project gate — only its internal <input> is) still meets the
    // server's real refusal, word for word.
    const listboxId = await page.evaluate(() => {
      const el = document.querySelector('#budgetSetupEditorView governed-select[kind="wbs"]');
      el._search('civil');
      return el._listbox.id;
    });
    const errNote = page.locator(`#${listboxId} .gov-select-note-error`);
    await expect(errNote).toContainText('Choose a project before choosing a WBS element.');
  });

  test('amount "1250000.00" computes the running total and posts amount_paise 125000000', async ({ page }) => {
    const editor = page.locator('#budgetSetupEditorView');
    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="project"]', 'PRJ-01', 'CAPEX-01 — Atha Group WBS Rollout');
    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="fiscal_year"]', 'FY26', 'FY26');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="wbs"]', 'WBS-01', '01 — Civil works');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_head"]', 'CIVIL', 'Civil');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_category"]', 'CAT-01', 'CAT-01 — Structural');
    await page.fill('#budgetSetupTitle', 'Phase 1 civil works');

    const amountInput = editor.locator('.budget-line-card').first().locator('input[placeholder="e.g. 25,00,000.00"]');
    await amountInput.fill('1250000.00');
    await expect(page.locator('#budgetSetupTotal')).toHaveText('₹12,50,000.00');

    let capturedBody = null;
    let capturedHeaders = null;
    await page.route('**/api/budget/originals**', async (route) => {
      const req = route.request();
      if (req.method() !== 'POST' || new URL(req.url()).pathname !== '/api/budget/originals') return route.fallback();
      capturedBody = req.postDataJSON();
      capturedHeaders = req.headers();
      // The response echoes NO lines back (see defaultOriginalDoc()'s comment
      // above: a lines-populated response crashes addLine()'s prefill path —
      // a real defect, reported rather than fixed here). This test's own
      // assertions are about the REQUEST body, which is captured above
      // regardless of what the response contains.
      return fulfillJson(route, 201, defaultOriginalDoc({ total_paise: 125000000 }));
    });

    await page.getByRole('button', { name: 'Save draft' }).click();
    await expect(page.locator('#budgetSetupEditorStatus .msg-success, #budgetSetupEditorStatus .msg')).toContainText('Draft saved.');

    expect(capturedBody.lines[0].amount_paise).toBe(125000000);
    expect(capturedHeaders['idempotency-key']).toBeTruthy();
    await expect(page.locator('#budgetSetupEditorView h2.section-title').first()).toContainText('ORB-2026-0001');
  });

  test('a 422 BUDGET_LINES_INVALID problem lands on the offending line and field', async ({ page }) => {
    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="project"]', 'PRJ-01', 'CAPEX-01 — Atha Group WBS Rollout');
    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="fiscal_year"]', 'FY26', 'FY26');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="wbs"]', 'WBS-01', '01 — Civil works');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_head"]', 'CIVIL', 'Civil');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_category"]', 'CAT-01', 'CAT-01 — Structural');
    await page.fill('#budgetSetupTitle', 'Phase 1 civil works');
    const amountInput = page.locator('#budgetSetupEditorView .budget-line-card').first().locator('input[placeholder="e.g. 25,00,000.00"]');
    await amountInput.fill('1250000.00');

    await page.route('**/api/budget/originals**', async (route) => {
      const req = route.request();
      if (req.method() !== 'POST' || new URL(req.url()).pathname !== '/api/budget/originals') return route.fallback();
      return fulfillJson(route, 422, {
        detail: {
          type: 'about:blank', title: 'Budget Lines Invalid', status: 422, code: 'BUDGET_LINES_INVALID',
          detail: 'One or more lines could not be validated.',
          problems: [{ line: 1, field: 'amount_paise', code: 'AMOUNT_EXCEEDS_CAP', message: 'This amount exceeds the line cap.' }],
        },
      });
    });

    await page.getByRole('button', { name: 'Save draft' }).click();
    const line = page.locator('#budgetSetupEditorView .budget-line-card').first();
    await expect(line.locator('.field-err').filter({ hasText: 'This amount exceeds the line cap.' })).toBeVisible();
    await expect(page.locator('#budgetSetupEditorStatus .msg-error')).toContainText('One or more lines could not be validated.');
  });

  test('Submit for approval POSTs /submit with the current budget id', async ({ page }) => {
    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="project"]', 'PRJ-01', 'CAPEX-01 — Atha Group WBS Rollout');
    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="fiscal_year"]', 'FY26', 'FY26');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="wbs"]', 'WBS-01', '01 — Civil works');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_head"]', 'CIVIL', 'Civil');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_category"]', 'CAT-01', 'CAT-01 — Structural');
    await page.fill('#budgetSetupTitle', 'Phase 1 civil works');
    const amountInput = page.locator('#budgetSetupEditorView .budget-line-card').first().locator('input[placeholder="e.g. 25,00,000.00"]');
    await amountInput.fill('1250000.00');

    await page.getByRole('button', { name: 'Save draft' }).click();
    await expect(page.locator('#budgetSetupEditorStatus')).toContainText('Draft saved.');

    const submitRequest = page.waitForRequest((req) => req.url().endsWith('/submit') && req.method() === 'POST');
    await page.getByRole('button', { name: 'Submit for approval' }).click();
    const req = await submitRequest;
    expect(new URL(req.url()).pathname).toBe('/api/budget/originals/BUD-0001/submit');
  });

  test('Submit for approval shows SUBMITTED with a link to the approval', async ({ page }) => {
    // FIXED DEFECT: budget-setup.js's doSubmit() used to append the success
    // message -- "Submitted. Approval instance …", the "Open approval
    // request" link, "Go to My Approval Inbox" -- then immediately await
    // getOriginal() and call applyDocToForm(fresh) one more time.
    // applyDocToForm() starts with resetEditorForm(), which unconditionally
    // clears editorStatusHost, wiping the confirmation it had just shown
    // before a user could see it. doSubmit() now builds the confirmation
    // node but appends it to editorStatusHost AFTER the post-submit refresh,
    // so it survives resetEditorForm()'s clear. page.on('pageerror') is kept
    // here (as everywhere else in this file) so any regression stays loud.
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err));

    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="project"]', 'PRJ-01', 'CAPEX-01 — Atha Group WBS Rollout');
    await presetGovernedSelect(page, '#budgetSetupEditorView .budget-doc-header governed-select[kind="fiscal_year"]', 'FY26', 'FY26');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="wbs"]', 'WBS-01', '01 — Civil works');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_head"]', 'CIVIL', 'Civil');
    await presetGovernedSelect(page, '#budgetSetupEditorView governed-select[kind="budget_category"]', 'CAT-01', 'CAT-01 — Structural');
    await page.fill('#budgetSetupTitle', 'Phase 1 civil works');
    const amountInput = page.locator('#budgetSetupEditorView .budget-line-card').first().locator('input[placeholder="e.g. 25,00,000.00"]');
    await amountInput.fill('1250000.00');

    await page.getByRole('button', { name: 'Save draft' }).click();
    await expect(page.locator('#budgetSetupEditorStatus')).toContainText('Draft saved.');

    await page.getByRole('button', { name: 'Submit for approval' }).click();
    const status = page.locator('#budgetSetupEditorStatus');
    await expect(status).toContainText('Submitted');
    await expect(status).toContainText('AR-0007');
    await expect(status.getByRole('link', { name: 'Open approval request' })).toBeVisible();
    await expect(status.getByRole('button', { name: 'Go to My Approval Inbox' })).toBeVisible();

    expect(pageErrors, `unexpected page error(s): ${pageErrors.map((e) => e.message).join('; ')}`).toEqual([]);
  });
});

/* ==================================================================
   3. #budget-setup — Upload budget (CSV import)
   ================================================================== */

test.describe('Budget Setup — upload', () => {
  test.beforeEach(async ({ page }) => {
    await installAllRouters(page);
    await signIn(page, ADMIN);
    await gotoScreen(page, 'budget-setup');
    await page.getByRole('tab', { name: 'Upload budget' }).click();
    await expect(page.locator('#budgetSetupPanelUpload')).toBeVisible();
  });

  test('the template link fetches /api/budget/originals/import/template', async ({ page }) => {
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download CSV template' }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe('original-budget-template.csv');
  });

  test('Preview posts the CSV and renders row/column problems; Import stays disabled while problems exist', async ({ page }) => {
    await presetGovernedSelect(page, '#budgetSetupPanelUpload governed-select[kind="project"]', 'PRJ-01', 'CAPEX-01 — Atha Group WBS Rollout');
    await page.fill('#budgetImportCsv', 'wbs_id,budget_head_id,budget_category_id,amount_rupees\nBAD,CIVIL,STRUCT,not-a-number\n');

    await page.route('**/api/budget/originals**', async (route) => {
      const req = route.request();
      if (req.method() !== 'POST' || new URL(req.url()).pathname !== '/api/budget/originals/import/preview') return route.fallback();
      return fulfillJson(route, 200, {
        rows: 1, valid: false, total_paise: 0,
        problems: [{ row: 2, column: 'amount_rupees', code: 'INVALID_AMOUNT', message: 'Amount must be a valid rupee value.' }],
      });
    });

    await page.getByRole('button', { name: 'Preview' }).click();
    const problems = page.locator('.budget-import-problems');
    await expect(problems).toContainText('Amount must be a valid rupee value.');
    await expect(problems).toContainText('2'); // the row number
    await expect(page.getByRole('button', { name: 'Import' })).toBeHidden();
  });

  test('Preview with no problems enables Import', async ({ page }) => {
    await presetGovernedSelect(page, '#budgetSetupPanelUpload governed-select[kind="project"]', 'PRJ-01', 'CAPEX-01 — Atha Group WBS Rollout');
    await page.fill('#budgetImportCsv', 'wbs_id,budget_head_id,budget_category_id,amount_rupees\nWBS-01,CIVIL,STRUCT,12500.00\n');

    await page.route('**/api/budget/originals**', async (route) => {
      const req = route.request();
      if (req.method() !== 'POST' || new URL(req.url()).pathname !== '/api/budget/originals/import/preview') return route.fallback();
      return fulfillJson(route, 200, { rows: 1, valid: true, total_paise: 1250000, problems: [] });
    });

    await page.getByRole('button', { name: 'Preview' }).click();
    await expect(page.locator('.budget-import-problems')).toContainText('1 row(s) valid.');
    await expect(page.getByRole('button', { name: 'Import' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Import' })).toBeEnabled();
  });
});

/* ==================================================================
   4. #budget-categories
   ================================================================== */

test.describe('Budget Categories', () => {
  test.beforeEach(async ({ page }) => {
    await installAllRouters(page);
  });

  test('list renders fixture rows', async ({ page }) => {
    await signIn(page, ADMIN);
    await gotoScreen(page, 'budget-categories');
    const rows = page.locator('#content table tbody tr');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText('STRUCT');
    await expect(rows.nth(1)).toContainText('ELEC');
    await expect(rows.nth(0).locator('.status')).toContainText('Active');
    await expect(rows.nth(1).locator('.status')).toContainText('Inactive');
  });

  test('create form: an invalid code shape is refused by the server with its own message', async ({ page }) => {
    // FIXED DEFECT: app/frontend/src/features/settings/budget-categories.js
    // openCreate() used to call fillDlg(null) BEFORE
    // document.body.appendChild(dialogEl). fillDlg(null) calls
    // parentEl.clear() on the dialog's <governed-select kind="budget_category">,
    // which had not yet been connected to the document --
    // components/budget/governed-select.js only builds its internal <input>
    // in connectedCallback() -- so this threw "TypeError: Cannot set
    // properties of undefined (setting 'value')" and the dialog never
    // opened. openCreate() now appends the dialog to the document first, so
    // fillDlg() always runs against a connected governed-select. A
    // page.on('pageerror') collector is kept so a regression here is loud.
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err));

    await signIn(page, ADMIN);
    await gotoScreen(page, 'budget-categories');
    await page.getByRole('button', { name: '+ New category' }).click();
    const dialog = page.locator('dialog[aria-labelledby="budgetCategoryDlgTitle"]');
    await expect(dialog).toBeVisible();
    await dialog.locator('#budgetCategoryCode').fill('bad code!');
    await dialog.locator('#budgetCategoryName').fill('Bad code test');

    await page.route('**/api/budget/categories**', async (route) => {
      const req = route.request();
      if (req.method() !== 'POST' || new URL(req.url()).pathname !== '/api/budget/categories') return route.fallback();
      return fulfillJson(route, 422, {
        detail: {
          type: 'about:blank', title: 'Invalid Category Code', status: 422, code: 'INVALID_CATEGORY_CODE',
          detail: 'code must be 2-40 characters of A-Z, 0-9, _ or -, starting with a letter or digit.',
        },
      });
    });
    await dialog.getByRole('button', { name: 'Save' }).click();
    await expect(dialog.locator('.msg-error')).toContainText('code must be 2-40 characters');

    expect(pageErrors, `unexpected page error(s): ${pageErrors.map((e) => e.message).join('; ')}`).toEqual([]);
  });

  test('edit sends expected_version matching the row being edited', async ({ page }) => {
    // FIXED DEFECT: same class of bug as the create-form test above, in
    // openEdit() -- it called fillDlg(row), which presets the parent
    // governed-select, BEFORE document.body.appendChild(dialogEl). "Edit"
    // was therefore also completely non-functional. openEdit() now appends
    // the dialog before calling fillDlg(). A page.on('pageerror') collector
    // is kept so a regression here is loud.
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err));

    await signIn(page, ADMIN);
    await gotoScreen(page, 'budget-categories');
    const row = page.locator('#content table tbody tr').filter({ hasText: 'ELEC' });
    await row.getByRole('button', { name: 'Edit' }).click();
    const dialog = page.locator('dialog[aria-labelledby="budgetCategoryDlgTitle"]');
    await expect(dialog).toBeVisible();

    let capturedBody = null;
    await page.route('**/api/budget/categories**', async (route) => {
      const req = route.request();
      if (req.method() !== 'PUT') return route.fallback();
      capturedBody = req.postDataJSON();
      return fulfillJson(route, 200, { category_id: 'CAT-02', ...capturedBody, active: true });
    });
    await dialog.getByRole('button', { name: 'Save' }).click();
    await expect(dialog).toBeHidden();
    expect(capturedBody.expected_version).toBe(2); // CATEGORIES_LIST's ELEC row carries version_no: 2

    expect(pageErrors, `unexpected page error(s): ${pageErrors.map((e) => e.message).join('; ')}`).toEqual([]);
  });

  test('deactivate asks for confirmation before calling the API', async ({ page }) => {
    await signIn(page, ADMIN);
    await gotoScreen(page, 'budget-categories');
    let deactivateCalled = false;
    await page.route('**/api/budget/categories**', async (route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (pathname.endsWith('/deactivate')) {
        deactivateCalled = true;
        return fulfillJson(route, 200, { category_id: 'CAT-01', active: false });
      }
      return route.fallback();
    });

    const row = page.locator('#content table tbody tr').filter({ hasText: 'STRUCT' });
    await row.getByRole('button', { name: 'Deactivate' }).click();
    const confirmDialog = page.locator('dialog[aria-labelledby="budgetCategoryConfirmTitle"]');
    await expect(confirmDialog).toBeVisible();
    expect(deactivateCalled, 'the API was called before the confirmation was accepted').toBe(false);

    await confirmDialog.getByRole('button', { name: 'Deactivate' }).click();
    await expect(confirmDialog).toBeHidden();
    expect(deactivateCalled).toBe(true);
  });

  test('write controls are absent for U-REQ (no budget.category.manage)', async ({ page }) => {
    await signIn(page, REQUESTOR);
    await gotoScreen(page, 'budget-categories');
    await expect(page.getByRole('button', { name: '+ New category' })).toBeHidden();
    const rows = page.locator('#content table tbody tr');
    await expect(rows).toHaveCount(2);
    for (let i = 0; i < 2; i += 1) {
      await expect(rows.nth(i).getByRole('button', { name: 'Edit' })).toHaveCount(0);
      await expect(rows.nth(i).getByRole('button', { name: 'Deactivate' })).toHaveCount(0);
    }
  });
});

/* ==================================================================
   5. Accessibility — axe-core, list / editor / categories
   ================================================================== */

test.describe('Budget Setup & Categories — accessibility', () => {
  test.beforeEach(async ({ page }) => {
    await installAllRouters(page);
    await signIn(page, ADMIN);
  });

  test('axe-core: Budget Setup list has no violations', async ({ page }) => {
    await gotoScreen(page, 'budget-setup');
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('axe-core: Budget Setup editor (one line) has no violations', async ({ page }) => {
    // FIXED DEFECT: budget-setup.js's per-line Justification field used to
    // render `h('label', {}, 'Justification')` next to (not wrapping) the
    // textarea, with neither a `for` on the label nor an `id` on the
    // textarea -- unlike the header's own Justification field, which
    // correctly pairs `h('label', { for: 'budgetSetupJustification' }, ...)`
    // with `id: 'budgetSetupJustification'`. axe-core's "label" rule (WCAG
    // 4.1.2, critical impact) failed on exactly this one control. addLine()
    // now generates a per-line unique id (`budgetLineJustification_<key>`)
    // and pairs it with the label's `for`, so no exclusion is needed here
    // any more -- every violation on this screen fails the build.
    await gotoScreen(page, 'budget-setup');
    await openNewBudget(page);
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('axe-core: Budget Categories has no violations', async ({ page }) => {
    await gotoScreen(page, 'budget-categories');
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
});

/* ==================================================================
   6. Visual regression — NEW baselines for this spec only
   ================================================================== */

test.describe('Budget Setup & Categories — rendering', () => {
  test.beforeEach(async ({ page }) => {
    await installAllRouters(page);
    await signIn(page, ADMIN);
  });

  test('Budget Setup list renders identically', async ({ page }) => {
    await gotoScreen(page, 'budget-setup');
    await expect(page).toHaveScreenshot('budget-setup-list.png', { fullPage: true });
  });

  test('Budget Setup editor with one line renders identically', async ({ page }) => {
    await gotoScreen(page, 'budget-setup');
    await openNewBudget(page);
    await expect(page).toHaveScreenshot('budget-setup-editor.png', { fullPage: true });
  });

  test('Budget Categories renders identically', async ({ page }) => {
    await gotoScreen(page, 'budget-categories');
    await expect(page).toHaveScreenshot('budget-setup-categories.png', { fullPage: true });
  });
});

/* ==================================================================
   7. Navigation rail — ANALYTICS / INTEGRATION MAPPING groups, the six
      approved <h2> groups, and the rail's overflow at this viewport
   ================================================================== */

test.describe('Navigation rail — groups', () => {
  test.beforeEach(async ({ page }) => {
    await installAllRouters(page);
    await signIn(page, ADMIN);
  });

  test('ANALYTICS and INTEGRATION MAPPING are aria-expanded buttons; the six approved groups stay <h2>; clicking a button group reveals its rows', async ({ page }) => {
    // Below 900px the rail is an overlay drawer, display:none until opened
    // (AUD-M-004; see spa-routing.spec.js's openNavIfCollapsed()) — the
    // group buttons still exist in the DOM at that width (evaluateAll below
    // reads them regardless), but clicking one requires the drawer open.
    const collapsed = await page.locator('#nav').evaluate((n) => getComputedStyle(n).display === 'none');
    if (collapsed) await page.locator('#navToggle').click();

    const buttonGroupNames = await page.locator('#nav button.nav-group').evaluateAll(
      (els) => els.map((e) => e.dataset.navGroup),
    );
    expect(buttonGroupNames.sort()).toEqual(['ANALYTICS', 'INTEGRATION MAPPING'].sort());
    for (const name of buttonGroupNames) {
      await expect(page.locator(`#nav button.nav-group[data-nav-group="${name}"]`)).toHaveAttribute('aria-expanded', /^(true|false)$/);
    }

    // textContent, not innerText: the rail's CSS renders .nav-group headings
    // in small caps / uppercase, and innerText reflects that visual transform
    // while textContent gives back exactly the string app.js's NAV array
    // literally carries ('Work', 'Project Control', ...).
    const h2GroupTexts = await page.locator('#nav h2.nav-group').evaluateAll(
      (els) => els.map((e) => (e.textContent || '').trim()),
    );
    const approvedSix = ['Work', 'Project Control', 'Procurement & Actuals', 'Closure', 'Integration', 'Governance'];
    expect(h2GroupTexts.sort()).toEqual(approvedSix.sort());

    const analyticsBtn = page.locator('#nav button.nav-group[data-nav-group="ANALYTICS"]');
    const initiallyExpanded = (await analyticsBtn.getAttribute('aria-expanded')) === 'true';
    await analyticsBtn.click();
    await expect(analyticsBtn).toHaveAttribute('aria-expanded', String(!initiallyExpanded));
    const analyticsRow = page.locator('#nav [data-nav="analytics-executive"]');
    if (!initiallyExpanded) {
      await expect(analyticsRow).toBeVisible();
    } else {
      await expect(analyticsRow).toHaveCount(0);
    }
  });

  test('the rail is measured at this viewport; an overflow is reported, never hidden by skipping the check', async ({ page }) => {
    const m = await page.evaluate(() => {
      const nav = document.getElementById('nav');
      const cs = getComputedStyle(nav);
      if (cs.display === 'none') return { hidden: true, rows: nav.querySelectorAll('.nav-item').length };
      const items = [...nav.querySelectorAll('.nav-item')];
      const kids = [...nav.children];
      let content = 0;
      if (kids.length) {
        const first = kids[0].getBoundingClientRect().top;
        const last = kids[kids.length - 1].getBoundingClientRect().bottom;
        content = (last - first) + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      }
      return {
        hidden: false, rows: items.length, rail: nav.clientHeight,
        content: Math.round(content), overflow: Math.round(content - nav.clientHeight),
      };
    });
    // eslint-disable-next-line no-console
    console.log(`[budget-setup nav measurement] ${JSON.stringify(m)}`);
    // A drawer (display:none below 900px) or a visible rail both have at
    // least one row; whichever it is, this reports the fact rather than
    // hiding it behind a passing assertion either way.
    expect(m.rows).toBeGreaterThan(0);
  });
});
