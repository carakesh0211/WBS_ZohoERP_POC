/* CAPEX & WBS Control Hub — POC front end.
   Dense, desktop-first enterprise UI. Terminology, statuses, tokens and message IDs
   all come from the frozen contract registries.

   Post-hardening front-end corrections (2026-08-06):
     * Every /api call is authenticated with a server-issued session (X-Session).
       The session id lives in sessionStorage so it dies with the browser tab; it is
       never written to localStorage.
     * Actions the signed-in user does not hold a permission for are not rendered.
       The server is still the authority; hiding them is courtesy, not security.
     * Money leaves the browser as a decimal STRING. No monetary value is ever put
       through a JavaScript Number.
     * Errors are read from detail.message. An object is never printed at a user.
     * AUD-M-003: real <button> elements, focus management, labels, table semantics.
     * AUD-M-004: the document does not scroll horizontally; wide tables scroll
       inside their own container.
*/

const S = {
  boot: null, me: null, perms: new Set(),
  view: 'home', project: 'PRJ-01', entity: '', plant: '',
  expanded: new Set(), data: {}, flash: null, zohoMode: null,
};

const SESSION_KEY = 'capex.session_id';

/* ---------------- session (sessionStorage only — never localStorage) ------- */
function getSession() { try { return sessionStorage.getItem(SESSION_KEY) || ''; } catch { return ''; } }
function setSession(id) {
  try { id ? sessionStorage.setItem(SESSION_KEY, id) : sessionStorage.removeItem(SESSION_KEY); }
  catch { /* storage unavailable: the session simply will not survive a reload */ }
}

/* ---------------- formatting (C6: Indian grouping, tabular numerals) ------- */
function inr(paise, opts = {}) {
  if (paise === null || paise === undefined) return '—';
  const neg = paise < 0;
  const r = Math.round(Math.abs(paise) / 100);
  let s = String(r);
  if (s.length > 3) {
    const last3 = s.slice(-3);
    let rest = s.slice(0, -3), parts = [];
    while (rest.length > 2) { parts.unshift(rest.slice(-2)); rest = rest.slice(0, -2); }
    if (rest) parts.unshift(rest);
    s = parts.join(',') + ',' + last3;
  }
  const txt = (opts.noSymbol ? '' : '₹') + s;
  return neg ? `(${txt})` : txt;
}
function inrShort(paise) {
  const r = Math.abs(paise) / 100, neg = paise < 0;
  let t;
  if (r >= 1e7) t = '₹' + (r / 1e7).toFixed(2) + ' Cr';
  else if (r >= 1e5) t = '₹' + (r / 1e5).toFixed(1) + 'L';
  else t = inr(paise);
  return neg ? `(${t})` : t;
}
function money(paise) {
  const cls = paise < 0 ? ' class="num neg"' : ' class="num"';
  return `<td${cls}>${inr(paise)}</td>`;
}
function pct(v) { return (v ?? 0).toFixed(1) + '%'; }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

/* Money leaves the browser as an exact decimal string. The backend rejects floats
   that are not exact 2dp values, and it is right to: rounding a float here is how
   money goes missing. */
function rupees(value, field = 'Amount') {
  const raw = String(value ?? '').trim().replace(/[,\s₹]/g, '');
  if (!raw) throw new Error(`${field} is required.`);
  if (!/^-?\d+(\.\d{1,2})?$/.test(raw)) {
    throw new Error(`${field} must be a rupee value with at most two decimal places, for example 2500000.50.`);
  }
  const neg = raw.startsWith('-');
  const [whole, frac = ''] = raw.replace('-', '').split('.');
  return (neg ? '-' : '') + whole + '.' + (frac + '00').slice(0, 2);
}

/* ---------------- status model (C3: 21 statuses, 5 semantic roles) --------- */
const ROLE = {
  'Draft': 'neutral', 'Submitted': 'progress', 'Under Review': 'progress', 'Approved': 'positive',
  'Rejected': 'negative', 'Returned': 'warning', 'Released': 'positive',
  'Partially Committed': 'progress', 'Fully Committed': 'progress',
  'Partially Actualised': 'progress', 'Fully Actualised': 'positive',
  'Budget Exceeded': 'negative', 'Exception Pending': 'warning',
  'Technically Completed': 'progress', 'Financially Completed': 'positive',
  'Awaiting Capitalisation': 'warning', 'Capitalised': 'positive', 'Closed': 'neutral',
  'Reopened': 'warning', 'Integration Failed': 'negative', 'Reconciliation Pending': 'warning',
  'Cancelled': 'neutral', 'PASS': 'positive', 'FAIL': 'negative', 'PARTIAL': 'warning',
  'NOT RUN': 'neutral', 'NOT AVAILABLE': 'negative', 'Connected': 'positive',
  'Not Connected': 'neutral', 'Active': 'positive', 'Disabled': 'neutral',
};
const SYM = { positive: '✔', negative: '✖', warning: '!', progress: '◐', neutral: '·' };
function status(s) {
  const role = ROLE[s] || 'neutral';
  return `<span class="status st-${role}"><span class="sym" aria-hidden="true">${SYM[role]}</span>${esc(s)}</span>`;
}
function band(b) {
  return { safe: 'st-positive', watch: 'st-warning', critical: 'st-warning', breach: 'st-negative' }[b] || '';
}

/* ---------------- exposure bar -------------------------------------------- */
function bar(n) {
  if (!n.budget) return '<span class="muted">no budget</span>';
  const c = Math.max(0, Math.min(100, n.commitment / n.budget * 100));
  const a = Math.max(0, Math.min(100, n.actual / n.budget * 100));
  const breach = n.exposure > n.budget ? ' breach' : '';
  const label = `Actual ${inr(n.actual)} · Commitment ${inr(n.commitment)} of ${inr(n.budget)}`;
  return `<div class="bar${breach}" role="img" aria-label="${esc(label)}" title="${esc(label)}">
    <i class="commit" data-left="${a}" data-width="${c}"></i><i class="actual" data-left="0" data-width="${a}"></i></div>`;
}

/* ---------------- api ------------------------------------------------------ */
class ApiError extends Error {
  constructor(message, { code = null, statusCode = 0, body = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code; this.statusCode = statusCode; this.body = body;
  }
}

/* The contract now returns {"detail":{"code","message"}}. Render detail.message and
   nothing else — printing the object is how "[object Object]" reached a user. */
function readError(payload, httpStatus) {
  const d = payload && payload.detail !== undefined ? payload.detail : payload;
  if (typeof d === 'string' && d.trim()) return { message: d, code: null };
  if (Array.isArray(d)) {                       // FastAPI request-validation shape
    const parts = d.map(e => {
      const where = Array.isArray(e.loc) ? e.loc.filter(x => x !== 'body').join('.') : '';
      return (where ? where + ': ' : '') + (e.msg || 'is invalid');
    }).filter(Boolean);
    return { message: parts.join('; ') || `The request was rejected (HTTP ${httpStatus}).`, code: 'VALIDATION_ERROR' };
  }
  if (d && typeof d === 'object') {
    return { message: d.message || d.detail || `The request was rejected (HTTP ${httpStatus}).`,
             code: d.code || null };
  }
  return { message: `The request was rejected (HTTP ${httpStatus}).`, code: null };
}

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const sid = getSession();
  if (sid) headers['X-Session'] = sid;

  let r;
  try {
    r = await fetch('/api' + path, {
      method: opts.method || 'GET',
      headers,
      body: opts.json !== undefined ? JSON.stringify(opts.json) : undefined,
    });
  } catch {
    throw new ApiError('The application could not be reached. Check that the server is running.',
                       { code: 'NETWORK' });
  }

  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch { data = txt || null; }

  if (!r.ok) {
    const { message, code } = readError(data, r.status);
    const err = new ApiError(message, { code, statusCode: r.status, body: data });
    if (r.status === 401) onUnauthorised(message);
    throw err;
  }
  return data;
}

/* ---------------- permissions --------------------------------------------- */
function can(...permissions) { return permissions.some(p => S.perms.has(p)); }

/* ---------------- module-hosted routes -----------------------------------
   The thirteen screens that live as ES modules rather than as `V` views in
   this file: the five built in Waves 2 and 3 (SCR-28, SCR-09, SCR-10, SCR-13,
   SCR-30) and the approval engine's eight, added in Wave 4. All thirteen are
   routable, deep-linkable, permission-gated, and LISTED IN THE PRIMARY
   NAVIGATION — the five since the product owner's written approval of
   2026-09-03, the eight as part of M4b.

   They were previously routable but unlisted, because the navigation rail is
   rendered inside every one of the client-approved screenshots and adding an
   entry changes all of them. That is a client design decision, and it has now
   been made: APPROVED UI CHANGE 1 of 2. The affected visual-regression
   baselines were re-captured deliberately, in the commit that made this
   change, and only the baselines that genuinely moved were re-captured — the
   rail is display:none below 900px, so the tablet-800 baselines did not move
   and were not touched.

   These rows are declared BEFORE the NAV table and are spliced into it BY
   REFERENCE below. That is the point: the navigation gate and the route gate
   are then the SAME OBJECT, so a permission loosened for the rail cannot
   diverge from the permission enforced on the route. The previous arrangement
   restated `need` in two places and relied on a test to notice when the two
   restatements disagreed.

   `id` deliberately does NOT collide with any existing shell view. `audit`,
   `budget` and `check` are separate approved screens; repointing those hashes
   would silently replace three approved screens with different ones.

   src/core/router.js carries the matching title, breadcrumb, host DOM and
   mount function for each id. The two declarations are held in step by
   tests/vrt/spa-routing.spec.js. */
const SCR_ROUTES = [
  { id: 'audit-trail', ico: '⧉', label: 'Audit Trail Viewer', need: ['audit.read'] },
  { id: 'budget-grid', ico: '▩', label: 'Budget Planning Grid (cells)', need: ['budget.read'] },
  { id: 'budget-compare', ico: '⇎', label: 'Budget Version Comparison', need: ['budget.read'] },
  { id: 'budget-availability', ico: '⊙', label: 'Budget Availability Check (cells)', need: ['budget.check'] },
  { id: 'settings', ico: '⚙', label: 'Settings & Master Data', need: ['settings.read', 'masters.read'] },

  /* Wave 4 / M4b — the approval engine's eight screens, on the same footing.

     Contract 4's permissions. approval.read is held by every role, so gating a
     configuration surface on it would be gating on nothing: the matrix, the
     version history and the simulator take approval.configure (Administrator),
     and delegation management takes approval.delegate.

     THESE ARE PRESENTATIONAL GATES. The server decides. A row hidden here is a
     courtesy — the same courtesy the rest of this shell extends — and the
     route gate below refuses the hash for the same reason, but neither is the
     enforcement point. `viewAllowed()` refusing a hash keeps a mistyped or
     bookmarked URL from rendering a screen whose data the server would refuse
     anyway; it is not what keeps the data safe. */
  { id: 'approval-inbox', ico: '⊞', label: 'My Approval Inbox', need: ['approval.read'] },
  { id: 'approval-request', ico: '▥', label: 'Approval Request Detail', need: ['approval.read'] },
  { id: 'approval-sla', ico: '◷', label: 'Escalation & SLA Monitor', need: ['approval.read'] },
  { id: 'approval-timeline', ico: '⧗', label: 'Approval Timeline', need: ['approval.read'] },
  { id: 'approval-matrix', ico: '▨', label: 'Approval Matrix Configuration', need: ['approval.configure'] },
  { id: 'approval-versions', ico: '⎘', label: 'Workflow Version History', need: ['approval.configure'] },
  { id: 'approval-simulator', ico: '⊛', label: 'Approval Rule Simulator', need: ['approval.configure'] },
  { id: 'approval-delegations', ico: '⇌', label: 'Delegation Management', need: ['approval.delegate'] },
];

/** The SCR_ROUTES row for an id, spliced into NAV by reference. */
function scr(id) {
  const row = SCR_ROUTES.find(r => r.id === id);
  if (!row) throw new Error(`scr(): no SCR route declared for "${id}".`);
  return row;
}

