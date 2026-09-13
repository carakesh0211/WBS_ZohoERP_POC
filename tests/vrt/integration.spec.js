// tests/vrt/integration.spec.js
//
// WAVE 5 — THE INTEGRATION PLATFORM'S TWELVE SCREENS AS ROUTES OF THE SPA SHELL.
//
//   SCR-31  Zoho ERP Connection Setup Wizard        #integration-setup
//   SCR-32  Zoho OAuth Authorisation and Consent    #integration-oauth
//   SCR-33  Zoho Organisation Selection and Mapping #integration-organisation
//   SCR-34  API Scope and Permission Validation     #integration-scopes
//   SCR-38  Integration Health and API Usage        #integration-health
//   (none)  Outbound Purchase Order Queue           #integration-outbound-po
//   (none)  Inbound GRN / Purchase Receive Status   #integration-inbound-grn
//   (none)  Inbound Vendor Bill Status              #integration-inbound-bill
//   SCR-39  Failed Sync and Retry Queue             #integration-retry
//   SCR-26  Sync History and Control Totals         #integration-events
//   SCR-18  Commitment-to-Actual Reconciliation     #integration-reconciliation
//   SCR-27  Reconciliation Exception Queue          #integration-exceptions
//
// THREE OF THE TWELVE CARRY `scr: null`. C8_screens.json is frozen at forty and
// has no screen for an outbound emission queue or for the acquisition status of
// a receive or a bill — SCR-15/16/17 are the FINANCIAL views of those documents
// and answer a different question. A plausible SCR-41 would manufacture
// traceability the registry does not provide, so the null is asserted below and
// reported to the lead as a contract gap.
//
// HOW THESE SCREENS ARE REACHABLE WITHOUT THIS STREAM EDITING app.js OR router.js
// ------------------------------------------------------------------------------
// Route registration and the navigation rail belong to the LEAD. This stream
// owns app/frontend/src/features/integration/** and neither of those two files,
// so it ships the screens plus manifest.js — which declares, in one place, the
// twelve route entries router.js spreads into SCREENS and the twelve gate rows
// app.js pastes into SCR_ROUTES.
//
// `installRoutes()` below performs EXACTLY those two splices in the running
// page, from that manifest, before the shell's first render. So this suite is
// not testing a mock of the wiring: it is testing the wiring, applied from the
// same declaration the lead will apply, and a manifest that does not work here
// will not work there. `PASTE_ROWS` is the literal transcription of the app.js
// half, and one test asserts it still deep-equals the manifest — so a manifest
// change that nobody carried into app.js fails the suite rather than shipping.
//
// THE PRIMARY NAVIGATION IS NOT TOUCHED, AND THIS FILE MEASURES THAT.
// A previous stream added eight rail entries, overflowed the rail by 194px at
// 1440 and 336px at 1024, and had them withdrawn. Nothing here goes into the
// rail; the tests below MEASURE that at all three configured viewports rather
// than asserting it in prose.
//
// WHAT THIS FILE PROVES THAT NOTHING ELSE DOES
//
//  1. REQ-INT-024, the client secret, in four independent checks — including
//     the two that matter: that a SERVER which returns a secret cannot get its
//     value into the DOM, and that the leak is REPORTED rather than silently
//     dropped.
//  2. An ABSENT endpoint renders as absent, not as empty — the distinction
//     between "the dead-letter queue is empty" and "this build has no
//     dead-letter endpoint".
//  3. A fallback NAMES ITSELF, in three flavours: the Wave 4 connector surface,
//     this application's own ledger, and — for control totals — no fallback at
//     all, because a total that compares us with ourselves always balances.
//  4. C16's separation holds: the QUEUED / SENT / FAILED badge renders and a C3
//     business status passed to it THROWS.
//  5. The `data-mounted` contract: a screen whose mount() throws is marked
//     data-mount-failed and never data-mounted.
//
// Conventions follow spa-routing.spec.js and approvals.spec.js: sign in through
// the real backend (session, permissions and the shell chrome are part of what
// is under test and must not be faked), then intercept ONLY the integration
// endpoints with page.route(). No fixed timeouts anywhere.
//
// This file owns no configuration. package.json and playwright.config.js are
// not declared or modified here.

const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

/* ---------------- identities ----------------
   Demo credentials are user_id + '!demo', provisioned idempotently by
   auth.provision_dev_identities.

   auth.PERMISSIONS: `connector.read` is held by Administrator and Auditor;
   `connector.manage` by Administrator alone; `budget.read` by every role. So
   U-ADM reaches all twelve, U-AUD reaches the nine read surfaces, and U-REQ
   reaches exactly the two reconciliation screens — which makes each of the
   three a positive control on a different gate rather than a bare negative. */
const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };
const AUDITOR = { user: 'U-AUD', password: 'U-AUD!demo' };
const REQUESTOR = { user: 'U-REQ', password: 'U-REQ!demo' };

/* ---------------- the twelve screens under test ---------------- */

const MANAGE_SCREENS = [
  { hash: 'integration-setup', scr: 'SCR-31', title: 'Zoho ERP Connection Setup Wizard' },
  { hash: 'integration-oauth', scr: 'SCR-32', title: 'Zoho OAuth Authorisation and Consent' },
  { hash: 'integration-organisation', scr: 'SCR-33', title: 'Zoho Organisation Selection and Mapping' },
];

const READ_SCREENS = [
  { hash: 'integration-scopes', scr: 'SCR-34', title: 'API Scope and Permission Validation' },
  { hash: 'integration-health', scr: 'SCR-38', title: 'Integration Health and API Usage' },
  { hash: 'integration-outbound-po', scr: null, title: 'Outbound Purchase Order Queue' },
  { hash: 'integration-inbound-grn', scr: null, title: 'Inbound GRN and Purchase Receive Status' },
  { hash: 'integration-inbound-bill', scr: null, title: 'Inbound Vendor Bill Status' },
  { hash: 'integration-retry', scr: 'SCR-39', title: 'Failed Sync and Retry Queue' },
  { hash: 'integration-events', scr: 'SCR-26', title: 'Integration Event Monitor — Sync History and Control Totals' },
];

/* Gated on `budget.read`, not on a connector permission: they read
   commitment-versus-actual data, their backing endpoint needs only an
   authenticated principal, and gating them on connector.read would hide a
   financial control from every finance role in the application. */
const LEDGER_SCREENS = [
  { hash: 'integration-reconciliation', scr: 'SCR-18', title: 'Commitment-to-Actual Reconciliation' },
  { hash: 'integration-exceptions', scr: 'SCR-27', title: 'Reconciliation Exception Queue' },
];

const SCREENS = [...MANAGE_SCREENS, ...READ_SCREENS, ...LEDGER_SCREENS];

/**
 * THE app.js PASTE, transcribed.
 *
 * manifest.js tells the lead to paste exactly these rows into `SCR_ROUTES`.
 * This constant is that paste, and `installRoutes()` applies it to the running
 * page the same way app.js will. The test
 * "the app.js paste still matches the manifest" asserts the two have not
 * drifted, so a manifest edit that nobody carried across fails here.
 */
const PASTE_ROWS = [
  { id: 'integration-setup', ico: '⊕', label: 'Connection Setup Wizard', need: ['connector.manage'] },
  { id: 'integration-oauth', ico: '⚿', label: 'OAuth Authorisation & Consent', need: ['connector.manage'] },
  { id: 'integration-organisation', ico: '⌾', label: 'Organisation Selection & Mapping', need: ['connector.manage'] },
  { id: 'integration-scopes', ico: '⊙', label: 'API Scope & Permission Validation', need: ['connector.read'] },
  { id: 'integration-health', ico: '◔', label: 'Integration Health & API Usage', need: ['connector.read'] },
  { id: 'integration-outbound-po', ico: '⇧', label: 'Outbound Purchase Order Queue', need: ['connector.read'] },
  { id: 'integration-inbound-grn', ico: '⇩', label: 'Inbound GRN / Purchase Receive Status', need: ['connector.read'] },
  { id: 'integration-inbound-bill', ico: '⇵', label: 'Inbound Vendor Bill Status', need: ['connector.read'] },
  { id: 'integration-retry', ico: '⟲', label: 'Failed Sync & Retry Queue', need: ['connector.read'] },
  { id: 'integration-events', ico: '⇄', label: 'Sync History & Control Totals', need: ['connector.read'] },
  { id: 'integration-reconciliation', ico: '⚖', label: 'Commitment-to-Actual Reconciliation', need: ['budget.read'] },
  { id: 'integration-exceptions', ico: '⚑', label: 'Reconciliation Exception Queue', need: ['budget.read'] },
];

/**
 * Publish, before the shell's first render, how many integration routes the
 * SHIPPED build declares -- so `waitForFunction(... === 12)` is an assertion
 * about app.js rather than about this file.
 *
 * It used to APPLY the manifest's two splices to the running page, because the
 * lead had never applied them to the application. That made every integration
 * test green against a build that existed only inside the test: the screens
 * were unreachable in a real browser for three waves, and the assertion at
 * "SCR_ROUTES and the manifest agree" was self-fulfilling because this function
 * had just written the rows it then read back.
 *
 * TIMING. `addInitScript` runs before any page script, when neither SCR_ROUTES
 * nor V exists yet, so the work is deferred to DOMContentLoaded — which fires
 * after app.js's top-level code (its <script> is the last element in <body>)
 * and before its bootstrap IIFE finishes its first `await api('/health')`. So
 * the routes are in place before `render()` ever reads them.
 *
 * The SYNCHRONOUS half is the gate rows: `viewAllowed()` runs synchronously
 * inside render() and a route missing from SCR_ROUTES at that moment is
 * corrected to home. The build is deferred behind a dynamic import of the
 * manifest, exactly as router.js defers its own feature modules — a view
 * function is allowed to be async and app.js awaits it.
 *
 * `SCR_ROUTES`, `V` and `setHeader` are app.js's own top-level declarations.
 * A top-level `const` in a classic script lives in the global lexical
 * environment, which is on the scope chain of any code evaluated in the same
 * realm, so they resolve here as free identifiers.
 */
async function installRoutes(page) {
  await page.addInitScript(() => {
    document.addEventListener('DOMContentLoaded', () => {
      // Count what the SHIPPED app declares. Nothing is pushed and no view is
      // overridden: app.js:442 already builds one `V` entry per SCR_ROUTES row,
      // which resolves through router.js's SCREENS, so the screens mount by the
      // same path a user's browser uses.
      window.__integrationRoutesInstalled =
        // eslint-disable-next-line no-undef
        SCR_ROUTES.filter((r) => r.id.startsWith('integration-')).length;
    });
  });
}

/* ---------------- fixtures ---------------- */

const CONNECTIONS = {
  items: [{
    connection_id: 'CONN-01',
    entity_id: 'ENT-ATHA',
    connector_name: 'Atha Steel — Zoho ERP',
    product: 'ERP',
    dc: 'IN',
    organization_id: '60021234567',
    mode: 'MOCK',
    client_id: '1000.ABCDEF',
    client_secret_present: true,
    oauth_status: 'Connected',
    granted_scopes: ['ERP.bills.READ'],
    access_token_expiry: '2026-09-06T12:00:00Z',
    token_last_refreshed: '2026-09-06T11:00:00Z',
  }],
};

const ORGANISATIONS = {
  items: [
    { organization_id: '60021234567', name: 'Atha Steel & Power Ltd', currency_code: 'INR' },
    { organization_id: '60029999999', name: 'Atha Cement Ltd', currency_code: 'INR' },
  ],
};

