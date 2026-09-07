/* app/frontend/src/features/integration/integration-api.js
   The integration platform's API surface for SCR-31, 32, 33, 34, 26, 38 and 39.

   This module is a CONTRACT SURFACE. Every piece of transport behaviour — the
   X-Session header, the fresh X-Correlation-Id, RFC-7807 parsing across both
   error envelopes, and the not-found-over-forbidden rule — lives once in
   core/api-client.js. This is the fifth feature to use that client and it adds
   no fetch, no header and no status handling of its own.

   Three things ARE added here, because none of them is transport and all three
   are load-bearing for honesty.

   1. ENDPOINT PRESENCE, PROBED RATHER THAN GUESSED
   ------------------------------------------------
   Wave 5's `/api/integrations/*` group (§13) is being built by streams 1–6
   while these screens are being built. A route that does not exist yet answers
   404 — and core/api-client.js correctly collapses a 404 on a read into
   `notfound`, which the shared state host renders as "no records were found".
   That sentence would be a LIE on a screen whose endpoint has not shipped:
   there is a world of difference between "the dead-letter queue is empty" and
   "this build has no dead-letter endpoint", and only one of them means the
   operator can stop worrying.

   So presence is established from the application's own OpenAPI document,
   which FastAPI serves at /openapi.json and which lists every route this build
   actually mounts. One fetch, cached for the life of the page. `available()`
   then answers a yes/no about a specific path template, and a screen that gets
   `no` renders an UNAVAILABLE state naming the exact route, rather than an
   empty state describing data that was never asked for.

   If the probe itself cannot be read (a deployment that hides the schema, a
   proxy that rewrites it) the module degrades to a narrower but still honest
   heuristic — see `classifyMissing()` — and NEVER silently assumes presence.

   2. THE COMPATIBILITY SURFACE, NAMED ON SCREEN
   ---------------------------------------------
   `/api/zoho/*` is the Wave-4 connector surface. It is real, it is mounted
   today, and it carries a genuine subset of what these screens need
   (connection profiles, required scopes, hard negatives, the event log,
   connectivity results, token health). Where a Wave-5 route is absent and a
   Wave-4 route answers the same question, the screen uses it AND SAYS SO in a
   visible source line. A silent fallback would be the same dishonesty as the
   empty state above, one layer down: the operator would read Wave-4 mock data
   as though the Wave-5 platform were live.

   Every call therefore returns `{ data, source }`, where `source` is one of
   'wave5' | 'wave4-compat' | 'unavailable', and no screen is allowed to render
   data without rendering its source.

   3. THE CLIENT SECRET NEVER REACHES A RENDERER (REQ-INT-024)
   -----------------------------------------------------------
   REQ-INT-024: the client secret is never returned by any API, never in the
   DOM, never logged. Two independent measures, because one of them is a
   promise about somebody else's code:

     * NOTHING HERE EVER SENDS ONE. `createConnection()` accepts a fixed field
       list and builds its request body from that list alone, so a caller that
       passes `client_secret` cannot smuggle it into the wire format. SCR-32
       has no secret input at all: the secret is installed server-side, and the
       screen configures a connection without it.

     * `scrub()` removes any secret-shaped key from EVERY response before it is
       returned, and reports that it did. That is a backstop against a future
       backend regression, not a substitute for the backend rule — but it is
       the half this stream can actually enforce, and a screen that receives
       `redacted: ['client_secret']` renders a visible warning rather than
       quietly dropping the evidence that a server leaked a secret.

   tests/vrt/integration.spec.js proves all three properties against the
   running application.
*/

import { ApiClientError, createApiClient } from '../../core/api-client.js';

/**
 * The integration family's error type. `err instanceof IntegrationApiError`
 * discriminates integration failures from approval, budget, audit and settings
 * failures, while `err instanceof ApiClientError` still catches all of them.
 */
export class IntegrationApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'IntegrationApiError';
  }
}

/**
 * Raised INSTEAD of a request when the route this screen needs is not mounted
 * in this build. It is deliberately NOT an ApiClientError: it is not a failure
 * of a call, it is the absence of one, and a screen must render it differently
 * from both an empty result and a server error.
 */
