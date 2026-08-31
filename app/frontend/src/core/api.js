/* app/frontend/src/core/api.js
   Fetch wrapper for the Audit Trail API (GET /api/audit/streams,
   /api/audit/entries, /api/audit/chain/verify). Handles correlation ids, RFC-7807
   problem+json error parsing and cursor paging.

   Contract (do not invent fields):
     GET /api/audit/streams
       -> {"items":[{"stream_key","entry_count","head_seq","last_at"}]}
     GET /api/audit/entries?stream_key=&object_type=&object_id=&cursor=&limit=
       -> {"items":[{"audit_id","stream_key","seq","at","actor","action",
                      "object_type","object_id","detail","correlation_id",
                      "entry_hash"}], "next_cursor", "has_more"}
     GET /api/audit/chain/verify?stream_key=
       -> {"stream_key","intact","entries_checked","first_break_seq","verified_at"}

   Session: app.js authenticates every /api call with a server-issued session
   held in sessionStorage (never localStorage) under 'capex.session_id' and
   sent as X-Session. This module reuses that same key so a tab that already
   signed in through the main shell keeps working here without a second
   login — but never creates or reads any other storage.
*/

const SESSION_KEY = 'capex.session_id';
const API_BASE = '/api/audit';

export class AuditApiError extends Error {
  /**
   * @param {string} message - safe to show a user directly.
   * @param {Object} [opts]
   * @param {number} [opts.status] - HTTP status, or 0 for a network failure.
   * @param {string|null} [opts.code]
   * @param {string|null} [opts.messageId]
   * @param {string|null} [opts.correlationId]
   * @param {'network'|'auth'|'notfound'|'validation'|'error'} [opts.kind] -
   *   lets callers pick a rendering without re-deriving it from `status`.
   *   'notfound' covers both a genuinely missing stream/object AND an
   *   out-of-scope read the server intentionally answers as 404 rather than
   *   403, per the UI contract's anti-oracle rule — the two must render
   *   identically, so this module deliberately does not expose a separate
   *   "forbidden" kind for reads.
   */
  constructor(message, { status = 0, code = null, messageId = null, correlationId = null, kind = 'error' } = {}) {
    super(message);
    this.name = 'AuditApiError';
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

async function get(path, params) {
  const correlationId = newCorrelationId();
  const headers = { Accept: 'application/json', 'X-Correlation-Id': correlationId };
  const sid = getSession();
  if (sid) headers['X-Session'] = sid;

  let response;
  try {
    response = await fetch(API_BASE + path + buildQuery(params), { method: 'GET', headers });
  } catch {
    throw new AuditApiError(
      'The audit service could not be reached. Check your connection and try again.',
      { status: 0, correlationId, kind: 'network' },
    );
  }

  if (response.status === 404) {
    const { detail, code, messageId } = await parseProblem(response);
    // Deliberately generic and identical whether the object never existed or
    // is simply out of this user's scope — a differently worded message here
    // would itself be the existence oracle the contract forbids.
    throw new AuditApiError(
      detail || 'No audit trail was found for these filters.',
      { status: 404, code, messageId, correlationId, kind: 'notfound' },
    );
  }

  if (response.status === 401) {
    const { detail, code, messageId } = await parseProblem(response);
    throw new AuditApiError(
      detail || 'Your session has ended. Sign in again to view the audit trail.',
      { status: 401, code, messageId, correlationId, kind: 'auth' },
    );
  }

  if (!response.ok) {
    const { detail, code, messageId } = await parseProblem(response);
    throw new AuditApiError(
      detail || `The audit service returned an unexpected error (HTTP ${response.status}).`,
      { status: response.status, code, messageId, correlationId, kind: response.status === 422 ? 'validation' : 'error' },
    );
  }

  const txt = await response.text();
  try {
    return txt ? JSON.parse(txt) : null;
  } catch {
    throw new AuditApiError(
      'The audit service returned a response that could not be understood.',
      { status: response.status, correlationId, kind: 'error' },
    );
  }
}

/** GET /api/audit/streams */
export function listStreams() {
  return get('/streams');
}

/**
 * GET /api/audit/entries
 * @param {Object} [filters]
 * @param {string} [filters.streamKey]
 * @param {string} [filters.objectType]
 * @param {string} [filters.objectId]
 * @param {string} [filters.cursor]
 * @param {number} [filters.limit]
 */
export function listEntries({ streamKey, objectType, objectId, cursor, limit } = {}) {
  return get('/entries', {
    stream_key: streamKey,
    object_type: objectType,
    object_id: objectId,
    cursor,
    limit,
  });
}

/** GET /api/audit/chain/verify?stream_key= */
export function verifyChain(streamKey) {
  return get('/verify', { stream_key: streamKey });
}