const SCOPES = {
  required: [{
    scope: 'ERP.bills.READ',
    business_impact_if_missing: 'Actual CWIP cannot be mirrored.',
    modules: ['bills'],
  }, {
    scope: 'ERP.purchaseorders.READ',
    business_impact_if_missing: 'Commitment cannot be mirrored.',
    modules: ['purchase-order'],
  }],
  /* THE ID AND THE TITLE MUST BE THE SAME FINDING.
     This fixture invented an OAS-02 that says what OAS-03 says.
     `research/20_verified/openapi_findings.json` is the register: OAS-02 is
     "Purchase Request does not exist anywhere in the official ERP API
     specification"; OAS-03 is the GRN-leg finding that carries the missing
     Purchase Receives list endpoint. A fixture that contradicts the register
     does not merely fail to catch a miscitation — it PINS one, which is what
     it did: the assertion below read `toContainText('OAS-02')` and so required
     the screen to keep citing the wrong finding. */
  hard_negatives: [{
    id: 'OAS-03',
    severity: 'HIGH',
    title: 'Zoho ERP Purchase Receives has no list endpoint',
    impact: 'PO-anchored discovery is the sole acquisition mechanism for GRNs.',
  }],
};

const EVENTS = {
  items: [{
    at: '2026-09-06T09:14:22Z',
    direction: 'INBOUND',
    queue: 'inbox',
    module: 'bills',
    endpoint: '/erp/v3/bills',
    http_method: 'GET',
    state: 'QUARANTINED',
    integration_state: 'FAILED',
    external_status_raw: 'partially_billed_weird',
    status_mapped: false,
    attempts: 3,
    correlation_id: 'a1b2c3d4-e5f6-4789-9abc-def012345678',
    message: 'Could not be attributed to a purchase order.',
  }, {
    at: '2026-09-06T09:20:00Z',
    direction: 'OUTBOUND',
    queue: 'outbox',
    module: 'purchase-order',
    endpoint: '/erp/v3/purchaseorders',
    http_method: 'POST',
    state: 'SENT',
    integration_state: 'SENT',
    attempts: 1,
    correlation_id: 'bbbbbbbb-cccc-4ddd-9eee-ffffffffffff',
  }],
  next_cursor: null,
  has_more: false,
};

/* One window in balance, one out of balance. Both halves are needed: a screen
   that only ever renders a balanced total would pass without ever proving it
   can say the word "OUT OF BALANCE". */
const CONTROL_TOTALS = {
  windows: [
    {
      module: 'purchase-order',
      window_start: '2026-09-06T00:00:00Z',
      local_count: 12,
      external_count: 12,
      local_paise: 4500000,
      external_paise: 4500000,
    },
    {
      module: 'bills',
      window_start: '2026-09-06T00:00:00Z',
      local_count: 9,
      external_count: 11,
      local_paise: 3300000,
      external_paise: 3910000,
    },
  ],
};

const HEALTH = {
  mode: 'MOCK',
  rate_budgets: [
    { window_kind: 'DAY', used: 1800, ceiling: 2000, window_start: '2026-09-06T00:00:00Z' },
    { window_kind: 'MINUTE', used: 4, ceiling: 100, window_start: '2026-09-06T09:20:00Z' },
  ],
  access_token_expires_at: '2026-09-06T12:00:00Z',
  token_last_refreshed: '2026-09-06T11:00:00Z',
  refresh_token_present: true,
  circuits: [
    { module: 'bills', state: 'CIRCUIT_OPEN', reason: 'Daily quota exhausted (429, code 45).', counts_toward_circuit: false, next_probe_at: '2026-09-07T00:00:00Z' },
    { module: 'purchase-order', state: 'CIRCUIT_CLOSED' },
  ],
  watermarks: [
    { module: 'bills', hwm: '2026-09-06T09:00:00Z', overlap_seconds: 300, last_sweep_at: '2026-09-05T18:00:00Z' },
    { module: 'purchasereceives', hwm: '2026-09-06T08:00:00Z', overlap_seconds: 300, last_sweep_at: null },
  ],
};

const DEAD_LETTERS = {
  items: [{
    queue: 'outbox',
    row_id: 'OUT-77',
    module: 'purchase-order',
    state: 'DEAD',
    local_id: 'PO-2026-0008',
    external_id: '4600000123',
    attempts: 8,
    next_attempt_at: null,
    error_code: 'ZOHO_5XX',
    last_error: 'Upstream returned 503 on the eighth attempt.',
    correlation_id: 'dddddddd-eeee-4fff-8aaa-bbbbbbbbbbbb',
  }, {
    queue: 'inbox',
    row_id: 'IN-42',
    module: 'bills',
    state: 'QUARANTINED',
    attempts: 1,
    error_code: 'UNMAPPED_EXTERNAL_STATUS',
    last_error: 'Raw status "weird" has no mapping.',
  }],
  next_cursor: null,
  has_more: false,
};

/* An outbox row that is DEAD and ALREADY CARRIES AN EXTERNAL ID — the send
   landed and only the recording of it failed. That is the one row on the
   outbound queue whose misreading costs a second purchase order. */
const OUTBOX = {
  items: [{
    row_id: 'OUT-77',
    local_id: 'PO-2026-0008',
    module: 'purchase-order',
    state: 'DEAD',
    integration_state: 'FAILED',
    external_id: '4600000123',
    ordered_paise: 250000000,
    attempts: 8,
    next_attempt_at: null,
    error_code: 'ZOHO_5XX',
    last_error: 'Upstream returned 503 on the eighth attempt.',
    correlation_id: 'dddddddd-eeee-4fff-8aaa-bbbbbbbbbbbb',
  }, {
    row_id: 'OUT-78',
    local_id: 'PO-2026-0009',
    module: 'purchase-order',
    state: 'PENDING',
    integration_state: 'QUEUED',
    external_id: null,
    ordered_paise: 90000000,
    attempts: 0,
  }],
  next_cursor: null,
  has_more: false,
};

const INBOX_GRN = {
  items: [{
    row_id: 'IN-11',
    external_id: 'PR-99001',
    local_id: 'GRN-2026-0003',
    anchor_po: 'PO-2026-0008',
    module: 'purchasereceives',
    state: 'PROCESSED',
    received_at: '2026-09-05T10:00:00Z',
    acquired_at: '2026-09-05T10:04:00Z',
    amount_paise: 120000000,
    correlation_id: 'eeeeeeee-ffff-4aaa-8bbb-cccccccccccc',
  }, {
    // No anchor: acquisition is PO-anchored, so this row's very existence is
    // the defect and the screen must say so rather than showing a blank.
    row_id: 'IN-12',
    external_id: 'PR-99002',
    local_id: null,
    anchor_po: null,
    module: 'purchasereceives',
    state: 'QUARANTINED',
    received_at: '2026-09-05T11:00:00Z',
    acquired_at: null,
    amount_paise: 4500000,
    error_code: 'UNATTRIBUTED_RECEIPT',
    last_error: 'No purchase order matched this receive.',
  }],
  next_cursor: null,
  has_more: false,
};

const INBOX_BILLS = {
  items: [{
    row_id: 'IN-42',
    external_id: 'BILL-77001',
    local_id: 'BILL-2026-0012',
    vendor_name: 'Larsen Fabricators',
    bill_date: '2026-09-04T00:00:00Z',
    amount_paise: 88000000,
    module: 'bills',
    state: 'QUARANTINED',
    external_status_raw: 'partially_billed_weird',
    status_mapped: false,
    accounting_effective: false,
    acquired_at: '2026-09-04T06:00:00Z',
    correlation_id: 'a1b2c3d4-e5f6-4789-9abc-def012345678',
  }, {
    row_id: 'IN-43',
    external_id: 'BILL-77002',
    local_id: 'BILL-2026-0013',
    vendor_name: 'Kirloskar Pumps',
    bill_date: '2026-09-05T00:00:00Z',
    amount_paise: 15000000,
    module: 'bills',
    state: 'PROCESSED',
    external_status_raw: 'paid',
    status_mapped: true,
    accounting_effective: true,
    acquired_at: '2026-09-05T06:00:00Z',
  }],
  next_cursor: null,
  has_more: false,
};

/* The Wave 5 reconciliation response, as `api/integrations.py::get_reconciliation`
   actually shapes it now that `013_procurement.sql` has backed it.

   `residual_released_paise` / `over_billed_paise` and the `identity_*` block are
   NOT decoration. The route holds itself to
   `ordered - billed = open + residual_released - over_billed` and reports the
   residual in paise, so SCR-18 can render whether the figures actually tied out
   rather than asserting it. Both rows below satisfy it exactly:

     row 1  25,00,000 - 10,00,000 = 15,00,000 + 0 - 0
     row 2   9,00,000 -  9,50,000 =        0 + 0 - 50,000

   `emission_state` is the half the ledger fallback cannot answer: the outbox row
   for the purchase order. Row 2 carries none, which is a real answer ("nothing
   was ever enqueued for it") and not a missing one. */
const RECONCILIATION = {
  source: 'wave5',
  rows: [{
    po_number: 'PO-2026-0008', line_no: 1, wbs_code: 'W-02-01', vendor_name: 'Larsen Fabricators',
    ordered_paise: 250000000, received_paise: 120000000, billed_paise: 100000000,
    open_commitment_paise: 150000000, received_not_billed_paise: 20000000,
    exposure_paise: 250000000, residual_released_paise: 0, over_billed_paise: 0,
    flag: 'received-unbilled', position: 'Partially billed',
    emission_state: {
      rows: 1, split: false, states: { SENT: 1 }, state: 'SENT',
      external_id: '4600000123', external_ids: ['4600000123'], attempts: 1,
    },
  }, {
    po_number: 'PO-2026-0009', line_no: 1, wbs_code: 'W-03', vendor_name: 'Kirloskar Pumps',
    ordered_paise: 90000000, received_paise: 90000000, billed_paise: 95000000,
    open_commitment_paise: 0, received_not_billed_paise: 0,
    exposure_paise: 95000000, residual_released_paise: 0, over_billed_paise: 5000000,
    flag: 'over-billed', position: 'Bill exceeds PO',
    // No outbox row at all. ABSENT, never a fabricated state: nothing has ever
    // been enqueued for this purchase order, and that is a real answer.
    emission_state: null,
  }],
  summary: {
    lines: 2,
    ordered_paise: 340000000,
    received_paise: 210000000,
    open_commitment_paise: 150000000,
    billed_paise: 195000000,
    received_not_billed_paise: 20000000,
    exposure_paise: 345000000,
    residual_released_paise: 0,
    over_billed_paise: 5000000,
    identity: 'ordered - billed = open_commitment + residual_released - over_billed',
    identity_residual_paise: 0,
    identity_balanced: true,
    exceptions: [{ po_number: 'PO-2026-0008' }, { po_number: 'PO-2026-0009' }],
  },
};