/* ---------------- navigation ---------------------------------------------- */
const NAV = [
  { g: 'Work' },
  { id: 'home', ico: '▣', label: 'Executive Dashboard' },
  { id: 'approvals', ico: '✔', label: 'My Approvals', badge: 'approvals' },
  { id: 'alerts', ico: '⚠', label: 'Alerts & Exceptions', badge: 'alerts' },
  /* The eight approval screens are ROUTES ONLY — deliberately absent from this
     table. See the Governance group below and docs/ui-change-2026-09/
     A3-approval-navigation.md for the measurements that decided it. */
  { g: 'Project Control' },
  { id: 'projects', ico: '▤', label: 'CAPEX Projects' },
  { id: 'wbs', ico: '⌗', label: 'WBS Explorer' },
  { id: 'budget', ico: '▦', label: 'Budget Planning Grid' },
  { id: 'check', ico: '◎', label: 'Budget Availability Check', need: ['budget.check'] },
  { id: 'revisions', ico: '↻', label: 'Budget Revisions' },
  scr('budget-grid'),
  scr('budget-compare'),
  scr('budget-availability'),
  { g: 'Procurement & Actuals' },
  { id: 'prs', ico: '✎', label: 'Purchase Requests' },
  { id: 'pos', ico: '▧', label: 'Commitments (PO)' },
  { id: 'grns', ico: '⇩', label: 'GRN & Receipts' },
  { id: 'bills', ico: '₹', label: 'Vendor Bills & CWIP' },
  { id: 'recon', ico: '⇄', label: 'Commitment Reconciliation' },
  { g: 'Closure' },
  { id: 'cap', ico: '★', label: 'Capitalisation' },
  { g: 'Integration' },
  { id: 'zoho', ico: '⚯', label: 'Zoho ERP Connector', need: ['connector.read'] },
  { id: 'inventory', ico: '≣', label: 'API Inventory', need: ['connector.read'] },
  { g: 'Governance' },
  { id: 'audit', ico: '⎙', label: 'Audit Trail', need: ['audit.read'] },
  scr('audit-trail'),
  /* THE APPROVAL ENGINE'S EIGHT SCREENS ARE REACHABLE BY ROUTE, NOT BY RAIL.

     They were listed here in 69f45e1 as change A3. A3 was INSTRUCTED by the
     lead but never APPROVED by the product owner, unlike A1 (the five Wave 2/3
     entries) and A2 (the avatar contrast fix), and measurement showed it broke
     the approved layout rather than extending it. Measured from the running
     application as U-ADM, who sees seven of the eight:

         viewport        rail    content   overflow
         desktop-1440    856px   1050px    194px
         laptop-1024     714px   1050px    336px
         tablet-800      rail is display:none

     The whole Governance group — including `Settings & Master Data`, one of
     the five entries A1 WAS approved to expose — fell below the fold of a
     scroll container that gives no visual cue it scrolls. Removing these eight
     returns the content to 840px, which fits desktop-1440 again.

     Even a SINGLE consolidated "Approvals" entry was measured and does not
     fit: it takes the content to 870px against an 856px rail. So no nav entry
     is landed here at all, and the decision is referred to the lead with the
     numbers rather than taken by this stream. Nothing else is lost by that —
     `viewAllowed()` resolves any SCR_ROUTES id whether or not it appears in
     this table, so all eight hashes deep-link, stay permission-gated and stay
     bookmarkable exactly as they did before. Adding a rail entry later is one
     `scr('…')` splice per row, and needs only the approval, not a redesign. */
  scr('settings'),
];

function navAllowed(n) { return !n.need || can(...n.need); }

/* The permission gate for ANY view id, whether it sits in NAV or in SCR_ROUTES.
   An id in neither table is unknown and is refused, so a mistyped hash can
   never resolve to an ungated screen. */
function viewAllowed(id) {
  const row = NAV.find(n => n.id === id) || SCR_ROUTES.find(n => n.id === id);
  return row ? navAllowed(row) : false;
}

function renderNav() {
  const el = document.getElementById('nav');
  const out = [];
  NAV.forEach((n, i) => {
    if (n.g) {
      // Suppress a group heading whose every item is out of reach for this role.
      const rest = NAV.slice(i + 1);
      const end = rest.findIndex(x => x.g);
      const items = (end === -1 ? rest : rest.slice(0, end));
      if (!items.some(navAllowed)) return;
      out.push(`<h2 class="nav-group">${esc(n.g)}</h2>`);
      return;
    }
    if (!navAllowed(n)) return;
    const badge = n.badge === 'approvals' ? S.counts?.approvals : n.badge === 'alerts' ? S.counts?.alerts : null;
    const pill = badge ? `<span class="pill ${n.badge === 'alerts' ? 'warn' : ''}">${badge}</span>` : '';
    const active = S.view === n.id;
    out.push(`<button type="button" class="nav-item${active ? ' active' : ''}" data-nav="${n.id}"${active ? ' aria-current="page"' : ''}>
      <span class="ico" aria-hidden="true">${n.ico}</span><span class="nav-label">${esc(n.label)}</span>${pill}</button>`);
  });
  el.innerHTML = out.join('');
}

function setHeader(title, crumbs, facts, actions) {
  document.getElementById('pageTitle').textContent = title;
  document.getElementById('breadcrumb').innerHTML =
    (crumbs || ['Home']).map((c, i, a) => i === a.length - 1
      ? esc(c)
      : `<button type="button" class="crumb" data-nav="home">${esc(c)}</button>`).join('<span aria-hidden="true"> › </span>');
  document.getElementById('headerFacts').innerHTML = (facts || []).map(f =>
    `<div class="fact"><span class="k">${esc(f[0])}</span><span class="v ${f[2] || ''}">${f[1]}</span></div>`).join('');
  document.getElementById('pageActions').innerHTML = actions || '';
}

function msg(kind, text, id, actions) {
  const sym = SYM[kind === 'error' ? 'negative' : kind === 'warning' ? 'warning' : kind === 'success' ? 'positive' : 'neutral'];
  const role = kind === 'error' ? ' role="alert"' : '';
  return `<div class="msg msg-${kind}"${role}><span class="ico" aria-hidden="true">${sym}</span>
    <div class="body">${text}${id ? `<div class="mid">${esc(id)}</div>` : ''}${actions ? `<div class="actions">${actions}</div>` : ''}</div></div>`;
}

/* A single, honest badge for anything produced by the mock connector. */
function mockBadge() {
  return `<span class="mock-chip" title="${esc(S.zohoMode?.note || 'No live Zoho tenant is connected.')}">MOCK · NOT VERIFIED</span>`;
}
function mockBanner(extra) {
  const note = S.zohoMode?.note || 'No live Zoho tenant is connected; integration is NOT VERIFIED.';
  return msg('warning',
    `<strong>Connector mode: ${esc(S.zohoMode?.mode || 'MOCK')} — NOT VERIFIED.</strong> ${esc(note)}` +
    (extra ? ` ${esc(extra)}` : ''),
    'MODE=' + (S.zohoMode?.mode || 'MOCK'));
}

/* ---------------- views ---------------------------------------------------- */
/* A view is an async function returning EITHER an HTML string (the original
   contract — the string is assigned to #content and passed through enhance()),
   OR an object { node, mount } for a screen that builds real DOM. In the second
   form the node is appended as-is and `mount` is awaited afterwards, so a
   feature module can look up ids inside its own host before it renders. Nothing
   in that path goes near innerHTML, so nothing in it can be an XSS site. */
const V = {};

/* ---------------- SCR-nn screens, routed through this shell ----------------
   SCR-28, SCR-09, SCR-10, SCR-13 and SCR-30 are ES modules; this file is a
   classic script. src/core/router.js declares each of them — hash, title,
   breadcrumb, host DOM, mount function — and is import()ed on first navigation
   to one of these routes, so a user who never opens one never downloads them.
   The standalone host pages (audit.html, budget.html, settings.html) still
   work; this is an additional way in, not a replacement. */
let scrRegistry = null;
function loadScrRegistry() {
  // Absolute: a dynamic import inside a classic script resolves against the
  // document base URL ('/'), not against /static/app.js.
  scrRegistry = scrRegistry || import('/static/src/core/router.js');
  return scrRegistry;
}

async function scrView(id) {
  const { screenById } = await loadScrRegistry();
  const screen = screenById(id);
  if (!screen) {
    setHeader('Screen not found', ['Home'], []);
    return msg('error', 'This screen is not registered in the screen registry.');
  }
  setHeader(screen.title, screen.crumbs, []);
  return screen.build();   // { node, mount }
}

// One view per SCR_ROUTES row, so the route table and the permission table can
// never disagree about which ids exist.
for (const { id } of SCR_ROUTES) {
  V[id] = () => scrView(id);
}

V.home = async () => {
  const q = new URLSearchParams();
  if (S.entity) q.set('entity_id', S.entity);
  if (S.plant) q.set('plant_id', S.plant);
  const d = await api('/dashboard?' + q);
  const t = d.totals;
  S.counts = {
    approvals: d.alerts.pending_revisions.length + d.alerts.budget_exceptions.length + d.alerts.awaiting_capitalisation.length,
    alerts: d.alerts.budget_exceptions.length + d.alerts.over_billed_lines.length + d.alerts.received_not_billed_lines.length,
  };
  document.getElementById('approvalCount').textContent = S.counts.approvals;
  document.getElementById('alertCount').textContent = S.counts.alerts;
  renderNav();

  setHeader('Executive CAPEX Dashboard', ['Home', 'Executive Dashboard'],
    [['Portfolio budget', inrShort(t.budget)], ['Exposure', inrShort(t.exposure)],
     ['Available', inrShort(t.available)], ['Utilisation', pct(t.utilisation_pct), band(t.band)]]);

  const tiles = `<div class="tiles">
    <div class="tile accent-info"><div class="k">Current Approved Budget</div><div class="v">${inrShort(t.budget)}</div><div class="sub">${d.projects.length} active projects</div></div>
    <div class="tile accent-info"><div class="k">Open PO Commitment</div><div class="v">${inrShort(t.commitment)}</div><div class="sub">unbilled balance of approved POs</div></div>
    <div class="tile accent-info"><div class="k">Actual CWIP</div><div class="v">${inrShort(t.actual)}</div><div class="sub">posted vendor bills</div></div>
    <div class="tile accent-${t.band === 'safe' ? 'safe' : t.band === 'breach' ? 'breach' : 'watch'}"><div class="k">Available Budget</div><div class="v">${inrShort(t.available)}</div><div class="sub">${pct(t.utilisation_pct)} utilised</div></div>
    <div class="tile accent-watch"><div class="k">Received, Not Billed</div><div class="v">${inrShort(t.received_not_billed)}</div><div class="sub">exposure awaiting vendor bill</div></div>
  </div>`;

  const rows = d.projects.map(p => `<tr>
    <th scope="row" class="mono"><button type="button" class="linkish" data-open="${esc(p.project_id)}">${esc(p.capex_code)}</button></th>
    <td>${esc(p.name)}</td>
    <td>${esc(p.plant)}</td><td>${status(p.status)}</td>
    ${money(p.budget)}${money(p.commitment)}${money(p.actual)}${money(p.available)}
    <td class="num ${band(p.band)}">${pct(p.utilisation_pct)}</td>
    <td class="col-bar">${bar(p)}</td></tr>`).join('');

  const alerts = [];
  d.alerts.budget_exceptions.forEach(e => alerts.push(msg('error',
    `<strong>${esc(e.pr_number)}</strong> — ${esc(e.exception_reason || 'Exceeds available budget.')}`, 'MSG-BUD-001')));
  d.alerts.over_billed_lines.forEach(l => alerts.push(msg('warning',
    `Bill exceeds purchase order on <strong>${esc(l.po_number)}</strong> (${esc(l.wbs_code)}): billed ${inr(l.billed_paise)} against an ordered value of ${inr(l.ordered_paise)}.`, 'MSG-PRC-004')));
  d.alerts.received_not_billed_lines.forEach(l => alerts.push(msg('warning',
    `<strong>${esc(l.wbs_code)}</strong> has ${inr(l.received_not_billed_paise)} received but not yet billed on ${esc(l.po_number)}. This is included in exposure and converts to actual CWIP when the vendor bills.`, 'MSG-PRC-005')));
  d.alerts.pending_revisions.forEach(r => alerts.push(msg('info',
    `Budget revision <strong>${esc(r.revision_id)}</strong> (${esc(r.kind)}) is awaiting approval — ${esc(r.reason)}`)));

  return tiles + `<div class="card"><h3>Budget vs Commitment vs Actual vs Available<span class="spacer"></span>
      <span class="muted normal-weight">select a CAPEX code to open its WBS hierarchy</span></h3>
    <div class="table-wrap"><table>
      <caption class="sr-only">Portfolio budget, commitment, actual CWIP and available budget by CAPEX project</caption>
      <thead><tr>
      <th scope="col">CAPEX Code</th><th scope="col">Project</th><th scope="col">Plant</th><th scope="col">Status</th>
      <th scope="col" class="num">Current Budget</th><th scope="col" class="num">Commitment</th><th scope="col" class="num">Actual CWIP</th>
      <th scope="col" class="num">Available</th><th scope="col" class="num">Utilisation</th><th scope="col">Exposure</th></tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr><td colspan="4">Portfolio total</td>${money(t.budget)}${money(t.commitment)}${money(t.actual)}${money(t.available)}
      <td class="num">${pct(t.utilisation_pct)}</td><td></td></tr></tfoot></table></div>
    <div class="legend"><span><i class="k-actual"></i>Actual CWIP</span>
      <span><i class="k-commit"></i>Open commitment</span>
      <span><i class="k-avail"></i>Available</span></div></div>`
    + (alerts.length ? `<div class="card"><h3>Exceptions requiring attention</h3><div class="card-body">${alerts.join('')}</div></div>` : '');
};

