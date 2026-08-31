// tests/vrt/budget.spec.js
//
// SCR-09 Budget Planning Grid, SCR-10 Budget Version Comparison and SCR-13
// Budget Availability Check — behavioural and accessibility coverage for the
// screens built in app/frontend/src/features/budget/** and
// app/frontend/src/components/budget/**, hosted by app/frontend/budget.html.
//
// Every response below is served through Playwright's page.route(). None of
// these modules carry static fake data — every figure on screen came back
// from a fetch() call — so these tests prove the screens are network-driven
// rather than fixture-driven, and let each of the four required states
// (loading, empty, error, permission) be exercised deterministically without
// a real backend, exactly like tests/vrt/audit-trail.spec.js does for SCR-28.
//
// Follows the conventions of tests/vrt/audit-trail.spec.js: no fixed
// timeouts; wait for the loading indicator to clear. Uses the same
// playwright.config.js webServer as every other VRT spec; this file does not
// own package.json or playwright.config.js and does not declare them.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const VIEWPORTS = [
  { name: 'desktop-1440', width: 1440, height: 900 },
  { name: 'laptop-1024', width: 1024, height: 900 },
  { name: 'tablet-800', width: 800, height: 900 },
];

async function routeJson(page, urlPattern, body, status = 200) {
  await page.route(urlPattern, (route) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  }));
}

const EMPTY_CELLS = { items: [], next_cursor: null, has_more: false };
const EMPTY_LINES = { items: [] };