const EXCEPTIONS = [
  {
    exception_id: 'EXC-0001',
    raised_at: '2026-09-05T11:02:00Z',
    object_type: 'grn',
    object_id: 'PR-99002',
    kind: 'UNATTRIBUTED_RECEIPT',
    detail: 'No purchase order matched this receive; it was not spread pro-rata.',
    local_paise: null,
    source_paise: 4500000,
    status: 'Open',
    owner_user_id: null,
    resolved_at: null,
    resolution: null,
  },
  {
    exception_id: 'EXC-0002',
    raised_at: '2026-09-04T06:05:00Z',
    object_type: 'bill',
    object_id: 'BILL-77001',
    kind: 'VALUE_MISMATCH',
    detail: 'Our value and the source value differ for the same bill.',
    local_paise: 88000000,
    source_paise: 91000000,
    status: 'Open',
    owner_user_id: 'U-FIN',
    resolved_at: null,
    resolution: null,
  },
  {
    exception_id: 'EXC-0003',
    raised_at: '2026-09-01T08:00:00Z',
    object_type: 'bill',
    object_id: 'BILL-70000',
    kind: 'UNMAPPED_EXTERNAL_STATUS',
    detail: 'Raw status "weird" has no mapping in C17.',
    local_paise: null,
    source_paise: null,
    status: 'Resolved',
    owner_user_id: 'U-ADM',
    resolved_at: '2026-09-02T09:00:00Z',
    resolution: 'C17 was extended and the record reprocessed.',
  },
];

/* ---------------- routing helpers ----------------
   Only the integration endpoints are intercepted. /api/auth/login,
   /api/bootstrap, /api/health and /api/dashboard reach the real server: the
   session, the permission set and the shell chrome are part of what is proven
   here and faking them would prove nothing. */

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

/**
 * Declare which `/api/integrations/*` paths this build "mounts".
 *
 * The screens read /openapi.json to tell an unmounted route from an empty
 * result, so the schema IS the switch that selects the available / unavailable
 * / compatibility behaviour under test. Stubbing it is stubbing the thing the
 * code actually reads, not a proxy for it.
 */
async function routeOpenApi(page, paths) {
  const schema = { openapi: '3.1.0', info: { title: 'stub', version: '0' }, paths: {} };
  for (const p of paths) schema.paths[p] = { get: {}, post: {}, put: {} };
  await routeJson(page, '**/openapi.json', schema);
}

const WAVE5_PATHS = [
  '/api/integrations/connections',
  '/api/integrations/connections/{connection_id}/authorize',
  '/api/integrations/connections/{connection_id}/organizations',
  '/api/integrations/connections/{connection_id}/organization',
  '/api/integrations/connections/{connection_id}/scopes',
  '/api/integrations/connections/{connection_id}/validate',
  '/api/integrations/connections/{connection_id}/health',
  '/api/integrations/events',
  '/api/integrations/control-totals',
  '/api/integrations/outbox',
  '/api/integrations/inbox',
  '/api/integrations/reconciliation',
  '/api/integrations/exceptions',
  '/api/integrations/dead-letters',
  '/api/integrations/dead-letters/{queue}/{row_id}/retry',
  '/api/integrations/dead-letters/{queue}/{row_id}/discard',
];

/** Every Wave 5 endpoint mounted and answering with the fixtures above. */
async function stubWave5(page, overrides = {}) {
  await routeOpenApi(page, overrides.paths || WAVE5_PATHS);
  await routeJson(page, '**/api/integrations/connections?**', overrides.connections || CONNECTIONS);
  await routeJson(page, '**/api/integrations/connections', overrides.connections || CONNECTIONS);
  await routeJson(page, '**/api/integrations/connections/*/organizations**', ORGANISATIONS);
  await routeJson(page, '**/api/integrations/connections/*/scopes**', SCOPES);
  await routeJson(page, '**/api/integrations/connections/*/health**', overrides.health || HEALTH);
  await routeJson(page, '**/api/integrations/connections/*/validate**', { mode: 'MOCK', results: [] });
  await routeJson(page, '**/api/integrations/events**', overrides.events || EVENTS);
  await routeJson(page, '**/api/integrations/control-totals**', overrides.totals || CONTROL_TOTALS);
  await routeJson(page, '**/api/integrations/outbox**', overrides.outbox || OUTBOX);
  // One inbox route, two modules: the screens pass ?module=, so the stub picks
  // the fixture from the query rather than needing two patterns that would
  // shadow each other.
  await page.route('**/api/integrations/inbox**', (route) => {
    const module = new URL(route.request().url()).searchParams.get('module');
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(module === 'bills'
        ? (overrides.inboxBills || INBOX_BILLS)
        : (overrides.inboxGrn || INBOX_GRN)),
    });
  });
  await routeJson(page, '**/api/integrations/reconciliation**', overrides.reconciliation || RECONCILIATION);
  await routeJson(page, '**/api/integrations/exceptions**', overrides.exceptions || EXCEPTIONS);
  await routeJson(page, '**/api/integrations/dead-letters?**', overrides.deadLetters || DEAD_LETTERS);
  await routeJson(page, '**/api/integrations/dead-letters', overrides.deadLetters || DEAD_LETTERS);
}

/**
 * The surfaces that really ARE mounted in this build, so a fallback has
 * something to fall back TO.
 *
 * Nothing is stubbed for any of them: the fallback under test hits the real
 * server, which is the point — a fallback proven against a stub proves only
 * that the stub answered.
 */
const REAL_PATHS = [
  '/api/health',
  '/api/zoho/connections',
  '/api/zoho/scopes',
  '/api/zoho/{connection_id}/test',
  '/api/zoho/{connection_id}/health',
  '/api/purchase-orders',
  '/api/grns',
  '/api/bills',
  '/api/reconciliation',
  '/api/reconciliation/exceptions',
];

/** No Wave 5 endpoint mounted at all — today's real state of this build. */
async function stubNothingMounted(page) {
  await routeOpenApi(page, REAL_PATHS);
}

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
  await page.waitForFunction(() => window.__integrationRoutesInstalled === 12, null, { timeout: 15_000 });
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
 * app.js appends the host node BEFORE it awaits mount() — a feature module
 * looks its roots up by id — so `.scr-host` appears while the screen is still
 * empty. `data-mounted` is the signal, and `data-mount-failed` ends the wait
 * too: a load failure diagnosed as a load failure is worth far more than the
 * same failure arriving as a fifteen-second timeout.
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
    return !!host && !host.querySelector('.audit-skel-row') && !host.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForLoadState('networkidle');
}

/**
 * Every `.status` chip under `#content`, read only once at least one exists.
 *
 * `page.evaluate` samples the DOM once. `settleScreen` waits for mount, for
 * loading nodes to clear and for networkidle -- none of which is "the table
 * has painted" -- so a bare sample can race a row that renders a frame later
 * on a slower runner. That is exactly how CI reported "received zero" for a
 * screen that had demonstrably rendered: the test above it asserts an UNMAPPED
 * chip, which IS a status chip, and passed in the same run.
 *
 * The first line is a retrying assertion, not a sleep. If chips genuinely
 * never render this still fails, and says so more usefully than a zero count.
 */
async function statusChips(page) {
  await expect(page.locator('#content .status').first()).toBeAttached();
  return page.evaluate(() => [...document.querySelectorAll('#content .status')]
    .map((el) => ({ text: el.textContent.trim(), sym: !!el.querySelector('.sym') })));
}

async function gotoScreen(page, hash) {
  await page.goto(`/#${hash}`);
  await page.waitForFunction(() => window.__integrationRoutesInstalled === 12, null, { timeout: 15_000 });
  await settleScreen(page);
}

function pageTitle(page) { return page.locator('#pageTitle'); }

/** Measure the nav rail from the running page. */
function measureRail(page) {
  return page.evaluate(() => {
    const nav = document.getElementById('nav');
    const cs = getComputedStyle(nav);
    if (cs.display === 'none') {
      return { hidden: true, rail: 0, content: 0, overflow: 0, rows: nav.querySelectorAll('.nav-item').length };
    }
    // A collapsed group's items panel stays in the DOM (for aria-controls)
    // behind `hidden`, and can be the last child overall (Stream F);
    // excluding it is what keeps "last child" meaning "last visible child".
    const kids = [...nav.children].filter((k) => !k.hidden);
    let content = 0;
    if (kids.length) {
      const first = kids[0].getBoundingClientRect().top;
      const last = kids[kids.length - 1].getBoundingClientRect().bottom;
      content = (last - first) + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    }
    return {
      hidden: false,
      rail: nav.clientHeight,
      content: Math.round(content),
      overflow: Math.round(content - nav.clientHeight),
      rows: nav.querySelectorAll('.nav-item').length,
    };
  });
}

/* ====================================================== routes, not rail === */

