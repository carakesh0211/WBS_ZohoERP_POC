// tests/vrt/approvals.spec.js
//
// THE APPROVAL ENGINE'S EIGHT SCREENS (Wave 4, M4b, stream 4).
//
//   My Approval Inbox                     #approval-inbox
//   Approval Request Detail               #approval-request
//   Approval Timeline and Audit History   #approval-timeline
//   Approval Matrix Configuration         #approval-matrix
//   Workflow Version History              #approval-versions
//   Approval Rule Simulator               #approval-simulator
//   Delegation Management                 #approval-delegations
//   Escalation and SLA Monitor            #approval-sla
//
// What is asserted here:
//   * all THIRTEEN routable screens — the five Wave 2/3 ones plus these eight —
//     are reachable, deep-linkable by hash, and Back/Forward restore the right
//     one;
//   * the four required states (loading, empty, error, permission-denied)
//     render on every one of the eight;
//   * a refused READ renders exactly as a not-found, never as "forbidden" —
//     the anti-oracle rule;
//   * no screen takes a client-side authorization decision: the decision
//     controls are rendered and the SERVER's refusal is what is displayed;
//   * axe-core is clean on each screen, Tab alone reaches every control with a
//     visible focus ring, and every status is distinguishable without colour;
//   * the rendered result does not drift, at 1440 / 1024 / 800 px.
//
// ---------------------------------------------------------------------------
// TWO CROSS-STREAM STUBS, BOTH DELIBERATE, BOTH DOCUMENTED
// ---------------------------------------------------------------------------
//
// 1. /api/approvals/**  — app/backend/api/approvals.py is stream 3's file and
//    does not exist at this stream's baseline. These screens are coded against
//    Wave 4 Contract 3's FROZEN routes and payloads, and every one of those
//    routes is stubbed here with page.route(). That is the intended
//    arrangement for concurrent streams.
//
// 2. /api/bootstrap     — Contract 4 adds approval.read / approval.act /
//    approval.configure / approval.delegate to auth.PERMISSIONS.
//    app/backend/auth.py is LEAD-OWNED and frozen, and at this stream's
//    baseline it carries none of them, so a real bootstrap returns a
//    permission set in which every approval screen is correctly invisible.
//
//    Rather than fake the whole bootstrap, the REAL server response is fetched
//    and the four contract-frozen permissions are added to it. Everything else
//    — the session, the identity, the entities, the projects — is the real
//    server's. What is being proved is that the shell's gate opens for exactly
//    those permissions and stays shut without them, and the negative half of
//    that pair is proved against an UNMODIFIED bootstrap, so the gate cannot
//    pass by being absent.
//
//    THIS STUB MUST BE DELETED once the approval permissions land in
//    auth.PERMISSIONS. It is reported in this stream's hand-off.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

/* ---------------- identities ---------------- */

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };

/* ---------------- the eight screens ---------------- */

const SCREENS = [
  {
    hash: 'approval-inbox',
    title: 'My Approval Inbox',
    navLabel: 'My Approval Inbox',
    need: ['approval.read'],
    statusHost: '#approvalInboxStatusHost',
    // The endpoint whose failure drives this screen's error / permission state.
    primary: '**/api/approvals/inbox**',
  },
  {
    hash: 'approval-request',
    title: 'Approval Request Detail',
    navLabel: 'Approval Request Detail',
    need: ['approval.read'],
    statusHost: '#approvalDetailStatusHost',
    primary: '**/api/approvals/INST-001',
    // With no ?instance in the URL this screen falls back to the caller's own
    // inbox, so its EMPTY state is "nothing to open", which is driven by an
    // empty inbox rather than by an empty instance body.
    emptyRoute: '**/api/approvals/inbox**',
  },
  {
    hash: 'approval-timeline',
    title: 'Approval Timeline and Audit History',
    navLabel: 'Approval Timeline',
    need: ['approval.read'],
    statusHost: '#approvalTimelineStatusHost',
    primary: '**/api/approvals/INST-001/timeline**',
  },
  {
    hash: 'approval-matrix',
    title: 'Approval Matrix Configuration',
    navLabel: 'Approval Matrix Configuration',
    need: ['approval.configure'],
    statusHost: '#approvalMatrixStatusHost',
    primary: '**/api/approvals/definitions?**',
  },
  {
    hash: 'approval-versions',
    title: 'Workflow Version History',
    navLabel: 'Workflow Version History',
    need: ['approval.configure'],
    statusHost: '#workflowVersionsStatusHost',
    primary: '**/api/approvals/definitions/*/versions**',
  },
  {
    hash: 'approval-simulator',
    title: 'Approval Rule Simulator',
    navLabel: 'Approval Rule Simulator',
    need: ['approval.configure'],
    statusHost: '#simulatorStatusHost',
    primary: '**/api/approvals/definitions/*/simulate',
    // The simulator does not load on mount; it starts in its empty state and
    // runs on submit. Its loading/error states are driven by that submit.
    runsOnSubmit: true,
    // Its endpoint is a POST, so core/api-client.js's not-found-over-forbidden
    // rule does not apply: a refused WRITE is reported as a refusal, because
    // the user needs to know the action was declined rather than that the
    // record vanished. See the note on the 403/404 test below.
    writeShaped: true,
  },
  {
    hash: 'approval-delegations',
    title: 'Delegation Management',
    navLabel: 'Delegation Management',
    need: ['approval.delegate'],
    statusHost: '#delegationsStatusHost',
    primary: '**/api/approvals/delegations**',
  },
  {
    hash: 'approval-sla',
    title: 'Escalation and SLA Monitor',
    navLabel: 'Escalation and SLA Monitor',
    need: ['approval.read'],
    statusHost: '#slaStatusHost',
    primary: '**/api/approvals/sla**',
  },
];

/** The five Wave 2/3 screens, so "thirteen reachable" is asserted as thirteen. */
const EXISTING_SCREENS = [
  { hash: 'audit-trail', title: 'Audit Trail Viewer' },
  { hash: 'budget-grid', title: 'Budget Planning Grid' },
  { hash: 'budget-compare', title: 'Budget Version Comparison' },
  { hash: 'budget-availability', title: 'Budget Availability Check' },
  { hash: 'settings', title: 'Settings and Master Data' },
];

const APPROVAL_PERMISSIONS = [
  'approval.read', 'approval.act', 'approval.configure', 'approval.delegate',
];

/* ---------------- fixtures ---------------- */

const INSTANCE_ROW = {
  instance_id: 'INST-001',
  object_type: 'purchase_request',
  object_id: 'PR-0007',
  object_version: 3,
  status: 'OPEN',
  current_stage_no: 2,
  current_stage_name: 'Finance review',
  amount_paise: 250000000,
  due_at: '2026-09-01T09:00:00Z',
  overdue: true,
  maker_user_id: 'U-REQ',
};