V.projects = async () => {
  const d = await api('/dashboard');
  setHeader('CAPEX Projects', ['Home', 'CAPEX Projects'], []);
  return `<div class="card"><h3>Project list</h3><div class="table-wrap"><table>
    <caption class="sr-only">CAPEX projects with budget, exposure, available budget and utilisation</caption>
    <thead><tr>
    <th scope="col">CAPEX Code</th><th scope="col">Project</th><th scope="col">Entity</th><th scope="col">Plant</th><th scope="col">Status</th>
    <th scope="col" class="num">Budget</th><th scope="col" class="num">Exposure</th><th scope="col" class="num">Available</th><th scope="col" class="num">Util.</th></tr></thead>
    <tbody>${d.projects.map(p => `<tr>
      <th scope="row" class="mono"><button type="button" class="linkish" data-open="${esc(p.project_id)}">${esc(p.capex_code)}</button></th>
      <td>${esc(p.name)}</td><td>${esc(p.entity)}</td>
      <td>${esc(p.plant)}</td><td>${status(p.status)}</td>
      ${money(p.budget)}${money(p.exposure)}${money(p.available)}
      <td class="num ${band(p.band)}">${pct(p.utilisation_pct)}</td></tr>`).join('')}</tbody></table></div></div>`;
};

V.wbs = async () => {
  const d = await api(`/projects/${S.project}/wbs`);
  S.data.wbs = d;
  const p = S.boot.projects.find(x => x.project_id === S.project);
  const t = d.totals;
  setHeader(`WBS Hierarchy — ${p.name}`, ['Home', 'CAPEX Projects', p.capex_code],
    [['CAPEX code', esc(p.capex_code)], ['Plant', esc(p.plant_name)], ['Status', status(p.status)],
     ['Current budget', inr(t.budget)], ['Exposure', inr(t.exposure)],
     ['Available', inr(t.available)], ['Utilisation', pct(t.utilisation_pct), band(t.band)]],
    `<button type="button" id="expandAll">Expand all</button><button type="button" id="collapseAll">Collapse all</button>`);

  const rows = [];
  const walk = (n, depth) => {
    const kids = n.children || [];
    const open = S.expanded.has(n.wbs_id);
    const tot = n.total;
    // An element that carries no budget of its own is not budget-controlled here:
    // control sits at the nearest ancestor that is. Showing a derived "available" on
    // such a row would read as a large overrun when the money was simply budgeted
    // one level up.
    const controlled = tot.budget !== 0;
    const availCell = controlled ? money(tot.available)
      : `<td class="num muted" title="Budget for this element is held at ${esc(n.budget_owner_code || 'a higher level')}">—</td>`;
    const utilCell = controlled ? `<td class="num ${band(tot.band)}">${pct(tot.utilisation_pct)}</td>`
      : `<td class="num muted">—</td>`;
    const budgetCell = controlled ? money(tot.budget)
      : n.budget_owner_code
        ? `<td class="num muted" title="Budget control sits at ${esc(n.budget_owner_code)}">at ${esc(n.budget_owner_code)}</td>`
        : `<td class="num muted" title="No approved budget on this element or any above it">no budget</td>`;

    // /api/projects/{id}/wbs now returns a per-budget-head breakdown on each node.
    const headNames = Object.keys(n.heads || {});
    const headTitle = headNames.length
      ? headNames.map(h => `${h}: budget ${inr(n.heads[h].budget)}, exposure ${inr(n.heads[h].exposure)}`).join(' | ')
      : '';

    const toggle = kids.length
      ? `<button type="button" class="tree-toggle" data-toggle="${esc(n.wbs_id)}" aria-expanded="${open}">
           <span aria-hidden="true">${open ? '▼' : '▶'}</span>
           <span class="sr-only">${open ? 'Collapse' : 'Expand'} ${esc(n.wbs_code)}</span></button>`
      : `<span class="tree-toggle leaf" aria-hidden="true"></span>`;

    rows.push(`<tr class="lvl-${n.level}" data-wbs="${esc(n.wbs_id)}">
      <th scope="row" class="wbs-cell" data-indent="${depth}">
        ${toggle}<span class="wbs-code mono">${esc(n.wbs_code)}</span></th>
      <td${headTitle ? ` title="${esc(headTitle)}"` : ''}>${esc(n.description)}${n.is_abandoned ? ' <span class="st-negative">(abandoned)</span>' : ''}</td>
      <td>${esc(n.budget_head || (headNames.length > 1 ? headNames.length + ' heads' : '—'))}</td>
      <td>${status(n.status)}</td>
      <td class="num">${(n.progress_pct ?? 0).toFixed(0)}%</td>
      ${budgetCell}${money(tot.commitment)}${money(tot.actual)}${money(tot.received_not_billed)}${availCell}
      ${utilCell}
      <td class="col-bar-sm">${controlled ? bar(tot) : ''}</td>
      <td>${n.allow_procurement ? '<span class="st-positive status"><span class="sym" aria-hidden="true">✔</span>Yes</span>' : '<span class="st-negative status"><span class="sym" aria-hidden="true">✖</span>No</span>'}</td>
    </tr>`);
    if (open) kids.forEach(c => walk(c, depth + 1));
  };
  d.tree.forEach(n => walk(n, 0));

  return `<div class="toolbar">
      <div class="field"><label for="prjSel">Project</label>${projectSelect()}</div>
      <div class="grow"></div>
      <span class="muted">Parent rows show the rolled-up total of the element and every descendant.</span>
    </div>
    <div class="card"><h3>WBS tree table</h3>
    <div class="table-wrap"><table>
      <caption class="sr-only">Work breakdown structure for ${esc(p.capex_code)} with rolled-up budget, commitment and actual CWIP</caption>
      <thead><tr>
      <th scope="col" class="col-wbs">WBS Element</th><th scope="col" class="col-desc">Description</th>
      <th scope="col">Budget Head</th><th scope="col">Status</th>
      <th scope="col" class="num">Progress</th><th scope="col" class="num">Budget</th><th scope="col" class="num">Commitment</th>
      <th scope="col" class="num">Actual CWIP</th>
      <th scope="col" class="num">Recd–Unbilled</th><th scope="col" class="num">Available</th><th scope="col" class="num">Util.</th>
      <th scope="col">Exposure</th><th scope="col">Procure?</th>
    </tr></thead><tbody>${rows.join('')}</tbody>
    <tfoot><tr><td colspan="5">Project total</td>${money(t.budget)}${money(t.commitment)}${money(t.actual)}${money(t.received_not_billed)}${money(t.available)}
      <td class="num">${pct(t.utilisation_pct)}</td><td></td><td></td></tr></tfoot></table></div></div>`;
};

V.budget = async () => {
  const d = await api(`/projects/${S.project}/budget-grid`);
  const t = d.totals;
  setHeader('Budget Planning Grid', ['Home', 'Budgets'],
    [['Original', inr(t.original)], ['Revisions', inr(t.revisions)], ['Current approved', inr(t.budget)],
     ['Available', inr(t.available)]]);
  return `<div class="toolbar"><div class="field"><label for="prjSel">Project</label>${projectSelect()}</div></div>
    <div class="card"><h3>Budget by WBS element and budget head</h3><div class="table-wrap"><table>
      <caption class="sr-only">Budget, revisions, commitment, actual CWIP and available budget for each WBS element and budget head</caption>
      <thead><tr>
      <th scope="col">WBS</th><th scope="col">Description</th><th scope="col">Budget Head</th>
      <th scope="col" class="num">Original</th><th scope="col" class="num">Revisions</th><th scope="col" class="num">Current Approved</th>
      <th scope="col" class="num">PO Commitment</th><th scope="col" class="num">Actual CWIP</th><th scope="col" class="num">Available</th>
      <th scope="col" class="num">Exposure %</th></tr></thead>
    <tbody>${d.rows.map(r => `<tr data-head="${esc(r.budget_head_id || '')}">
      <th scope="row" class="mono">${esc(r.wbs_code)}</th><td>${esc(r.description)}</td>
      <td>${esc(r.budget_head || '—')}<span class="sr-only"> (${esc(r.budget_head_id || 'no head id')})</span></td>
      ${money(r.original)}${money(r.revisions)}${money(r.budget)}${money(r.commitment)}${money(r.actual)}${money(r.available)}
      <td class="num ${band(r.band)}">${pct(r.utilisation_pct)}</td></tr>`).join('')}</tbody>
    <tfoot><tr><td colspan="3">Total</td>${money(t.original)}${money(t.revisions)}${money(t.budget)}
      ${money(t.commitment)}${money(t.actual)}${money(t.available)}<td class="num">${pct(t.utilisation_pct)}</td></tr></tfoot>
    </table></div></div>
    ${msg('info', 'Each row is one WBS element and one budget head. The original approved budget is immutable — supplements, returns and transfers are recorded as separate versioned lines, each carrying requestor, approver, date, reason and approval reference.')}`;
};

V.check = async () => {
  const d = await api(`/projects/${S.project}/wbs`);
  const flat = [];
  const walk = n => { flat.push(n); (n.children || []).forEach(walk); };
  d.tree.forEach(walk);
  S.data.flat = flat;
  setHeader('Budget Availability Check', ['Home', 'Budgets', 'Availability Check'], []);

  const heads = S.boot.budget_heads || [];
  return `<div class="split"><div>
    <div class="card"><h3>Simulate a proposed commitment</h3><div class="card-body">
      <div class="toolbar flush">
        <div class="field f1"><label for="prjSel">Project</label>${projectSelect()}</div>
        <div class="field f2"><label for="ckWbs">WBS element</label>
          <select id="ckWbs">${flat.map(n => `<option value="${esc(n.wbs_id)}">${' '.repeat((n.level - 1) * 3)}${esc(n.wbs_code)} — ${esc(n.description)}</option>`).join('')}</select></div>
        <div class="field"><label for="ckHead">Budget head</label>
          <select id="ckHead"><option value="">All heads</option>${heads.map(h =>
            `<option value="${esc(h.budget_head_id)}">${esc(h.name)}</option>`).join('')}</select></div>
        <div class="field"><label for="ckAmt">Proposed value (₹)</label>
          <input id="ckAmt" class="num" type="text" inputmode="decimal" value="2000000.00"
                 aria-describedby="ckAmtHint"></div>
        <div class="field field-action"><button class="btn-primary" type="button" id="ckRun">Run check</button></div>
      </div>
      <p class="muted small" id="ckAmtHint">Rupees, at most two decimal places. The value is sent to the server as an exact decimal string, never as a floating-point number.</p>
      <div id="ckResult" class="mt-12" aria-live="polite"></div>
    </div></div></div>
    <div class="card"><h3>How the check works</h3><div class="card-body">
      <div class="kv">
        <dt>Exposure</dt><dd>Open PO Commitment + Actual CWIP</dd>
        <dt>Available Budget</dt><dd>Current Approved Budget − Exposure</dd>
        <dt>Utilisation</dt><dd>Exposure ÷ Current Approved Budget</dd>
      </div>
      <p class="muted mt-10">Budget held on a WBS element covers that element and every descendant, so the check evaluates the rolled-up position. Checking only the element's own line would let a parent's budget look untouched while its children consumed it.</p>
      <p class="muted">Thresholds: warning at 80%, exception approval at 90%, hard stop when exposure exceeds the approved budget.</p>
    </div></div></div>`;
};