test.describe('Wave 5 integration screens — routes, and nothing in the rail', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
  });

  test('not one of the twelve appears in the primary navigation', async ({ page }) => {
    // The exact-sequence assertion lives in spa-routing.spec.js and is not
    // duplicated here. What IS asserted is the property this stream is
    // responsible for: none of its twelve ids reached the rail, for the
    // principal that holds every permission they are gated on. A gated entry
    // that reappeared would otherwise be invisible to a run as a principal who
    // cannot see it.
    await settleShell(page);
    // Fable 5.1 Stream F made every NAV group collapsible and defaults all but
    // Work/Project Control to closed, so DOM order among `.nav-item` elements
    // now depends on expand state. Opening the four groups that used to be
    // static `<h2>` headings (Procurement & Actuals, Closure, Integration,
    // Governance) restores exactly the pre-Stream-F rail contents this
    // assertion depends on -- ANALYTICS and INTEGRATION MAPPING are
    // deliberately left at their default (collapsed), because `fx-rates` was
    // only ever the LAST rendered item because those two groups render
    // nothing while closed.
    for (const g of ['Procurement & Actuals', 'Closure', 'Integration', 'Governance']) {
      await page.click(`[data-nav-group="${g}"]`);
    }
    const ids = await page.evaluate(
      () => [...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.nav),
    );
    for (const s of SCREENS) {
      expect(ids, `${s.scr || s.hash} is a route, not a rail entry`).not.toContain(s.hash);
    }
    expect(ids[0]).toBe('home');
    // Fable 5.1 (A4) listed `budget-categories` and `fx-rates` after
    // `settings`; the property this stream owns -- none of ITS twelve is on
    // the rail -- is asserted above and is unchanged.
    expect(ids[ids.length - 1]).toBe('fx-rates');
  });

  for (const viewport of [
    { name: 'desktop-1440', width: 1440, height: 900 },
    { name: 'laptop-1024', width: 1024, height: 768 },
  ]) {
    test(`the rail is unchanged at ${viewport.name}, measured`, async ({ page }) => {
      // MEASURED, not argued. The numbers are recorded in the run's report:
      // this test asserts only the INVARIANT — that registering twelve routes
      // did not change the rail's row count or its content height — because
      // pinning the absolute pixel figures here would turn an unrelated change
      // to an approved nav label into a failure of this stream's suite.
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await settleShell(page);
      const withRoutes = await measureRail(page);

      // The same measurement in a page where the routes were never installed.
      const bare = await page.evaluate(() => {
        const nav = document.getElementById('nav');
        return {
          rows: nav.querySelectorAll('.nav-item').length,
          ids: [...nav.querySelectorAll('.nav-item')].map((b) => b.dataset.nav),
        };
      });
      expect(withRoutes.rows).toBe(bare.rows);
      expect(bare.ids.filter((id) => id.startsWith('integration-'))).toEqual([]);

      // eslint-disable-next-line no-console
      console.log(`[rail] ${viewport.name}: rows=${withRoutes.rows} content=${withRoutes.content}px `
        + `rail=${withRoutes.rail}px overflow=${withRoutes.overflow}px`);
      // A row measures 30px, so twelve rows would be 360px. Recorded here so
      // the report can quote it rather than re-deriving it.
      expect(withRoutes.content + 360 - withRoutes.rail).toBeGreaterThan(0);
    });
  }

  test('the rail is display:none at tablet-800 and nothing scrolls sideways', async ({ page }) => {
    await page.setViewportSize({ width: 800, height: 1024 });
    await gotoScreen(page, 'integration-events');
    const m = await measureRail(page);
    expect(m.hidden).toBe(true);
    // eslint-disable-next-line no-console
    console.log('[rail] tablet-800: display:none, rail not rendered');
    const horizontal = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(horizontal, 'the shell scrolled horizontally').toBe(false);
  });

  for (const s of SCREENS) {
    test(`${s.scr || s.hash} — /#${s.hash} deep-links on a cold load and mounts`, async ({ page }) => {
      await gotoScreen(page, s.hash);
      await expect(pageTitle(page)).toHaveText(s.title);
      expect(new URL(page.url()).hash).toBe(`#${s.hash}`);
      await expect(page.locator('#content .scr-host[data-mounted="1"]')).toHaveCount(1);
      // The data-mounted trap: a screen marked mounted must actually have
      // rendered something, and must NOT also be marked failed.
      await expect(page.locator('#content .scr-host[data-mount-failed="1"]')).toHaveCount(0);
      await expect(page.locator('#content .integration-card')).not.toHaveCount(0);
    });
  }

  test('the app.js paste still matches the manifest', async ({ page }) => {
    // THE DRIFT GATE ON THE HANDOVER. app.js is a classic script and cannot
    // import the manifest, so its SCR_ROUTES rows are a PASTE. This asserts the
    // paste applied to the running page is still exactly what the manifest
    // tells the lead to paste — a manifest edit nobody carried across fails
    // here rather than shipping a route with the wrong gate.
    const manifest = await page.evaluate(async () => {
      const mod = await import('/static/src/features/integration/manifest.js');
      return {
        rows: mod.INTEGRATION_NAV_ROWS,
        screens: mod.INTEGRATION_SCREENS.map((s) => ({
          id: s.id, scr: s.scr, need: s.need, title: s.title, hasBuild: typeof s.build === 'function',
        })),
      };
    });
    const live = await page.evaluate(
      // eslint-disable-next-line no-undef
      () => SCR_ROUTES.filter((r) => r.id.startsWith('integration-'))
        .map((r) => ({ id: r.id, ico: r.ico, label: r.label, need: r.need })),
    );

    expect(manifest.rows).toEqual(PASTE_ROWS);
    expect(live).toEqual(PASTE_ROWS);
    expect(manifest.screens).toHaveLength(12);
    for (const screen of manifest.screens) {
      expect(screen.hasBuild, `${screen.id} has no build()`).toBe(true);
      const row = PASTE_ROWS.find((r) => r.id === screen.id);
      expect(row, `${screen.id} has no SCR_ROUTES row`).toBeTruthy();
      expect(row.need, `${screen.id}'s permission gate drifted between the two tables`)
        .toEqual(screen.need);
    }
  });

  test('nine screens carry a real C8 number and three carry null, not an invented one', async ({ page }) => {
    // C8_screens.json is frozen at forty. An outbound emission queue and the
    // acquisition status of a receive or a bill are not in it — SCR-15/16/17
    // are the FINANCIAL views of those documents and answer a different
    // question. Inventing SCR-41/42/43 would manufacture traceability the
    // registry does not provide.
    const byId = await page.evaluate(async () => {
      const mod = await import('/static/src/features/integration/manifest.js');
      return Object.fromEntries(mod.INTEGRATION_SCREENS.map((s) => [s.id, s.scr]));
    });
    const named = Object.values(byId).filter(Boolean).sort();
    expect(named).toEqual([
      'SCR-18', 'SCR-26', 'SCR-27', 'SCR-31', 'SCR-32', 'SCR-33', 'SCR-34', 'SCR-38', 'SCR-39',
    ]);
    expect(byId['integration-outbound-po']).toBeNull();
    expect(byId['integration-inbound-grn']).toBeNull();
    expect(byId['integration-inbound-bill']).toBeNull();
    // And nothing invented a number in the 41+ range, which is the specific
    // mistake this assertion exists to catch.
    for (const scr of named) {
      expect(Number(scr.slice(4))).toBeLessThanOrEqual(40);
    }
  });
});

/* ============================================================= permissions == */

test.describe('Wave 5 integration screens — permission gates', () => {
  test('an Auditor reaches the nine read surfaces and none of the three configuration ones', async ({ page }) => {
    // A POSITIVE CONTROL ON THE SAME ASSERTION. An Auditor who could reach
    // nothing would prove nothing about the gate — it would be
    // indistinguishable from a broken route table.
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page, AUDITOR);
    const verdicts = await page.evaluate((all) => Object.fromEntries(
      // eslint-disable-next-line no-undef
      all.map((hash) => [hash, viewAllowed(hash)]),
    ), SCREENS.map((s) => s.hash));

    for (const s of READ_SCREENS) {
      expect(verdicts[s.hash], `an Auditor must reach ${s.scr || s.hash}`).toBe(true);
    }
    for (const s of MANAGE_SCREENS) {
      expect(verdicts[s.hash], `${s.scr} needs connector.manage, which an Auditor does not hold`).toBe(false);
    }
    // budget.read is held by every role including Auditor.
    for (const s of LEDGER_SCREENS) {
      expect(verdicts[s.hash], `an Auditor holds budget.read and must reach ${s.scr}`).toBe(true);
    }

    // And the refusal is real, not just a boolean: the hash does not render.
    await page.goto('/#integration-setup');
    await settleShell(page);
    await expect(pageTitle(page)).not.toHaveText('Zoho ERP Connection Setup Wizard');
  });

  test('a Requestor reaches the two reconciliation screens and none of the ten connector ones', async ({ page }) => {
    // The SECOND positive control, and the one that pins the deliberate split:
    // SCR-18 and SCR-27 are gated on budget.read precisely so a finance role
    // is not locked out of a financial control by a connector permission.
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page, REQUESTOR);
    const verdicts = await page.evaluate((all) => Object.fromEntries(
      // eslint-disable-next-line no-undef
      all.map((hash) => [hash, viewAllowed(hash)]),
    ), SCREENS.map((s) => s.hash));

    for (const s of [...MANAGE_SCREENS, ...READ_SCREENS]) {
      expect(verdicts[s.hash], `${s.scr || s.hash} needs a connector permission a Requestor lacks`).toBe(false);
    }
    for (const s of LEDGER_SCREENS) {
      expect(verdicts[s.hash], `${s.scr} is gated on budget.read, which every role holds`).toBe(true);
    }
  });

  test('SCR-39 is gated on connector.read even though it offers a write', async ({ page }) => {
    // DELIBERATE, and worth pinning. An Auditor must be able to SEE what is
    // dead-lettered; the retry itself is refused by the server for a caller
    // without connector.manage. Gating the whole screen on connector.manage
    // would hide the queue from the role most likely to be asked about it.
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page, AUDITOR);
    await gotoScreen(page, 'integration-retry');
    await expect(pageTitle(page)).toHaveText('Failed Sync and Retry Queue');
  });
});

/* ================================================== REQ-INT-024, the secret = */

test.describe('REQ-INT-024 — the client secret never reaches the browser', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('SCR-32 renders no control that could accept a secret', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-oauth');

    const controls = await page.evaluate(() => [...document.querySelectorAll(
      '#content input, #content textarea, #content select, #content [contenteditable]',
    )].map((el) => ({
      tag: el.tagName,
      type: el.type || '',
      id: el.id || '',
      name: el.name || '',
      label: (document.querySelector(`label[for="${el.id}"]`) || {}).textContent || '',
    })));

    expect(controls.filter((c) => c.type === 'password'),
      'a password input on this screen is a secret input by another name').toHaveLength(0);
    for (const c of controls) {
      expect(/secret|credential|password/i.test(`${c.id} ${c.name} ${c.label}`),
        `control ${c.id || c.name || c.tag} could be mistaken for a secret field`).toBe(false);
    }
    // SCR-31 is the other place a secret would naturally be typed.
    await gotoScreen(page, 'integration-setup');
    const setupControls = await page.evaluate(() => [...document.querySelectorAll('#content input, #content textarea')]
      .map((el) => `${el.id} ${el.name} ${el.type}`));
    for (const descriptor of setupControls) {
      expect(/secret|password/i.test(descriptor), `${descriptor} accepts a secret`).toBe(false);
    }
  });

  test('the create-connection request body carries no secret-shaped key', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);

    const bodies = [];
    await page.route('**/api/integrations/connections', async (route) => {
      if (route.request().method() === 'POST') {
        bodies.push(route.request().postData());
        return route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ connection_id: 'CONN-NEW' }),
        });
      }
      return route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify(CONNECTIONS),
      });
    });

    await gotoScreen(page, 'integration-setup');
    await page.fill('#setupName', 'Test connector');
    await page.fill('#setupEntity', 'ENT-TEST');
    await page.fill('#setupClientId', '1000.PUBLICCLIENTID');
    await page.click('#content button[type="submit"]');
    await expect(page.locator('#setupFormStatus .msg-success')).toBeVisible();

    expect(bodies).toHaveLength(1);
    const parsed = JSON.parse(bodies[0]);
    // DEEP scan of the actual wire format, not a source read. A key added by a
    // future refactor is caught here even if nobody re-reads the module.
    const keys = [];
    (function walk(node) {
      if (Array.isArray(node)) return node.forEach(walk);
      if (!node || typeof node !== 'object') return undefined;
      for (const [k, v] of Object.entries(node)) { keys.push(k); walk(v); }
      return undefined;
    }(parsed));
    for (const key of keys) {
      expect(/secret|password|refresh_token|access_token/i.test(key),
        `the create request carried "${key}"`).toBe(false);
    }
    expect(keys).toContain('client_id');   // the public half IS sent
  });

  test('a server that returns a client secret cannot get its value into the DOM', async ({ page }) => {
    // THE TEST THAT ACTUALLY MATTERS. The two above prove this client does not
    // SEND a secret. This one proves that a backend which breaks REQ-INT-024
    // still cannot leak one through this UI — the property that survives
    // somebody else's regression.
    const LEAKED = 'SUPER_SECRET_VALUE_DO_NOT_RENDER_9f3a';
    await stubWave5(page, {
      connections: {
        items: [{
          ...CONNECTIONS.items[0],
          client_secret: LEAKED,
          tokens: { refresh_token: `RT_${LEAKED}`, access_token: `AT_${LEAKED}` },
        }],
      },
    });
    await signIn(page);
    await gotoScreen(page, 'integration-oauth');

    const html = await page.evaluate(() => document.documentElement.outerHTML);
    expect(html.includes(LEAKED), 'the leaked secret reached the DOM').toBe(false);

    // And the leak is REPORTED, not silently dropped. A silent drop would let
    // the regression ship: the screen would look identical either way.
    const warning = page.locator('.integration-redaction');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText('client_secret');
    await expect(warning).toContainText('REQ-INT-024');

    // The metadata that is NOT a secret survives, so the screen keeps its only
    // honest signal about whether authorisation can proceed.
    await expect(page.locator('#oauthDetail')).toContainText('Installed server-side');
  });

  test('scrub() removes secret-shaped keys and keeps the presence flag', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);
    const result = await page.evaluate(async () => {
      const mod = await import('/static/src/features/integration/integration-api.js');
      const out = mod.scrub({
        connections: [{
          connection_id: 'C1',
          client_id: 'public',
          client_secret: 'LEAK',
          client_secret_present: true,
          zoho_client_secret: 'LEAK2',
          tokens: { refresh_token: 'LEAK3', access_token: 'LEAK4' },
          access_token_expiry: '2026-09-06T12:00:00Z',
        }],
      });
      return { redacted: out.redacted, json: JSON.stringify(out.value) };
    });
    expect(result.redacted.sort()).toEqual([
      'connections[0].client_secret',
      'connections[0].tokens.access_token',
      'connections[0].tokens.refresh_token',
      'connections[0].zoho_client_secret',
    ]);
    expect(result.json).not.toContain('LEAK');
    // Fail-closed, with a short explicit allow-list: the presence flag and the
    // expiry are metadata ABOUT a secret and carry none, so they survive.
    expect(result.json).toContain('client_secret_present');
    expect(result.json).toContain('access_token_expiry');
    expect(result.json).toContain('public');
  });
});