const INBOX = { items: [INSTANCE_ROW], next_cursor: null, has_more: false };

const INSTANCE = {
  ...INSTANCE_ROW,
  object_content_sha: 'b7d3f0a19c4e2f8a6d5b0c1e9f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c',
  definition_id: 'DEF-PR-01',
  definition_code: 'PR-STANDARD',
  definition_version: 4,
  opened_at: '2026-08-28T09:00:00Z',
  closed_at: null,
  correlation_id: 'c0ffee00-1111-4222-8333-444455556666',
  reason_codes: [
    { code: 'BUDGET_INSUFFICIENT', label: 'Insufficient budget' },
    { code: 'SPEC_UNCLEAR', label: 'Specification unclear' },
  ],
  stages: [
    {
      stage_no: 1,
      name: 'Budget control',
      status: 'APPROVED',
      quorum_type: 'ALL',
      quorum_n: null,
      quorum_required: 1,
      quorum_met: 1,
      due_at: '2026-08-29T09:00:00Z',
      skip_reason: null,
      assignments: [{ assignee_user_id: 'U-PM', state: 'ACTED', assigned_via: 'ROLE' }],
    },
    {
      stage_no: 2,
      name: 'Finance review',
      status: 'ESCALATED',
      quorum_type: 'N_OF_M',
      quorum_n: 2,
      quorum_required: 2,
      quorum_met: 0,
      due_at: '2026-09-01T09:00:00Z',
      skip_reason: null,
      assignments: [
        { assignee_user_id: 'U-FIN', state: 'PENDING', assigned_via: 'ROLE' },
        { assignee_user_id: 'U-CFO', state: 'PENDING', assigned_via: 'DELEGATION', delegated_from: 'U-PFC' },
      ],
    },
    {
      // A SKIPPED stage is RECORDED, NEVER OMITTED. Its presence in this
      // fixture is deliberate: the screen must show what did not run, and why.
      stage_no: 3,
      name: 'Capitalisation review',
      status: 'SKIPPED',
      quorum_type: 'ANY',
      quorum_n: null,
      quorum_required: 0,
      quorum_met: 0,
      due_at: null,
      skip_reason: 'Object is not a capitalisation candidate.',
      assignments: [],
    },
  ],
};

const TIMELINE = {
  items: [
    {
      action_id: 1, seq: 1, at: '2026-08-28T09:05:00Z',
      actor_user_id: 'U-PM', acting_for_user_id: null,
      action: 'APPROVE', stage_no: 1, reason_code: null, reason_text: null,
      prev_hash: '0000000000000000000000000000000000000000000000000000000000000000',
      entry_hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012a3b4c5d6e7f80912a3b4c5d6e7f809',
    },
    {
      action_id: 2, seq: 2, at: '2026-08-30T11:20:00Z',
      actor_user_id: 'U-CFO', acting_for_user_id: 'U-PFC',
      action: 'COMMENT', stage_no: 2, reason_code: 'SPEC_UNCLEAR',
      reason_text: 'Awaiting revised technical specification.',
      prev_hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012a3b4c5d6e7f80912a3b4c5d6e7f809',
      entry_hash: 'f80912a3b4c5d6e7a1b2c3d4e5f60718293a4b5c6d7e8f9012a3b4c5d6e7f801',
    },
  ],
  next_cursor: null,
  has_more: false,
  chain: {
    stream_key: 'approval:INST-001',
    intact: true,
    entries_checked: 2,
    first_break_seq: null,
    verified_at: '2026-09-01T00:00:00Z',
  },
};

const DEFINITION = {
  definition_id: 'DEF-PR-01',
  code: 'PR-STANDARD',
  object_type: 'purchase_request',
  version: 4,
  status: 'ACTIVE',
  entity_id: null,
  effective_from: '2026-07-01T00:00:00Z',
  effective_to: null,
  rule_count: 2,
  stage_count: 3,
  rules: [
    { rule_id: 'R1', priority: 10, predicate: { amount_paise: { gte: 100000000 } } },
    { rule_id: 'R2', priority: 20, predicate: {} },
  ],
  stages: [
    {
      stage_no: 1, name: 'Budget control', quorum_type: 'ALL', quorum_n: null,
      sla_hours: 24, escalate_after_hours: 48, allow_delegation: true,
      requires_reason: false, reason_code_set: null,
      approvers: [{ approver_kind: 'ROLE', approver_ref: 'BudgetController' }],
    },
    {
      stage_no: 2, name: 'Finance review', quorum_type: 'N_OF_M', quorum_n: 2,
      sla_hours: 48, escalate_after_hours: 72, allow_delegation: true,
      requires_reason: true, reason_code_set: 'FINANCE',
      approvers: [{ approver_kind: 'ROLE', approver_ref: 'FinanceApprover' }],
    },
  ],
};

const DRAFT_DEFINITION = {
  ...DEFINITION,
  definition_id: 'DEF-PR-02',
  version: 5,
  status: 'DRAFT',
  effective_from: null,
};

const DEFINITIONS = { items: [DEFINITION, DRAFT_DEFINITION], next_cursor: null, has_more: false };

const VERSIONS = {
  items: [
    {
      version: 4, status: 'ACTIVE', effective_from: '2026-07-01T00:00:00Z', effective_to: null,
      stage_count: 3, rule_count: 2, instance_count: 41, created_by: 'U-ADM',
    },
    {
      version: 3, status: 'RETIRED', effective_from: '2026-01-01T00:00:00Z',
      effective_to: '2026-06-30T23:59:59Z', stage_count: 2, rule_count: 1,
      instance_count: 187, created_by: 'U-ADM',
    },
  ],
  next_cursor: null,
  has_more: false,
};

const SIMULATION = {
  status: 'OPEN',
  definition_code: 'PR-STANDARD',
  definition_version: 4,
  matched_rule_priority: 10,
  stages: [
    {
      stage_no: 1, name: 'Budget control', applies: true, quorum_type: 'ALL',
      would_assign_to: ['U-PM'], skip_reason: null,
    },
    {
      stage_no: 2, name: 'Finance review', applies: true, quorum_type: 'N_OF_M',
      quorum_n: 2, would_assign_to: ['U-FIN', 'U-CFO'], skip_reason: null,
    },
    {
      stage_no: 3, name: 'Capitalisation review', applies: false, quorum_type: 'ANY',
      would_assign_to: [], skip_reason: 'Object is not a capitalisation candidate.',
    },
  ],
};

const UNROUTABLE_SIMULATION = {
  status: 'EXCEPTION_PENDING',
  code: 'NO_INDEPENDENT_APPROVER',
  definition_code: 'PR-STANDARD',
  definition_version: 4,
  matched_rule_priority: 10,
  stages: [
    {
      stage_no: 1, name: 'Budget control', applies: true, quorum_type: 'ALL',
      would_assign_to: [], skip_reason: null,
    },
  ],
};