V.prs = async () => {
  const prs = await api('/purchase-requests');
  setHeader('Purchase Requests', ['Home', 'Purchase Requests'], [],
    can('pr.create') ? `<button class="btn-primary" type="button" id="newPr">Create purchase request</button>` : '');
  const mayApprove = can('pr.approve');
  const mayApproveExc = can('pr.approve_exception');
  const roleText = esc((S.me.roles || []).join(', ') || 'none');
  return (can('pr.create') ? '' : msg('info', `Your role (${roleText}) cannot raise purchase requests, so that action is not offered.`))
    + `<div class="card"><h3>Purchase request control view</h3><div class="table-wrap"><table>
    <caption class="sr-only">Purchase requests with budget check result, status and approver</caption>
    <thead><tr>
    <th scope="col">PR Number</th><th scope="col">Project</th><th scope="col">WBS</th><th scope="col">Budget Head</th><th scope="col">Description</th>
    <th scope="col" class="num">Value</th><th scope="col">Budget Check</th><th scope="col">Status</th><th scope="col">Approver</th>
    <th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>${prs.map(p => {
      const isExc = p.status === 'Exception Pending';
      const decidable = ['Submitted', 'Exception Pending'].includes(p.status);
      const allowed = isExc ? mayApproveExc : mayApprove;
      return `<tr>
      <th scope="row" class="mono">${esc(p.pr_number)}</th><td class="mono">${esc(p.capex_code)}</td>
      <td class="mono">${esc(p.wbs_code)}</td><td>${esc(p.budget_head_name)}</td>
      <td>${esc(p.description)}</td>${money(p.amount_paise)}
      <td>${p.check_result ? (p.check_result === 'EXCEEDS_BUDGET'
        ? '<span class="status st-negative"><span class="sym" aria-hidden="true">✖</span>Exceeds budget</span>'
        : '<span class="status st-positive"><span class="sym" aria-hidden="true">✔</span>Within budget</span>') : '<span class="muted">not run</span>'}</td>
      <td>${status(p.status)}</td><td>${esc(p.approver || '—')}</td>
      <td>${decidable && allowed
        ? `<button class="btn-sm" type="button" data-approve-pr="${esc(p.pr_id)}" data-exc="${isExc ? 1 : 0}" data-ref="${esc(p.pr_number)}">Approve<span class="sr-only"> ${esc(p.pr_number)}</span></button>`
        : decidable ? '<span class="muted">awaiting another role</span>' : ''}</td>
    </tr>`; }).join('')}</tbody></table></div></div>`;
};

V.pos = async () => {
  const pos = await api('/purchase-orders');
  setHeader('Commitments — Purchase Orders', ['Home', 'Commitments'], []);
  const mayAmend = can('po.amend'), mayCancel = can('po.cancel'), mayClose = can('po.close');
  return `<div class="card"><h3>Purchase order commitment view</h3><div class="table-wrap"><table>
    <caption class="sr-only">Purchase orders with ordered value, billed value and open commitment, with their lines</caption>
    <thead><tr>
    <th scope="col">PO Number</th><th scope="col">Vendor</th><th scope="col">Project</th><th scope="col">Status</th><th scope="col">Curr.</th>
    <th scope="col" class="num">Ordered</th><th scope="col" class="num">Billed</th><th scope="col" class="num">Open Commitment</th>
    <th scope="col">Amend</th><th scope="col">Actions</th></tr></thead>
    <tbody>${pos.map(p => {
      const released = ['Cancelled', 'Closed'].includes(p.status);
      const acts = released ? '<span class="muted">released</span>'
        : [mayAmend ? `<button class="btn-sm" type="button" data-amend="${esc(p.po_id)}">Amend<span class="sr-only"> ${esc(p.po_number)}</span></button>` : '',
           mayCancel ? `<button class="btn-sm" type="button" data-cancel="${esc(p.po_id)}">Cancel<span class="sr-only"> ${esc(p.po_number)}</span></button>` : '',
           mayClose ? `<button class="btn-sm" type="button" data-close="${esc(p.po_id)}">Close<span class="sr-only"> ${esc(p.po_number)}</span></button>` : '',
          ].filter(Boolean).join(' ') || '<span class="muted">not permitted for your role</span>';
      return `
      <tr><th scope="row" class="mono">${esc(p.po_number)}</th><td>${esc(p.vendor_name)}</td><td class="mono">${esc(p.capex_code)}</td>
        <td>${status(p.status)}</td><td>${esc(p.currency)}${p.currency !== 'INR' ? ` @${esc(p.exchange_rate)}` : ''}</td>
        ${money(p.ordered_paise)}${money(p.billed_paise)}${money(p.open_commitment_paise)}
        <td class="num">${p.amendment_no || '—'}</td>
        <td>${acts}</td></tr>
      ${p.lines.map(l => `<tr class="muted"><td></td><td colspan="4" class="sub-line">Line ${l.line_no} · ${esc(l.wbs_code)} · ${esc(l.description)}</td>
        ${money(l.amount_paise + l.non_creditable_tax_paise + l.freight_paise)}${money(l.billed_paise)}${money(l.open_commitment_paise)}
        <td colspan="2">${esc(l.position)}</td></tr>`).join('')}
    `; }).join('')}</tbody></table></div></div>`;
};

V.grns = async () => {
  const g = await api('/grns');
  setHeader('GRN & Unbilled Receipts', ['Home', 'GRN and Receipts'], []);
  return `<div class="card"><h3>Goods receipts</h3><div class="table-wrap"><table>
    <caption class="sr-only">Goods receipt notes with their purchase order, value and lines</caption>
    <thead><tr>
    <th scope="col">GRN</th><th scope="col">PO</th><th scope="col">Received</th><th scope="col">Type</th>
    <th scope="col" class="num">Value</th><th scope="col">Lines</th></tr></thead>
    <tbody>${g.map(r => `<tr><th scope="row" class="mono">${esc(r.grn_number)}</th><td class="mono">${esc(r.po_number)}</td>
      <td>${esc(r.received_at)}</td><td>${r.is_reversal ? '<span class="st-warning status"><span class="sym" aria-hidden="true">!</span>Reversal</span>' : 'Receipt'}</td>
      ${money(r.amount_paise)}<td>${r.lines.map(l => `${esc(l.wbs_code)} × ${esc(l.quantity)}`).join(', ')}</td></tr>`).join('')}
    </tbody></table></div></div>
    ${msg('warning', 'Zoho ERP purchase-receive lines carry no link back to a purchase-order line, no project or WBS coding, and the module exposes no list endpoint. Received-but-unbilled exposure is therefore reconstructed inside this application rather than read from Zoho. <strong>OAS-03</strong>', 'UNVERIFIED - REQUIRES ZOHO CONFIRMATION')}`;
};

V.bills = async () => {
  const b = await api('/bills');
  setHeader('Vendor Bills & Actual CWIP', ['Home', 'Vendor Bills'], []);
  return `<div class="card"><h3>Vendor bills posted to CWIP</h3><div class="table-wrap"><table>
    <caption class="sr-only">Vendor bills and credit notes posted to capital work in progress</caption>
    <thead><tr>
    <th scope="col">Bill</th><th scope="col">PO</th><th scope="col">Vendor</th><th scope="col">Date</th><th scope="col">Type</th>
    <th scope="col" class="num">Value</th><th scope="col">WBS coding</th></tr></thead>
    <tbody>${b.map(r => `<tr><th scope="row" class="mono">${esc(r.bill_number)}</th><td class="mono">${esc(r.po_number || '—')}</td>
      <td>${esc(r.vendor_name)}</td><td>${esc(r.bill_date)}</td>
      <td>${r.doc_type === 'CREDIT_NOTE' ? '<span class="st-warning status"><span class="sym" aria-hidden="true">!</span>Credit note</span>' : 'Bill'}</td>
      ${money(r.amount_paise)}<td>${r.lines.map(l => `${esc(l.wbs_code)} · ${esc(l.budget_head_name)}`).join('<br>')}</td></tr>`).join('')}
    </tbody></table></div></div>
    ${msg('info', 'Bill lines carry <code>purchaseorder_item_id</code> in Zoho ERP, so commitment-to-actual conversion is matched at line level and a purchase order can never be counted twice.')}`;
};

V.recon = async () => {
  const d = await api('/reconciliation');
  const s = d.summary;
  setHeader('Commitment-to-Actual Reconciliation', ['Home', 'Reconciliation'],
    [['Lines', s.lines], ['Open commitment', inr(s.open_commitment_paise)],
     ['Billed', inr(s.billed_paise)], ['Received not billed', inr(s.received_not_billed_paise)],
     ['Exceptions', s.exceptions.length, s.exceptions.length ? 'st-warning' : '']]);
  return `<div class="card"><h3>Line-level reconciliation — purchase order → receipt → bill</h3><div class="table-wrap"><table>
    <caption class="sr-only">Line-level reconciliation of ordered, received and billed values with open commitment and exposure</caption>
    <thead><tr>
    <th scope="col">PO</th><th scope="col">WBS</th><th scope="col">Description</th><th scope="col" class="num">Ordered</th><th scope="col" class="num">Received</th>
    <th scope="col" class="num">Billed</th><th scope="col" class="num">Open Commitment</th><th scope="col" class="num">Recd–Unbilled</th>
    <th scope="col" class="num">Exposure</th><th scope="col">Position</th></tr></thead>
    <tbody>${d.rows.map(r => `<tr>
      <th scope="row" class="mono">${esc(r.po_number)}</th><td class="mono">${esc(r.wbs_code)}</td><td>${esc(r.description)}</td>
      ${money(r.ordered_paise)}${money(r.received_paise)}${money(r.billed_paise)}${money(r.open_commitment_paise)}
      ${money(r.received_not_billed_paise)}${money(r.exposure_paise)}
      <td>${r.flag === 'over-billed' ? '<span class="status st-negative"><span class="sym" aria-hidden="true">✖</span>' + esc(r.position) + '</span>'
        : r.flag === 'received-unbilled' ? '<span class="status st-warning"><span class="sym" aria-hidden="true">!</span>' + esc(r.position) + '</span>'
        : r.flag === 'released' ? '<span class="status st-neutral"><span class="sym" aria-hidden="true">·</span>' + esc(r.position) + '</span>'
        : esc(r.position)}</td></tr>`).join('')}</tbody>
    <tfoot><tr><td colspan="3">Total</td><td class="num"></td><td class="num"></td>${money(s.billed_paise)}${money(s.open_commitment_paise)}
      ${money(s.received_not_billed_paise)}<td class="num"></td><td></td></tr></tfoot></table></div></div>
    ${msg('success', 'Anti-double-count proof: for any purchase order, <strong>billed + open commitment = ordered</strong>. Billing moves value from commitment to actual and never adds to both, so total exposure is unchanged by billing progress.')}`;
};

V.revisions = async () => {
  const r = await api('/budget-revisions');
  setHeader('Budget Revisions', ['Home', 'Budget Revisions'], [],
    can('revision.create') ? `<button class="btn-primary" type="button" id="newRev">Request revision</button>` : '');
  const mayApprove = can('revision.approve');
  return `<div class="card"><h3>Revision register — the original budget is never overwritten</h3><div class="table-wrap"><table>
    <caption class="sr-only">Budget revision register with requestor, approver, approval reference and status</caption>
    <thead><tr>
    <th scope="col">Revision</th><th scope="col">Project</th><th scope="col">Type</th><th scope="col">Reason</th><th scope="col">Requested by</th>
    <th scope="col">Approver</th><th scope="col">Approval ref</th><th scope="col">Effective</th><th scope="col" class="num">Value</th>
    <th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>${r.map(x => `<tr><th scope="row" class="mono">${esc(x.revision_id)}</th><td class="mono">${esc(x.capex_code)}</td>
      <td>${esc(x.kind)}</td><td>${esc(x.reason)}</td><td>${esc(x.requested_by)}</td><td>${esc(x.approver || '—')}</td>
      <td class="mono">${esc(x.approval_ref || '—')}</td><td>${esc(x.effective_date || '—')}</td>
      ${money(x.lines.reduce((a, l) => a + l.amount_paise, 0))}<td>${status(x.status)}</td>
      <td>${x.status === 'Submitted'
        ? (mayApprove
          ? `<button class="btn-sm" type="button" data-approve-rev="${esc(x.revision_id)}">Approve<span class="sr-only"> ${esc(x.revision_id)}</span></button>`
          : '<span class="muted">awaiting Finance Approver</span>')
        : ''}</td></tr>`).join('')}
    </tbody></table></div></div>`;
};

