/* app/frontend/src/features/procurement/internal-fulfilment-api.js
   The client for app/backend/api/internal_fulfilment.py (migration 034),
   mounted beside api/procurement.py under the same router floor
   (budget.read, authenticated) with a per-route permission on top. Same
   shape as procurement-api.js: a base path, an error class, this family's
   wording; the session header, correlation id, RFC-7807 parsing and
   not-found-over-forbidden rule live once in core/api-client.js.

     PUT  /api/procurement/purchase-requests/{pr_id}/lines/{pr_line_id}/fulfilment   fulfilment.decide
     GET  /api/procurement/purchase-requests/{pr_id}/fulfilment                       imr.read
     POST /api/procurement/internal-material-requests                                imr.create
     GET  /api/procurement/internal-material-requests                                imr.read
     GET  /api/procurement/internal-material-requests/{imr_id}                       imr.read
     POST .../{imr_id}/approve                                                       imr.approve
     PUT  .../{imr_id}/valuation                                                     imr.allocate
     POST .../{imr_id}/allocate                                                      imr.allocate
     POST .../{imr_id}/issue|/return|/consume|/transfer                              imr.issue
     POST .../{imr_id}/cancel                                                        imr.cancel

   Every request body here is the exact pydantic model the router declares
   (`extra="forbid"`): only the fields named in the route's own docstring in
   app/backend/api/internal_fulfilment.py are ever sent. Money leaves the
   browser as an integer number of paise (unit_rate_paise) — never a float —
   and quantities leave as the decimal STRING the caller typed, unparsed:
   the server is the only place a quantity is ever compared or summed.
*/

import { ApiClientError, createApiClient } from '../../core/api-client.js';
import { problemDetail } from './procurement-api.js';

export class InternalFulfilmentApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'InternalFulfilmentApiError';
  }
}

const client = createApiClient({
  basePath: '/api/procurement',
  ErrorClass: InternalFulfilmentApiError,
  messages: {
    network: 'The internal fulfilment service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to continue.',
    notfound: 'The internal fulfilment service did not find what this request named.',
    forbidden: 'Your role cannot do this. Contact an administrator if you need access.',
    error: (status) => `The internal fulfilment service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The internal fulfilment service returned a response that could not be understood.',
  },
});

/** The RFC-7807 refusal, verbatim — see procurement-api.js::problemDetail. */
export { problemDetail };

/** GET /api/procurement/purchase-requests/{pr_id}/fulfilment (imr.read) */
export function getFulfilment(prId) {
  return client.get(`/purchase-requests/${encodeURIComponent(prId)}/fulfilment`);
}

/**
 * PUT .../lines/{pr_line_id}/fulfilment (fulfilment.decide)
 * @param {string} prId
 * @param {string} prLineId
 * @param {Object} body - {mode, internal_quantity?, reason?, item_external_id?,
 *   item_description?, from_location_id?, to_location_id?, unit_rate_paise?,
 *   valuation_note?}
 */
export function putFulfilmentDecision(prId, prLineId, body) {
  return client.put(
    `/purchase-requests/${encodeURIComponent(prId)}/lines/${encodeURIComponent(prLineId)}/fulfilment`,
    body,
  );
}

/** POST /api/procurement/internal-material-requests (imr.create) */
export function createImr(body) {
  return client.post('/internal-material-requests', body);
}

/**
 * GET /api/procurement/internal-material-requests (imr.read)
 * @param {Object} [filters]
 * @param {string} [filters.projectId]
 * @param {string} [filters.prId]
 * @param {string} [filters.status]
 * @param {number} [filters.limit]
 */
export function listImrs({
  projectId, prId, status, limit,
} = {}) {
  return client.get('/internal-material-requests', {
    project_id: projectId, pr_id: prId, status, limit,
  });
}

/** GET /api/procurement/internal-material-requests/{imr_id} (imr.read) */
export function getImr(imrId) {
  return client.get(`/internal-material-requests/${encodeURIComponent(imrId)}`);
}

/**
 * POST .../{imr_id}/approve (imr.approve). 403 SELF_APPROVAL when the
 * signed-in user raised the request.
 * @param {string} imrId
 * @param {Object} [body] - {approved_quantity?, reason?, acting_for_user_id?, version_no?}
 */
export function approveImr(imrId, body = {}) {
  return client.post(`/internal-material-requests/${encodeURIComponent(imrId)}/approve`, body);
}

/**
 * PUT .../{imr_id}/valuation (imr.allocate). 409 VALUATION_FROZEN after
 * allocation.
 * @param {string} imrId
 * @param {Object} body - {unit_rate_paise (int), note (required), reference?, version_no?}
 */
export function setValuation(imrId, body) {
  return client.put(`/internal-material-requests/${encodeURIComponent(imrId)}/valuation`, body);
}

/**
 * POST .../{imr_id}/allocate (imr.allocate). 409 INTERNAL_VALUATION_MISSING,
 * 409 BUDGET_EXCEEDED.
 * @param {string} imrId
 * @param {Object} body - {idempotency_key, version_no?}
 */
export function allocateImr(imrId, body) {
  return client.post(`/internal-material-requests/${encodeURIComponent(imrId)}/allocate`, body);
}

/** POST .../{imr_id}/issue (imr.issue). 422 ISSUE_EXCEEDS_ALLOCATION. */
export function issueImr(imrId, body) {
  return client.post(`/internal-material-requests/${encodeURIComponent(imrId)}/issue`, body);
}

/** POST .../{imr_id}/return (imr.issue). 422 RETURN_EXCEEDS_ISSUED. */
export function returnImr(imrId, body) {
  return client.post(`/internal-material-requests/${encodeURIComponent(imrId)}/return`, body);
}

/** POST .../{imr_id}/consume (imr.issue). 422 CONSUME_EXCEEDS_ISSUED. */
export function consumeImr(imrId, body) {
  return client.post(`/internal-material-requests/${encodeURIComponent(imrId)}/consume`, body);
}

/**
 * POST .../{imr_id}/transfer (imr.issue). 422 TRANSFER_EXCEEDS_ALLOCATION,
 * 422 TRANSFER_SAME_STORE.
 * @param {string} imrId
 * @param {Object} body - {quantity, to_location_id (required), idempotency_key, reference?, note?}
 */
export function transferImr(imrId, body) {
  return client.post(`/internal-material-requests/${encodeURIComponent(imrId)}/transfer`, body);
}

/** POST .../{imr_id}/cancel (imr.cancel). 409 STOCK_IN_THE_FIELD. */
export function cancelImr(imrId, body) {
  return client.post(`/internal-material-requests/${encodeURIComponent(imrId)}/cancel`, body);
}