const CELLS_FIXTURE = {
  items: [
    {
      wbs_id: 'WBS-01', wbs_path: '01', budget_head_id: 'CIVIL',
      budget_paise: 500000000, original_paise: 450000000, revisions_paise: 50000000, future_budget_paise: 0,
      ordered_paise: 0, commitment_paise: 120000000, actual_paise: 80000000, received_paise: 60000000,
      received_not_billed_paise: 5000000, pr_reserved_paise: 10000000,
      exposure_paise: 210000000, available_paise: 290000000, recomputed_at: '2026-08-20T09:00:00Z',
    },
    {
      wbs_id: 'WBS-01-01', wbs_path: '01.01', budget_head_id: 'CIVIL',
      budget_paise: 300000000, original_paise: 300000000, revisions_paise: 0, future_budget_paise: 0,
      ordered_paise: 0, commitment_paise: 100000000, actual_paise: 70000000, received_paise: 50000000,
      received_not_billed_paise: 3000000, pr_reserved_paise: 5000000,
      exposure_paise: 175000000, available_paise: 125000000, recomputed_at: '2026-08-20T09:00:00Z',
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

const LINES_FIXTURE = {
  items: [
    {
      budget_line_id: 'BL-0001', wbs_id: 'WBS-01', budget_head_id: 'CIVIL', kind: 'ORIGINAL',
      amount_paise: 450000000, effective_from: '2026-01-01', effective_to: null, status: 'Approved',
      justification: 'Initial sanctioned budget.', created_at: '2026-01-01T10:00:00Z', created_by: 'U-PFC', version_no: 1,
    },
    {
      budget_line_id: 'BL-0002', wbs_id: 'WBS-01', budget_head_id: 'CIVIL', kind: 'REVISION',
      amount_paise: 50000000, effective_from: '2026-04-01', effective_to: null, status: 'Approved',
      justification: 'Scope addition approved by steering committee.', created_at: '2026-04-01T10:00:00Z', created_by: 'U-PM', version_no: 2,
    },
  ],
};

const VERSIONS_FIXTURE = {
  items: [
    { version: 'V2', label: 'Revised', created_at: '2026-06-01T00:00:00Z', total_paise: 520000000 },
    { version: 'V1', label: 'Original', created_at: '2026-01-01T00:00:00Z', total_paise: 500000000 },
  ],
};

const COMPARE_FIXTURE = {
  rows: [
    { wbs_id: 'WBS-01', budget_head_id: 'CIVIL', left_paise: 450000000, right_paise: 500000000, delta_paise: 50000000 },
    { wbs_id: 'WBS-01-01', budget_head_id: 'CIVIL', left_paise: 300000000, right_paise: 300000000, delta_paise: 0 },
    { wbs_id: 'WBS-01-02', budget_head_id: 'ELEC', left_paise: 150000000, right_paise: 130000000, delta_paise: -20000000 },
  ],
};

function availabilityFixture(verdict, overrides = {}) {
  return {
    wbs_id: 'WBS-01-02', budget_head_id: 'ELEC', owning_wbs_id: 'WBS-01',
    budget_paise: 200000000, exposure_paise: 214000000, available_paise: -14000000, requested_paise: 5000000,
    verdict, shortfall_paise: verdict === 'EXCEEDS_BUDGET' ? 19000000 : 0,
    checked_at: '2026-08-20T09:30:00Z',
    ...overrides,
  };
}

/** Stub every endpoint budget.html's three panels can call, with a
 * reasonable default, so a test focused on one panel does not need to know
 * about the others' fetches. Register test-specific overrides AFTER calling
 * this — Playwright gives the most-recently-added matching route priority. */
async function stubBudgetDefaults(page) {
  await routeJson(page, '**/api/budget/cells**', CELLS_FIXTURE);
  await routeJson(page, '**/api/budget/lines**', EMPTY_LINES);
  await routeJson(page, '**/api/budget/versions**', VERSIONS_FIXTURE);
  await routeJson(page, '**/api/budget/compare**', COMPARE_FIXTURE);
  await routeJson(page, '**/api/budget/availability**', availabilityFixture('OK'));
}

async function gotoBudget(page) {
  await page.goto('/static/budget.html');
}

/** Wait for the grid panel's initial fetch cycle to clear rather than for a
 * fixed timeout — mirrors tests/vrt/audit-trail.spec.js::settle(). */
async function settleGrid(page) {
  await page.waitForFunction(() => {
    const panel = document.getElementById('budgetPanelGrid');
    return !!panel && !panel.querySelector('.audit-skel-row') && !panel.querySelector('.loading');
  });
  await page.waitForLoadState('networkidle');
}

function tab(page, name) {
  return page.locator(`#budgetTab${name}`);
}
function panel(page, name) {
  return page.locator(`#budgetPanel${name}`);
}

async function selectTab(page, name) {
  await tab(page, name).click();
}

test.describe('Budget screens — rendering', () => {
  for (const vp of VIEWPORTS) {
    test(`renders at ${vp.width}px (${vp.name})`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await stubBudgetDefaults(page);
      await gotoBudget(page);
      await settleGrid(page);

      await expect(panel(page, 'Grid').locator('table')).toBeVisible();
      // Each cell row has a sibling detail row (hidden until "View lines" is
      // pressed) for the lazily-loaded budget-lines panel, so only the
      // visible cell rows are counted here.
      await expect(panel(page, 'Grid').locator('tbody tr:not([hidden])')).toHaveCount(3);

      // AUD-M-004: the document itself never scrolls horizontally, even at
      // the narrowest supported viewport, though the grid's own wide table
      // is allowed to scroll inside its .table-wrap.
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      );
      expect(overflows, `budget grid scrolls the document horizontally at ${vp.width}px`).toBe(false);
    });
  }
});

test.describe('SCR-09 Budget Planning Grid — required states', () => {
  test('loading state renders a table skeleton, never a blank panel', async ({ page }) => {
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await routeJson(page, '**/api/budget/lines**', EMPTY_LINES);
    await routeJson(page, '**/api/budget/versions**', VERSIONS_FIXTURE);
    await routeJson(page, '**/api/budget/compare**', COMPARE_FIXTURE);
    await routeJson(page, '**/api/budget/availability**', availabilityFixture('OK'));
    await page.route('**/api/budget/cells**', async (route) => {
      await held;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CELLS_FIXTURE) });
    });

    await gotoBudget(page);
    await expect(panel(page, 'Grid').locator('.audit-skel-row').first()).toBeVisible();
    const panelText = (await panel(page, 'Grid').innerText()).trim();
    expect(panelText.length, 'loading state rendered a blank panel').toBeGreaterThan(0);

    release();
    await settleGrid(page);
    await expect(panel(page, 'Grid').locator('.audit-skel-row')).toHaveCount(0);
  });

  test('empty state explains what would appear and why it is empty', async ({ page }) => {
    await stubBudgetDefaults(page);
    await routeJson(page, '**/api/budget/cells**', EMPTY_CELLS);
    await gotoBudget(page);
    await settleGrid(page);

    const empty = panel(page, 'Grid').locator('#budgetGridStatusHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/no budget cells/i);
    await expect(empty).toContainText(/budget head/i);
  });

  test('error state shows the server detail, actionable, never a raw traceback', async ({ page }) => {
    await stubBudgetDefaults(page);
    await routeJson(page, '**/api/budget/cells**', {
      type: 'about:blank', title: 'Internal Server Error', status: 500,
      detail: 'The budget engine is temporarily unavailable. Try again in a moment.',
      code: 'BUDGET_ENGINE_UNAVAILABLE',
    }, 500);
    await gotoBudget(page);
    await settleGrid(page);

    const banner = panel(page, 'Grid').locator('.msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The budget engine is temporarily unavailable');
    await expect(banner).not.toContainText(/traceback/i);
    await expect(banner).not.toContainText(/\bat \S+:\d+:\d+/);
    await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('permission state renders a 404 as not-found, never as forbidden', async ({ page }) => {
    await stubBudgetDefaults(page);
    await routeJson(page, '**/api/budget/cells**', {
      type: 'about:blank', title: 'Not Found', status: 404,
      detail: 'No budget data was found for these filters.',
    }, 404);
    await gotoBudget(page);
    await settleGrid(page);

    const empty = panel(page, 'Grid').locator('#budgetGridStatusHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText('No budget data was found');
    await expect(panel(page, 'Grid')).not.toContainText(/forbidden|access denied|\b403\b/i);
  });
});

test.describe('SCR-09 Budget Planning Grid — tree and drill-down', () => {
  test('drilling a cell loads and shows its budget lines', async ({ page }) => {
    await stubBudgetDefaults(page);
    await routeJson(page, '**/api/budget/lines**', LINES_FIXTURE);
    await gotoBudget(page);
    await settleGrid(page);

    // Located by DOM position, not accessible name: the button's own name
    // changes from "View lines" to "Hide lines" on click, so a
    // getByRole(...).first() locator would silently re-resolve to a
    // different (still-unclicked) button after that text changes.
    const firstDrill = panel(page, 'Grid').locator('table tbody tr').first().locator('button.linkish');
    await firstDrill.click();
    await expect(panel(page, 'Grid').getByText('BL-0001')).toBeVisible();
    await expect(panel(page, 'Grid').getByText('BL-0002')).toBeVisible();
    await expect(firstDrill).toHaveText('Hide lines');
  });

  test('collapsing a WBS node hides its descendant rows', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);

    const grid = panel(page, 'Grid');
    await expect(grid.locator('tbody tr:not([hidden])')).toHaveCount(3);
    await grid.locator('.tree-toggle:not(.leaf)').first().click();
    const remaining = await grid.locator('tbody tr:not([hidden])').count();
    expect(remaining, 'collapsing the root node did not hide any rows').toBeLessThan(3);
  });
});

