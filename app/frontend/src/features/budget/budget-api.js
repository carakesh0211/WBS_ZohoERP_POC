/* app/frontend/src/features/budget/budget-api.js
   Fetch wrapper for the Budget API (SCR-09 Budget Planning Grid, SCR-10
   Budget Version Comparison, SCR-13 Budget Availability Check).

   The frozen contract is docs/WAVE2_CONTRACTS.md ("API contract — Budget").
   No field is invented here that is not in that contract.

     GET /api/budget/cells?project_id=&wbs_id=&budget_head_id=&cursor=&limit=
       -> {"items":[{"wbs_id","wbs_path","budget_head_id",
                     "budget_paise","original_paise","revisions_paise","future_budget_paise",
                     "ordered_paise","commitment_paise","actual_paise","received_paise",
                     "received_not_billed_paise","pr_reserved_paise",
                     "exposure_paise","available_paise","recomputed_at"}],
           "next_cursor","has_more"}

     GET /api/budget/availability?wbs_id=&budget_head_id=&amount_paise=
       -> {"wbs_id","budget_head_id","owning_wbs_id",
           "budget_paise","exposure_paise","available_paise","requested_paise",
           "verdict":"OK|WATCH|CRITICAL|EXCEEDS_BUDGET",
           "shortfall_paise","checked_at"}

     GET /api/budget/lines?project_id=&wbs_id=&version=
       -> {"items":[{"budget_line_id","wbs_id","budget_head_id",
                     "kind":"ORIGINAL|REVISION|TRANSFER","amount_paise",
                     "effective_from","effective_to","status","justification",
                     "created_at","created_by","version_no"}]}

     GET /api/budget/versions?project_id=
       -> {"items":[{"version","label","created_at","total_paise"}]}
     GET /api/budget/compare?project_id=&left=&right=
       -> {"rows":[{"wbs_id","budget_head_id","left_paise","right_paise","delta_paise"}]}

   This module mirrors the conventions already established by
   core/api.js (session header, X-Correlation-Id, RFC-7807 problem+json
   parsing, a 404 that is deliberately identical whether the object never
   existed or is simply out of the caller's scope — the anti-oracle rule from
   the UI contract). core/api.js itself is lead-owned and hard-wired to the
   audit API's base path, so it cannot be reused directly for a different API
   family; the pattern is duplicated here rather than the module, and is
   flagged to the lead as a candidate to factor into a shared, base-path
   parameterised core/api-client.js.

   Session: app.js authenticates every /api call with a server-issued session
   held in sessionStorage (never localStorage) under 'capex.session_id' and
   sent as X-Session. This module reuses that same key so a tab that already
   signed in through the main shell keeps working here without a second
   login — but never creates or reads any other storage.
*/

const SESSION_KEY = 'capex.session_id';
const API_BASE = '/api/budget';

export class BudgetApiError extends Error {
  /**
   * @param {string} message - safe to show a user directly.
   * @param {Object} [opts]
   * @param {number} [opts.status] - HTTP status, or 0 for a network failure.
   * @param {string|null} [opts.code]
   * @param {string|null} [opts.messageId]
   * @param {string|null} [opts.correlationId]
   * @param {'network'|'auth'|'notfound'|'validation'|'error'} [opts.kind] -
   *   lets callers pick a rendering without re-deriving it from `status`.
   *   'notfound' covers both a genuinely missing object AND an out-of-scope
   *   read the server intentionally answers as 404 rather than 403, per the
   *   UI contract's anti-oracle rule — a 403 on an id is an existence oracle.
   */
  constructor(message, { status = 0, code = null, messageId = null, correlationId = null, kind = 'error' } = {}) {
    super(message);
    this.name = 'BudgetApiError';
    this.status = status;
    this.code = code;
    this.messageId = messageId;
    this.correlationId = correlationId;
    this.kind = kind;
  }
}

function getSession() {
  try { return sessionStorage.getItem(SESSION_KEY) || ''; } catch { return ''; }
}

function newCorrelationId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
  } catch { /* fall through to the manual id below */ }
  return 'cid-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

function buildQuery(params) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue;
    q.set(key, value);
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