V.cap = async () => {
  const caps = await api('/capitalisation');
  setHeader('Capitalisation Workbench', ['Home', 'Capitalisation'], []);
  const mayAlloc = can('capitalisation.allocate'), mayApprove = can('capitalisation.approve');
  return caps.map(c => `<div class="card"><h3>${esc(c.cap_number)} — ${esc(c.capex_code)} ${esc(c.project_name)}
      <span class="spacer"></span>${status(c.status)}</h3><div class="card-body">
    <div class="kv">
      <dt>CWIP balance</dt><dd>${inr(c.cwip_balance_paise)}</dd>
      <dt>Open commitment</dt><dd class="${c.open_commitment_paise ? 'neg' : ''}">${inr(c.open_commitment_paise)}</dd>
      <dt>Received not billed</dt><dd class="${c.received_not_billed_paise ? 'neg' : ''}">${inr(c.received_not_billed_paise)}</dd>
      <dt>Allocated</dt><dd>${inr(c.total_paise)}</dd>
      <dt>Capitalisation date</dt><dd>${esc(c.cap_date || '—')}</dd>
      <dt>Posting status</dt><dd>${esc(c.posting_status || '—')}</dd>
    </div>
    ${!c.ready ? msg('warning', `${esc(c.capex_code)} has ${inr(c.open_commitment_paise)} in open commitments and ${inr(c.received_not_billed_paise)} received but unbilled. Resolve or explicitly write off these before capitalising, or the asset value will be understated.`, 'MSG-CAP-001') : ''}
    ${c.allocations.length ? `<div class="table-wrap"><table>
      <caption class="sr-only">Asset allocations for ${esc(c.cap_number)}</caption>
      <thead><tr><th scope="col">Asset</th><th scope="col">Category</th><th scope="col">WBS</th><th scope="col" class="num">Amount</th></tr></thead>
      <tbody>${c.allocations.map(a => `<tr><th scope="row">${esc(a.asset_name)}</th><td>${esc(a.asset_category || '—')}</td>
        <td class="mono">${esc(a.wbs_id || '—')}</td>${money(a.amount_paise)}</tr>`).join('')}</tbody></table></div>`
      : '<p class="muted">No asset allocations yet.</p>'}
    <div class="btn-row">
      ${mayAlloc ? `<button type="button" data-alloc="${esc(c.cap_id)}">Allocate to asset<span class="sr-only"> for ${esc(c.cap_number)}</span></button>` : ''}
      ${mayApprove ? `<button class="btn-primary" type="button" data-approve-cap="${esc(c.cap_id)}">Approve capitalisation<span class="sr-only"> ${esc(c.cap_number)}</span></button>` : ''}
      ${!mayAlloc && !mayApprove ? '<span class="muted">Your role can view this request but cannot allocate or approve it.</span>' : ''}
    </div>
    </div></div>`).join('') || '<div class="empty"><div class="big" aria-hidden="true">★</div>No capitalisation requests.</div>';
};

V.approvals = async () => {
  const d = await api('/dashboard');
  setHeader('My Approval Inbox', ['Home', 'Approvals'], []);
  const items = [
    ...d.alerts.budget_exceptions.map(e => ({
      t: 'Purchase request — exception', k: e.pr_number, v: e.amount_paise, why: e.exception_reason,
      act: can('pr.approve_exception') ? `data-approve-pr="${esc(e.pr_id)}" data-exc="1" data-ref="${esc(e.pr_number)}"` : null,
      need: 'Finance Approver',
    })),
    ...d.alerts.pending_revisions.map(r => ({
      t: 'Budget revision', k: r.revision_id, v: null, why: r.reason,
      act: can('revision.approve') ? `data-approve-rev="${esc(r.revision_id)}"` : null,
      need: 'Finance Approver',
    })),
    ...d.alerts.awaiting_capitalisation.map(c => ({
      t: 'Capitalisation request', k: c.cap_number, v: c.total_paise, why: `${c.capex_code} ${c.name}`,
      act: can('capitalisation.approve') ? `data-approve-cap="${esc(c.cap_id)}"` : null,
      need: 'Capitalisation Approver',
    })),
  ];
  if (!items.length) return '<div class="empty"><div class="big" aria-hidden="true">✔</div>Nothing awaiting your approval.</div>';
  return `<div class="card"><h3>${items.length} items awaiting decision</h3><div class="table-wrap"><table>
    <caption class="sr-only">Items awaiting a decision, with type, reference and value</caption>
    <thead><tr>
    <th scope="col">Type</th><th scope="col">Reference</th><th scope="col" class="num">Value</th><th scope="col">Detail</th>
    <th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>${items.map(i => `<tr><td>${esc(i.t)}</td><th scope="row" class="mono">${esc(i.k)}</th>
      <td class="num">${i.v != null ? inr(i.v) : '—'}</td><td>${esc(i.why || '')}</td>
      <td>${i.act
        ? `<button class="btn-sm btn-primary" type="button" ${i.act}>Approve<span class="sr-only"> ${esc(i.k)}</span></button>`
        : `<span class="muted">requires ${esc(i.need)}</span>`}</td></tr>`).join('')}</tbody></table></div></div>`;
};

V.alerts = async () => {
  const d = await api('/dashboard');
  setHeader('Alerts & Exceptions', ['Home', 'Alerts'], []);
  const out = [];
  d.alerts.budget_exceptions.forEach(e => out.push(msg('error', `<strong>${esc(e.pr_number)}</strong> — ${esc(e.exception_reason)}`, 'MSG-BUD-001')));
  d.alerts.over_billed_lines.forEach(l => out.push(msg('warning',
    `Bill exceeds purchase order on <strong>${esc(l.po_number)}</strong> (${esc(l.wbs_code)}): billed ${inr(l.billed_paise)} against ordered ${inr(l.ordered_paise)}, an excess of ${inr(l.billed_paise - l.ordered_paise)}.`, 'MSG-PRC-004')));
  d.alerts.received_not_billed_lines.forEach(l => out.push(msg('warning',
    `<strong>${esc(l.wbs_code)}</strong>: ${inr(l.received_not_billed_paise)} received but not billed on ${esc(l.po_number)}.`, 'MSG-PRC-005')));
  return out.length ? out.join('') : '<div class="empty"><div class="big" aria-hidden="true">✔</div>No open exceptions.</div>';
};

V.zoho = async () => {
  const conns = await api('/zoho/connections');
  const { connections, data_centres } = conns;
  const c = connections[0];
  const health = await api(`/zoho/${c.connection_id}/health`);
  const sc = await api('/zoho/scopes');
  S.data.conn = c.connection_id;
  // The connector reports its own mode. Show it rather than the app's assumption.
  S.zohoMode = { mode: health.mode || conns.mode || S.zohoMode?.mode || 'MOCK', note: S.zohoMode?.note };

  const mayManage = can('connector.manage');
  setHeader('Zoho ERP Connector', ['Home', 'Integration', 'Zoho ERP Connector'],
    [['Connection', esc(c.name)], ['Mode', `<span class="mock-chip">${esc(S.zohoMode.mode)} · NOT VERIFIED</span>`],
     ['Data centre', esc(c.data_centre)], ['OAuth', status(c.oauth_status)],
     ['Organisation', esc(c.zoho_org_name || '—')],
     ['Scopes granted', `${health.scopes.granted} / ${health.scopes.required}`,
      health.scopes.missing.length ? 'st-warning' : 'st-positive']],
    mayManage
      ? `<button class="btn-primary" type="button" id="zAuth">Authorise</button>
         <button type="button" id="zAuthPartial">Authorise (withhold 2 scopes)</button>
         <button type="button" id="zRefresh">Refresh token</button>
         <button type="button" id="zTest">Run connectivity tests</button>`
      : '');

  const neg = sc.hard_negatives.map(n => msg(n.severity === 'CRITICAL' ? 'error' : 'warning',
    `<strong>${esc(n.id)} — ${esc(n.title)}</strong><br>${esc(n.impact)}`)).join('');

  return mockBanner('Every OAuth status, token expiry, connectivity result and record count shown on this screen is synthesised locally. None of it is evidence that a Zoho tenant works.')
    + (mayManage ? '' : msg('info', 'Your role can read the connector but cannot authorise, refresh or test it, so those actions are not offered.'))
    + `<div class="card"><h3>Connection profile ${mockBadge()}</h3><div class="card-body">
      <div class="kv">
        <dt>Environment</dt><dd>${esc(c.environment)}</dd>
        <dt>Accounts domain</dt><dd class="mono">${esc(c.accounts_domain)}</dd>
        <dt>API domain</dt><dd class="mono">${esc(c.api_domain)}</dd>
        <dt>Client ID</dt><dd class="mono muted">${esc(c.client_id_ref)}</dd>
        <dt>Client secret</dt><dd class="mono muted">${esc(c.client_secret_ref)} — never displayed after saving</dd>
        <dt>Refresh token</dt><dd class="mono muted">${esc(c.refresh_token_ref)}</dd>
        <dt>Redirect URI</dt><dd class="mono">${esc(c.redirect_uri)}</dd>
        <dt>Token expiry</dt><dd>${esc(c.access_token_expiry || '—')} <span class="muted">(simulated)</span></dd>
      </div></div></div>

    <div class="card"><h3>Data centres</h3><div class="table-wrap"><table>
      <caption class="sr-only">Zoho data centres with accounts and API domains and the evidence source for each</caption>
      <thead><tr>
      <th scope="col">Region</th><th scope="col">Accounts domain</th><th scope="col">API domain</th><th scope="col">Evidence source</th></tr></thead>
      <tbody>${data_centres.map(d => `<tr><th scope="row">${esc(d.code)}</th><td class="mono">${esc(d.accounts)}</td>
        <td class="mono">${esc(d.api)}</td><td>${d.erp_documented
          ? '<span class="status st-positive"><span class="sym" aria-hidden="true">✔</span>Zoho ERP documentation</span>'
          : '<span class="status st-warning"><span class="sym" aria-hidden="true">!</span>Zoho CRM documentation — not ERP-documented</span>'}</td></tr>`).join('')}
      </tbody></table></div></div>

    <div class="card"><h3>OAuth scopes — least privilege, derived from the specification ${mockBadge()}</h3>
      <div class="card-body">${health.scopes.missing.length
        ? msg('error', `Missing scopes: <span class="mono">${health.scopes.missing.map(esc).join(', ')}</span>. Re-authorise the connection and grant them.`, 'MSG-INT-001')
        : msg('success', 'All required scopes are granted in the simulated authorisation. No live consent screen has been completed.')}
      </div>
      <div class="table-wrap"><table>
        <caption class="sr-only">Required OAuth scopes, the modules they cover and whether each is granted</caption>
        <thead><tr><th scope="col">Scope</th><th scope="col">Modules</th><th scope="col" class="num">Ops</th>
        <th scope="col">Business impact if missing</th><th scope="col">Granted</th></tr></thead>
      <tbody>${sc.required.map(s => `<tr><th scope="row" class="mono">${esc(s.scope)}</th>
        <td>${s.modules.map(esc).join(', ')}</td><td class="num">${s.operation_count}</td>
        <td>${esc(s.business_impact_if_missing)}</td>
        <td>${health.scopes.missing.includes(s.scope)
          ? '<span class="status st-negative"><span class="sym" aria-hidden="true">✖</span>Missing</span>'
          : (health.connection.oauth_status === 'Connected' ? '<span class="status st-positive"><span class="sym" aria-hidden="true">✔</span>Granted</span>' : '<span class="muted">—</span>')}</td></tr>`).join('')}
      </tbody></table></div></div>

    <div class="card"><h3>Module connectivity test console ${mockBadge()}</h3><div id="zTestOut" class="card-body" aria-live="polite">
      <p class="muted">${mayManage
        ? 'Run the tests to check authentication, organisation access, scope coverage and each required module. No network call leaves this machine — the request that would be issued is constructed from the verified specification.'
        : 'Running the tests requires the connector.manage permission.'}</p></div></div>

    <div class="card"><h3>Documented API limitations that shape this design</h3><div class="card-body">${neg}
      ${msg('info', esc(health.rate_limit_note))}
      ${msg('info', esc(health.webhook_note))}</div></div>

    <div class="card"><h3>Integration event log ${mockBadge()}</h3><div class="table-wrap"><table>
      <caption class="sr-only">Simulated integration events with endpoint, method, status and correlation id</caption>
      <thead><tr>
      <th scope="col">At</th><th scope="col">Direction</th><th scope="col">Module</th><th scope="col">Endpoint</th><th scope="col">Method</th>
      <th scope="col">Status</th><th scope="col">Correlation</th><th scope="col">Message</th></tr></thead>
      <tbody>${health.events.map(e => `<tr><th scope="row">${esc(e.at)}</th><td>${esc(e.direction)}</td><td>${esc(e.module)}</td>
        <td class="mono">${esc(e.endpoint || '—')}</td><td>${esc(e.http_method || '—')}</td><td>${status(e.status)}</td>
        <td class="mono">${esc(e.correlation_id || '')}</td><td>${esc(e.message || '')}</td></tr>`).join('')
        || '<tr><td colspan="8" class="muted pad-10">No events yet.</td></tr>'}</tbody></table></div></div>`;
};