test.describe('SCR-10 Budget Version Comparison', () => {
  async function loadCompareTab(page) {
    await selectTab(page, 'Compare');
    await panel(page, 'Compare').getByRole('button', { name: 'Load versions' }).click();
    await expect(panel(page, 'Compare').locator('#budgetCompareLeft')).toBeEnabled();
  }

  test('comparing two versions renders a delta column with a direction glyph, not colour alone', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await loadCompareTab(page);

    await panel(page, 'Compare').getByRole('button', { name: 'Compare' }).click();
    await expect(panel(page, 'Compare').locator('table tbody tr').first()).toBeVisible();
    // The increased row shows an up glyph, the decreased row a down glyph —
    // present as literal text content, independent of colour.
    await expect(panel(page, 'Compare')).toContainText('▲');
    await expect(panel(page, 'Compare')).toContainText('▼');
  });

  test('zero-delta rows can be collapsed and their count is stated', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await loadCompareTab(page);
    await panel(page, 'Compare').getByRole('button', { name: 'Compare' }).click();
    await expect(panel(page, 'Compare').locator('table tbody tr')).toHaveCount(3);

    const hideBtn = panel(page, 'Compare').getByRole('button', { name: 'Hide unchanged rows' });
    await hideBtn.click();
    await expect(panel(page, 'Compare').locator('table tbody tr')).toHaveCount(2);
    await expect(panel(page, 'Compare')).toContainText(/1 unchanged row hidden/i);
  });

  test('empty state before any comparison explains what to do', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await selectTab(page, 'Compare');

    const empty = panel(page, 'Compare').locator('#budgetCompareStatus .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/no comparison to show/i);
  });

  test('error state on compare shows the server detail with a retry action', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await loadCompareTab(page);
    await routeJson(page, '**/api/budget/compare**', {
      type: 'about:blank', title: 'Internal Server Error', status: 500,
      detail: 'The comparison engine is temporarily unavailable.',
    }, 500);

    await panel(page, 'Compare').getByRole('button', { name: 'Compare' }).click();
    const banner = panel(page, 'Compare').locator('.msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The comparison engine is temporarily unavailable');
    await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('permission state on compare renders not-found, never forbidden', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await loadCompareTab(page);
    await routeJson(page, '**/api/budget/compare**', {
      type: 'about:blank', title: 'Not Found', status: 404,
      detail: 'No comparison data was found for this scope.',
    }, 404);

    await panel(page, 'Compare').getByRole('button', { name: 'Compare' }).click();
    const empty = panel(page, 'Compare').locator('#budgetCompareStatus .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText('No comparison data was found');
    await expect(panel(page, 'Compare')).not.toContainText(/forbidden|access denied|\b403\b/i);
  });
});