const DELEGATIONS = {
  items: [
    {
      delegation_id: 'DLG-001', delegator_user_id: 'U-PFC', delegate_user_id: 'U-CFO',
      scope_key: 'ENT-01', from: '2026-08-01T00:00:00Z', to: '2026-09-30T00:00:00Z',
      active: true, revoked_at: null, revoke_reason: null,
    },
    {
      delegation_id: 'DLG-002', delegator_user_id: 'U-FIN', delegate_user_id: 'U-PM',
      scope_key: null, from: '2026-06-01T00:00:00Z', to: '2026-06-30T00:00:00Z',
      active: false, revoked_at: '2026-06-15T10:00:00Z',
      revoke_reason: 'Returned from leave early.',
    },
  ],
  next_cursor: null,
  has_more: false,
};

const SLA = {
  items: [
    {
      instance_id: 'INST-001', object_type: 'purchase_request',
      stage_no: 2, stage_name: 'Finance review', status: 'ESCALATED',
      due_at: '2026-09-01T09:00:00Z', hours_overdue: 30.5,
      assignees: ['U-FIN', 'U-CFO'],
      escalated_at: '2026-09-02T09:00:00Z', escalated_to: ['U-ADM'],
    },
    {
      instance_id: 'INST-002', object_type: 'budget_revision',
      stage_no: 1, stage_name: 'Budget control', status: 'PENDING',
      due_at: '2026-09-04T09:00:00Z', hours_overdue: 2.25,
      assignees: ['U-PM'], escalated_at: null, escalated_to: null,
    },
  ],
  next_cursor: null,
  has_more: false,
};

/* ---------------- routing helpers ---------------- */

function json(body, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill(json(body, status)));
}

/**
 * Grant the four Contract-4 approval permissions by AUGMENTING the real
 * bootstrap response, not by replacing it. See the header note.
 */
async function grantApprovalPermissions(page, permissions = APPROVAL_PERMISSIONS) {
  await page.route('**/api/bootstrap*', async (route) => {
    const response = await route.fetch();
    let body;
    try {
      body = await response.json();
    } catch {
      // If the real bootstrap failed, pass the failure through untouched
      // rather than inventing a session that the server never issued.
      return route.fulfill({ response });
    }
    const merged = new Set([...(body.permissions || []), ...permissions]);
    return route.fulfill({ response, json: { ...body, permissions: [...merged].sort() } });
  });
}

/**
 * Every approval endpoint, with a deterministic success default.
 *
 * ORDER MATTERS, AND IT IS THE OPPOSITE OF THE OBVIOUS ONE. page.route()
 * `unshift`s its handler, so the route registered LAST is matched FIRST. The
 * broadest pattern is therefore registered FIRST and each more specific one
 * after it, otherwise `**​/api/approvals/*` would swallow `/inbox`, `/sla`,
 * `/delegations` and `/definitions` and every screen would render one
 * approval instance.
 *
 * The same rule is what lets an individual test override one endpoint: a
 * page.route() added after this function wins over everything in it.
 */
async function stubApprovalApis(page) {
  // Broadest first: GET /api/approvals/{instance_id}.
  await routeJson(page, '**/api/approvals/*', INSTANCE);
  await routeJson(page, '**/api/approvals/*/timeline**', TIMELINE);
  await routeJson(page, '**/api/approvals/*/decide', { status: 'APPROVED' });
  await routeJson(page, '**/api/approvals/definitions**', DEFINITIONS);
  await routeJson(page, '**/api/approvals/definitions/*/versions**', VERSIONS);
  await routeJson(page, '**/api/approvals/definitions/*/simulate', SIMULATION);
  await routeJson(page, '**/api/approvals/definitions/*/activate', { status: 'ACTIVE' });
  // Most specific last, so these win over the single-instance catch-all above.
  await routeJson(page, '**/api/approvals/inbox**', INBOX);
  await routeJson(page, '**/api/approvals/sla**', SLA);
  await routeJson(page, '**/api/approvals/delegations**', DELEGATIONS);
}

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

/**
 * Wait for a screen to be MOUNTED, not merely hosted.
 *
 * app.js appends the host node before it awaits the feature module's mount(),
 * because the module looks its own roots up by id. So `.scr-host` appears
 * while the screen is still empty; `data-mounted="1"` is set only after mount
 * resolved, and that is what to wait on.
 */