/** Parse an RFC-7807 problem+json body. Never throws. */
async function parseProblem(response) {
  let body = null;
  try {
    const txt = await response.text();
    body = txt ? JSON.parse(txt) : null;
  } catch {
    body = null;
  }
  const isObj = body && typeof body === 'object';
  return {
    detail: isObj
      ? ((body.detail && body.detail.message) || (typeof body.detail === 'string'
          ? body.detail : null) || body.title || null)
      : (typeof body === 'string' ? body : null),
    // Two envelopes exist in this API: the services.BusinessError handler
    // emits `code` at the top level, while a router raising HTTPException
    // produces `{detail: {code}}`. Reading only the first meant every
    // error from the Wave 2 routers arrived with a null code.
    code: isObj ? (body.code || (body.detail && body.detail.code) || null) : null,
    messageId: isObj ? (body.message_id || null) : null,
    raw: body,
  };
}

async function get(path, params) {
  const correlationId = newCorrelationId();
  const headers = { Accept: 'application/json', 'X-Correlation-Id': correlationId };
  const sid = getSession();
  if (sid) headers['X-Session'] = sid;

  let response;
  try {
    response = await fetch(API_BASE + path + buildQuery(params), { method: 'GET', headers });
  } catch {
    throw new BudgetApiError(
      'The budget service could not be reached. Check your connection and try again.',
      { status: 0, correlationId, kind: 'network' },
    );
  }

  if (response.status === 404) {
    const { detail, code, messageId } = await parseProblem(response);
    // Deliberately generic and identical whether the object never existed or
    // is simply out of this user's scope — a differently worded message here
    // would itself be the existence oracle the UI contract forbids.
    throw new BudgetApiError(
      detail || 'No budget data was found for these filters.',
      { status: 404, code, messageId, correlationId, kind: 'notfound' },
    );
  }

  if (response.status === 401) {
    const { detail, code, messageId } = await parseProblem(response);
    throw new BudgetApiError(
      detail || 'Your session has ended. Sign in again to view budget data.',
      { status: 401, code, messageId, correlationId, kind: 'auth' },
    );
  }

  if (!response.ok) {
    const { detail, code, messageId } = await parseProblem(response);
    throw new BudgetApiError(
      detail || `The budget service returned an unexpected error (HTTP ${response.status}).`,
      { status: response.status, code, messageId, correlationId, kind: response.status === 422 ? 'validation' : 'error' },
    );
  }

  const txt = await response.text();
  try {
    return txt ? JSON.parse(txt) : null;
  } catch {
    throw new BudgetApiError(
      'The budget service returned a response that could not be understood.',
      { status: response.status, correlationId, kind: 'error' },
    );
  }
}

/**
 * GET /api/budget/cells
 * @param {Object} [filters]
 * @param {string} [filters.projectId]
 * @param {string} [filters.wbsId]
 * @param {string} [filters.budgetHeadId]
 * @param {string} [filters.cursor]
 * @param {number} [filters.limit]
 */
export function listCells({ projectId, wbsId, budgetHeadId, cursor, limit } = {}) {
  return get('/cells', {
    project_id: projectId,
    wbs_id: wbsId,
    budget_head_id: budgetHeadId,
    cursor,
    limit,
  });
}

/**
 * GET /api/budget/availability
 * @param {Object} args
 * @param {string} args.wbsId
 * @param {string} args.budgetHeadId
 * @param {number} args.amountPaise
 */
export function checkAvailability({ wbsId, budgetHeadId, amountPaise }) {
  return get('/availability', {
    wbs_id: wbsId,
    budget_head_id: budgetHeadId,
    amount_paise: amountPaise,
  });
}

/**
 * GET /api/budget/lines
 * @param {Object} [filters]
 * @param {string} [filters.projectId]
 * @param {string} [filters.wbsId]
 * @param {string} [filters.version]
 */
export function listLines({ projectId, wbsId, version } = {}) {
  return get('/lines', { project_id: projectId, wbs_id: wbsId, version });
}

/** GET /api/budget/versions?project_id= (SCR-10 comparison) */
export function listVersions(projectId) {
  return get('/versions', { project_id: projectId });
}

/**
 * GET /api/budget/compare
 * @param {Object} args
 * @param {string} args.projectId
 * @param {string} args.left
 * @param {string} args.right
 */
export function compareVersions({ projectId, left, right }) {
  return get('/compare', { project_id: projectId, left, right });
}
