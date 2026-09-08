/* app/frontend/src/features/closure/closure-api.js
   THE CONTRACT SURFACE for Wave 7's ten screens.

   Every path template below is reproduced CHARACTER FOR CHARACTER from
   app/backend/api/closure.py, because presence is decided by string equality
   against /openapi.json: a route that serves perfectly well under
   `{capId}` is reported ABSENT by a screen asking for `{cap_id}`, and the
   screen then renders an unavailable state over a working endpoint. Wave 5
   learned that the expensive way and wrote it into
   features/integration/integration-api.js's header; this file inherits both
   the lesson and the machinery.

   WHAT IS REUSED, AND WHY IT IS NOT COPIED
   ----------------------------------------
   `probeMountedPaths`, `available`, `classifyMissing`, `classifyUnavailable`,
   `unavailableNoteFrom`, `scrub` and `EndpointUnavailableError` come from
   features/integration/integration-api.js unchanged. That module states the
   reasoning for each at length and is exercised by 2,078 lines of VRT. A
   second implementation of "is this route mounted?" is a second answer that
   can disagree with the first, and the probe cache would be duplicated too --
   two fetches of /openapi.json per screen, and two chances to classify the
   same 503 differently.

   THE THREE STATES THIS MODULE KEEPS APART
   ----------------------------------------
     * a successful EMPTY response      -> the screen renders `empty`;
     * a route that is NOT MOUNTED, or that is mounted and answers 503 with
       `unavailable: true`  -> `EndpointUnavailableError`, and the screen
       renders `unavailable`, naming what is missing;
     * anything else                    -> the shared client's error, and the
       screen renders `error` or `permission`.

   Collapsing the first two is the defect this whole shape exists to prevent.
   "No capitalisation requests were found" and "this build has no
   capitalisation endpoint" are different facts, and only one of them means the
   reader can stop worrying.

   MONEY NEVER BECOMES A NUMBER HERE. Every `*_paise` value travels as the
   integer the server sent and is formatted by core/format.js's formatINR at
   render time. No arithmetic on money happens in this module or in any screen
   that uses it: a JavaScript float is not a safe container for rupees, and a
   total computed in the browser is a total nobody can reconcile.
*/

import { createApiClient } from '../../core/api-client.js';
import {
  EndpointUnavailableError, IntegrationApiError, available,
  classifyMissing, classifyUnavailable, scrub, unavailableNoteFrom,
} from '../integration/integration-api.js';

export { EndpointUnavailableError };

const MESSAGES = {
  network: 'The closure service could not be reached. Check your connection and try again.',
  auth: 'Your session has ended. Sign in again to continue.',
  notfound: 'No closure records were found for these filters.',
  forbidden: 'You do not have permission to do this. Approving a capitalisation '
    + 'requires the capitalisation.approve permission.',
  conflict: 'This record changed since it was loaded. Reload it before saving again.',
  validation: 'The submitted values could not be accepted. Correct the highlighted fields and try again.',
  error: (status) => `The closure service returned an unexpected error (HTTP ${status}).`,
  unreadable: 'The closure service returned a response that could not be understood.',
};

/* Two clients over the ONE shared implementation. `/api/closure` is the
   closure chain; `/api/control` is the three read-only control views SCR-11,
   SCR-12 and SCR-14 need. Naming them separately keeps a stack trace able to
   say which surface answered. */
const closure = createApiClient({
  basePath: '/api/closure', ErrorClass: IntegrationApiError, messages: MESSAGES,
});
const control = createApiClient({
  basePath: '/api/control', ErrorClass: IntegrationApiError, messages: MESSAGES,
});

/** The path templates, exactly as FastAPI declares them. */
export const TEMPLATES = {
  position: '/api/closure/projects/{project_id}/position',
  reviews: '/api/closure/reviews',
  reviewSubmit: '/api/closure/reviews/{review_id}/submit',
  reviewDecide: '/api/closure/reviews/{review_id}/decide',
  requests: '/api/closure/requests',
  request: '/api/closure/requests/{cap_id}',
  requestSubmit: '/api/closure/requests/{cap_id}/submit',
  requestApprove: '/api/closure/requests/{cap_id}/approve',
  requestReject: '/api/closure/requests/{cap_id}/reject',
  allocations: '/api/closure/requests/{cap_id}/allocations',
  budgetRevisions: '/api/control/budget-revisions',
  budgetTransfers: '/api/control/budget-transfers',
  purchaseRequests: '/api/control/purchase-requests',
};

const CLOSURE_NOTE = 'This build does not mount the closure API. It arrives with '
  + 'migration 018_closure.sql and app/backend/api/closure.py; until then this '
  + 'screen has nothing to read and says so rather than showing an empty list.';

/**
 * Run one call, but only if this build mounts its route.
 *
 * @param {string} template - checked against the OpenAPI probe.
 * @param {Function} call - invoked only when the route is present, or when
 *   presence is UNKNOWN (in which case a FastAPI-shaped 404 is treated as
 *   absent after the fact).
 * @param {string} [note] - what an operator can do about it, rendered verbatim.
 * @returns {Promise<{data:*, template:string, source:string, redacted:string[]}>}
 * @throws {EndpointUnavailableError} when the route is not mounted, or is
 *   mounted and declares it cannot answer.
 */