export class EndpointUnavailableError extends Error {
  /**
   * @param {string} path - the route template, e.g. '/api/integrations/events'.
   * @param {string} [note] - what the operator can do about it.
   */
  constructor(path, note) {
    super(`This build does not mount ${path}.`);
    this.name = 'EndpointUnavailableError';
    this.path = path;
    this.note = note || '';
  }
}

const MESSAGES = {
  network: 'The integration service could not be reached. Check your connection and try again.',
  auth: 'Your session has ended. Sign in again to continue.',
  notfound: 'No integration records were found for these filters.',
  forbidden: 'You do not have permission to do this. Managing a connection requires the connector.manage permission.',
  conflict: 'This connection changed since it was loaded. Reload it before saving again.',
  validation: 'The submitted values could not be accepted. Correct the highlighted fields and try again.',
  error: (status) => `The integration service returned an unexpected error (HTTP ${status}).`,
  unreadable: 'The integration service returned a response that could not be understood.',
};

/* Two clients over the ONE shared implementation, because the two surfaces are
   different families with different base paths and must stay tellable apart in
   a stack trace. Neither adds transport behaviour. */
const wave5 = createApiClient({
  basePath: '/api/integrations', ErrorClass: IntegrationApiError, messages: MESSAGES,
});
const wave4 = createApiClient({
  basePath: '/api/zoho', ErrorClass: IntegrationApiError, messages: MESSAGES,
});
/* A THIRD base path, for the Wave 2/3 TRANSACTION surface — /api/purchase-orders,
   /api/grns, /api/bills, /api/reconciliation. It is not the connector surface
   and it is not the integration platform: it is this application's own ledger,
   which is exactly what the three transaction-queue screens and the two
   reconciliation screens fall back to when the Wave 5 inbox/outbox routes are
   absent. Naming it separately keeps a stack trace able to say which of the
   three surfaces answered. */
const ledger = createApiClient({
  basePath: '/api', ErrorClass: IntegrationApiError, messages: MESSAGES,
});

/* ------------------------------------------------------------------ *
 * 1. Endpoint presence
 * ------------------------------------------------------------------ */

/** Cached OpenAPI path set: Set<string> once read, null while unread. */
let mountedPaths = null;
let probePromise = null;
let probeFailed = false;

/**
 * Read the paths this build actually mounts.
 *
 * Deliberately NOT routed through createApiClient: /openapi.json is not part
 * of either API family, carries no session requirement, and a failure here is
 * not a user-facing error — it degrades the module to `classifyMissing()`
 * rather than failing a screen.
 *
 * @returns {Promise<Set<string>|null>} null when the schema could not be read.
 */