/* ============================================ absent endpoint vs empty list = */

test.describe('An absent endpoint renders as absent, never as empty', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('SCR-39 with no dead-letter route says the route is missing, not that the queue is empty', async ({ page }) => {
    // THE DISTINCTION THIS WHOLE MECHANISM EXISTS FOR. A 404 from an unmounted
    // route would otherwise render through the shared state host as "no
    // records were found", and on a dead-letter queue that sentence tells an
    // operator to stop worrying about a queue nobody is reading.
    await stubNothingMounted(page);
    await signIn(page);
    await gotoScreen(page, 'integration-retry');

    const unavailable = page.locator('.integration-unavailable');
    await expect(unavailable).toBeVisible();
    await expect(unavailable).toContainText('/api/integrations/dead-letters');
    await expect(unavailable).toContainText('not because the queue is empty');
    // It is NOT the empty state and NOT the error state.
    await expect(page.locator('#retryStatus .empty')).toHaveCount(0);
    await expect(page.locator('#retryStatus .msg-error')).toHaveCount(0);
    // And it offers no Retry button, because retrying cannot succeed.
    await expect(page.locator('.integration-unavailable button')).toHaveCount(0);
  });

  test('an EMPTY successful response renders the empty state, which reads differently', async ({ page }) => {
    await stubWave5(page, { deadLetters: { items: [], next_cursor: null, has_more: false } });
    await signIn(page);
    await gotoScreen(page, 'integration-retry');
    await expect(page.locator('#retryStatus .empty')).toBeVisible();
    await expect(page.locator('#retryStatus')).toContainText('Nothing is dead-lettered');
    await expect(page.locator('.integration-unavailable')).toHaveCount(0);
  });

  test('a Wave 4 compatibility fallback names itself on screen', async ({ page }) => {
    // A silent fallback would let an operator read Wave 4 mock connector
    // output as Wave 5 platform state.
    await stubNothingMounted(page);
    await signIn(page);
    await gotoScreen(page, 'integration-setup');
    const source = page.locator('.integration-source[data-source="wave4-compat"]').first();
    await expect(source).toBeVisible();
    await expect(source).toContainText('/api/zoho/connections');
    await expect(source).toContainText('is NOT the integration platform');
  });

  test('a mounted Wave 5 route is named as the Wave 5 route', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-events');
    // SCOPED TO THE SYNC-HISTORY LOADER. SCR-26 carries two loaders — control
    // totals and the event log — and each renders its own source line. A
    // `.first()` here would silently assert about whichever card happens to be
    // higher up the page, which is exactly the class of test that passes for
    // the wrong reason.
    const source = page
      .locator('.integration-loader', { has: page.locator('#eventsStatus') })
      .locator('.integration-source[data-source="wave5"]');
    await expect(source).toBeVisible();
    await expect(source).toContainText('/api/integrations/events');
    await expect(page.locator('.integration-unavailable')).toHaveCount(0);
  });

  test('a real server error is still an error, with a retry', async ({ page }) => {
    await routeOpenApi(page, WAVE5_PATHS);
    await routeJson(page, '**/api/integrations/control-totals**', CONTROL_TOTALS);
    await routeJson(page, '**/api/integrations/events**', { detail: 'boom' }, 500);
    await signIn(page);
    await gotoScreen(page, 'integration-events');
    await expect(page.locator('#eventsStatus .msg-error')).toBeVisible();
    await expect(page.locator('#eventsStatus button')).toContainText('Retry');
    await expect(page.locator('.integration-unavailable')).toHaveCount(0);
  });
});

/* ============================================= the ledger fallback is named = */

test.describe('The ledger fallback answers a different question, and says so', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await stubNothingMounted(page);
    await signIn(page);
  });

  test('the outbound queue falls back to the PO ledger and marks every emission column unmeasured', async ({ page }) => {
    // THE MOST IMPORTANT HONESTY TEST ON THE THREE TRANSACTION SCREENS.
    // /api/purchase-orders answers "what have we ordered". It does NOT answer
    // "did it reach Zoho". Rendering the first as the second — with a dash, a
    // zero or a fabricated PENDING — is exactly the failure the whole source
    // mechanism exists to prevent.
    await gotoScreen(page, 'integration-outbound-po');

    const source = page.locator('.integration-source[data-source="ledger-compat"]').first();
    await expect(source).toBeVisible();
    await expect(source).toContainText('/api/purchase-orders');
    await expect(source).toContainText('says NOTHING about what was sent');

    const caveat = page.locator('#outboundCaveat .msg-warning');
    await expect(caveat).toBeVisible();
    await expect(caveat).toContainText('purchase orders, not emissions');

    // And the columns say it too, cell by cell. `notMeasured()` carries a
    // glyph and the words "not measured" — never a dash, which on a table
    // whose other rows carry values reads as "this one has none".
    const unmeasured = page.locator('#content .integration-unmeasured');
    expect(await unmeasured.count()).toBeGreaterThan(0);
    await expect(unmeasured.first()).toContainText('not measured');

    // NO OPERATIONAL STATUS CHIP WAS INVENTED FOR A LEDGER ROW.
    //
    // Written as a collected list rather than as `not.toContainText`, which
    // PASSES VACUOUSLY when the locator resolves to nothing and would
    // therefore have reported success on a screen that rendered no chips AND
    // on a screen that rendered a hundred wrong ones, indistinguishably.
    const chips = await page.evaluate(
      () => [...document.querySelectorAll('#content .status')].map((el) => el.textContent.trim()),
    );
    const invented = chips.filter((t) => /PENDING|SENT|DEAD|FAILED|QUEUED/.test(t));
    expect(invented, 'an emission status was invented for a row that came from the ledger')
      .toEqual([]);
  });

  test('the inbound GRN screen states OAS-03 whether or not the inbox is mounted', async ({ page }) => {
    // Purchase Receives have NO list endpoint. Acquisition is PO-anchored, so
    // a receive against a PO we never saw is invisible — not missing, not
    // late. That is a standing condition of the screen and it is stated as a
    // note, not as an error, because nothing has gone wrong.
    //
    // OAS-03 IS THE FINDING, and this assertion used to demand OAS-02 — the
    // Purchase REQUEST finding, a different module entirely. A citation is a
    // promise that following the id reaches the evidence, so the WRONG id is
    // asserted against too: a reader sent to OAS-02 lands on a statement about
    // Purchase Requests that says nothing about receives.
    await gotoScreen(page, 'integration-inbound-grn');
    const note = page.locator('#grnAnchorNote');
    await expect(note).toBeVisible();
    await expect(note).toContainText('OAS-03');
    await expect(note, 'the note cites the Purchase Request finding for a receives fact')
      .not.toContainText('OAS-02');
    await expect(note).toContainText('PO-anchored');
    await expect(note).toContainText('invisible');
    await expect(page.locator('#grnCaveat .msg-warning')).toContainText('not acquisitions');
  });

  test('the inbound bill screen marks its own status as ours, not Zoho’s raw one', async ({ page }) => {
    await gotoScreen(page, 'integration-inbound-bill');
    await expect(page.locator('#billCaveat .msg-warning')).toContainText('not acquisitions');
    // Our own status must NOT be presented as the raw external status Zoho
    // sent, which is a mapping decision that was never made here.
    await expect(page.locator('#content')).toContainText('OURS, NOT ZOHO');
  });

  test('SCR-18 reconciles from the ledger, which is where the reconciliation actually lives', async ({ page }) => {
    await gotoScreen(page, 'integration-reconciliation');
    const source = page.locator('.integration-source[data-source="ledger-compat"]').first();
    await expect(source).toBeVisible();
    await expect(source).toContainText('/api/reconciliation');
    // The summary tiles come from the SERVER's sums.
    await expect(page.locator('#reconTiles')).toContainText('Open commitment');
    await expect(page.locator('#reconTiles')).toContainText('Received, not billed');
  });

  test('SCR-27 reads the real exception table and offers no resolve control', async ({ page }) => {
    // Resolution is a write with an audit consequence and no endpoint exists.
    // A DISABLED button would read as a permission problem, which is a
    // different and false explanation.
    await gotoScreen(page, 'integration-exceptions');
    const note = page.locator('#exceptionResolveNote');
    await expect(note).toBeVisible();
    await expect(note).toContainText('not available in this build');
    await expect(page.locator('#content button[disabled]')).toHaveCount(0);
  });

  test('control totals have NO fallback and say why', async ({ page }) => {
    // THE ONE PLACE A FALLBACK WOULD BE ACTIVELY DANGEROUS. A control total
    // synthesised from our own ledger compares us with ourselves and always
    // balances — an assurance somebody would sign.
    await gotoScreen(page, 'integration-events');
    const unavailable = page.locator('#eventsTotalsStatus, .integration-unavailable');
    await expect(page.locator('.integration-unavailable').first()).toContainText(
      '/api/integrations/control-totals');
    await expect(unavailable.first()).toBeVisible();
    await expect(page.locator('#content')).toContainText('always balance');
    // And no figure was rendered for it.
    await expect(page.locator('#eventsControlTotals .tile')).toHaveCount(0);
  });
});

/* ====================================== what this build ACTUALLY serves ===== */