V.inventory = async () => {
  const inv = await api('/zoho/inventory');
  setHeader('Zoho ERP API Inventory', ['Home', 'Integration', 'API Inventory'],
    [['Operations', inv.row_count], ['Modules', inv.modules.length],
     ['On /erp/v3', inv.api_version_split.v3], ['On /erp/v1', inv.api_version_split.v1, 'st-warning']]);
  const need = new Set(inv.required_modules.map(m => m.module));
  return msg('info', `Generated mechanically from Zoho's own published OpenAPI bundle (sha256 <span class="mono">${esc(inv.spec_sha256.slice(0, 16))}…</span>, retrieved 2026-08-05). Every endpoint, method and OAuth scope below is vendor-authoritative — none is written from memory. This inventory is specification evidence; it is <strong>not</strong> evidence that any call has been executed against a tenant.`)
    + msg('warning', `<strong>The ERP API is not uniformly <span class="mono">/erp/v3</span>.</strong> ${inv.api_version_split.v1} operations across 14 modules are served from <span class="mono">/erp/v1</span>, so the connector resolves the base path per module rather than per connection.`, 'OAS-01')
    + `<div class="card"><h3>Modules</h3><div class="table-wrap"><table>
      <caption class="sr-only">Zoho ERP modules with API version, operation count and required OAuth scopes</caption>
      <thead><tr>
      <th scope="col">Module</th><th scope="col">Business object</th><th scope="col">API</th><th scope="col" class="num">Operations</th>
      <th scope="col">OAuth scopes</th><th scope="col">Needed</th></tr></thead>
      <tbody>${inv.modules.map(m => `<tr${need.has(m.module) ? ' class="sel"' : ''}>
        <th scope="row" class="mono">${esc(m.module)}</th><td>${esc(m.title || '')}</td>
        <td class="mono ${m.api_version === 'v1' ? 'st-warning' : ''}">${esc(m.api_version)}</td>
        <td class="num">${m.operations}</td><td class="mono xs">${m.scopes.map(esc).join(', ') || '—'}</td>
        <td>${need.has(m.module) ? '<span class="status st-positive"><span class="sym" aria-hidden="true">✔</span>Yes</span>' : ''}</td></tr>`).join('')}
      </tbody></table></div></div>`;
};

V.audit = async () => {
  const a = await api('/audit');
  let verify = null;
  try { verify = await api('/audit/verify'); } catch { /* chain verification is optional detail */ }
  setHeader('Audit Trail', ['Home', 'Audit Trail'], [['Entries', a.length]]);
  const banner = verify
    ? msg(verify.ok === false ? 'error' : 'success',
        `Hash-chain verification: ${esc(String(verify.ok ?? verify.status ?? 'checked'))}${verify.checked != null ? ` over ${esc(verify.checked)} entries` : ''}.`)
    : '';
  return banner + `<div class="card"><h3>Complete maker–checker audit trail</h3><div class="table-wrap"><table>
    <caption class="sr-only">Audit trail entries with time, actor, action, object and detail</caption>
    <thead><tr>
    <th scope="col">At</th><th scope="col">Actor</th><th scope="col">Action</th><th scope="col">Object</th>
    <th scope="col">Reference</th><th scope="col">Detail</th></tr></thead>
    <tbody>${a.map(r => `<tr><th scope="row">${esc(r.at)}</th><td>${esc(r.actor)}</td><td class="mono">${esc(r.action)}</td>
      <td>${esc(r.object_type)}</td><td class="mono">${esc(r.object_id)}</td><td>${esc(r.detail)}</td></tr>`).join('')}
    </tbody></table></div></div>`;
};

/* ---------------- helpers -------------------------------------------------- */
function projectSelect() {
  return `<select id="prjSel">${S.boot.projects.map(p =>
    `<option value="${esc(p.project_id)}" ${p.project_id === S.project ? 'selected' : ''}>${esc(p.capex_code)} — ${esc(p.name)}</option>`).join('')}</select>`;
}

/* Post-process injected markup so table and label semantics hold even where a
   template forgot them. AUD-M-003. */
const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function enhance(root) {
  if (!root) return;

  // The server sends Content-Security-Policy: style-src 'self', which blocks every
  // inline style attribute. Anything genuinely dynamic (bar geometry, tree
  // indentation) is therefore applied through the CSSOM, which CSP does not
  // restrict — a style attribute in the markup would simply be dropped and the
  // console would fill with violations.
  root.querySelectorAll('.bar > i[data-width]').forEach(i => {
    i.style.left = (i.dataset.left || 0) + '%';
    i.style.width = (i.dataset.width || 0) + '%';
  });
  root.querySelectorAll('[data-indent]').forEach(el => {
    el.style.paddingLeft = (8 + Number(el.dataset.indent || 0) * 16) + 'px';
  });

  root.querySelectorAll('table').forEach(t => {
    t.querySelectorAll('thead th:not([scope])').forEach(th => th.setAttribute('scope', 'col'));
    t.querySelectorAll('tbody th:not([scope])').forEach(th => th.setAttribute('scope', 'row'));
    if (!t.querySelector('caption') && !t.hasAttribute('aria-label')) {
      const h = t.closest('.card')?.querySelector(':scope > h3');
      const cap = document.createElement('caption');
      cap.className = 'sr-only';
      cap.textContent = (h ? h.textContent : 'Data table').trim().replace(/\s+/g, ' ');
      t.insertBefore(cap, t.firstChild);
    }
  });
  // Associate every stray label with its control.
  root.querySelectorAll('label:not([for])').forEach(l => {
    if (l.querySelector('input,select,textarea')) return;   // wrapping label: already associated
    const ctl = l.parentElement?.querySelector('input[id],select[id],textarea[id]');
    if (ctl) l.setAttribute('for', ctl.id);
  });
  // Any control still without an accessible name gets one from nearby text.
  root.querySelectorAll('input,select,textarea').forEach(ctl => {
    if (ctl.getAttribute('aria-label') || ctl.labels?.length) return;
    const t = ctl.closest('.field')?.querySelector('label')?.textContent?.trim();
    if (t) ctl.setAttribute('aria-label', t);
  });
}

/* ---------------- dialog (labelled, focus-trapped, Escape closes) ---------- */
let dlgReturnFocus = null;

function dialogMessage(kind, text) {
  const box = document.getElementById('dlgMsg');
  box.innerHTML = msg(kind, text);
  box.hidden = false;
}
function clearDialogMessage() {
  const box = document.getElementById('dlgMsg');
  box.innerHTML = ''; box.hidden = true;
}

function dialog(title, bodyHtml, onOk, okLabel = 'Confirm', { okClass = 'btn-primary' } = {}) {
  const dlg = document.getElementById('dlg');
  document.getElementById('dlgTitle').textContent = title;
  document.getElementById('dlgBody').innerHTML = bodyHtml;
  clearDialogMessage();
  document.getElementById('dlgFoot').innerHTML =
    `<button type="button" id="dlgCancel">Cancel</button>` +
    (onOk ? `<button type="button" class="${okClass}" id="dlgOk">${esc(okLabel)}</button>` : '');
  enhance(document.getElementById('dlgBody'));

  dlgReturnFocus = document.activeElement;
  dlg.showModal();

  // Move focus into the dialog: the first field if there is one, else the primary action.
  const first = document.getElementById('dlgBody').querySelector(FOCUSABLE)
             || document.getElementById('dlgOk') || document.getElementById('dlgCancel');
  first?.focus();

  document.getElementById('dlgCancel').onclick = () => dlg.close();
  const ok = document.getElementById('dlgOk');
  if (ok) ok.onclick = async () => {
    ok.disabled = true;
    try {
      await onOk();
      dlg.close();
      await render();
    } catch (e) {
      // A SilentError means the handler has already written its own explanation
      // into the dialog; overwriting it would replace a precise refusal with a
      // generic one.
      if (e && e.name !== 'SilentError') {
        dialogMessage('error', esc(e.message || 'The action could not be completed.'));
      }
    } finally {
      ok.disabled = false;
    }
  };
}

/* ---------------- sign-in / sign-out --------------------------------------- */
function showAuth(message, kind = 'error') {
  document.getElementById('shell').hidden = true;
  const auth = document.getElementById('authScreen');
  auth.hidden = false;
  const box = document.getElementById('loginMsg');
  box.innerHTML = message ? msg(kind, esc(message)) : '';
  document.body.dataset.auth = 'signed-out';
  const dlg = document.getElementById('dlg');
  if (dlg.open) dlg.close();
  const u = document.getElementById('loginUser');
  if (!u.value) u.focus();
}

function showShell() {
  document.getElementById('authScreen').hidden = true;
  document.getElementById('shell').hidden = false;
  document.body.dataset.auth = 'signed-in';
}

function onUnauthorised(message) {
  setSession('');
  S.boot = null; S.me = null; S.perms = new Set();
  showAuth(message || 'Your session is no longer valid. Sign in again.');
}

async function signIn(userId, password) {
  const r = await api('/auth/login', { method: 'POST', json: { user_id: userId, password } });
  if (!r || !r.session_id) throw new ApiError('Sign-in did not return a session.');
  setSession(r.session_id);
  await start();
}

async function signOut() {
  try { await api('/auth/logout', { method: 'POST' }); }
  catch { /* an already-dead session is still a successful sign-out from here */ }
  setSession('');
  S.boot = null; S.me = null; S.perms = new Set();
  document.getElementById('loginPass').value = '';
  showAuth('You are signed out.', 'success');
}

function paintIdentity() {
  const me = S.me || {};
  const initials = (me.name || me.user_id || '··').split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
  document.getElementById('userAvatar').textContent = initials;
  document.getElementById('userName').textContent = me.name || me.user_id || '—';
  document.getElementById('userRoles').textContent = (me.roles || []).join(', ') || 'no roles';
  document.getElementById('signOutBtn').setAttribute(
    'aria-label', `Sign out ${me.name || me.user_id || ''}`.trim());
}

/* ---------------- render --------------------------------------------------- */
function flash(kind, text) { S.flash = { kind, text }; }

/* The .msg / .msg-error box of msg(), built as real nodes. Used on the DOM
   rendering path, where no HTML string is ever produced and therefore no
   escaping rule has to be remembered. */
function errorNode(message) {
  const box = document.createElement('div');
  box.className = 'msg msg-error';
  box.setAttribute('role', 'alert');
  const ico = document.createElement('span');
  ico.className = 'ico';
  ico.setAttribute('aria-hidden', 'true');
  ico.textContent = SYM.negative;
  const body = document.createElement('div');
  body.className = 'body';
  body.appendChild(document.createTextNode(String(message ?? '')));
  box.appendChild(ico);
  box.appendChild(body);
  return box;
}

