/* app/frontend/src/core/api.js
   Audit Trail API surface (SCR-28). Every mechanic — the X-Session header,
   X-Correlation-Id, RFC-7807 parsing across BOTH of this API's error
   envelopes, and the not-found-over-forbidden rule — now lives once in
   core/api-client.js. This module is only the contract: a base path, an error
   class and this family's wording.

   Contract (do not invent fields):
     GET /api/audit/streams
       -> {"items":[{"stream_key","entry_count","head_seq","last_at"}]}
     GET /api/audit/entries?stream_key=&object_type=&object_id=&action=&actor=&cursor=&limit=
       -> {"items":[{"audit_id","stream_key","seq","at","actor","action",
                      "object_type","object_id","detail","correlation_id",
                      "entry_hash"}], "next_cursor", "has_more"}
     GET /api/audit/chain/verify?stream_key=
       -> {"stream_key","intact","entries_checked","first_break_seq","verified_at"}

   Stream B (2026-09-13) added `action` and `actor` as exact-match query
   params on /entries, so the Administrator's deliberate self-approval
   override (action ADMIN_SELF_APPROVAL_OVERRIDE) can be found without
   scrolling the whole trail.

   Session: see core/api-client.js. sessionStorage only, never localStorage.
*/

import { ApiClientError, createApiClient } from './api-client.js';

export class AuditApiError extends ApiClientError {
  /**
   * @param {string} message - safe to show a user directly.
   * @param {Object} [opts] - see ApiClientError.
   *
   * `kind` is 'network' | 'auth' | 'notfound' | 'validation' | 'error'.
   * 'notfound' covers both a genuinely missing stream/object AND an
   * out-of-scope read the server intentionally answers as 404 rather than
   * 403, per the UI contract's anti-oracle rule — the two must render
   * identically, so this family deliberately never surfaces a separate
   * "forbidden" kind. Every audit endpoint is a GET, so the shared client's
   * not-found-over-forbidden rule maps a 403 here to 'notfound' too.
   */
  constructor(message, opts) {
    super(message, opts);
    this.name = 'AuditApiError';
  }
}

const client = createApiClient({
  basePath: '/api/audit',
  ErrorClass: AuditApiError,
  messages: {
    network: 'The audit service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to view the audit trail.',
    // Deliberately generic and identical whether the object never existed or
    // is simply out of this user's scope — a differently worded message here
    // would itself be the existence oracle the contract forbids.
    notfound: 'No audit trail was found for these filters.',
    error: (status) => `The audit service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The audit service returned a response that could not be understood.',
  },
});

/** GET /api/audit/streams */
export function listStreams() {
  return client.get('/streams');
}

/**
 * GET /api/audit/entries
 * @param {Object} [filters]
 * @param {string} [filters.streamKey]
 * @param {string} [filters.objectType]
 * @param {string} [filters.objectId]
 * @param {string} [filters.action] - exact match, e.g. ADMIN_SELF_APPROVAL_OVERRIDE
 * @param {string} [filters.actor] - exact match, a user id
 * @param {string} [filters.cursor]
 * @param {number} [filters.limit]
 */
export function listEntries({ streamKey, objectType, objectId, action, actor, cursor, limit } = {}) {
  return client.get('/entries', {
    stream_key: streamKey,
    object_type: objectType,
    object_id: objectId,
    action,
    actor,
    cursor,
    limit,
  });
}

/** GET /api/audit/chain/verify?stream_key= */
export function verifyChain(streamKey) {
  // '/chain/verify', not '/verify'. The backend moved this path so it stops
  // colliding with the legacy SQLite GET /api/audit/verify, which the
  // PostgreSQL router was shadowing. The base path supplies '/api/audit', so
  // only this segment changes.
  return client.get('/chain/verify', { stream_key: streamKey });
}