test.describe('Against the real build, with nothing stubbed', () => {
  /**
   * THE HONESTY STATEMENT, DERIVED RATHER THAN WRITTEN DOWN.
   *
   * The lead has to tell the client which of these screens work, which are
   * partial and which are placeholders. A sentence in a report goes stale the
   * moment a backend stream lands a route; this walks all twelve against the
   * REAL server — no page.route() at all, not even for /openapi.json, so the
   * screens read this deployment's own schema — and prints what each one
   * actually rendered.
   *
   * The assertion is the part that matters: every screen must have rendered
   * EITHER data with a named source OR an explicit "not available in this
   * build" naming the missing route. What none of them may render is the empty
   * state, because "no records were found" on a dead-letter queue or an
   * exception queue is the one sentence that tells an operator to stop
   * worrying about something nobody is reading.
   */
  for (const s of SCREENS) {
    test(`${s.scr || s.hash} renders data-with-a-source or unavailable, never a bare empty state`, async ({ page }) => {
      await installRoutes(page);
      await signIn(page);
      await gotoScreen(page, s.hash);

      // THE THIRD SITE IN THIS FILE WITH THE SAME UNSYNCHRONISED READ.
      //
      // `page.evaluate` samples the DOM exactly once. `gotoScreen` waits for
      // the mount flag, for loading nodes to clear and for networkidle --
      // none of which is "the data has painted". CI caught it at tablet-800
      // only, while desktop-1440 and laptop-1024 both passed in the same run:
      //
      //   desktop-1440  sources=[wave4-compat] unavailable=[...] empty=0
      //   laptop-1024   sources=[wave4-compat] unavailable=[...] empty=0
      //   tablet-800    sources=[none]         unavailable=[none] empty=1
      //
      // `empty=1` is the tell: the screen had rendered its intermediate empty
      // placeholder and the fetch had not yet resolved. Nothing about the
      // viewport changed what the screen renders -- it changed the timing.
      //
      // NOT A WAIT. No `waitForTimeout`, no sleep. The read is made to happen
      // AFTER the condition it depends on, using the same retrying assertion
      // every other check in this file already uses. If a screen genuinely
      // renders neither a source nor an unavailable block, this still fails --
      // with a clearer message than a zero count.
      await expect(
        page.locator('#content .integration-source, #content .integration-unavailable').first(),
      ).toBeAttached();

      const observed = await page.evaluate(() => {
        const root = document.getElementById('content');
        // VISIBLE only. A data table that has been hidden still holds its
        // rendered empty cell, and an assertion that counted it would fail a
        // screen whose reader can see nothing of the sort — the shared table
        // component clears its rows by rendering the empty state and then
        // hiding the whole table.
        const shown = (el) => el.offsetParent !== null;
        const sources = [...root.querySelectorAll('.integration-source')]
          .filter(shown).map((el) => el.dataset.source);
        const unavailable = [...root.querySelectorAll('.integration-unavailable code')]
          .filter(shown).map((el) => el.textContent.trim());
        return {
          sources,
          unavailable,
          empties: [...root.querySelectorAll('.empty')].filter(shown)
            .map((el) => el.textContent.trim().slice(0, 80)),
          errors: [...root.querySelectorAll('.msg-error')].filter(shown)
            .map((el) => el.textContent.trim().slice(0, 120)),
        };
      });

      // eslint-disable-next-line no-console
      console.log(`[build] ${s.scr || '(no C8 id)'} ${s.hash}: `
        + `sources=[${observed.sources.join(', ') || 'none'}] `
        + `unavailable=[${observed.unavailable.join(', ') || 'none'}] `
        + `empty=${observed.empties.length} error=${observed.errors.length}`);

      expect(observed.errors, `${s.hash} rendered a server error against the real build`).toEqual([]);
      expect(
        observed.sources.length > 0 || observed.unavailable.length > 0,
        `${s.hash} rendered neither a named source nor an explicit unavailable state — a reader `
        + 'cannot tell where anything on it came from',
      ).toBe(true);
      // A VISIBLE empty state is only honest when the same screen also tells
      // the reader where it looked — either a named source (so "nothing found"
      // is a real answer from a real endpoint) or an explicit unavailable
      // block (so it is not read as an answer at all). A visible "no records
      // were found" with neither is the exact sentence this whole mechanism
      // exists to prevent.
      for (const empty of observed.empties) {
        expect(
          observed.sources.length > 0 || observed.unavailable.length > 0,
          `${s.hash} rendered an empty state ("${empty}") with neither a source line nor an `
          + 'unavailable block, which is indistinguishable from an endpoint that does not exist',
        ).toBe(true);
      }
    });
  }
});

/* ================================================ the transaction queues ==== */

test.describe('The transaction queues, with the Wave 5 routes mounted', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
  });

  test('an outbox row with an external id is flagged ALREADY LANDED even though it is DEAD', async ({ page }) => {
    // Zoho documents no idempotency header; dedupe is the unique cf_capex_ref.
    // A DEAD row carrying an external id means the send LANDED and only the
    // recording failed, so a retry must update rather than create. An operator
    // who cannot see that is one click from a second purchase order.
    await gotoScreen(page, 'integration-outbound-po');
    const cell = page.locator('.integration-external').first();
    await expect(cell).toContainText('4600000123');
    await expect(cell).toContainText('ALREADY LANDED');
    // The row that never landed says so, in words, not by omission.
    await expect(page.locator('#content')).toContainText('none recorded');
    // Value is Indian-grouped integer paise through formatINR.
    await expect(page.locator('#content')).toContainText('₹25,00,000.00');
  });

  test('the outbound queue offers no retry control — that write lives on SCR-39', async ({ page }) => {
    // Duplicating a write that can emit a purchase order would double the
    // number of places that mistake can be made.
    await gotoScreen(page, 'integration-outbound-po');
    await expect(page.locator('#content button[data-retry-row]')).toHaveCount(0);
  });

  test('a receive with no anchoring purchase order is flagged UNANCHORED', async ({ page }) => {
    await gotoScreen(page, 'integration-inbound-grn');
    await expect(page.locator('#content')).toContainText('UNANCHORED');
    await expect(page.locator('#content')).toContainText('PO-2026-0008');
  });

  test('an unmapped bill status is shown verbatim and never replaced by a business status', async ({ page }) => {
    await gotoScreen(page, 'integration-inbound-bill');
    const raw = page.locator('.integration-raw').first();
    await expect(raw).toContainText('partially_billed_weird');
    await expect(raw).toContainText('UNMAPPED');
    await expect(page.locator('#content')).not.toContainText('PARTIALLY_ACTUALISED');
  });

  test('acquisition and accounting effect are two columns, not one', async ({ page }) => {
    // A bill can be acquired perfectly and still post nothing. Merging the two
    // would leave an operator chasing a missing actual unable to tell which
    // half failed.
    await gotoScreen(page, 'integration-inbound-bill');
    const headers = await page.evaluate(
      () => [...document.querySelectorAll('#content table th')].map((th) => th.textContent.trim()),
    );
    expect(headers).toContain('Inbox state');
    expect(headers).toContain('Moves the ledger');
  });

  test('control totals name both sides and say which window is out of balance', async ({ page }) => {
    await gotoScreen(page, 'integration-events');
    const tiles = page.locator('#eventsControlTotals');
    await expect(tiles).toContainText('in balance');
    await expect(tiles).toContainText('OUT OF BALANCE');
    // Both sides are named separately: a single signed "variance" would lose
    // which side is short, which is the first question anybody asks.
    await expect(tiles).toContainText('ours');
    await expect(tiles).toContainText('Zoho');
  });
});

/* ============================================== SCR-18, the arithmetic ====== */

test.describe('SCR-18 — open commitment is ordered less BILLED, never less received', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
  });

  test('the server’s figures are rendered unchanged, and nothing is recomputed here', async ({ page }) => {
    // A receipt does not release a commitment; a bill does. Line 1 is ordered
    // 25,00,000 / received 12,00,000 / billed 10,00,000, so open commitment is
    // 15,00,000 (ordered − billed) and NOT 13,00,000 (ordered − received).
    await gotoScreen(page, 'integration-reconciliation');
    const row = page.locator('#content table tbody tr').first();
    await expect(row).toContainText('₹15,00,000.00');
    await expect(row).not.toContainText('₹13,00,000.00');
    // Received-not-billed is its own quantity and is never added to it.
    await expect(row).toContainText('₹2,00,000.00');
  });

  test('the flags carry a word and a symbol, never colour alone', async ({ page }) => {
    // The `--warning` token fails WCAG AA 4.5:1 on every background in the
    // frozen stylesheet. That is a known open item this stream may not fix, so
    // nothing here may depend on the colour.
    await gotoScreen(page, 'integration-reconciliation');
    await expect(page.locator('#content')).toContainText('RECEIVED, NOT BILLED');
    await expect(page.locator('#content')).toContainText('OVER-BILLED');
    const chips = await statusChips(page);
    for (const chip of chips) {
      expect(chip.text.length, 'a status chip rendered with no text label').toBeGreaterThan(0);
      expect(chip.sym, `"${chip.text}" carries no non-colour symbol`).toBe(true);
    }
  });

  test('the screen says whether the figures actually reconciled, and does not assume it', async ({ page }) => {
    // The whole screen is a claim that ordered, received, billed and open tie
    // out. A claim nobody checks is a claim nobody can trust, so the server
    // states the identity it holds itself to and reports the residual in
    // paise, and this renders the answer.
    await gotoScreen(page, 'integration-reconciliation');
    const tiles = page.locator('#reconTiles');
    await expect(tiles).toContainText('Reconciles to the paisa');
    await expect(tiles).toContainText('BALANCED');
    // A word, not a colour: the `--warning` token fails WCAG AA on every
    // background in the frozen stylesheet, so nothing may depend on it.
    await expect(tiles).not.toContainText('DOES NOT BALANCE');
  });

  test('an unbalanced total is reported as unbalanced, never smoothed over', async ({ page }) => {
    // Money has either been counted twice or lost. An operator about to sign
    // the total off is entitled to know the parts did not add up, and the
    // amount by which.
    await stubWave5(page, {
      reconciliation: {
        ...RECONCILIATION,
        summary: {
          ...RECONCILIATION.summary,
          identity_residual_paise: 6000000,
          identity_balanced: false,
        },
      },
    });
    await gotoScreen(page, 'integration-reconciliation');
    const tiles = page.locator('#reconTiles');
    await expect(tiles).toContainText('DOES NOT BALANCE');
    await expect(tiles).toContainText('₹60,000.00');
    await expect(tiles).toContainText('must not be signed off');
  });

  test('whether a purchase order ever reached Zoho is stated, and a dash never stands in for no', async ({ page }) => {
    // THE ONLY REASON THE WAVE 5 ROUTE EXISTS. /api/reconciliation knows what
    // we ordered; it does not know whether the order was ever emitted. Three
    // distinct renderings, and collapsing any two would be the defect.
    await gotoScreen(page, 'integration-reconciliation');
    const rows = page.locator('#content table tbody tr');
    await expect(rows.nth(0)).toContainText('SENT');
    // No outbox row at all is a REAL answer, not a missing one.
    await expect(rows.nth(1)).toContainText('never enqueued');
  });

  test('a split purchase order whose emissions disagree shows the breakdown, not a winner', async ({ page }) => {
    // On a product whose custom fields are header-only, a multi-cell purchase
    // order is SPLIT — one emission per control cell — so its outbox rows can
    // legitimately disagree. Picking the worst state would hide a success and
    // picking the first would hide a failure; both are a summary that conceals
    // the one thing worth seeing.
    await stubWave5(page, {
      reconciliation: {
        ...RECONCILIATION,
        rows: [{
          ...RECONCILIATION.rows[0],
          emission_state: {
            rows: 3, split: true, states: { SENT: 2, DEAD: 1 }, state: null,
            external_id: null, external_ids: ['4600000123', '4600000124'], attempts: 8,
          },
        }],
      },
    });
    await gotoScreen(page, 'integration-reconciliation');
    const row = page.locator('#content table tbody tr').first();
    await expect(row).toContainText('2×SENT');
    await expect(row).toContainText('1×DEAD');
  });

  test('the ledger fallback says the emission column was not measured, never “no”', async ({ page }) => {
    // "We did not look" is not "it did not happen". The ledger holds no record
    // of what was emitted, and a dash there would read as a negative answer.
    await stubNothingMounted(page);
    await gotoScreen(page, 'integration-reconciliation');
    const source = page.locator('.integration-source[data-source="ledger-compat"]').first();
    await expect(source).toBeVisible();
    const unmeasured = page.locator('#content .integration-unmeasured');
    expect(await unmeasured.count()).toBeGreaterThan(0);
    await expect(unmeasured.first()).toContainText('not measured');
  });

  test('the control totals band is the server’s summary, not a sum of the visible rows', async ({ page }) => {
    // A total computed from a paginated page is silently the total of that
    // page — a number an operator would sign off.
    await gotoScreen(page, 'integration-reconciliation');
    const tiles = page.locator('#reconTiles');
    await expect(tiles).toContainText('₹15,00,000.00');   // summary.open_commitment_paise
    await expect(tiles).toContainText('₹19,50,000.00');   // summary.billed_paise
    await expect(tiles).toContainText('server-side count');
  });
});

/* ============================================== SCR-27, the exceptions ====== */