async function render() {
  if (!getSession() || !S.boot) return;

  // An unknown hash, or one naming a screen this principal may not see, is a
  // CORRECTION, not a navigation. It replaces the entry so pressing Back cannot
  // walk straight back into it. Anything else is a real navigation and gets its
  // own history entry, so Back returns to the previous screen instead of
  // leaving the application entirely — which is what `replaceState` for both
  // cases used to do.
  const corrected = !V[S.view] || !viewAllowed(S.view);
  if (corrected) S.view = 'home';
  const target = '#' + S.view;
  if (location.hash !== target) {
    // No hash at all is the first paint after sign-in: stamping the URL is
    // normalisation too, and pushing there would leave a Back that goes
    // nowhere visible.
    if (corrected || !location.hash) history.replaceState(null, '', target);
    else history.pushState(null, '', target);
  }

  const el = document.getElementById('content');
  el.innerHTML = '<div class="loading">Loading…</div>';
  const rendering = S.view;
  let result;
  try {
    result = await (V[S.view] || V.home)();
  } catch (e) {
    if (e instanceof ApiError && e.statusCode === 401) return;   // sign-in already shown
    result = msg('error', `Could not load this view. ${esc(e.message)}`, e.code || undefined);
  }
  // A view that awaited (every SCR-nn screen import()s its module) can land
  // after the user has already navigated on. Painting it now would put one
  // screen's content under another screen's title.
  if (S.view !== rendering) return;

  const banner = S.flash ? msg(S.flash.kind, S.flash.text) : '';
  S.flash = null;

  if (result && typeof result === 'object' && result.node instanceof Node) {
    // DOM-building view. The node is appended before mount() runs so a feature
    // module can find its own live region and roots by id.
    el.innerHTML = banner;
    el.appendChild(result.node);
    let mountError = null;
    if (typeof result.mount === 'function') {
      try {
        await result.mount(el);
      } catch (e) {
        mountError = e;
      }
    }
    // The host node lands in the document BEFORE mount() is awaited, because a
    // feature module looks its own roots and live region up by id. So the
    // presence of .scr-host says nothing about whether the screen has actually
    // rendered. This flag does, and it is what anything waiting on the screen —
    // a test, or a later render — should watch for. A view that navigated away
    // mid-mount is skipped: S.view moved on and this node is already detached.
    if (S.view === rendering && result.node.nodeType === 1) {
      if (mountError) {
        // A FAILED MOUNT IS NOT A MOUNT, AND SAYING IT IS HIDES THE FAILURE.
        //
        // This used to append the error to #content and then set
        // data-mounted='1' anyway. Both halves were wrong. The flag is the
        // whole application's "this screen has rendered" signal, so a screen
        // whose mount() threw — a feature module that failed to fetch, an
        // on-demand stylesheet that 404ed, a throw inside the module — was
        // announced as rendered while being empty. Anything waiting on the
        // flag then observed a screen with no controls in it and had to guess
        // why: the symptom was "this screen has no focusable control", which
        // describes the design rather than the failure, and reads as flakiness
        // rather than as a load error.
        //
        // The error also belongs INSIDE the host. Appended to #content it sat
        // outside the .scr-host every screen-scoped assertion — and every
        // screen-scoped stylesheet — is written against.
        result.node.appendChild(
          errorNode(`This screen could not be loaded. ${mountError.message}`));
        result.node.dataset.mountFailed = '1';
      } else {
        result.node.dataset.mounted = '1';
      }
    }
    // enhance() is deliberately NOT called here: these screens build their own
    // DOM through core/dom.js, which already applies geometry through the
    // CSSOM and table semantics at construction time. Running it would make
    // the shell-hosted rendering differ from the standalone host page's.
  } else {
    el.innerHTML = banner + (result || '');
    enhance(el);
  }
  renderNav();
  closeNavOnNarrow();

  // Deep-link support for the availability check: ?wbs=W-03&amt=2000000&auto=1
  if (S.view === 'check') {
    const q = new URLSearchParams(location.search);
    if (q.get('auto') === '1') {
      const w = document.getElementById('ckWbs'), a = document.getElementById('ckAmt');
      if (q.get('wbs') && w) w.value = q.get('wbs');
      if (q.get('amt') && a) a.value = q.get('amt');
      document.getElementById('ckRun')?.click();
    }
  }
}

/* ---------------- responsive navigation (AUD-M-004) ------------------------ */
const NARROW = window.matchMedia('(max-width: 900px)');

function navIsOpen() { return document.getElementById('nav').classList.contains('open'); }

function setNav(open) {
  const nav = document.getElementById('nav');
  const scrim = document.getElementById('navScrim');
  const toggle = document.getElementById('navToggle');
  if (NARROW.matches) {
    nav.classList.toggle('open', open);
    nav.classList.remove('collapsed');
    scrim.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) nav.querySelector('.nav-item')?.focus();
  } else {
    nav.classList.remove('open');
    nav.classList.toggle('collapsed', !open);
    scrim.hidden = true;
    toggle.setAttribute('aria-expanded', String(open));
  }
}
function closeNavOnNarrow() { if (NARROW.matches && navIsOpen()) setNav(false); }

NARROW.addEventListener('change', () => {
  // Below 900px the drawer starts closed; above it the rail is shown.
  setNav(!NARROW.matches);
});

/* ---------------- events --------------------------------------------------- */
document.getElementById('loginForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = document.getElementById('loginBtn');
  const box = document.getElementById('loginMsg');
  const user = document.getElementById('loginUser').value.trim();
  const pass = document.getElementById('loginPass').value;
  if (!user || !pass) { box.innerHTML = msg('error', 'Enter both a user id and a password.'); return; }
  btn.disabled = true;
  box.innerHTML = '<div class="loading">Signing in…</div>';
  try {
    await signIn(user, pass);
  } catch (e) {
    box.innerHTML = msg('error', esc(e.message || 'Sign-in failed.'));
    document.getElementById('loginPass').value = '';
    document.getElementById('loginPass').focus();
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('dlg').addEventListener('close', () => {
  // Escape and the close button both land here; restore focus to the opener.
  clearDialogMessage();
  const target = dlgReturnFocus;
  dlgReturnFocus = null;
  if (target && document.contains(target)) target.focus();
  else document.getElementById('content')?.focus();
});
document.getElementById('dlgClose').addEventListener('click', () => document.getElementById('dlg').close());

document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('[data-nav],[data-toggle],[data-open],[data-approve-pr],[data-approve-rev],[data-approve-cap],[data-alloc],[data-amend],[data-cancel],[data-close]');
  if (!t) return;

  if (t.dataset.nav) { S.view = t.dataset.nav; await render(); return; }

  if (t.dataset.toggle) {
    const id = t.dataset.toggle;
    S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id);
    await render();
    // Keep the keyboard where it was: refocus the same toggle after the re-render.
    document.querySelector(`[data-toggle="${CSS.escape(id)}"]`)?.focus();
    return;
  }
  if (t.dataset.open) { S.project = t.dataset.open; S.view = 'wbs'; await render(); return; }

  if (t.dataset.approvePr) {
    const isExc = t.dataset.exc === '1';
    const ref = t.dataset.ref || t.dataset.approvePr;
    dialog(isExc ? 'Exception approval — reason required' : 'Approve purchase request',
      (isExc ? msg('warning', 'This request exceeds available budget. A reason is mandatory and will be stored in the audit trail with your name and the current time.', 'MSG-SEC-003') : '')
      + approvingAs(`You are approving ${esc(ref)}. The server records the signed-in user as the approver; it cannot be nominated from this screen, and you may not approve anything you raised yourself.`)
      + (isExc ? `<div class="field"><label for="dReason">Exception reason</label><textarea id="dReason" rows="3" placeholder="Why is this overrun justified?"></textarea></div>` : ''),
      async () => {
        const reason = document.getElementById('dReason')?.value?.trim();
        if (isExc && !reason) throw new Error('An exception reason is mandatory.');
        await api(`/purchase-requests/${t.dataset.approvePr}/approve`,
          { method: 'POST', json: { reason: reason || null } });
        flash('success', `${esc(ref)} approved.`);
      }, 'Approve');
    return;
  }

  if (t.dataset.approveRev) {
    const ref = t.dataset.approveRev;
    dialog('Approve budget revision',
      msg('info', 'The original approved budget is not modified. The revision is recorded as a separate versioned line carrying requestor, approver, date, reason and approval reference.')
      + approvingAs(`You are approving ${esc(ref)}.`),
      async () => {
        await api(`/budget-revisions/${ref}/approve`, { method: 'POST', json: {} });
        flash('success', `${esc(ref)} approved.`);
      }, 'Approve');
    return;
  }

  if (t.dataset.approveCap) {
    const id = t.dataset.approveCap;
    dialog('Approve capitalisation',
      `<p>Allocations must total the CWIP balance exactly. Any remainder must be recorded as a write-off with a reason.</p>`
      + approvingAs('')
      + `<div class="field"><label for="capOverride">Override reference (only if an approved override exists)</label>
           <input id="capOverride" placeholder="Leave blank for a normal approval"></div>`,
      async () => {
        const ref = document.getElementById('capOverride').value.trim();
        await api(`/capitalisation/${id}/approve`, { method: 'POST', json: { override_ref: ref || null } });
        flash('success', 'Capitalisation approved.');
      }, 'Approve');
    return;
  }

  if (t.dataset.alloc) {
    dialog('Allocate CWIP to a fixed asset',
      `<div class="field"><label for="aName">Asset name</label><input id="aName" value="Rolling Mill Line 1"></div>
       <div class="field"><label for="aCat">Asset category</label><input id="aCat" value="Plant &amp; Machinery"></div>
       <div class="field"><label for="aAmt">Amount (₹)</label><input id="aAmt" class="num" type="text" inputmode="decimal" value="1000000.00"></div>
       <div class="field check"><input type="checkbox" id="aWrite"><label for="aWrite">Record as write-off</label></div>
       <div class="field"><label for="aWhy">Write-off reason (required for a write-off)</label><input id="aWhy"></div>`,
      async () => {
        await api(`/capitalisation/${t.dataset.alloc}/allocate`, {
          method: 'POST', json: {
            asset_name: document.getElementById('aName').value,
            asset_category: document.getElementById('aCat').value,
            amount_rupees: rupees(document.getElementById('aAmt').value, 'Amount'),
            is_writeoff: document.getElementById('aWrite').checked,
            writeoff_reason: document.getElementById('aWhy').value || null,
          }
        });
        flash('success', 'Allocation recorded.');
      }, 'Allocate');
    return;
  }

  if (t.dataset.amend) {
    const pos = await api('/purchase-orders');
    const po = pos.find(p => p.po_id === t.dataset.amend);
    if (!po || !po.lines.length) { flash('error', 'That purchase order has no amendable lines.'); await render(); return; }
    dialog(`Amend ${po.po_number}`,
      `<div class="field"><label for="mLine">Line</label><select id="mLine">${po.lines.map(l =>
        `<option value="${esc(l.po_line_id)}">Line ${l.line_no} · ${esc(l.wbs_code)} · ${inr(l.amount_paise)}</option>`).join('')}</select></div>
       <div class="field"><label for="mAmt">New line value (₹)</label>
         <input id="mAmt" class="num" type="text" inputmode="decimal" value="${(po.lines[0].amount_paise / 100).toFixed(2)}"></div>
       <div class="field"><label for="mExc">Approved exception reference (only if the increase exceeds budget)</label>
         <input id="mExc" placeholder="Leave blank unless an exception has been approved"></div>
       ${msg('info', 'An increase is revalidated against the WBS budget before it is accepted. A decrease releases commitment immediately.')}`,
      async () => {
        const exc = document.getElementById('mExc').value.trim();
        try {
          await api(`/purchase-orders/${t.dataset.amend}/amend`, {
            method: 'POST', json: {
              po_line_id: document.getElementById('mLine').value,
              new_amount_rupees: rupees(document.getElementById('mAmt').value, 'New line value'),
              exception_ref: exc || null,
            }
          });
          flash('success', `${esc(po.po_number)} amended.`);
        } catch (e) {
          if (e instanceof ApiError && e.code === 'AMENDMENT_EXCEEDS_BUDGET') {
            // A refusal, not a failure to communicate. Say plainly that nothing changed.
            dialogMessage('error',
              `<strong>Amendment refused — it has NOT been applied.</strong><br>${esc(e.message)}` +
              `<br><span class="muted">${esc(po.po_number)} is unchanged. Raise a budget revision, or supply an approved exception reference above, then try again.</span>`);
            throw new SilentError();
          }
          throw e;
        }
      }, 'Amend');
    return;
  }

  if (t.dataset.cancel || t.dataset.close) {
    const id = t.dataset.cancel || t.dataset.close;
    const isCancel = !!t.dataset.cancel;
    dialog(isCancel ? 'Cancel purchase order' : 'Close purchase order',
      msg('warning', isCancel
        ? 'Cancelling releases the entire open commitment back to the WBS budget. This is recorded in the audit trail.'
        : 'Closing releases only the unbilled residual back to the WBS budget.')
      + `<div class="field"><label for="cReason">Reason</label><input id="cReason" placeholder="Recorded in the audit trail"></div>`,
      async () => {
        await api(`/purchase-orders/${id}/${isCancel ? 'cancel' : 'close'}`,
          { method: 'POST', json: { reason: document.getElementById('cReason').value || null } });
        flash('success', isCancel ? 'Purchase order cancelled.' : 'Purchase order closed.');
      },
      isCancel ? 'Cancel PO' : 'Close PO', { okClass: isCancel ? 'btn-danger' : 'btn-primary' });
    return;
  }
});

