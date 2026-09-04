/* app/frontend/src/features/approvals/approvals-api.js
   The approval engine's API surface, transcribed from Wave 4 Contract 3.

   This module is a CONTRACT SURFACE and nothing else. Every piece of transport
   behaviour — the X-Session header, the fresh X-Correlation-Id, RFC-7807
   parsing across both error envelopes, and the not-found-over-forbidden rule —
   lives once in core/api-client.js. This is the fourth feature to use that
   client and it deliberately adds no fetch, no header and no status handling
   of its own; the whole point of api-client.js was to stop the fourth copy
   from being written.

   Contract 3, frozen:

     GET  /api/approvals/inbox?state=&object_type=&cursor=&limit=
     POST /api/approvals/{instance_id}/decide
          {"action":"APPROVE|REJECT|RETURN", "reason_code", "reason_text",
           "idempotency_key": <required>, "object_version": <required>}
     POST /api/approvals/{instance_id}/recall     {"reason_text"}
     POST /api/approvals/{instance_id}/cancel     {"reason_text"}
     POST /api/approvals/{instance_id}/resubmit   {"reason_text"}
     GET  /api/approvals/{instance_id}
     GET  /api/approvals/{instance_id}/timeline
     GET  /api/approvals/definitions?object_type=&status=
     POST /api/approvals/definitions                (creates a DRAFT version)
     POST /api/approvals/definitions/{id}/activate
     POST /api/approvals/definitions/{id}/simulate  {"object": {...}}
     GET  /api/approvals/definitions/{id}/versions
     GET  /api/approvals/delegations
     POST /api/approvals/delegations  {"delegate_user_id","scope_key","from","to"}
     POST /api/approvals/delegations/{id}/revoke    {"reason_text"}
     GET  /api/approvals/sla?overdue=true

   app/backend/api/approvals.py is owned by stream 3. This module codes against
   the frozen routes above and nothing else; it does not import, inspect or
   assume anything about that module's internals.

   Idempotency (Contract 8). Every decision carries an `idempotency_key` and
   the `object_version` it was taken against. The key is minted ONCE per
   decision attempt and REUSED across retries of that same attempt — a key
   minted per HTTP call would make every retry a fresh decision, which is the
   precise failure the contract exists to prevent. `newIdempotencyKey()` is
   exported so a screen can mint one when the user opens a decision, and the
   screen holds it until that decision succeeds or is abandoned.
*/

import { ApiClientError, createApiClient, newCorrelationId } from '../../core/api-client.js';

/**
 * The approval family's error type. `err instanceof ApprovalApiError`
 * discriminates approval failures from budget, audit and settings failures,
 * while `err instanceof ApiClientError` still catches all four.
 */
export class ApprovalApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'ApprovalApiError';
  }
}