test.describe('SCR-27 — an unmatched document is never guessed at', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-exceptions');
  });

  test('both sides and the difference are three separate figures', async ({ page }) => {
    // Collapsing them into one "variance" loses which side is short, which is
    // the first question anyone resolving an exception asks.
    const headers = await page.evaluate(
      () => [...document.querySelectorAll('#content table th')].map((th) => th.textContent.trim()),
    );
    expect(headers).toContain('Our value');
    expect(headers).toContain('Source value');
    expect(headers).toContain('Difference');
    // 88,00,000 − 91,00,000 = (30,000.00) — negative in parentheses, because
    // colour is never the sole carrier of sign.
    await expect(page.locator('#content')).toContainText('(₹30,000.00)');
  });

  test('an exception with no paired values says so rather than showing a zero', async ({ page }) => {
    // An unmapped status is about a code, not an amount. A zero difference
    // there would read as "these agree".
    const unmeasured = page.locator('#content .integration-unmeasured');
    expect(await unmeasured.count()).toBeGreaterThan(0);
    await expect(unmeasured.first()).toContainText('not measured');
  });

  test('an open exception says that it blocks a period close', async ({ page }) => {
    // This queue is a gate on the accounting calendar, not a diagnostic list.
    // An operator who does not know that will not understand the refusal.
    await expect(page.locator('#exceptionTiles')).toContainText('cannot be closed');
    await expect(page.locator('#exceptionTiles')).toContainText('Open exceptions');
  });

  test('an unknown exception kind is shown verbatim, never filed under the nearest category', async ({ page }) => {
    const explained = await page.evaluate(
      () => [...document.querySelectorAll('#content table tbody tr')]
        .map((tr) => tr.textContent),
    );
    expect(explained.join(' ')).toContain('UNATTRIBUTED_RECEIPT');
    expect(explained.join(' ')).toContain('not spread pro-rata');
  });
});

/* ================================================= C16, the two registries == */

test.describe('C16 — integration statuses never become business statuses', () => {
  test.beforeEach(async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
  });

  test('the integration_state badge renders QUEUED / SENT / FAILED and refuses a C3 status', async ({ page }) => {
    await gotoScreen(page, 'integration-events');

    const verdict = await page.evaluate(async () => {
      const mod = await import('/static/src/features/integration/integration-kit.js');
      const ok = ['QUEUED', 'SENT', 'FAILED'].map((v) => mod.integrationStateBadge(v).textContent);
      const threw = {};
      for (const bad of ['APPROVED', 'INTEGRATION_FAILED', 'CLOSED', 'NONSENSE']) {
        try { mod.integrationStateBadge(bad); threw[bad] = null; } catch (e) { threw[bad] = e.message; }
      }
      return { ok, threw };
    });
    expect(verdict.ok.join(' ')).toContain('QUEUED');
    // A C3 business status must THROW rather than render an operational fact
    // where a business status belongs. That is C16's separation, enforced.
    expect(verdict.threw.APPROVED).toContain('C3 business status');
    expect(verdict.threw.INTEGRATION_FAILED).toContain('C3 business status');
    expect(verdict.threw.CLOSED).toContain('C3 business status');
    expect(verdict.threw.NONSENSE).toContain('not one of');
  });

  test('an unmapped raw external status is shown verbatim and labelled, never guessed', async ({ page }) => {
    await gotoScreen(page, 'integration-events');
    const raw = page.locator('.integration-raw').first();
    await expect(raw).toContainText('partially_billed_weird');
    await expect(raw).toContainText('UNMAPPED');
    // And it was NOT replaced with a plausible-looking business status.
    await expect(page.locator('#content')).not.toContainText('PARTIALLY_ACTUALISED');
  });

  test('operational statuses carry a text label and a symbol, not colour alone', async ({ page }) => {
    await gotoScreen(page, 'integration-events');
    const chips = await statusChips(page);
    expect(chips.length).toBeGreaterThan(0);
    for (const chip of chips) {
      expect(chip.text.length, 'a status chip rendered with no text label').toBeGreaterThan(0);
      expect(chip.sym, `"${chip.text}" carries no non-colour symbol`).toBe(true);
    }
  });
});

/* ======================================================== SCR-38 honesty ==== */

test.describe('SCR-38 — the daily ceiling leads, and nothing is claimed from silence', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('the daily budget is the first tile and the meter is measurable as text', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-health');
    const first = page.locator('#healthTiles .tile').first();
    await expect(first).toContainText('Daily call budget');
    // The bar is not the only signal: the figures and the percentage are TEXT,
    // and the meter carries an accessible value. A bar whose only signal is
    // its length is unreadable to a screen reader and unmeasurable in a
    // screenshot.
    await expect(first).toContainText('1800 of 2000 calls');
    await expect(first).toContainText('90.0%');
    await expect(first.locator('[role="meter"]')).toHaveAttribute('aria-valuenow', '90');
  });

  test('the meter width is set through the CSSOM, so no style attribute is emitted', async ({ page }) => {
    // The CSP is `style-src 'self'` with no 'unsafe-inline': a markup style
    // attribute is blocked outright, while CSSOM assignment is not governed by
    // the policy at all. The fill therefore HAS an inline width in the CSSOM
    // and the page must have raised no CSP violation.
    const violations = [];
    await page.addInitScript(() => {
      window.__csp = [];
      document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective));
    });
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-health');
    const width = await page.locator('.integration-meter-fill').first().evaluate((el) => el.style.width);
    expect(parseFloat(width)).toBeCloseTo(90, 1);
    expect(width.endsWith('%')).toBe(true);
    violations.push(...await page.evaluate(() => window.__csp || []));
    expect(violations.filter((v) => v.includes('style'))).toEqual([]);
  });

  test('a circuit opened by a quota is distinguished from one opened by a fault', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-health');
    const circuits = page.locator('#healthCircuits');
    await expect(circuits).toContainText('CIRCUIT_OPEN');
    await expect(circuits).toContainText('Daily quota exhausted');
    // A quota is not a fault. Counting it toward the breaker would open the
    // circuit for a module that is working perfectly and is simply out of
    // budget for the day.
    await expect(circuits).toContainText('does not count toward the breaker');
  });

  test('an unreported refresh token is "not reported", never "held"', async ({ page }) => {
    // Asserting "held" from an absent field is the one claim on this panel an
    // operator would act on. Silence must not become a claim.
    const health = { ...HEALTH };
    delete health.refresh_token_present;
    await stubWave5(page, { health });
    await signIn(page);
    await gotoScreen(page, 'integration-health');
    await expect(page.locator('#healthToken')).toContainText('Not reported');
    await expect(page.locator('#healthToken')).not.toContainText('Held server-side');
  });

  test('a module with no completeness sweep says so rather than showing a blank', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-health');
    await expect(page.locator('#content')).toContainText('Never swept');
    // And the overlap the poll re-reads is stated, because a high-water mark
    // alone cannot promise completeness and the overlap is what narrows the
    // window it can miss.
    await expect(page.locator('#content')).toContainText('300 s');
  });
});

/* ======================================================== SCR-39 retry ====== */

test.describe('SCR-39 — manual retry cannot duplicate a document', () => {
  test.beforeEach(async ({ page }) => { await installRoutes(page); });

  test('an outbox retry carries an Idempotency-Key and reuses it across a failed attempt', async ({ page }) => {
    // Zoho documents no idempotency header, so dedupe is synthesised through a
    // unique custom field. A key minted per HTTP call would make every retry a
    // fresh emission — the duplicate purchase order the contract exists to
    // prevent. First attempt fails, second succeeds; the key must be identical.
    const keys = [];
    let attempt = 0;
    await routeOpenApi(page, WAVE5_PATHS);
    await routeJson(page, '**/api/integrations/dead-letters?**', DEAD_LETTERS);
    await routeJson(page, '**/api/integrations/dead-letters', DEAD_LETTERS);
    await page.route('**/api/integrations/dead-letters/*/*/retry', (route) => {
      keys.push(route.request().headers()['idempotency-key']);
      attempt += 1;
      return attempt === 1
        ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'upstream' }) })
        : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    });
    await signIn(page);
    page.on('dialog', (d) => d.accept());
    await gotoScreen(page, 'integration-retry');

    const retry = page.locator('button[data-retry-row="outbox:OUT-77"]');
    await retry.click();
    await expect(page.locator('#retryActionStatus .msg-error')).toBeVisible();
    await retry.click();
    await expect(page.locator('#retryActionStatus .msg-success')).toBeVisible();

    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[1], 'a fresh key on retry makes every retry a fresh emission').toBe(keys[0]);
  });

  test('a QUARANTINED row offers no retry button at all', async ({ page }) => {
    // Re-running the same failed attribution against the same unchanged data
    // cannot succeed. Offering the action would suggest otherwise.
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-retry');
    await expect(page.locator('button[data-retry-row="outbox:OUT-77"]')).toBeVisible();
    await expect(page.locator('button[data-retry-row="inbox:IN-42"]')).toHaveCount(0);
    await expect(page.locator('#content')).toContainText('Not retryable here');
  });

  test('a QUARANTINED inbox row can be DISCARDED even though it cannot be retried', async ({ page }) => {
    // Two different verbs, and the screen must not merge them. Re-running the
    // same failed attribution against the same unchanged data cannot succeed,
    // so there is no Retry. Ending the payload's life CAN succeed, and without
    // it the queue fills with rows nobody can act on until the real failures
    // are invisible among them.
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-retry');
    await expect(page.locator('button[data-retry-row="inbox:IN-42"]')).toHaveCount(0);
    await expect(page.locator('button[data-discard-row="inbox:IN-42"]')).toBeVisible();
  });

  test('no discard is offered on an outbox row, because C16 gives the outbox no such state', async ({ page }) => {
    // The backend refuses an outbox discard with a coded 409. A control that
    // is always refused reads as a permission problem, which is a different
    // and false explanation, so it is not rendered at all — and NOT rendered
    // disabled, which says "you may not" rather than "there is no such
    // operation".
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-retry');
    await expect(page.locator('button[data-discard-row="outbox:OUT-77"]')).toHaveCount(0);
    // Absent, and specifically NOT present-and-disabled: disabled says "you
    // may not", and the truth is "there is no such operation".
    await expect(page.locator('button[data-discard-row][disabled]')).toHaveCount(0);
    await expect(page.locator('#content')).toContainText('There is no discard for an outbox document');
  });

  test('a discard with no reason sends nothing at all', async ({ page }) => {
    // Discarding ends an inbound document's life without applying it. One
    // recorded with no explanation cannot be told apart from one done by
    // accident, so the screen refuses before calling rather than letting the
    // server's BLANK_DISCARD_REASON be the prompt.
    let called = 0;
    await routeOpenApi(page, WAVE5_PATHS);
    await routeJson(page, '**/api/integrations/dead-letters?**', DEAD_LETTERS);
    await routeJson(page, '**/api/integrations/dead-letters', DEAD_LETTERS);
    await page.route('**/api/integrations/dead-letters/*/*/discard', (route) => {
      called += 1;
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });
    await signIn(page);
    page.on('dialog', (d) => d.dismiss());
    await gotoScreen(page, 'integration-retry');

    await page.locator('button[data-discard-row="inbox:IN-42"]').click();
    await expect(page.locator('#retryActionStatus')).toContainText('cancelled');
    expect(called, 'a cancelled prompt still issued the discard').toBe(0);
  });

  test('a discard carries the reason and an Idempotency-Key distinct from any retry key', async ({ page }) => {
    // Retry and discard are opposite decisions about the same row. Sharing one
    // key would let a transport retry of a discard be deduplicated against an
    // earlier retry of the same row, or the reverse.
    const seen = [];
    await routeOpenApi(page, WAVE5_PATHS);
    await routeJson(page, '**/api/integrations/dead-letters?**', DEAD_LETTERS);
    await routeJson(page, '**/api/integrations/dead-letters', DEAD_LETTERS);
    await page.route('**/api/integrations/dead-letters/*/*/discard', (route) => {
      seen.push({
        key: route.request().headers()['idempotency-key'],
        body: route.request().postDataJSON(),
      });
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });
    await signIn(page);
    page.on('dialog', (d) => d.accept('the purchase order was cancelled in the tenant'));
    await gotoScreen(page, 'integration-retry');

    await page.locator('button[data-discard-row="inbox:IN-42"]').click();
    await expect(page.locator('#retryActionStatus .msg-success')).toBeVisible();
    expect(seen).toHaveLength(1);
    expect(seen[0].key, 'a discard with no idempotency key').toBeTruthy();
    expect(seen[0].key.startsWith('discard-'),
      'the discard key is not distinguishable from a retry key').toBe(true);
    expect(seen[0].body.reason).toBe('the purchase order was cancelled in the tenant');
  });

  test('an outbox row that already has an external id is flagged as already landed', async ({ page }) => {
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-retry');
    const cell = page.locator('.integration-external').first();
    await expect(cell).toContainText('4600000123');
    await expect(cell).toContainText('ALREADY LANDED');
  });
});

