/* app/frontend/src/features/settings/api.js
   Fetch wrapper for the Wave 2 "Settings and masters" API — see
   docs/WAVE2_CONTRACTS.md ("API contract — Settings and masters", stream 4
   owns / stream 5 consumes). This module is the settings-feature equivalent
   of core/api.js (which is read-only, lead-owned, and scoped to the Audit
   Trail's GET-only endpoints) — it adds POST/PUT and the settings + masters
   base paths this screen needs, while reusing the exact same conventions:
   RFC-7807 problem+json parsing, X-Correlation-Id, X-Session from
   sessionStorage, and cursor paging.

   Contract (do not invent fields):
     {collection} in organisations | entities | divisions | branches | zones
                    | plants | locations | departments

     GET  /api/settings/{collection}?cursor=&limit=&q=&is_active=
       -> {"items":[{ "<id field>", "code","name", ...,
                       "is_active","created_at","created_by",
                       "updated_at","updated_by","version_no"}],
           "next_cursor","has_more"}
     POST /api/settings/{collection}                 -> the created row
     PUT  /api/settings/{collection}/{id}             -> the updated row
          body MUST carry version_no; mismatch -> 409 VERSION_CONFLICT
     POST /api/settings/{collection}/{id}/deactivate  -> {"id","is_active":false}

     GET  /api/masters/items | /api/masters/vendors
          ?cursor=&limit=&q=&source=&mapping_status=&is_active=
       -> {"items":[{ "item_id"|"vendor_id","code","name",
                       "source":"LOCAL|IMPORT|ZOHO",
                       "external_source","external_id","external_last_modified",
                       "source_of_truth_status","duplicate_of","mapping_status",
                       "is_active","version_no", ...}],
           "next_cursor","has_more"}
     POST /api/masters/{kind}                  source forced to LOCAL
     PUT  /api/masters/{kind}/{id}              refuses to edit a ZOHO-owned field
     POST /api/masters/{kind}/{id}/deactivate
     GET  /api/masters/{kind}/duplicates
       -> {"items":[{"id","code","name","duplicate_of","reason"}]}

   Vendor tax identity (gst_no, pan_no) arrives masked from the server by
   contract; this module never attempts to reconstruct, mask or unmask any
   value — whatever the server sends is rendered exactly as sent.
*/

const SESSION_KEY = 'capex.session_id';

export class SettingsApiError extends Error {
  /**
   * @param {string} message - safe to show a user directly.
   * @param {Object} [opts]
   * @param {number} [opts.status] - HTTP status, or 0 for a network failure.
   * @param {string|null} [opts.code]
   * @param {string|null} [opts.messageId]
   * @param {string|null} [opts.correlationId]
   * @param {'network'|'auth'|'notfound'|'forbidden'|'validation'|'conflict'|'error'} [opts.kind]
   */
  constructor(message, {
    status = 0, code = null, messageId = null, correlationId = null, kind = 'error',
  } = {}) {
    super(message);
    this.name = 'SettingsApiError';
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
    detail: isObj ? (body.detail || body.title || null) : (typeof body === 'string' ? body : null),
    code: isObj ? (body.code || null) : null,
    messageId: isObj ? (body.message_id || null) : null,
    raw: body,
  };
}

/**
 * @param {number} status
 * @param {string|null} code
 * @param {string} method - a 403 on a GET is treated identically to a 404
 *   (out-of-scope read renders not-found; a differently worded 403 would
 *   itself be the existence oracle the UI contract forbids). A 403 on a
 *   write is a distinct 'forbidden' kind — there is no id-oracle concern
 *   for an action, and the user needs to know it was a permission refusal,
 *   not a missing record.
 */
function classifyStatus(status, code, method) {
  if (status === 404) return 'notfound';
  if (status === 403) return method === 'GET' ? 'notfound' : 'forbidden';
  if (status === 401) return 'auth';
  if (status === 409 || code === 'VERSION_CONFLICT') return 'conflict';
  if (status === 422) return 'validation';
  return 'error';
}

async function request(method, path, { params, body } = {}) {
  const correlationId = newCorrelationId();
  const headers = { Accept: 'application/json', 'X-Correlation-Id': correlationId };
  const sid = getSession();
  if (sid) headers['X-Session'] = sid;

  const init = { method, headers };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path + buildQuery(params), init);
  } catch {
    throw new SettingsApiError(
      'The settings service could not be reached. Check your connection and try again.',
      { status: 0, correlationId, kind: 'network' },
    );
  }

  if (!response.ok) {
    const { detail, code, messageId } = await parseProblem(response);
    const kind = classifyStatus(response.status, code, method);
    const fallback = {
      notfound: 'The requested record was not found.',
      forbidden: 'You do not have permission to make this change. Contact an administrator if you need access.',
      auth: 'Your session has ended. Sign in again to continue.',
      conflict: 'Someone else changed this record since it was loaded. Reload to see the latest version before trying again.',
      validation: 'The submitted values could not be accepted. Correct the highlighted fields and try again.',
      error: `The settings service returned an unexpected error (HTTP ${response.status}).`,
    }[kind];
    throw new SettingsApiError(detail || fallback, {
      status: response.status, code, messageId, correlationId, kind,
    });
  }

  const txt = await response.text();
  try {
    return txt ? JSON.parse(txt) : null;
  } catch {
    throw new SettingsApiError(
      'The settings service returned a response that could not be understood.',
      { status: response.status, correlationId, kind: 'error' },
    );
  }
}

/* ---------------- /api/settings/{collection} ---------------- */

export function listSettings(collection, { cursor, limit, q, isActive } = {}) {
  return request('GET', `/api/settings/${collection}`, {
    params: { cursor, limit, q, is_active: isActive },
  });
}

export function createSetting(collection, body) {
  return request('POST', `/api/settings/${collection}`, { body });
}

export function updateSetting(collection, id, body) {
  return request('PUT', `/api/settings/${collection}/${encodeURIComponent(id)}`, { body });
}

export function deactivateSetting(collection, id) {
  return request('POST', `/api/settings/${collection}/${encodeURIComponent(id)}/deactivate`, { body: {} });
}

/* ---------------- /api/masters/{kind} ---------------- */

export function listMasters(kind, {
  cursor, limit, q, source, mappingStatus, isActive,
} = {}) {
  return request('GET', `/api/masters/${kind}`, {
    params: {
      cursor, limit, q, source, mapping_status: mappingStatus, is_active: isActive,
    },
  });
}

export function createMaster(kind, body) {
  return request('POST', `/api/masters/${kind}`, { body });
}

export function updateMaster(kind, id, body) {
  return request('PUT', `/api/masters/${kind}/${encodeURIComponent(id)}`, { body });
}

export function deactivateMaster(kind, id) {
  return request('POST', `/api/masters/${kind}/${encodeURIComponent(id)}/deactivate`, { body: {} });
}

export function listDuplicates(kind) {
  return request('GET', `/api/masters/${kind}/duplicates`);
}