async function settleScreen(page) {
  await settleShell(page);
  await page.waitForSelector('#content .scr-host[data-mounted="1"]',
    { state: 'attached', timeout: 15_000 });
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

async function prepared(page, { permissions } = {}) {
  await stubApprovalApis(page);
  await grantApprovalPermissions(page, permissions);
  await signIn(page);
}

/* ============================================================ navigation */

test.describe('Approval screens — routing and navigation', () => {
  test.beforeEach(async ({ page }) => { await prepared(page); });

  test('all thirteen routable screens are reachable and deep-linkable', async ({ page }) => {
    const all = [
      ...EXISTING_SCREENS,
      ...SCREENS.map((s) => ({ hash: s.hash, title: s.title })),
    ];
    expect(all).toHaveLength(13);

    for (const s of all) {
      // A COLD load of the hash, which is what a bookmark or a pasted link is.
      await page.goto(`/#${s.hash}`);
      await settleScreen(page);
      await expect(page.locator('#pageTitle'), `${s.hash} did not open`).toHaveText(s.title);
      // Not silently corrected to the dashboard.
      expect(new URL(page.url()).hash).toBe(`#${s.hash}`);
      await expect(page.locator('#content .scr-host')).toHaveCount(1);
    }
  });

  test('every approval screen is listed in the primary navigation, permission-gated', async ({ page }) => {
    await settleShell(page);
    const ids = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    for (const s of SCREENS) {
      expect(ids, `${s.hash} is not listed in the primary navigation`).toContain(s.hash);
    }
  });

  test('an approval screen is NOT listed, and NOT routable, without its permission', async ({ page }) => {
    // The negative half of the pair, proved against an UNMODIFIED bootstrap:
    // no approval permission is granted at all, so the gate must be shut. If
    // the gate were simply absent, this test would fail — which is exactly why
    // it is run without the permission stub rather than with a narrowed one.
    const bare = await page.context().newPage();
    await stubApprovalApis(bare);
    await signIn(bare);
    await settleShell(bare);

    const ids = await bare.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    for (const s of SCREENS) {
      expect(ids, `${s.hash} was listed for a principal holding no approval permission`)
        .not.toContain(s.hash);
    }

    // And the route itself is refused, not merely unlisted. An unlisted but
    // reachable route is a URL away from being no gate at all.
    await bare.goto('/#approval-matrix');
    await settleShell(bare);
    expect(new URL(bare.url()).hash, 'an ungranted approval route resolved')
      .toBe('#home');
    await expect(bare.locator('#content .scr-host')).toHaveCount(0);
    await bare.close();
  });

  test('a screen is gated on its OWN permission, not on approval.read alone', async ({ page }) => {
    // approval.read is held by every role (Contract 4), so gating a
    // configuration screen on it would be gating on nothing. Granting read
    // only must still leave the configure and delegate screens shut.
    const reader = await page.context().newPage();
    await stubApprovalApis(reader);
    await grantApprovalPermissions(reader, ['approval.read', 'approval.act']);
    await signIn(reader);
    await settleShell(reader);

    const ids = await reader.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    expect(ids).toContain('approval-inbox');
    expect(ids).toContain('approval-sla');
    expect(ids, 'the matrix opened for a principal without approval.configure')
      .not.toContain('approval-matrix');
    expect(ids, 'the simulator opened for a principal without approval.configure')
      .not.toContain('approval-simulator');
    expect(ids, 'delegations opened for a principal without approval.delegate')
      .not.toContain('approval-delegations');

    await reader.goto('/#approval-delegations');
    await settleShell(reader);
    expect(new URL(reader.url()).hash).toBe('#home');
    await reader.close();
  });

  test('Back and Forward move between approval screens and restore each one', async ({ page }) => {
    const walk = SCREENS.slice(0, 4);
    for (const s of walk) {
      await page.evaluate((h) => { window.location.hash = h; }, s.hash);
      await settleScreen(page);
    }
    for (let i = walk.length - 2; i >= 0; i -= 1) {
      await page.goBack();
      await settleScreen(page);
      await expect(page.locator('#pageTitle'),
        `Back did not restore ${walk[i].hash}`).toHaveText(walk[i].title);
    }
    for (let i = 1; i < walk.length; i += 1) {
      await page.goForward();
      await settleScreen(page);
      await expect(page.locator('#pageTitle'),
        `Forward did not restore ${walk[i].hash}`).toHaveText(walk[i].title);
    }
  });

  test('leaving an approval screen and returning re-mounts it cleanly', async ({ page }) => {
    await gotoScreen(page, 'approval-inbox');
    await page.evaluate(() => { window.location.hash = 'home'; });
    await settleShell(page);
    await page.evaluate(() => { window.location.hash = 'approval-inbox'; });
    await settleScreen(page);
    // Exactly one host, not two stacked copies.
    await expect(page.locator('#content .scr-host')).toHaveCount(1);
    await expect(page.locator('#approvalInboxStatusHost')).toHaveCount(1);
  });

  test('no approval route displaces an existing shell view', async ({ page }) => {
    const existing = ['home', 'approvals', 'alerts', 'projects', 'wbs', 'budget', 'check',
      'revisions', 'prs', 'pos', 'grns', 'bills', 'recon', 'cap', 'zoho', 'inventory', 'audit',
      'audit-trail', 'budget-grid', 'budget-compare', 'budget-availability', 'settings'];
    for (const s of SCREENS) {
      expect(existing, `${s.hash} reused an existing view id`).not.toContain(s.hash);
    }
    // And the pre-existing '#approvals' shell view still renders its own screen,
    // built by app.js rather than mounted from a module.
    await page.goto('/#approvals');
    await settleShell(page);
    await expect(page.locator('#content .scr-host')).toHaveCount(0);
    await expect(page.locator('#approvalInboxStatusHost')).toHaveCount(0);
  });

  test('the engine-backed inbox and the pre-engine shell view are distinct screens', async ({ page }) => {
    // A COLLISION THIS STREAM FOUND AND IS REPORTING RATHER THAN RESOLVING.
    //
    // app.js's '#approvals' view already sets the page title to "My Approval
    // Inbox" — which is C8_screens.json's `name_verbatim` for SCR-03 — but it
    // is a PRE-ENGINE PLACEHOLDER: it reads /api/dashboard's alert buckets
    // (budget exceptions, pending revisions, awaiting capitalisation) and knows
    // nothing about approval instances, stages, quorum or delegation.
    //
    // '#approval-inbox' is the real SCR-03: it reads GET /api/approvals/inbox.
    // So two different screens now carry the same page title, and one of them
    // is the client-approved '#approvals' view with its own baselines — which
    // this stream may not retitle, because that would be an unapproved change
    // to the approved UI.
    //
    // This test does not paper over it. It pins BOTH screens so that whichever
    // way the lead resolves it — retire the placeholder, or rename it — the
    // change is deliberate and this test is what has to be updated to make it.
    await page.goto('/#approvals');
    await settleShell(page);
    const placeholderTitle = await page.locator('#pageTitle').innerText();

    await gotoScreen(page, 'approval-inbox');
    const engineTitle = await page.locator('#pageTitle').innerText();

    expect(placeholderTitle, 'the pre-engine placeholder stopped using SCR-03\'s name')
      .toBe('My Approval Inbox');
    expect(engineTitle, 'the engine-backed inbox is not using SCR-03\'s verbatim name')
      .toBe('My Approval Inbox');

    // They are nonetheless different screens, and the rail distinguishes them:
    // 'My Approvals' for the placeholder, 'My Approval Inbox' for the engine.
    const labels = await page.evaluate(() => {
      const byId = (id) => document.querySelector(`#nav .nav-item[data-nav="${id}"]`);
      const label = (el) => (el ? el.querySelector('.nav-label').textContent : null);
      return { placeholder: label(byId('approvals')), engine: label(byId('approval-inbox')) };
    });
    expect(labels.placeholder).toBe('My Approvals');
    expect(labels.engine).toBe('My Approval Inbox');
    expect(labels.placeholder).not.toBe(labels.engine);
  });

  test('the shell never scrolls horizontally on any approval route', async ({ page }) => {
    for (const s of SCREENS) {
      await gotoScreen(page, s.hash);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      );
      expect(overflows, `${s.hash} scrolls horizontally`).toBe(false);
    }
  });
});

/* ==================================================== the four states */