export function probeMountedPaths() {
  if (mountedPaths || probeFailed) return Promise.resolve(mountedPaths);
  if (probePromise) return probePromise;
  probePromise = (async () => {
    try {
      const response = await fetch('/openapi.json', { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(String(response.status));
      const schema = await response.json();
      const paths = schema && schema.paths;
      if (!paths || typeof paths !== 'object') throw new Error('no paths');
      mountedPaths = new Set(Object.keys(paths));
    } catch {
      probeFailed = true;
      mountedPaths = null;
    }
    return mountedPaths;
  })();
  return probePromise;
}

/** Test seam: forget the probe so a spec can re-run it against a new stub. */
export function resetProbe() {
  mountedPaths = null; probePromise = null; probeFailed = false;
}

/**
 * Is `template` mounted in this build?
 *
 * @param {string} template - an OpenAPI path template exactly as FastAPI
 *   declares it, e.g. '/api/integrations/connections/{connection_id}'.
 * @returns {Promise<boolean|null>} null when presence is UNKNOWN because the
 *   schema could not be read. Unknown is returned as unknown — never coerced
 *   to true, which would make a screen claim a route exists, and never to
 *   false, which would make it claim one is missing.
 */
export async function available(template) {
  const paths = await probeMountedPaths();
  if (!paths) return null;
  return paths.has(template);
}

/**
 * The narrower fallback used when the schema could not be read.
 *
 * FastAPI answers an unmounted path with `{"detail": "Not Found"}` and no
 * `code`; every 404 this application raises deliberately carries a `code`
 * (CONNECTION_NOT_FOUND, MODULE_UNKNOWN, …) or a message of its own. So a
 * code-less 404 whose detail is exactly FastAPI's default is treated as an
 * absent route, and anything else is treated as a real refusal or a real miss.
 *
 * This is a heuristic and is labelled as one. It is only ever reached when the
 * authoritative probe above is unavailable.
 *
 * @returns {boolean} true when the error looks like an unmounted route.
 */
export function classifyMissing(err) {
  if (!err || err.status !== 404) return false;
  if (err.code) return false;
  return String(err.message || '').trim().toLowerCase() === 'not found';
}

/**
 * The route is mounted and has DECLARED it cannot answer.
 *
 * `api/integrations.py` answers six operations with a 503 carrying
 * `unavailable: true` plus a `missing` noun, and it chose 503 over 404
 * deliberately — a 404 is collapsed by the shared client into "no records were
 * found", which on a route that cannot answer is a lie about data rather than
 * a statement about the build.
 *
 * Until this existed, `firstAvailable` recognised only `classifyMissing`'s
 * 404, so every one of those six deliberate refusals reached the screen as
 * "The integration service returned an unexpected error (HTTP 503)". The
 * backend avoided 404 because this module mishandled it, and this module
 * mishandled what the backend chose instead.
 *
 * THE TEST IS THE FLAG, NOT THE STATUS. A 503 from a proxy, a cold start or a
 * crashed upstream is a real fault and must keep rendering as one. Treating
 * every 503 as "unavailable" would trade one dishonest screen for another —
 * a permanently calm "not built yet" over an integration that had broken.
 *
 * @returns {boolean} true when the server declared the capability absent.
 */
export function classifyUnavailable(err) {
  if (!err || err.status !== 503) return false;
  const body = err.body;
  const detail = body && typeof body === 'object' ? body.detail : null;
  return !!(detail && typeof detail === 'object' && detail.unavailable === true);
}

/**
 * What the server said is missing, for the screen to render verbatim.
 *
 * Preferred over this module's own note because the server knows which half is
 * absent and this module does not: "Zoho's side of the comparison" and "no
 * database is configured for this process" are different problems with
 * different remedies, and a generic "Wave 5 is building this endpoint" would
 * flatten both into a wait.
 */
export function unavailableNoteFrom(err) {
  const detail = err && err.body && typeof err.body === 'object' ? err.body.detail : null;
  if (!detail || typeof detail !== 'object') return '';
  const parts = [detail.missing, detail.remedy].filter(
    (x) => typeof x === 'string' && x.trim(),
  );
  return parts.join(' ');
}

/* ------------------------------------------------------------------ *
 * 2. Secret redaction (REQ-INT-024)
 * ------------------------------------------------------------------ */

/**
 * Key names that must never reach a renderer. Matched case-insensitively
 * against the whole key, and as a substring, so `zoho_client_secret` and
 * `secretRef` are both caught.
 */
const SECRET_KEY = /(secret|refresh_token|access_token|password|private_key|credential_value)/i;

/**
 * A key that NAMES a secret without carrying one. `client_secret_present` is
 * exactly what SCR-32 needs — whether the operator has installed the secret
 * server-side — and redacting it would remove the screen's only honest signal.
 * The allow-list is explicit and short on purpose: anything not on it is
 * redacted, so a new leaking field fails closed.
 */
const SECRET_METADATA = new Set([
  'client_secret_present', 'client_secret_ref', 'client_secret_updated_at',
  'access_token_expires_at', 'access_token_expiry', 'refresh_token_present',
  'refresh_token_ref', 'token_last_refreshed',
]);

/**
 * Deep-copy `value`, removing every secret-shaped key.
 *
 * @returns {{value: *, redacted: string[]}} the scrubbed structure and the
 *   dotted paths of everything removed. A non-empty `redacted` is a SERVER
 *   DEFECT and every screen renders it as one.
 */
export function scrub(value, path = '') {
  const redacted = [];
  function walk(node, at) {
    if (Array.isArray(node)) return node.map((item, i) => walk(item, `${at}[${i}]`));
    if (!node || typeof node !== 'object') return node;
    const out = {};
    for (const [key, child] of Object.entries(node)) {
      const here = at ? `${at}.${key}` : key;
      if (SECRET_KEY.test(key) && !SECRET_METADATA.has(key)) {
        redacted.push(here);
        continue;
      }
      out[key] = walk(child, here);
    }
    return out;
  }
  return { value: walk(value, path), redacted };
}

/* ------------------------------------------------------------------ *
 * 3. The calls
 * ------------------------------------------------------------------ */

/**
 * Run the first source whose route this build mounts.
 *
 * @param {Array<{source:'wave5'|'wave4-compat', template:string, call:Function}>} candidates
 *   in preference order. `template` is checked against the probe; `call`
 *   is only invoked for a candidate that is present, or whose presence is
 *   UNKNOWN (in which case a code-less 404 falls through to the next).
 * @param {string} unavailableNote - what an operator can do when nothing is
 *   mounted. Rendered verbatim.
 * @returns {Promise<{data:*, source:string, template:string, redacted:string[]}>}
 * @throws {EndpointUnavailableError} when no candidate is mounted.
 * @throws {IntegrationApiError} when a mounted candidate failed for a real reason.
 */
async function firstAvailable(candidates, unavailableNote) {
  let lastAbsent = null;
  let declaredNote = '';
  for (const candidate of candidates) {
    const present = await available(candidate.template);
    if (present === false) { lastAbsent = candidate; continue; }
    try {
      const raw = await candidate.call();
      const { value, redacted } = scrub(raw);
      return {
        data: value, source: candidate.source, template: candidate.template, redacted,
      };
    } catch (err) {
      // MOUNTED, AND IT SAYS IT CANNOT ANSWER.
      //
      // For a reader that is indistinguishable from absent, so it is handled
      // the same way: fall through to the next source. SCR-31/32/33/38 reach
      // `/api/zoho/connections` as `wave4-compat` and SCR-18 reaches the
      // ledger as `ledger-compat` — with the source still named on screen,
      // which is the whole point of this module. Before this branch existed a
      // mounted-but-unbacked route threw past every remaining candidate and
      // rendered a red HTTP 503.
      //
      // The server's own reason is kept for the case where nothing answers:
      // it knows which half is missing and this module does not.
      if (classifyUnavailable(err)) {
        lastAbsent = candidate;
        declaredNote = unavailableNoteFrom(err) || declaredNote;
        continue;
      }
      // Presence UNKNOWN plus a FastAPI-shaped 404 means the route is absent
      // after all; try the next source rather than reporting an empty result.
      if (present === null && classifyMissing(err)) { lastAbsent = candidate; continue; }
      throw err;
    }
  }
  throw new EndpointUnavailableError(
    (lastAbsent && lastAbsent.template) || candidates[0].template,
    declaredNote || unavailableNote,
  );
}

const WAVE5_NOTE = 'Wave 5 streams 1–6 are building this endpoint. Until it is mounted, '
  + 'this screen has nothing to read and says so rather than showing an empty list.';

/** SCR-31 / 32 / 33 / 38: the connection profiles this application holds. */
export function listConnections() {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections',
      call: () => wave5.get('/connections'),
    },
    {
      source: 'wave4-compat',
      template: '/api/zoho/connections',
      call: () => wave4.get('/connections'),
    },
  ], WAVE5_NOTE);
}