/* ============================================================ mode honesty == */

test.describe('Mode indicators — a screen showing mock data says so', () => {
  /* The two reconciliation screens are deliberately excluded: they read this
     application's own ledger, which has no connector mode at all, and stamping
     a MOCK connector badge on a purely local financial reconciliation would be
     its own small dishonesty. */
  for (const s of [...MANAGE_SCREENS, ...READ_SCREENS]) {
    test(`${s.scr || s.hash} carries a mode banner naming the mode`, async ({ page }) => {
      await installRoutes(page);
      await stubWave5(page);
      await signIn(page);
      await gotoScreen(page, s.hash);
      const banner = page.locator('#content .msg').first();
      await expect(banner).toContainText('Connector mode: MOCK');
      await expect(banner).toContainText('NOT VERIFIED');
      await expect(page.locator('#content')).toContainText('MODE=MOCK');
    });
  }

  test('the mode badge reuses the approved .mock-chip and names all four modes honestly', async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-events');
    const labels = await page.evaluate(async () => {
      const mod = await import('/static/src/features/integration/integration-kit.js');
      return ['MOCK', 'SANDBOX', 'LIVE_READ', 'LIVE_WRITE', 'GIBBERISH'].map((m) => {
        const el = mod.modeBadge(m);
        return { text: el.textContent, cls: el.className };
      });
    });
    // §11.9: mockBadge() BECOMES a real mode indicator; it is extended, not
    // deleted, and it keeps the client-approved .mock-chip appearance.
    for (const l of labels) expect(l.cls).toBe('mock-chip');
    expect(labels[0].text).toBe('MOCK · NOT VERIFIED');
    expect(labels[1].text).toBe('SANDBOX · NOT VERIFIED');
    // Only the two live modes may drop the suffix, and reaching either needs
    // an authorisation Phase 0B has not granted.
    expect(labels[2].text).toBe('LIVE_READ');
    expect(labels[3].text).toBe('LIVE_WRITE');
    // Unknown reads as unverified: the safe direction to be wrong in.
    expect(labels[4].text).toBe('MODE UNKNOWN · NOT VERIFIED');
  });
});

/* ======================================================== stylesheet hygiene = */

test.describe('integration.css cannot reach the approved design', () => {
  test('it is served, declares no raw colour and no token, and every selector is scoped', async ({ page }) => {
    // THE SAME GATE approvals.css CARRIES, FOR THE SAME REASON — and the
    // reason is a defect that actually shipped. settings.css contains two
    // UNSCOPED selectors (`input:disabled, textarea:disabled` and `.field
    // label`) which restyle disabled controls and label wrapping on every
    // client-approved shell view the moment that stylesheet loads globally.
    // That is why router.js injects these stylesheets per route, and this test
    // is why integration.css would be harmless even if that containment
    // failed.
    //
    // styles.css is byte-frozen behind a SHA-256 pin and this stream may not
    // edit it, so a new colour must be a token change in C6 — never a hex in a
    // feature stylesheet.
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
    const res = await page.request.get('/static/src/features/integration/integration.css');
    expect(res.ok(), 'integration.css is not being served from the feature directory').toBe(true);
    const css = (await res.text()).replace(/\/\*[\s\S]*?\*\//g, '');

    expect(css.match(/#[0-9A-Fa-f]{3,8}\b/g) || [],
      'integration.css declares a raw colour instead of a C6 token').toEqual([]);
    expect(css.includes(':root'),
      'integration.css declares a custom property; tokens belong in styles.css').toBe(false);

    // Every selector must be anchored on an `integration-` class or a media
    // rule. A bare element or utility selector here would leak into the
    // seventeen approved shell views.
    const unscoped = [];
    for (const block of css.split('}')) {
      const head = block.split('{')[0];
      if (!head || !block.includes('{')) continue;
      for (const sel of head.split(',')) {
        const s = sel.trim();
        if (!s || s.startsWith('@') || s.startsWith('/')) continue;
        if (!/(^|[\s>+~])\.integration-/.test(s)) unscoped.push(s);
      }
    }
    expect(unscoped, `integration.css has selectors not scoped to a feature class:\n${unscoped.join('\n')}`)
      .toEqual([]);

    // And every var() it names must be a token the frozen stylesheet actually
    // declares. A typo'd token silently resolves to nothing, which renders as
    // an unstyled element rather than as an error.
    const frozen = await page.request.get('/static/styles.css');
    const rootBlock = (await frozen.text()).match(/:root\s*\{([\s\S]*?)\}/);
    const declared = new Set((rootBlock ? rootBlock[1].match(/--[a-z0-9-]+(?=\s*:)/g) : []) || []);
    const used = [...new Set(css.match(/var\((--[a-z0-9-]+)/g) || [])]
      .map((m) => m.replace('var(', ''));
    expect(used.length, 'integration.css names no token at all — check the regex, not the file')
      .toBeGreaterThan(0);
    for (const token of used) {
      expect([...declared], `integration.css uses ${token}, which styles.css does not declare`)
        .toContain(token);
    }
  });
});

/* ============================================================ accessibility = */

test.describe('Wave 5 integration screens — accessibility', () => {
  for (const s of SCREENS) {
    test(`axe-core: ${s.scr || s.hash} has no violations inside the shell`, async ({ page }) => {
      await installRoutes(page);
      await stubWave5(page);
      await signIn(page);
      await gotoScreen(page, s.hash);
      const results = await new AxeBuilder({ page }).include('#content').analyze();

      // STRUCTURAL RULES: zero, with no allowance of any kind. Heading order,
      // form labels, list semantics, ARIA validity, name-role-value — every
      // one of these is inside this stream's control and every one must be
      // clean.
      const structural = results.violations
        .filter((v) => v.id !== 'color-contrast')
        .flatMap((v) => v.nodes.map((n) => `${v.id} :: ${n.target.join(' ')}`));
      expect(structural).toEqual([]);

      // COLOUR CONTRAST: pre-existing defects in the byte-frozen,
      // client-approved styles.css, which these screens are the first to put
      // under axe at scale. None is introduced here and none is fixable here —
      // styles.css is SHA-256 pinned in CI and this stream may not edit it —
      // so the exact COLOURS are pinned, exactly as tests/test_contracts.py
      // pins KNOWN_OFF_TOKEN_HEXES for the same reason.
      //
      //   #a66a00 — the `--warning` token itself (C6-frozen), used by
      //             `.status.st-warning` at 12px: 4.48:1 on white, failing by
      //             0.02;
      //   #917139 — that same token at `opacity:.75` inside `.msg .mid`:
      //             4.12:1 on `--warning-bg`;
      //   #6b7280 — the `--n500` token behind `.muted`, under 4.5:1 at the
      //             small sizes the frozen stylesheet uses it at.
      //
      // Pinning the colours rather than the RULE is what keeps this a gate: a
      // contrast failure with any other colour — one introduced by this
      // stream's own stylesheet, for instance — still fails here. REPORTED to
      // the lead; the fix is a C6 token change plus a styles.css re-pin, which
      // is a design decision and not this stream's to take.
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

      const FROZEN_FOREGROUNDS = ['#a66a00', '#917139', '#6b7280'];
      const FROZEN_BACKGROUNDS = [
        '#ffffff',   // --n0
        '#f7f8f9',   // --n50
        '#eff1f3',   // --n100
        '#eaf4f6',   // --primary-50, the tbody tr:hover tint
        '#fdf3e2',   // --warning-bg
      ];
      for (const c of contrast) {
        expect(FROZEN_FOREGROUNDS,
          `${c.target} fails contrast at ${c.ratio}:1 with foreground ${c.fg}, which is NOT one of `
          + 'the known frozen-stylesheet colours — this is a new defect')
          .toContain(c.fg);
        expect(FROZEN_BACKGROUNDS,
          `${c.target} fails contrast at ${c.ratio}:1 over background ${c.bg}, which is not a frozen `
          + ':root token — this stream introduced a background it should not have')
          .toContain(c.bg);
      }
    });
  }

  test('every control is reachable by Tab alone, with a visible focus ring', async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-events');
    // Tag every control with a unique marker FIRST. Keying by tag/id/class
    // silently merges two identical buttons — the toolbar's and the
    // pagination's, for instance — and then reports 7 of 8 reached when in
    // fact all 8 were.
    const controls = await page.evaluate(() => {
      const els = [...document.getElementById('content').querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled])',
      )]
        // A control that is not rendered is not in the tab order, and must not
        // be counted as unreachable. The pagination's "Load more" button is
        // `hidden` while there is no further page — correctly, since offering
        // it would promise a page that does not exist.
        .filter((el) => el.offsetParent !== null);
      els.forEach((el, i) => { el.dataset.tabProbe = String(i); });
      return els.length;
    });
    expect(controls).toBeGreaterThan(0);

    // Start the traversal from the top of the document, then walk far enough
    // to clear the shell chrome and the nav rail before reaching #content.
    // Focusing a control inside #content directly would skip exactly the part
    // of the path this test exists to check.
    await page.evaluate(() => { document.body.focus(); });
    const reached = new Set();
    for (let i = 0; i < controls + 60; i += 1) {
      await page.keyboard.press('Tab');
      const info = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el || !document.getElementById('content').contains(el)) return null;
        const cs = getComputedStyle(el);
        return {
          probe: el.dataset.tabProbe,
          key: `${el.tagName}#${el.id}.${el.className}`,
          outline: cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0,
          shadow: cs.boxShadow !== 'none',
        };
      });
      if (!info) continue;
      expect(info.outline || info.shadow, `${info.key} has no visible focus indicator`).toBe(true);
      if (info.probe !== undefined) reached.add(info.probe);
    }
    expect(reached.size, 'a control inside the screen is unreachable by Tab').toBe(controls);
  });

  test('the live region exists once and is announced to', async ({ page }) => {
    await installRoutes(page);
    await stubWave5(page);
    await signIn(page);
    await gotoScreen(page, 'integration-events');
    const regions = page.locator('#integrationLiveRegion');
    await expect(regions).toHaveCount(1);
    await expect(regions).toHaveAttribute('aria-live', 'polite');
    await expect(regions).not.toBeEmpty();
  });
});