/* Thrown when the dialog has already rendered its own explanation. */
class SilentError extends Error { constructor() { super(''); this.name = 'SilentError'; } }

function approvingAs(extra) {
  const me = S.me || {};
  return msg('info',
    `Approving as <strong>${esc(me.name || me.user_id || 'the signed-in user')}</strong> (${esc((me.roles || []).join(', ') || 'no roles')}).`
    + (extra ? ` ${extra}` : ''));
}

document.addEventListener('change', async (ev) => {
  if (ev.target.id === 'prjSel') { S.project = ev.target.value; await render(); }
  if (ev.target.id === 'entitySel') { S.entity = ev.target.value; await render(); }
  if (ev.target.id === 'plantSel') { S.plant = ev.target.value; await render(); }
});

document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button');
  const id = btn?.id;
  if (!id) return;

  if (id === 'navToggle') { setNav(!(NARROW.matches ? navIsOpen() : !document.getElementById('nav').classList.contains('collapsed'))); return; }
  if (id === 'navScrim') { setNav(false); return; }
  if (id === 'signOutBtn') { await signOut(); return; }

  if (id === 'densityBtn') {
    const cosy = document.body.classList.toggle('cosy');
    document.getElementById('densityLabel').textContent = cosy ? 'Cosy' : 'Compact';
    btn.setAttribute('aria-pressed', String(cosy));
    return;
  }
  if (id === 'expandAll') {
    const all = n => { S.expanded.add(n.wbs_id); (n.children || []).forEach(all); };
    S.data.wbs.tree.forEach(all); await render(); return;
  }
  if (id === 'collapseAll') { S.expanded.clear(); await render(); return; }

  if (id === 'ckRun') {
    const out = document.getElementById('ckResult');
    let amount;
    try { amount = rupees(document.getElementById('ckAmt').value, 'Proposed value'); }
    catch (e) { out.innerHTML = msg('error', esc(e.message)); return; }
    out.innerHTML = '<div class="loading">Checking…</div>';
    let r;
    try {
      const head = document.getElementById('ckHead')?.value || null;
      r = await api('/budget-check', {
        method: 'POST', json: {
          wbs_id: document.getElementById('ckWbs').value,
          amount_rupees: amount,
          budget_head_id: head,
        }
      });
    } catch (e) {
      out.innerHTML = msg('error', esc(e.message), e.code || undefined);
      return;
    }
    const kind = r.severity === 'error' ? 'error' : r.severity === 'warning' ? 'warning' : 'success';
    out.innerHTML = msg(kind, esc(r.message), r.message_id)
      + (r.position ? `<div class="card"><h3>Position at ${esc(r.wbs_code || '')} (rolled up)</h3><div class="card-body"><div class="kv">
        <dt>Current approved budget</dt><dd>${inr(r.position.budget)}</dd>
        <dt>Open PO commitment</dt><dd>${inr(r.position.commitment)}</dd>
        <dt>Actual CWIP</dt><dd>${inr(r.position.actual)}</dd>
        <dt>Received not billed</dt><dd>${inr(r.position.received_not_billed)}</dd>
        <dt>Exposure</dt><dd>${inr(r.position.exposure)}</dd>
        <dt>Available before</dt><dd>${inr(r.available_paise)}</dd>
        <dt>Proposed</dt><dd>${inr(r.proposed_paise)}</dd>
        <dt>Available after</dt><dd class="${r.available_after_paise < 0 ? 'neg' : ''}">${inr(r.available_after_paise)}</dd>
      </div></div></div>` : '');
    enhance(out);
    return;
  }

  if (id === 'newPr') {
    const d = await api(`/projects/${S.project}/wbs`);
    const flat = []; const walk = n => { flat.push(n); (n.children || []).forEach(walk); }; d.tree.forEach(walk);
    const usable = flat.filter(n => n.allow_procurement);
    dialog('Create purchase request',
      (usable.length ? '' : msg('warning', 'No WBS element on this project currently allows procurement.'))
      + `<div class="field"><label for="pWbs">WBS element</label><select id="pWbs">${usable.map(n =>
        `<option value="${esc(n.wbs_id)}">${esc(n.wbs_code)} — ${esc(n.description)}</option>`).join('')}</select></div>
       <div class="field"><label for="pHead">Budget head</label><select id="pHead">${S.boot.budget_heads.map(h =>
        `<option value="${esc(h.budget_head_id)}">${esc(h.name)}</option>`).join('')}</select></div>
       <div class="field"><label for="pDesc">Description</label><input id="pDesc" value="New procurement request"></div>
       <div class="field"><label for="pAmt">Value (₹)</label><input id="pAmt" class="num" type="text" inputmode="decimal" value="500000.00"></div>
       ${msg('info', 'The budget availability check runs automatically on submission. An over-budget request is routed to exception approval rather than being blocked outright.')}`,
      async () => {
        const r = await api('/purchase-requests', {
          method: 'POST', json: {
            project_id: S.project, wbs_id: document.getElementById('pWbs').value,
            budget_head_id: document.getElementById('pHead').value,
            description: document.getElementById('pDesc').value,
            amount_rupees: rupees(document.getElementById('pAmt').value, 'Value'),
          }
        });
        if (r.check && r.check.verdict === 'EXCEEDS_BUDGET') {
          flash('warning', `${esc(r.pr_number || 'The request')} was created but exceeds available budget: ${esc(r.check.message)} It is routed to exception approval and has NOT been approved.`);
        } else {
          flash('success', `${esc(r.pr_number || 'Purchase request')} created.`);
        }
      }, 'Submit');
    return;
  }

  if (id === 'newRev') {
    const d = await api(`/projects/${S.project}/wbs`);
    const flat = []; const walk = n => { flat.push(n); (n.children || []).forEach(walk); }; d.tree.forEach(walk);
    dialog('Request budget revision',
      `<div class="field"><label for="rWbs">WBS element</label><select id="rWbs">${flat.map(n =>
        `<option value="${esc(n.wbs_id)}">${esc(n.wbs_code)} — ${esc(n.description)}</option>`).join('')}</select></div>
       <div class="field"><label for="rHead">Budget head</label><select id="rHead">${S.boot.budget_heads.map(h =>
        `<option value="${esc(h.budget_head_id)}">${esc(h.name)}</option>`).join('')}</select></div>
       <div class="field"><label for="rKind">Type</label><select id="rKind"><option>SUPPLEMENT</option><option>RETURN</option></select></div>
       <div class="field"><label for="rAmt">Amount (₹)</label><input id="rAmt" class="num" type="text" inputmode="decimal" value="500000.00"></div>
       <div class="field"><label for="rWhy">Justification</label><textarea id="rWhy" rows="3"></textarea></div>`,
      async () => {
        await api('/budget-revisions', {
          method: 'POST', json: {
            project_id: S.project, wbs_id: document.getElementById('rWbs').value,
            budget_head_id: document.getElementById('rHead').value,
            kind: document.getElementById('rKind').value,
            amount_rupees: rupees(document.getElementById('rAmt').value, 'Amount'),
            reason: document.getElementById('rWhy').value || 'Not stated',
          }
        });
        flash('success', 'Revision submitted for approval.');
      }, 'Submit for approval');
    return;
  }

  if (id === 'zAuth' || id === 'zAuthPartial') {
    const withhold = id === 'zAuthPartial' ? ['ERP.fixedasset.CREATE', 'ERP.accountants.CREATE'] : [];
    try {
      await api(`/zoho/${S.data.conn}/authorise`, { method: 'POST', json: { withhold } });
      flash('warning', 'Simulated OAuth authorisation recorded. No consent screen was shown and no token was issued by Zoho — this is MOCK state.');
    } catch (e) { flash('error', esc(e.message)); }
    await render(); return;
  }
  if (id === 'zRefresh') {
    try {
      await api(`/zoho/${S.data.conn}/refresh`, { method: 'POST' });
      flash('warning', 'Simulated token refresh recorded. No token exchange took place — this is MOCK state.');
    } catch (e) { flash('error', esc(e.message)); }
    await render(); return;
  }
  if (id === 'zTest') {
    const out = document.getElementById('zTestOut');
    out.innerHTML = '<div class="loading">Running module connectivity tests…</div>';
    let r;
    try { r = await api(`/zoho/${S.data.conn}/test`, { method: 'POST' }); }
    catch (e) { out.innerHTML = msg('error', esc(e.message), e.code || undefined); return; }
    const mode = r.mode || S.zohoMode?.mode || 'MOCK';
    out.innerHTML =
      msg('warning', `<strong>Mode: ${esc(mode)} — these results are NOT VERIFIED.</strong> ${esc(S.zohoMode?.note || 'No network call was made.')} Response times and record counts below are synthesised, not measured.`)
      + `<p>${r.passed} passed, ${r.failed} failed of ${r.tested} modules.</p>
      <div class="table-wrap"><table>
      <caption class="sr-only">Simulated module connectivity test results in ${esc(mode)} mode</caption>
      <thead><tr><th scope="col">Module</th><th scope="col">Endpoint</th><th scope="col">Method</th><th scope="col">Scope</th>
      <th scope="col">Result</th><th scope="col" class="num">ms (simulated)</th><th scope="col" class="num">Records (simulated)</th>
      <th scope="col">Error</th><th scope="col">Correlation</th></tr></thead>
      <tbody>${r.results.map(x => `<tr><th scope="row">${esc(x.module)}</th><td class="mono xs">${esc(x.endpoint || '—')}</td>
        <td>${esc(x.http_method || '—')}</td><td class="mono xs">${esc(x.scope || '—')}</td>
        <td>${status(x.result)}</td><td class="num">${esc(x.response_ms || '—')}</td><td class="num">${esc(x.record_count || '—')}</td>
        <td>${esc(x.error_message || '')}</td><td class="mono">${esc(x.correlation_id)}</td></tr>`).join('')}
      </tbody></table></div>`;
    enhance(out);
    return;
  }
});

/* ---------------- boot ----------------------------------------------------- */
async function start() {
  S.boot = await api('/bootstrap');
  S.me = S.boot.me;
  S.perms = new Set(S.boot.permissions || []);
  showShell();
  paintIdentity();

  const es = document.getElementById('entitySel');
  es.innerHTML = '<option value="">All entities</option>' +
    S.boot.entities.map(e => `<option value="${esc(e.entity_id)}">${esc(e.name)}</option>`).join('');
  const ps = document.getElementById('plantSel');
  ps.innerHTML = '<option value="">All plants</option>' +
    S.boot.plants.map(p => `<option value="${esc(p.plant_id)}">${esc(p.name)}</option>`).join('');

  if (S.boot.projects.length && !S.boot.projects.some(p => p.project_id === S.project)) {
    S.project = S.boot.projects[0].project_id;
  }
  S.expanded.add('W-02'); S.expanded.add('W-03'); S.expanded.add('W-02-01');

  const h = location.hash.slice(1);
  if (h && V[h]) S.view = h;
  setNav(!NARROW.matches);
  await render();
}

(async function () {
  // Mode is published on the public health endpoint, so the MOCK label is available
  // even before anyone signs in.
  try {
    const h = await api('/health');
    S.zohoMode = { mode: h.zoho_mode || 'MOCK', note: h.zoho_mode_note || '' };
  } catch { S.zohoMode = { mode: 'MOCK', note: '' }; }

  // Both events, because the two arrive from different directions and neither
  // covers the other: `hashchange` for a typed or pasted fragment, `popstate`
  // for Back and Forward across the entries render() now pushes. The `v !==
  // S.view` guard means whichever fires second is a no-op rather than a second
  // render of the same screen.
  const syncFromHash = async () => {
    const v = location.hash.slice(1);
    if (v && V[v] && v !== S.view && S.boot) { S.view = v; await render(); }
  };
  window.addEventListener('hashchange', syncFromHash);
  window.addEventListener('popstate', syncFromHash);

  if (getSession()) {
    try { await start(); }
    catch (e) {
      if (e instanceof ApiError && e.statusCode === 401) { /* showAuth already called */ }
      else { setSession(''); showAuth(e.message); }
    }
  } else {
    showAuth('');
  }
  document.body.dataset.ready = '1';   // capture tooling waits for this
})();