/**
 * SCR-31: create a connection profile.
 *
 * REQ-INT-024. The request body is built from a FIXED field list, so a caller
 * cannot smuggle a secret into it even by passing one. `mode` is deliberately
 * absent: `integration_connection.mode` defaults to MOCK in the schema (C2),
 * and a screen that could set LIVE_WRITE at creation time would be a screen
 * that can start live traffic without the explicit authorisation §11.9
 * requires.
 */
export function createConnection(form) {
  const body = {
    entity_id: form.entity_id,
    product: form.product,
    dc: form.dc,
    connector_name: form.connector_name,
    client_id: form.client_id,
    // organization_id is chosen on SCR-33, after discovery, not typed here.
  };
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections',
      call: () => wave5.post('/connections', body),
    },
  ], WAVE5_NOTE);
}

/** SCR-32: the authorisation URL the operator must open. Never a token. */
export function beginAuthorisation(connectionId) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections/{connection_id}/authorize',
      call: () => wave5.post(`/connections/${encodeURIComponent(connectionId)}/authorize`, {}),
    },
  ], WAVE5_NOTE);
}

/** SCR-33: organisations visible to the authorised credential. */
export function listOrganisations(connectionId) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections/{connection_id}/organizations',
      call: () => wave5.get(`/connections/${encodeURIComponent(connectionId)}/organizations`),
    },
  ], WAVE5_NOTE);
}