const client = createApiClient({
  basePath: '/api/approvals',
  ErrorClass: ApprovalApiError,
  messages: {
    network: 'The approval service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to continue reviewing approvals.',
    notfound: 'No approval records were found for these filters.',
    forbidden: 'You are not able to take this decision. It may be assigned to someone else, or you may be a contributor on this object.',
    conflict: 'This approval changed since it was loaded. Reload it before deciding again.',
    validation: 'The decision could not be accepted. Correct the highlighted fields and try again.',
    error: (status) => `The approval service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The approval service returned a response that could not be understood.',
  },
});

/**
 * A fresh idempotency key for ONE decision attempt.
 *
 * Reuses core/api-client.js's id generator rather than declaring a second
 * source of randomness: it already prefers crypto.randomUUID and already has a
 * documented fallback for a context where crypto is unavailable.
 */
export function newIdempotencyKey() {
  return newCorrelationId();
}

/* ---------------------------------------------------------------- inbox */

/**
 * GET /inbox — the caller's own assignments, within their scope.
 *
 * Contract 6: the server returns only instances inside the caller's scope AND
 * only assignments addressed to them unless they hold approval.configure. That
 * filtering is the server's, not this client's; nothing here narrows a result
 * set on the caller's behalf.
 */
export function listInbox({ state, objectType, cursor, limit } = {}) {
  return client.get('/inbox', {
    state, object_type: objectType, cursor, limit,
  });
}

/* ------------------------------------------------------- one instance */

export function getInstance(instanceId) {
  return client.get(`/${encodeURIComponent(instanceId)}`);
}

export function getTimeline(instanceId, { cursor, limit } = {}) {
  return client.get(`/${encodeURIComponent(instanceId)}/timeline`, { cursor, limit });
}

/**
 * POST /{instance_id}/decide.
 *
 * `idempotencyKey` and `objectVersion` are REQUIRED by Contract 3 and are
 * checked here rather than being allowed to reach the server as `undefined`.
 * A missing key would be rejected server-side with IDEMPOTENCY_KEY_REQUIRED,
 * but a decision screen that forgot to mint one is a client defect and should
 * fail loudly at the call site, not as a round trip.
 */
export function decide(instanceId, {
  action, reasonCode, reasonText, idempotencyKey, objectVersion,
}) {
  if (!idempotencyKey) {
    throw new ApprovalApiError('A decision cannot be sent without an idempotency key.', {
      code: 'IDEMPOTENCY_KEY_REQUIRED', kind: 'validation',
    });
  }
  if (objectVersion === undefined || objectVersion === null || objectVersion === '') {
    throw new ApprovalApiError('A decision cannot be sent without the object version it was taken against.', {
      code: 'OBJECT_VERSION_STALE', kind: 'validation',
    });
  }
  return client.post(`/${encodeURIComponent(instanceId)}/decide`, {
    action,
    reason_code: reasonCode || null,
    reason_text: reasonText || null,
    idempotency_key: idempotencyKey,
    object_version: objectVersion,
  });
}

export function recall(instanceId, reasonText) {
  return client.post(`/${encodeURIComponent(instanceId)}/recall`, { reason_text: reasonText || null });
}

export function cancel(instanceId, reasonText) {
  return client.post(`/${encodeURIComponent(instanceId)}/cancel`, { reason_text: reasonText || null });
}

export function resubmit(instanceId, reasonText) {
  return client.post(`/${encodeURIComponent(instanceId)}/resubmit`, { reason_text: reasonText || null });
}

/* --------------------------------------------------------- definitions */

export function listDefinitions({ objectType, status, cursor, limit } = {}) {
  return client.get('/definitions', {
    object_type: objectType, status, cursor, limit,
  });
}

export function createDefinition(body) {
  return client.post('/definitions', body);
}

export function activateDefinition(definitionId) {
  return client.post(`/definitions/${encodeURIComponent(definitionId)}/activate`, {});
}

export function listDefinitionVersions(definitionId, { cursor, limit } = {}) {
  return client.get(`/definitions/${encodeURIComponent(definitionId)}/versions`, { cursor, limit });
}

/**
 * POST /definitions/{id}/simulate — evaluate a candidate object against a
 * definition WITHOUT creating an instance.
 *
 * The simulator is the only honest way to answer "who would this route to?"
 * before an object is raised. It must show an unroutable object as
 * EXCEPTION_PENDING, because that is what the engine would do; a simulator
 * that quietly showed "no approval required" would teach a configurator that
 * a fail-closed defect is a fine outcome.
 */
export function simulate(definitionId, object) {
  return client.post(`/definitions/${encodeURIComponent(definitionId)}/simulate`, { object });
}

/* --------------------------------------------------------- delegations */

export function listDelegations({ cursor, limit } = {}) {
  return client.get('/delegations', { cursor, limit });
}

export function createDelegation({ delegateUserId, scopeKey, from, to }) {
  return client.post('/delegations', {
    delegate_user_id: delegateUserId,
    scope_key: scopeKey,
    from,
    to,
  });
}

export function revokeDelegation(delegationId, reasonText) {
  return client.post(`/delegations/${encodeURIComponent(delegationId)}/revoke`, {
    reason_text: reasonText || null,
  });
}

/* ----------------------------------------------------------- SLA / escalation */

export function listSla({ overdue = true, cursor, limit } = {}) {
  return client.get('/sla', { overdue: overdue ? 'true' : undefined, cursor, limit });
}