test.describe('Approval screens — the four required states', () => {
  for (const s of SCREENS) {
    test(`${s.hash} — loading shows an indicator, never a blank panel`, async ({ page }) => {
      await stubApprovalApis(page);
      await grantApprovalPermissions(page);
      // Hold the primary request open so the loading state is observable.
      let release;
      const held = new Promise((resolve) => { release = resolve; });
      await page.route(s.primary, async (route) => {
        await held;
        await route.fulfill(json({ items: [], next_cursor: null, has_more: false }));
      });
      await signIn(page);

      await page.goto(`/#${s.hash}`);
      await page.waitForSelector('#content .scr-host', { state: 'attached', timeout: 15_000 });

      if (s.runsOnSubmit) {
        // The simulator does not fetch on mount. Its loading state belongs to
        // the run, so trigger one.
        await page.waitForSelector('#simulatorForm', { timeout: 15_000 });
        await page.locator('#simulatorForm button[type="submit"]').click();
      }

      const host = page.locator(s.statusHost);
      await expect(host.locator('.loading, .audit-skel-row').first(),
        `${s.hash} showed no loading indicator`).toBeVisible({ timeout: 15_000 });
      // A blank panel is the failure being excluded: something must be there.
      await expect(host).not.toBeEmpty();
      release();
    });

    test(`${s.hash} — empty says so, and says what would appear`, async ({ page }) => {
      await stubApprovalApis(page);
      await grantApprovalPermissions(page);
      await routeJson(page, s.emptyRoute || s.primary, emptyBodyFor(s));
      await signIn(page);
      await page.goto(`/#${s.hash}`);
      await settleScreen(page);

      if (s.runsOnSubmit) {
        // The simulator's own empty state is its pre-run state.
        await expect(page.locator(`${s.statusHost} .empty`)).toBeVisible();
      } else {
        await expect(page.locator(`${s.statusHost} .empty`),
          `${s.hash} did not render an empty state`).toBeVisible();
      }
      const copy = await page.locator(`${s.statusHost} .empty`).innerText();
      expect(copy.trim().length, `${s.hash}'s empty state has no explanation`)
        .toBeGreaterThan(10);
      // An empty result is not an error and must never be dressed as one.
      await expect(page.locator(`${s.statusHost} .msg-error`)).toHaveCount(0);
    });

    test(`${s.hash} — a server error is shown, actionable, never a traceback`, async ({ page }) => {
      await stubApprovalApis(page);
      await grantApprovalPermissions(page);
      await routeJson(page, s.primary, {
        code: 'INTERNAL', message: 'The approval service is temporarily unavailable.',
      }, 503);
      await signIn(page);
      await page.goto(`/#${s.hash}`);
      await settleScreen(page);
      if (s.runsOnSubmit) {
        await page.locator('#simulatorForm button[type="submit"]').click();
      }

      const banner = page.locator(`${s.statusHost} .msg-error`);
      await expect(banner, `${s.hash} did not report the failure`).toBeVisible({ timeout: 15_000 });
      await expect(banner).toContainText('The approval service is temporarily unavailable.');
      // Actionable: a way out, not just bad news.
      await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
      // Never a raw traceback or an object printed at a user.
      const txt = await banner.innerText();
      expect(txt).not.toContain('[object Object]');
      expect(txt).not.toMatch(/Traceback|File "|at Object\./);
    });

    test(`${s.hash} — a refused read renders as not-found, never as forbidden`, async ({ page }) => {
      // THE ANTI-ORACLE RULE. A 403 on a read is indistinguishable from a 404,
      // in wording as well as in status: a differently worded message is an
      // existence oracle just as surely as a different status code is.
      await stubApprovalApis(page);
      await grantApprovalPermissions(page);
      await routeJson(page, s.primary, {
        code: 'FORBIDDEN', message: 'No approval records were found for these filters.',
      }, 403);
      await signIn(page);
      await page.goto(`/#${s.hash}`);
      await settleScreen(page);
      if (s.runsOnSubmit) {
        await page.locator('#simulatorForm button[type="submit"]').click();
      }

      const host = page.locator(s.statusHost);
      await expect(host).not.toBeEmpty({ timeout: 15_000 });
      const txt = (await host.innerText()).toLowerCase();
      expect(txt, `${s.hash} leaked a permission refusal on a read`)
        .not.toMatch(/forbidden|access denied|not authoris|not authoriz|permission|\b403\b/);
    });

    test(`${s.hash} — a 403 read and a 404 read render identically`, async ({ page }) => {
      // THE ANTI-ORACLE RULE IS A READ RULE, BY CONSTRUCTION.
      //
      // core/api-client.js's classifyStatus collapses 403 into 'notfound' for
      // GET and HEAD only. On a write it keeps 'forbidden', on the documented
      // grounds that there is no id to leak which the caller did not already
      // name, and that the user needs to know the action was refused rather
      // than that the record vanished.
      //
      // The simulator's endpoint is POST /definitions/{id}/simulate, so it
      // takes the write path and this equivalence does not apply to it. That
      // is asserted separately below rather than skipped here.
      //
      // REPORTED, NOT FIXED: `simulate` is semantically a read — it computes a
      // routing preview and creates nothing — carried over POST because it
      // takes a body. A caller who guesses a definition id can therefore still
      // distinguish "exists but forbidden" from "does not exist". classifyStatus
      // lives in core/api-client.js and the status choice is the server's, so
      // neither is this stream's to change.
      const render = async (status) => {
        const p = await page.context().newPage();
        await stubApprovalApis(p);
        await grantApprovalPermissions(p);
        await routeJson(p, s.primary, {
          code: status === 403 ? 'FORBIDDEN' : 'NOT_FOUND',
          message: 'No approval records were found for these filters.',
        }, status);
        await signIn(p);
        await p.goto(`/#${s.hash}`);
        await settleScreen(p);
        if (s.runsOnSubmit) {
          await p.locator('#simulatorForm button[type="submit"]').click();
          await p.waitForTimeout(0);
        }
        const t = await p.locator(s.statusHost).innerText();
        await p.close();
        return t.trim();
      };
      const on403 = await render(403);
      const on404 = await render(404);

      if (s.writeShaped) {
        // NOT skipped — asserted. A skip here would quietly stop checking a
        // screen. On a write the two renderings SHOULD differ: 404 is the
        // neutral not-found state, 403 is an explicit refusal the user needs
        // to see. Both must still be shown, and neither may be a traceback.
        expect(on403, 'a refused write rendered nothing').not.toBe('');
        expect(on404, 'a not-found write rendered nothing').not.toBe('');
        expect(on403, 'a refused write was rendered as a bare not-found')
          .not.toBe(on404);
        expect(on404.toLowerCase(),
          'the not-found rendering leaked a permission refusal')
          .not.toMatch(/forbidden|access denied|\b403\b/);
        return;
      }

      expect(on403).toBe(on404);
    });
  }
});

/** The empty-result body shape each screen's primary endpoint returns. */
function emptyBodyFor(s) {
  if (s.hash === 'approval-request') {
    // A detail endpoint has no "empty list": absence is a 404, which the
    // screen renders as its permission/not-found state. Its own empty state is
    // the no-reference case, driven by an empty inbox instead.
    return { items: [], next_cursor: null, has_more: false };
  }
  if (s.hash === 'approval-simulator') return SIMULATION;
  return { items: [], next_cursor: null, has_more: false };
}

/* ============================================ behaviour that must not drift */

test.describe('Approval screens — engine invariants that must reach the user', () => {
  test.beforeEach(async ({ page }) => { await prepared(page); });

  test('a skipped stage is shown, with its reason, never omitted', async ({ page }) => {
    // C15: "RECORDED, NEVER OMITTED — an auditor must be able to see what did
    // not run, and why."
    await gotoScreen(page, 'approval-request');
    const stages = page.locator('#approvalDetailStages');
    await expect(stages).toContainText('Capitalisation review');
    await expect(stages).toContainText('Skipped');
    await expect(stages).toContainText('Object is not a capitalisation candidate.');
  });

  test('an escalated stage still names its original assignees', async ({ page }) => {
    // C15: "escalation adds capacity, it never removes accountability."
    await gotoScreen(page, 'approval-sla');
    const row = page.locator('table tbody tr', { hasText: 'INST-001' }).first();
    await expect(row).toContainText('U-FIN');
    await expect(row).toContainText('U-CFO');
    await expect(row).toContainText('U-ADM');   // the escalation target, as well
  });

  test('a delegated action shows BOTH identities in the audit history', async ({ page }) => {
    // Contract 5 checks actor AND acting_for. An audit history that collapsed
    // the two would hide the pair the rule exists to protect.
    await gotoScreen(page, 'approval-timeline');
    const row = page.locator('table tbody tr', { hasText: 'COMMENT' }).first();
    await expect(row).toContainText('U-CFO');
    await expect(row).toContainText('U-PFC');
    await expect(row).toContainText('acting for');
  });

  test('the timeline shows the hash chain, not just the human-readable action', async ({ page }) => {
    await gotoScreen(page, 'approval-timeline');
    await expect(page.locator('#approvalTimelineChain')).toContainText('approval:INST-001');
    await expect(page.locator('#approvalTimelineChain')).toContainText('Intact');
    const head = page.locator('table thead');
    await expect(head).toContainText('Previous hash');
    await expect(head).toContainText('Entry hash');
  });

  test('an ACTIVE definition offers no edit control, only a new version', async ({ page }) => {
    // Contract 1: an ACTIVE definition is immutable, enforced in the database.
    await gotoScreen(page, 'approval-matrix');
    const activeRow = page.locator('table tbody tr', { hasText: 'PR-STANDARD' }).first();
    await expect(activeRow).toContainText('Immutable');
    await expect(activeRow.getByRole('button', { name: /^Edit/ })).toHaveCount(0);
    // A DRAFT, by contrast, can be activated.
    //
    // The row is located by the CONTROL it offers, not by matching its text.
    // Text matching was tried and was wrong twice over: Playwright's `hasText`
    // string form is case-INSENSITIVE, so 'DRAFT' also matched the ACTIVE row's
    // "Immutable - draft a new version"; and both fixture definitions share the
    // code PR-STANDARD, so the code does not identify a row either. What this
    // assertion is actually about is "the row that offers Activate", so that is
    // what it locates.
    const activateButtons = page.getByRole('button', { name: 'Activate' });
    await expect(activateButtons,
      'exactly one definition - the DRAFT - should offer activation').toHaveCount(1);

    const draftRow = page.locator('table tbody tr').filter({ has: activateButtons });
    await expect(draftRow).toHaveCount(1);
    await expect(draftRow).toContainText('DRAFT');
    await expect(draftRow).toContainText('PR-STANDARD');

    // And the ACTIVE row is not the one offering it.
    await expect(activeRow.getByRole('button', { name: 'Activate' })).toHaveCount(0);
  });

  test('the simulator reports an unroutable object as an exception, never as no-approval-needed', async ({ page }) => {
    // Contract 2: there is NO ROUTE TO AUTO-APPROVAL. A simulator that showed
    // an unroutable object as "nothing to do" would teach a configurator that
    // a fail-closed defect is an acceptable outcome.
    await page.route('**/api/approvals/definitions/*/simulate',
      (route) => route.fulfill(json(UNROUTABLE_SIMULATION)));
    await gotoScreen(page, 'approval-simulator');
    await page.locator('#simulatorForm button[type="submit"]').click();

    const result = page.locator('#simulatorResultHost');
    await expect(result).toContainText('would NOT be approved', { timeout: 15_000 });
    await expect(result).toContainText('Exception pending');
    await expect(result).toContainText('No independent approver');
    const txt = (await result.innerText()).toLowerCase();
    expect(txt).not.toContain('no approval required');
    expect(txt).not.toContain('auto-approved');
  });

  test('a decision sends an idempotency key and the object version it was taken against', async ({ page }) => {
    // Contract 8. Without both, a replay applies twice and a stale decision
    // approves a document that has since moved.
    const bodies = [];
    await page.route('**/api/approvals/*/decide', (route) => {
      bodies.push(JSON.parse(route.request().postData() || '{}'));
      return route.fulfill(json({ status: 'APPROVED' }));
    });
    await gotoScreen(page, 'approval-request');
    await page.locator('#approvalDecisionForm button[type="submit"]').click();
    await expect.poll(() => bodies.length).toBeGreaterThan(0);

    expect(bodies[0].idempotency_key, 'no idempotency key was sent').toBeTruthy();
    expect(bodies[0].object_version, 'no object version was sent').toBe(3);
    expect(bodies[0].action).toBe('APPROVE');
  });

  test('a refused decision shows the SERVER refusal — no client-side authorization', async ({ page }) => {
    // Contract 4: whether a caller may act on an instance is assignment plus
    // maker-checker, checked inside the transaction. The browser cannot know
    // it, so the controls are rendered and the server's refusal is displayed.
    await page.route('**/api/approvals/*/decide', (route) => route.fulfill(json({
      code: 'SELF_APPROVAL',
      message: 'You raised or edited this object, so you may not approve it.',
    }, 403)));
    await gotoScreen(page, 'approval-request');

    // The control exists. It is not hidden on a guess the client cannot make.
    const submit = page.locator('#approvalDecisionForm button[type="submit"]');
    await expect(submit).toBeVisible();
    await submit.click();

    const outcome = page.locator('#approvalDetailOutcome');
    await expect(outcome).toContainText('you may not approve it', { timeout: 15_000 });
    await expect(outcome).toContainText('must be decided by someone independent');
  });

  test('an idempotent replay reports the ORIGINAL outcome, not a second decision', async ({ page }) => {
    await page.route('**/api/approvals/*/decide', (route) => route.fulfill(json({
      code: 'IDEMPOTENT_REPLAY', status: 'APPROVED',
    })));
    await gotoScreen(page, 'approval-request');
    await page.locator('#approvalDecisionForm button[type="submit"]').click();
    const outcome = page.locator('#approvalDetailOutcome');
    await expect(outcome).toContainText('already been recorded', { timeout: 15_000 });
    await expect(outcome).toContainText('not applied twice');
  });

  test('a revoked delegation stays on the list as a record', async ({ page }) => {
    await gotoScreen(page, 'approval-delegations');
    const row = page.locator('table tbody tr', { hasText: 'DLG-002' }).first();
    await expect(page.locator('table tbody')).toContainText('U-PM');
    await expect(row).toContainText('Revoked');
    await expect(row).toContainText('Returned from leave early.');
    await expect(row.getByRole('button', { name: 'Revoke' })).toHaveCount(0);
  });

  test('a retired workflow version keeps the count of what was decided under it', async ({ page }) => {
    await gotoScreen(page, 'approval-versions');
    const row = page.locator('table tbody tr', { hasText: 'RETIRED' }).first();
    await expect(row).toContainText('187');
  });

  test('a lifecycle action asks for a reason in a real dialog, and refuses an empty one', async ({ page }) => {
    // The reason is written into an append-only, hash-chained audit record and
    // read later by whoever reconstructs what happened, so it must actually be
    // captured — and captured somewhere that can be labelled, described and
    // validated. window.prompt() can do none of those and is suppressed
    // outright in some embedding contexts, where the action would either
    // proceed with no reason or silently not happen.
    let sent = null;
    await page.route('**/api/approvals/*/recall', (route) => {
      sent = JSON.parse(route.request().postData() || '{}');
      return route.fulfill(json({ status: 'RECALLED' }));
    });
    await gotoScreen(page, 'approval-request');
    await page.getByRole('button', { name: 'Recall', exact: true }).click();

    const dialog = page.locator('dialog[open]');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('Recall');
    // A real, associated label — not placeholder text.
    await expect(dialog.getByLabel('Reason')).toBeVisible();

    // An empty reason is refused, in the dialog, without sending anything.
    await dialog.getByRole('button', { name: 'Recall', exact: true }).click();
    await expect(dialog.locator('.msg-error')).toBeVisible();
    expect(sent, 'a lifecycle action was sent with no reason').toBeNull();

    await dialog.getByLabel('Reason').fill('Raised against the wrong WBS element.');
    await dialog.getByRole('button', { name: 'Recall', exact: true }).click();
    await expect.poll(() => sent).not.toBeNull();
    expect(sent.reason_text).toBe('Raised against the wrong WBS element.');
    await expect(page.locator('dialog[open]')).toHaveCount(0);
  });

  test('a refused lifecycle action keeps the dialog open with the typed reason intact', async ({ page }) => {
    await page.route('**/api/approvals/*/recall', (route) => route.fulfill(json({
      code: 'STAGE_NOT_OPEN', message: 'This approval is no longer open.',
    }, 409)));
    await gotoScreen(page, 'approval-request');
    await page.getByRole('button', { name: 'Recall', exact: true }).click();

    const dialog = page.locator('dialog[open]');
    await dialog.getByLabel('Reason').fill('Superseded by a revised request.');
    await dialog.getByRole('button', { name: 'Recall', exact: true }).click();

    await expect(dialog.locator('.msg-error')).toContainText('no longer open');
    // Still open, and what the user wrote is still there to correct.
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel('Reason')).toHaveValue('Superseded by a revised request.');
  });
});