/** SCR-33: bind one organisation to this connection. */
export function mapOrganisation(connectionId, organizationId) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections/{connection_id}/organization',
      call: () => wave5.put(
        `/connections/${encodeURIComponent(connectionId)}/organization`,
        { organization_id: organizationId },
      ),
    },
  ], WAVE5_NOTE);
}

/**
 * SCR-34: required scopes, what has been granted, and the documented absences.
 *
 * The Wave-4 compatibility route answers the same question honestly — it reads
 * the verified endpoint inventory rather than a live tenant — so it is offered
 * as a named fallback rather than withheld.
 */
export function listScopes(connectionId) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections/{connection_id}/scopes',
      call: () => wave5.get(`/connections/${encodeURIComponent(connectionId)}/scopes`),
    },
    { source: 'wave4-compat', template: '/api/zoho/scopes', call: () => wave4.get('/scopes') },
  ], WAVE5_NOTE);
}

/** SCR-34: run the module-by-module scope and connectivity validation. */
export function validateScopes(connectionId) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections/{connection_id}/validate',
      call: () => wave5.post(`/connections/${encodeURIComponent(connectionId)}/validate`, {}),
    },
    {
      source: 'wave4-compat',
      template: '/api/zoho/{connection_id}/test',
      call: () => wave4.post(`/${encodeURIComponent(connectionId)}/test`),
    },
  ], WAVE5_NOTE);
}

/** SCR-26: the integration event log, cursor-paginated. */
export function listEvents(params) {
  return firstAvailable([
    { source: 'wave5', template: '/api/integrations/events', call: () => wave5.get('/events', params) },
  ], WAVE5_NOTE);
}

/** SCR-38: rate budget, circuit state, watermarks and token health. */
export function getHealth(connectionId) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/connections/{connection_id}/health',
      call: () => wave5.get(`/connections/${encodeURIComponent(connectionId)}/health`),
    },
    {
      source: 'wave4-compat',
      template: '/api/zoho/{connection_id}/health',
      call: () => wave4.get(`/${encodeURIComponent(connectionId)}/health`),
    },
  ], WAVE5_NOTE);
}

/** SCR-39: inbox and outbox rows in DEAD or FAILED — the dead-letter queue. */
export function listDeadLetters(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/dead-letters',
      call: () => wave5.get('/dead-letters', params),
    },
  ], WAVE5_NOTE);
}

/**
 * SCR-39: manual retry of one dead-lettered row.
 *
 * `Idempotency-Key` is honoured on every mutating route (§13) and is minted
 * ONCE per retry attempt by the caller and reused across transport retries of
 * that same attempt — a key minted per HTTP call would make every retry a
 * fresh emission, which for an outbox row is a duplicate purchase order.
 *
 * @param {'inbox'|'outbox'} queue
 * @param {string} rowId
 * @param {string} idempotencyKey
 */
export function retryDeadLetter(queue, rowId, idempotencyKey) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/dead-letters/{queue}/{row_id}/retry',
      call: () => wave5.request(
        'POST',
        `/dead-letters/${encodeURIComponent(queue)}/${encodeURIComponent(rowId)}/retry`,
        { body: {}, headers: { 'Idempotency-Key': idempotencyKey } },
      ),
    },
  ], WAVE5_NOTE);
}

/**
 * The global connector mode, as a last resort.
 *
 * §11.9 makes `zoho.MODE` PER CONNECTION, so a connection row's own `mode` is
 * always preferred and this is only read when no connection is in hand — on
 * SCR-31 before a profile exists, for instance. /api/health is unauthenticated
 * and cheap.
 *
 * @returns {Promise<{mode: string, note: string}>}
 */