test.describe('SCR-13 Budget Availability Check', () => {
  async function fillAndCheck(page, { wbs = 'WBS-01-02', head = 'ELEC', amount = '50,000.00' } = {}) {
    await selectTab(page, 'Availability');
    const avail = panel(page, 'Availability');
    await avail.locator('#availWbs').fill(wbs);
    await avail.locator('#availHead').fill(head);
    await avail.locator('#availAmount').fill(amount);
    await avail.getByRole('button', { name: 'Check availability' }).click();
  }

  test('empty state before any check explains what to do', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await selectTab(page, 'Availability');

    const empty = panel(page, 'Availability').locator('#availResultHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/no check has been run/i);
  });

  test('error state shows the server detail with a retry action', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await routeJson(page, '**/api/budget/availability**', {
      type: 'about:blank', title: 'Internal Server Error', status: 500,
      detail: 'The availability engine is temporarily unavailable.',
    }, 500);

    await fillAndCheck(page);
    // Scoped to the result host: the always-present (but normally hidden)
    // #availAmountError field-validation box also carries class "msg-error",
    // which is not the banner this assertion means to find.
    const banner = panel(page, 'Availability').locator('#availResultHost .msg-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The availability engine is temporarily unavailable');
    await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('permission state renders not-found, never forbidden', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await routeJson(page, '**/api/budget/availability**', {
      type: 'about:blank', title: 'Not Found', status: 404,
      detail: 'No budget data was found for this WBS element.',
    }, 404);

    await fillAndCheck(page);
    const empty = panel(page, 'Availability').locator('#availResultHost .empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText('No budget data was found');
    await expect(panel(page, 'Availability')).not.toContainText(/forbidden|access denied|\b403\b/i);
  });

  test('client-side validation rejects a non-numeric amount without calling the API', async ({ page }) => {
    await stubBudgetDefaults(page);
    let called = false;
    await page.route('**/api/budget/availability**', async (route) => {
      called = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(availabilityFixture('OK')) });
    });
    await gotoBudget(page);
    await settleGrid(page);
    await fillAndCheck(page, { amount: 'not-a-number' });

    await expect(panel(page, 'Availability').locator('#availAmountError')).toBeVisible();
    expect(called, 'the API was called despite invalid client-side input').toBe(false);
  });

  const VERDICT_CASES = [
    { verdict: 'OK', label: 'OK', glyph: '✔' },
    { verdict: 'WATCH', label: 'Watch', glyph: '▲' },
    { verdict: 'CRITICAL', label: 'Critical', glyph: '⚠' },
    { verdict: 'EXCEEDS_BUDGET', label: 'Exceeds budget', glyph: '⛔' },
  ];

  for (const vc of VERDICT_CASES) {
    test(`verdict ${vc.verdict} renders with its own glyph and label text`, async ({ page }) => {
      await stubBudgetDefaults(page);
      await routeJson(page, '**/api/budget/availability**', availabilityFixture(vc.verdict));
      await gotoBudget(page);
      await settleGrid(page);
      await fillAndCheck(page);

      const badge = panel(page, 'Availability').locator('.budget-verdict');
      await expect(badge).toBeVisible();
      await expect(badge).toContainText(vc.label);
      await expect(badge).toContainText(vc.glyph);

      if (vc.verdict === 'EXCEEDS_BUDGET') {
        const body = panel(page, 'Availability');
        await expect(body).toContainText('Shortfall');
        await expect(body).toContainText('WBS-01'); // owning_wbs_id from the fixture
      }
    });
  }

  test('the four verdicts are pairwise distinguishable by text and glyph alone', async ({ page }) => {
    // Collects the rendered (label, glyph) pair for every verdict and asserts
    // no two are identical — the concrete proof that colour is not the only
    // carrier of the distinction, since these are read straight from
    // textContent rather than any computed style.
    await stubBudgetDefaults(page);
    const seen = new Set();
    for (const vc of VERDICT_CASES) {
      await routeJson(page, '**/api/budget/availability**', availabilityFixture(vc.verdict));
      if (seen.size === 0) {
        await gotoBudget(page);
        await settleGrid(page);
      }
      await fillAndCheck(page);
      const badgeText = await panel(page, 'Availability').locator('.budget-verdict').innerText();
      expect(seen.has(badgeText), `verdict ${vc.verdict} renders identical text to an earlier verdict: "${badgeText}"`).toBe(false);
      seen.add(badgeText);
    }
    expect(seen.size).toBe(VERDICT_CASES.length);
  });
});