async function callIfMounted(template, call, note = CLOSURE_NOTE) {
  const present = await available(template);
  if (present === false) throw new EndpointUnavailableError(template, note);
  let raw;
  try {
    raw = await call();
  } catch (err) {
    // MOUNTED, AND IT SAYS IT CANNOT ANSWER. Indistinguishable from absent for
    // a reader, so it is handled the same way -- and the SERVER's own reason is
    // preferred over this module's, because it knows which half is missing.
    if (classifyUnavailable(err)) {
      throw new EndpointUnavailableError(
        template, unavailableNoteFrom(err) || note);
    }
    // Presence UNKNOWN plus a FastAPI-shaped 404 means the route is absent
    // after all, rather than an empty result.
    if (present === null && classifyMissing(err)) {
      throw new EndpointUnavailableError(template, note);
    }
    throw err;
  }
  // A backstop, not a substitute for the backend rule: a screen that receives
  // `redacted: [...]` renders a visible warning rather than quietly dropping
  // the evidence that a server leaked a secret.
  const { value, redacted } = scrub(raw);
  return { data: value, template, source: 'wave7-closure', redacted };
}

/* ------------------------------------------------------------------ *
 * SCR-20 / SCR-21: position and blockers
 * ------------------------------------------------------------------ */

/** The CWIP position and the blocker list for one project. */
export function getPosition(projectId, capId) {
  return callIfMounted(
    TEMPLATES.position,
    () => closure.get(`/projects/${encodeURIComponent(projectId)}/position`,
                      capId ? { cap_id: capId } : undefined),
  );
}

/* ------------------------------------------------------------------ *
 * SCR-20: completion reviews
 * ------------------------------------------------------------------ */

export function listReviews(params) {
  return callIfMounted(TEMPLATES.reviews, () => closure.get('/reviews', params));
}

export function createReview(body) {
  return callIfMounted(TEMPLATES.reviews, () => closure.post('/reviews', body));
}

export function submitReview(reviewId, body) {
  return callIfMounted(
    TEMPLATES.reviewSubmit,
    () => closure.post(`/reviews/${encodeURIComponent(reviewId)}/submit`, body),
  );
}

export function decideReview(reviewId, body) {
  return callIfMounted(
    TEMPLATES.reviewDecide,
    () => closure.post(`/reviews/${encodeURIComponent(reviewId)}/decide`, body),
  );
}

/* ------------------------------------------------------------------ *
 * SCR-21: capitalisation requests
 * ------------------------------------------------------------------ */

export function listRequests(params) {
  return callIfMounted(TEMPLATES.requests, () => closure.get('/requests', params));
}

export function createRequest(body) {
  return callIfMounted(TEMPLATES.requests, () => closure.post('/requests', body));
}

export function submitRequest(capId) {
  return callIfMounted(
    TEMPLATES.requestSubmit,
    () => closure.post(`/requests/${encodeURIComponent(capId)}/submit`, {}),
  );
}

export function approveRequest(capId, body) {
  return callIfMounted(
    TEMPLATES.requestApprove,
    () => closure.post(`/requests/${encodeURIComponent(capId)}/approve`, body),
  );
}

export function rejectRequest(capId, body) {
  return callIfMounted(
    TEMPLATES.requestReject,
    () => closure.post(`/requests/${encodeURIComponent(capId)}/reject`, body),
  );
}

/* ------------------------------------------------------------------ *
 * SCR-22: asset allocations
 * ------------------------------------------------------------------ */

export function listAllocations(capId) {
  return callIfMounted(
    TEMPLATES.allocations,
    () => closure.get(`/requests/${encodeURIComponent(capId)}/allocations`),
  );
}

export function createAllocation(capId, body) {
  return callIfMounted(
    TEMPLATES.allocations,
    () => closure.post(`/requests/${encodeURIComponent(capId)}/allocations`, body),
  );
}

/* ------------------------------------------------------------------ *
 * SCR-11 / SCR-12 / SCR-14: the read-only control views
 * ------------------------------------------------------------------ */

const CONTROL_NOTE = 'This build does not mount the read-only control views. '
  + 'They arrive with app/backend/api/closure.py; the write side of these '
  + 'documents lives on /api/budget/* and /api/procurement/* and is unaffected.';

export function listBudgetRevisions(params) {
  return callIfMounted(
    TEMPLATES.budgetRevisions,
    () => control.get('/budget-revisions', params), CONTROL_NOTE);
}

export function listBudgetTransfers(params) {
  return callIfMounted(
    TEMPLATES.budgetTransfers,
    () => control.get('/budget-transfers', params), CONTROL_NOTE);
}

export function listPurchaseRequests(params) {
  return callIfMounted(
    TEMPLATES.purchaseRequests,
    () => control.get('/purchase-requests', params), CONTROL_NOTE);
}