export async function getGlobalMode() {
  try {
    const response = await fetch('/api/health', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(String(response.status));
    const body = await response.json();
    return {
      mode: body.zoho_mode || 'MOCK',
      note: body.zoho_mode_note || '',
    };
  } catch {
    // Unknown mode is reported as MOCK, which is the SAFE direction to be
    // wrong in: it understates rather than overstates how live this is.
    return { mode: 'MOCK', note: 'The connector mode could not be read; assuming MOCK.' };
  }
}

/* ------------------------------------------------------------------ *
 * 4. The transaction queues and the reconciliation surfaces
 *
 * These five screens have the same problem as the seven above and one extra
 * wrinkle: their Wave 5 routes do not exist, AND the question each one asks
 * has a genuinely honest answer available from this application's OWN ledger.
 *
 * That is a different kind of fallback from the Wave 4 one, and it is labelled
 * differently ('ledger-compat', not 'wave4-compat') because it means something
 * different. /api/purchase-orders answers "what have we ordered"; it does NOT
 * answer "did the purchase order reach Zoho", which is the question the
 * outbound queue exists to ask. Rendering the first as though it were the
 * second is exactly the dishonesty this module is built to prevent, so every
 * screen using this fallback says on screen which question it is answering and
 * which one it cannot.
 * ------------------------------------------------------------------ */

/**
 * The outbound emission queue: what we have tried to send to Zoho, and what
 * happened. Falls back to the local purchase-order ledger, which knows what
 * exists but not what was emitted.
 */
export function listOutbox(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/outbox',
      call: () => wave5.get('/outbox', params),
    },
    {
      source: 'ledger-compat',
      template: '/api/purchase-orders',
      call: () => ledger.get('/purchase-orders'),
    },
  ], WAVE5_NOTE);
}

/**
 * Inbound Purchase Receive / GRN acquisition status.
 *
 * OAS-02 is why this screen is not a simple list: Zoho ERP publishes NO list
 * endpoint for Purchase Receives, so acquisition is PO-anchored — each PO is
 * walked for its receives. A screen that showed a flat "GRNs received" count
 * without saying that would imply a completeness the mechanism cannot offer.
 */
export function listInboundGrn(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/inbox',
      call: () => wave5.get('/inbox', { ...(params || {}), module: 'purchasereceives' }),
    },
    { source: 'ledger-compat', template: '/api/grns', call: () => ledger.get('/grns') },
  ], WAVE5_NOTE);
}

/** Inbound vendor-bill acquisition status. */
export function listInboundBills(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/inbox',
      call: () => wave5.get('/inbox', { ...(params || {}), module: 'bills' }),
    },
    { source: 'ledger-compat', template: '/api/bills', call: () => ledger.get('/bills') },
  ], WAVE5_NOTE);
}

/**
 * SCR-18: commitment against actual, line by line, with the summary the
 * workbench leads on.
 *
 * The ledger route is not a degraded source here — it IS the reconciliation,
 * computed by domain.reconciliation() from this application's own commitments
 * and actuals. What the Wave 5 route would add is the integration_state of
 * each side, so the screen says which of the two it is showing.
 */
export function getReconciliation(projectId) {
  const params = projectId ? { project_id: projectId } : undefined;
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/reconciliation',
      call: () => wave5.get('/reconciliation', params),
    },
    {
      source: 'ledger-compat',
      template: '/api/reconciliation',
      call: () => ledger.get('/reconciliation', params),
    },
  ], WAVE5_NOTE);
}

/**
 * SCR-27: the unmatched-document exception queue.
 *
 * A QUARANTINED inbox payload — one that could not be attributed to a purchase
 * order, or whose raw external status has no mapping — raises a
 * reconciliation_exception rather than being guessed at, spread pro-rata or
 * dropped. This is where those land.
 */
export function listReconciliationExceptions(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/exceptions',
      call: () => wave5.get('/exceptions', params),
    },
    {
      source: 'ledger-compat',
      template: '/api/reconciliation/exceptions',
      call: () => ledger.get('/reconciliation/exceptions'),
    },
  ], WAVE5_NOTE);
}