/* ==================================================== accessibility */

test.describe('Approval screens — accessibility', () => {
  test.beforeEach(async ({ page }) => { await prepared(page); });

  for (const s of SCREENS) {
    test(`axe-core: ${s.hash} has no violations`, async ({ page }) => {
      await gotoScreen(page, s.hash);

      // The mounted screen itself.
      const screenOnly = await new AxeBuilder({ page }).include('#content').analyze();
      expect(screenOnly.violations, JSON.stringify(screenOnly.violations, null, 2)).toEqual([]);

      // And the whole page around it, with nothing excluded, so a screen
      // cannot pass by sitting in a broken shell.
      const wholePage = await new AxeBuilder({ page }).analyze();
      expect(wholePage.violations, JSON.stringify(wholePage.violations, null, 2)).toEqual([]);
    });

    test(`${s.hash} — Tab alone reaches every control, with visible focus`, async ({ page }) => {
      await gotoScreen(page, s.hash);

      // Focus is checked on the element Tab ACTUALLY LANDS ON, never by calling
      // el.focus() from script. `:focus-visible` is a browser heuristic: a
      // programmatic focus() on a <button> deliberately does NOT match it, so a
      // test that focuses each control itself measures the heuristic rather
      // than the design and reports a false failure on every button.
      const SELECTOR = '#content .scr-host button, #content .scr-host input, '
        + '#content .scr-host select, #content .scr-host textarea, '
        + '#content .scr-host a[href], #content .scr-host [tabindex="0"]';

      // Only VISIBLE controls: a hidden table's controls are not reachable by
      // Tab and should not be, so counting them would make this unsatisfiable.
      const expected = await page.evaluate((sel) => [...document.querySelectorAll(sel)]
        .filter((el) => el.offsetParent !== null && !el.disabled)
        .map((el) => (el.id || '') + '|' + el.tagName + '|' + (el.textContent || '').slice(0, 24)),
      SELECTOR);
      expect(expected.length, `${s.hash} rendered no focusable control`).toBeGreaterThan(0);

      await page.locator('#content').focus();
      const seen = new Set();
      const ringSeen = new Set();
      // Generous bound: a native date input alone consumes four Tab presses.
      for (let i = 0; i < expected.length + 40; i += 1) {
        await page.keyboard.press('Tab');
        const info = await page.evaluate(() => {
          const el = document.activeElement;
          if (!el || el === document.body) return null;
          if (!el.closest('#content .scr-host')) return { inside: false };
          const cs = getComputedStyle(el);
          return {
            inside: true,
            key: (el.id || '') + '|' + el.tagName + '|' + (el.textContent || '').slice(0, 24),
            isDate: el.tagName === 'INPUT' && el.getAttribute('type') === 'date',
            focusVisible: el.matches(':focus-visible'),
            outlineWidth: parseFloat(cs.outlineWidth) || 0,
            outlineStyle: cs.outlineStyle,
          };
        });
        if (!info || !info.inside) continue;
        seen.add(info.key);

        // A NATIVE DATE INPUT HAS FOUR TAB STOPS, AND THE LAST ONE IS NOT OURS.
        //
        // Chromium's <input type="date"> tabs through day, month and year, and
        // then to the calendar-picker button inside its SHADOW tree.
        // document.activeElement is still the <input> on that fourth stop, but
        // :focus-visible is false and outline-style is 'none', because the
        // thing actually focused is a shadow-internal control that Chromium
        // draws its own indicator on and that no page stylesheet can reach.
        //
        // Asserting a ring there measures the browser, not the design. It is
        // NOT waved through, though: `ringSeen` below records that the input
        // did take a properly-ringed focus on one of its own stops, so a date
        // input that genuinely lost its ring still fails.
        if (info.isDate && !info.focusVisible) continue;

        // styles.css draws one high-contrast ring through :focus-visible, and
        // Tab is exactly the interaction that triggers it.
        expect(info.focusVisible, `${s.hash}: ${info.key} took focus with no :focus-visible ring`)
          .toBe(true);
        expect(info.outlineStyle, `${s.hash}: ${info.key} has outline-style none while focused`)
          .not.toBe('none');
        expect(info.outlineWidth, `${s.hash}: ${info.key} has a zero-width focus outline`)
          .toBeGreaterThan(0);
        ringSeen.add(info.key);
      }

      // Every control that Tab reached must have shown a real focus ring at
      // least once, date inputs included.
      const ringless = [...seen].filter((k) => !ringSeen.has(k));
      expect(ringless, `${s.hash}: controls that never showed a focus ring`).toEqual([]);

      // EVERY visible control, not merely "at least one". A control that can be
      // clicked but never tabbed to is unreachable for a keyboard-only user,
      // and "> 0" would pass a screen where only the first field was reachable.
      const missed = expected.filter((k) => !seen.has(k));
      expect(missed, `${s.hash}: Tab never reached ${missed.length} of ${expected.length} controls`)
        .toEqual([]);
    });

    test(`${s.hash} — every status is distinguishable without colour`, async ({ page }) => {
      await gotoScreen(page, s.hash);
      const statuses = page.locator('#content .scr-host .status');
      const n = await statuses.count();
      if (n === 0) return;   // a screen may legitimately show no status chip

      const textless = await page.evaluate(
        () => [...document.querySelectorAll('#content .scr-host .status')]
          .filter((el) => !el.textContent.trim()).length,
      );
      expect(textless, `${s.hash}: a status rendered with no text label`).toBe(0);

      // And each carries a non-colour symbol as well as its label.
      const withoutSymbol = await page.evaluate(
        () => [...document.querySelectorAll('#content .scr-host .status')]
          .filter((el) => !el.querySelector('.sym')).length,
      );
      expect(withoutSymbol, `${s.hash}: a status carried no non-colour indicator`).toBe(0);
    });
  }

  test('approval statuses that share a tone still differ by glyph', async ({ page }) => {
    // The reason approval-status-badge.js exists at all: two statuses with the
    // same semantic role must not be indistinguishable to someone who cannot
    // separate them by colour, and RETURNED / ESCALATED are both 'warning'.
    await gotoScreen(page, 'approval-request');
    const glyphs = await page.evaluate(async () => {
      const mod = await import('/static/src/components/approvals/approval-status-badge.js');
      const out = {};
      for (const ns of ['instance', 'stage', 'assignment']) {
        out[ns] = mod.approvalStatusCodes(ns)
          .map((code) => mod.approvalStatusMeta(ns, code).glyph);
      }
      return out;
    });
    for (const [ns, list] of Object.entries(glyphs)) {
      expect(new Set(list).size, `${ns}: two statuses share a glyph — ${list.join(' ')}`)
        .toBe(list.length);
    }
  });
});