test.describe('Budget screens — accessibility', () => {
  test('keyboard-only traversal reaches every control on the Planning Grid tab, with visible focus', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);

    // Only budgetTabGrid is expected in the plain Tab sequence: this is a
    // standard WAI-ARIA tabs widget with roving tabindex, so the other two
    // tabs are correctly tabindex="-1" (reachable by arrow key once inside
    // the tablist, not by Tab) — see the separate "arrow keys move between
    // tabs" test below, which is where that reachability is proven.
    const requiredIds = ['budgetTabGrid', 'budgetGridProject', 'budgetGridWbs', 'budgetGridHead'];
    // "Load more" is intentionally not required here: CELLS_FIXTURE sets
    // has_more:false, so the pagination control is legitimately hidden
    // (and therefore untabbable) — mirroring
    // tests/vrt/audit-trail.spec.js's equivalent keyboard test.
    const requiredLabels = ['Apply filters', 'Clear filters', 'View lines'];
    const reachedIds = new Set();
    const reachedLabels = new Set();
    const noOutline = [];

    await page.evaluate(() => document.body.focus());
    // The page has roughly a dozen focusable controls on this tab; stop well
    // before Tab would cycle past the last one and out of the document
    // (into browser chrome, then back to an unstyled document.body) — a
    // fixed large iteration count would otherwise make the final
    // "visible focus" check flaky depending on exactly how many controls a
    // given viewport happens to render.
    for (let i = 0; i < 40; i += 1) {
      await page.keyboard.press('Tab');
      const info = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el || el === document.body) return null;
        return {
          id: el.id || null,
          text: (el.textContent || '').trim().slice(0, 40),
          outlineStyle: getComputedStyle(el).outlineStyle,
        };
      });
      if (!info) break; // tabbed out of the document — every in-page control has been visited
      if (info.id) reachedIds.add(info.id);
      if (info.text) reachedLabels.add(info.text);
      if (info.outlineStyle === 'none') noOutline.push(info.id || info.text);
    }

    for (const id of requiredIds) {
      expect(reachedIds.has(id), `#${id} was never reached by keyboard Tab traversal`).toBe(true);
    }
    for (const label of requiredLabels) {
      expect(reachedLabels.has(label), `control labelled "${label}" was never reached by keyboard Tab traversal`).toBe(true);
    }
    expect(noOutline, `these focused controls had no visible focus outline: ${noOutline.join(', ')}`).toEqual([]);
  });

  test('arrow keys move between tabs per the WAI-ARIA tabs pattern', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);

    await tab(page, 'Grid').focus();
    await page.keyboard.press('ArrowRight');
    await expect(tab(page, 'Compare')).toBeFocused();
    await expect(tab(page, 'Compare')).toHaveAttribute('aria-selected', 'true');
    await expect(panel(page, 'Compare')).toBeVisible();

    await page.keyboard.press('ArrowRight');
    await expect(tab(page, 'Availability')).toBeFocused();
    await expect(panel(page, 'Availability')).toBeVisible();

    await page.keyboard.press('ArrowLeft');
    await expect(tab(page, 'Compare')).toBeFocused();
  });

  test('axe-core: Planning Grid tab has no violations', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('axe-core: Version Comparison tab (with results) has no violations', async ({ page }) => {
    await stubBudgetDefaults(page);
    await gotoBudget(page);
    await settleGrid(page);
    await selectTab(page, 'Compare');
    await panel(page, 'Compare').getByRole('button', { name: 'Load versions' }).click();
    await expect(panel(page, 'Compare').locator('#budgetCompareLeft')).toBeEnabled();
    await panel(page, 'Compare').getByRole('button', { name: 'Compare' }).click();
    await expect(panel(page, 'Compare').locator('table tbody tr').first()).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('axe-core: Availability Check tab (with an EXCEEDS_BUDGET result) has no violations', async ({ page }) => {
    await stubBudgetDefaults(page);
    await routeJson(page, '**/api/budget/availability**', availabilityFixture('EXCEEDS_BUDGET'));
    await gotoBudget(page);
    await settleGrid(page);
    await selectTab(page, 'Availability');
    const avail = panel(page, 'Availability');
    await avail.locator('#availWbs').fill('WBS-01-02');
    await avail.locator('#availHead').fill('ELEC');
    await avail.locator('#availAmount').fill('50,000.00');
    await avail.getByRole('button', { name: 'Check availability' }).click();
    await expect(avail.locator('.budget-verdict')).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
});