/**
 * SCR-27's triage queue: the exceptions nobody could attribute to an entity.
 *
 * SEPARATE FROM listReconciliationExceptions, AND IT HAS TO BE.
 *
 * `reconciliation_exception.entity_id` is nullable on purpose — an
 * UNSANCTIONED_COMMITMENT is discovered on a purchase order we hold no local
 * record of, so at the moment it is raised there is no entity to file it
 * under. Those rows carry `local_paise` and `source_paise`: the exact sums by
 * which somebody's books do not tie out. They are visible only to a principal
 * holding `reconciliation.triage`, enforced by RLS
 * (`migrations/pg/012_unattributed_triage.sql`), and the ordinary queue above
 * cannot return them at all — its scope predicate compiles to
 * `entity_id = ANY(...)`, and SQL NULL equals nothing.
 *
 * NO FALLBACK. The SQLite ledger has no equivalent, and falling back to the
 * ordinary queue would render an empty triage screen that looks like "nothing
 * to attribute" when it means "this build cannot ask".
 */
export function listUnattributedExceptions(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/exceptions/unattributed',
      call: () => wave5.get('/exceptions/unattributed', params),
    },
  ], 'Exceptions that could not be attributed to an entity are visible only to an administrator '
    + 'holding reconciliation.triage. This build has no PostgreSQL integration API mounted, so the '
    + 'triage queue cannot be read — the rows still exist and still block capitalisation.');
}

/**
 * File one unattributed exception under an entity. Audited, and one-way.
 *
 * The write can only ever move a row OUT of the unattributed bucket: the
 * backend's WHERE carries `entity_id IS NULL`, so this can never re-point an
 * already-attributed exception from one entity to another, and never push one
 * INTO the bucket where only triage can see it. `reason` is mandatory and is
 * written into the hash-chained audit entry.
 */
export function attributeException(exceptionId, body) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/exceptions/{exception_id}/attribute',
      call: () => wave5.post(`/exceptions/${encodeURIComponent(exceptionId)}/attribute`, body),
    },
  ], 'Attributing an exception requires the PostgreSQL integration API and the reconciliation.triage '
    + 'permission. Nothing is attributed locally as a fallback: a guess about whose discrepancy this '
    + 'is would be recorded in the audit trail as a decision.');
}

/**
 * Close a reconciliation exception. Permission-gated, audited, and one-way.
 *
 * NOT THE SAME ACTION AS attributeException, AND THE SCREEN MUST NOT MERGE
 * THEM. Attributing says whose discrepancy this is; it changes nothing about
 * whether the exception still blocks. Resolving says the discrepancy has been
 * DEALT WITH, and `status = 'Open'` is what blocks capitalisation and period
 * close — so this call releases a financial-control gate.
 *
 * `status` is one of Resolved / Accepted / Written_off, from C18's frozen
 * `exception_status` namespace. They are not synonyms and must not be offered
 * as one control: Resolved means the discrepancy was corrected, Accepted means
 * it is real and tolerated, Written_off means the amount will not be
 * recovered. `note` is mandatory and reaches the hash-chained audit entry.
 */
export function resolveException(exceptionId, body) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/exceptions/{exception_id}/resolve',
      call: () => wave5.post(`/exceptions/${encodeURIComponent(exceptionId)}/resolve`, body),
    },
  ], 'Closing a reconciliation exception requires the PostgreSQL integration API and the '
    + 'reconciliation.triage permission. Nothing is closed locally as a fallback: an exception '
    + 'that stopped blocking without being recorded as resolved would be a silent drop.');
}

/**
 * SCR-26's control totals: what we sent, what we received, and whether the two
 * sides agree for a sync window.
 *
 * THERE IS NO FALLBACK, DELIBERATELY. A control total is a statement that the
 * count and value on OUR side equals the count and value on ZOHO's side. No
 * local endpoint knows the second half of that sentence, so a "control total"
 * synthesised from the ledger alone would be a tautology dressed as an
 * assurance — the most dangerous single number this application could render.
 * Absent means absent.
 */
export function getControlTotals(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/control-totals',
      call: () => wave5.get('/control-totals', params),
    },
  ], 'A control total compares OUR count and value against ZOHO\'s for the same window. No local '
    + 'endpoint knows Zoho\'s side, so nothing here is synthesised from the ledger: a control total '
    + 'that only ever compares us with ourselves would always balance.');
}