/* =========================================================== CSP / XSS */

const APPROVAL_SOURCES = [
  '/static/src/features/approvals/approvals-api.js',
  '/static/src/features/approvals/approval-inbox.js',
  '/static/src/features/approvals/approval-detail.js',
  '/static/src/features/approvals/approval-timeline.js',
  '/static/src/features/approvals/approval-matrix.js',
  '/static/src/features/approvals/workflow-versions.js',
  '/static/src/features/approvals/rule-simulator.js',
  '/static/src/features/approvals/delegations.js',
  '/static/src/features/approvals/sla-monitor.js',
  '/static/src/components/approvals/approval-status-badge.js',
  '/static/src/components/approvals/state-host.js',
  '/static/src/components/approvals/screen-kit.js',
  '/static/src/components/approvals/reason-dialog.js',
];

test.describe('Approval screens — the CSP contract', () => {
  test('no CSP violation is reported on any approval route', async ({ page }) => {
    // main.py sends `style-src 'self'` with no 'unsafe-inline', so a markup
    // style="…" attribute is blocked outright and reported. CSSOM assignment
    // is not governed by the policy at all, which is why this asserts on the
    // browser's own violation reports rather than on a `[style]` selector.
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
    await prepared(page);

    for (const s of SCREENS) {
      await gotoScreen(page, s.hash);
      const inPage = await page.evaluate(() => window.__cspViolations || []);
      expect(inPage, `${s.hash}: ${inPage.join('; ')}`).toEqual([]);
    }
    expect(reports, reports.join('\n')).toEqual([]);
  });

  test('no approval source file contains a style="…" literal', async ({ page }) => {
    // The static half of the same contract. core/dom.js's h() throws on a
    // `style` attribute, but a hand-assembled template string would slip past
    // it, so the SHIPPED files are read back over HTTP and checked.
    await prepared(page);
    for (const f of APPROVAL_SOURCES) {
      const res = await page.request.get(f);
      expect(res.ok(), `${f} is not being served`).toBe(true);
      const body = await res.text();
      // Comments are stripped first: these files document the rule by quoting
      // the very attribute they forbid.
      const code = body
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
      const emitted = code.split('\n').filter((line) => /style\s*=\s*["'`]/.test(line));
      expect(emitted, `${f} emits a style attribute:\n${emitted.join('\n')}`).toEqual([]);
    }
  });

  test('approvals.css introduces no raw colour and no unscoped selector', async ({ page }) => {
    // Two rules at once. A raw hex here would be a colour outside the frozen
    // token registry; an unscoped selector would restyle the approved shell
    // views the moment this stylesheet is loaded, which is exactly the
    // settings.css defect that forced per-route stylesheet injection.
    await prepared(page);
    const res = await page.request.get('/static/approvals.css');
    expect(res.ok(), 'approvals.css is not being served').toBe(true);
    const css = (await res.text()).replace(/\/\*[\s\S]*?\*\//g, '');

    expect(css.match(/#[0-9A-Fa-f]{3,8}\b/g) || [],
      'approvals.css declares a raw colour instead of using a token').toEqual([]);
    expect(css.includes(':root'),
      'approvals.css declares a custom property; tokens belong in styles.css').toBe(false);

    // Every selector must be anchored on an `approval-` class or a media rule.
    const unscoped = [];
    for (const block of css.split('}')) {
      const head = block.split('{')[0];
      if (!head || !block.includes('{')) continue;
      for (const sel of head.split(',')) {
        const s = sel.trim();
        if (!s || s.startsWith('@') || s.startsWith('/')) continue;
        if (!/(^|[\s>+~])\.approval-/.test(s) && !/^\.approval-/.test(s)) unscoped.push(s);
      }
    }
    expect(unscoped, `approvals.css has selectors not scoped to a feature class:\n${unscoped.join('\n')}`)
      .toEqual([]);
  });
});

/* ==================================================== visual regression */

test.describe('Approval screens — visual regression', () => {
  test.beforeEach(async ({ page }) => { await prepared(page); });

  for (const s of SCREENS) {
    test(`${s.hash} renders identically`, async ({ page }) => {
      await gotoScreen(page, s.hash);
      await expect(page).toHaveScreenshot(`approvals-${s.hash}.png`, { fullPage: true });
    });
  }
});
