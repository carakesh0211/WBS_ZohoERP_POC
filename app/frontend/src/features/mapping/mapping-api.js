/* app/frontend/src/features/mapping/mapping-api.js
   The API surface for SCR-35, SCR-36, SCR-37 and SCR-40.

   THIS MODULE INVENTS NO BACKEND ROUTE.
   -------------------------------------
   Every call below either names a route this build actually mounts — proven by
   the same /openapi.json probe integration-api.js established — or names the
   routes it looked for and did not find, and raises `EndpointUnavailableError`
   so the screen renders an UNAVAILABLE state instead of an empty one.

   The distinction is the whole reason this file exists rather than a set of
   ad-hoc fetches. "No field mappings are configured" and "this build serves no
   field-mapping registry" look identical in a table and mean opposite things:
   the first says an administrator has work to do, the second says there is
   nothing an administrator can do here at all. Only one of them is true today
   for SCR-36, and it is the second.

   WHAT IS REUSED RATHER THAN REBUILT
   ----------------------------------
   `available()`, `scrub()`, `classifyMissing()`, `classifyUnavailable()`,
   `unavailableNoteFrom()` and `EndpointUnavailableError` come from
   integration-api.js unchanged. That module is the Wave 5 contract surface and
   its OpenAPI probe is cached for the life of the page — a second probe here
   would be a second fetch answering the same question, and a second place for
   the answer to drift. `getHealth()` and `listConnections()` are likewise
   imported rather than re-declared: SCR-37 asks the same question SCR-38 asks
   and must not be able to get a different answer.

   `firstAvailable()` below is NOT exported by integration-api.js, so it is
   restated here — thirty lines, in this stream's own directory, rather than an
   edit to a file this stream does not own.

   REQ-INT-024 — NO SECRET REACHES A RENDERER
   ------------------------------------------
   Every response returned by this module goes through integration-api.js's
   `scrub()`, which deletes any key matching /secret|refresh_token|access_token|
   password|private_key|credential_value/i and REPORTS what it deleted. SCR-40
   is a credential ACTIVITY log: it renders actor, action, time, connection,
   result and correlation id — metadata about a credential — and there is no
   code path in this file by which a credential VALUE could reach it. A server
   that returned one produces a visible defect warning, not a silent drop.
*/

import { createApiClient } from '../../core/api-client.js';
import {
  EndpointUnavailableError,
  IntegrationApiError,
  available,
  classifyMissing,
  classifyUnavailable,
  scrub,
  unavailableNoteFrom,
} from '../integration/integration-api.js';

export {
  EndpointUnavailableError,
  getGlobalMode,
  getHealth,
  listConnections,
  listEvents,
  listScopes,
} from '../integration/integration-api.js';

const MESSAGES = {
  network: 'The service could not be reached. Check your connection and try again.',
  auth: 'Your session has ended. Sign in again to continue.',
  notfound: 'No records were found for these filters.',
  forbidden: 'You do not have permission to do this.',
  conflict: 'This record changed since it was loaded. Reload it before saving again.',
  validation: 'The submitted values could not be accepted. Correct the highlighted fields and try again.',
  error: (status) => `The service returned an unexpected error (HTTP ${status}).`,
  unreadable: 'The service returned a response that could not be understood.',
};

/* Four base paths, four families, one shared transport. Named separately so a
   stack trace says which surface answered — the same reasoning integration-api
   .js applies to its three. */
const masters = createApiClient({
  basePath: '/api/masters', ErrorClass: IntegrationApiError, messages: MESSAGES,
});
const zoho = createApiClient({
  basePath: '/api/zoho', ErrorClass: IntegrationApiError, messages: MESSAGES,
});
const audit = createApiClient({
  basePath: '/api/audit', ErrorClass: IntegrationApiError, messages: MESSAGES,
});
const root = createApiClient({
  basePath: '', ErrorClass: IntegrationApiError, messages: MESSAGES,
});

/**
 * Run the first candidate whose route template this build mounts.
 *
 * Identical in behaviour to integration-api.js's private function of the same
 * name, and deliberately so: a screen in this feature and a screen in that one
 * must not disagree about what "absent" means. A candidate whose presence is
 * UNKNOWN is tried, and a FastAPI-shaped code-less 404 (or a declared 503
 * carrying `unavailable: true`) falls through to the next rather than being
 * reported as an empty result.
 *
 * @param {Array<{source:string, template:string, call:Function}>} candidates
 * @param {string} unavailableNote - rendered verbatim when nothing is mounted.
 * @returns {Promise<{data:*, source:string, template:string, redacted:string[]}>}
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
      if (classifyUnavailable(err)) {
        lastAbsent = candidate;
        declaredNote = unavailableNoteFrom(err) || declaredNote;
        continue;
      }
      if (present === null && classifyMissing(err)) { lastAbsent = candidate; continue; }
      throw err;
    }
  }
  throw new EndpointUnavailableError(
    (lastAbsent && lastAbsent.template) || candidates[0].template,
    declaredNote || unavailableNote,
  );
}

/* ------------------------------------------------------------------ *
 * SCR-35 — master data mappings
 * ------------------------------------------------------------------ */

/**
 * The FOUR values `mapping_status` may hold, frozen by the CHECK constraint on
 * `item_master` and `vendor_master` in migrations/pg/005_master_data.sql.
 *
 * Listed here rather than derived from the data because a workbench must be
 * able to say "no row is in NEEDS_REVIEW" — which requires knowing that
 * NEEDS_REVIEW exists. A filter built from the values that happen to be
 * present cannot distinguish an empty bucket from an absent one.
 */
export const MAPPING_STATUSES = Object.freeze([
  'UNMAPPED', 'MAPPED', 'NEEDS_REVIEW', 'DUPLICATE_SUSPECT',
]);

/** The master kinds `/api/masters/{kind_name}` serves. */
export const MASTER_KINDS = Object.freeze([
  { kind: 'vendors', label: 'Vendors', idKey: 'vendor_id' },
  { kind: 'items', label: 'Items', idKey: 'item_id' },
]);

const MASTERS_NOTE = 'The master-data API is not mounted in this build, so no vendor or item '
  + 'mapping can be read. Nothing is synthesised locally: a mapping table assembled from '
  + 'somewhere other than the master registry would be a guess about which local record '
  + 'corresponds to which Zoho record, and that guess posts documents against the wrong vendor.';

/**
 * SCR-35: one page of the vendor or item master, with its mapping governance
 * columns.
 *
 * `?q=`, `?source=`, `?mapping_status=` and `?is_active=` are the filters the
 * route declares (docs/WAVE2_CONTRACTS.md). Filtering SERVER-side rather than
 * in the browser is not an optimisation here: the response is cursor-paginated
 * and carries no total, so a client-side filter would silently filter one page
 * and present the result as though it were the whole set.
 */
export function listMasters(kind, params) {
  const template = '/api/masters/{kind_name}';
  return firstAvailable([
    {
      source: 'masters',
      template,
      call: () => masters.get(`/${encodeURIComponent(kind)}`, params),
    },
  ], MASTERS_NOTE);
}

/**
 * SCR-35: the duplicate candidates the master registry itself has flagged.
 *
 * A CONFLICT on this screen is never computed in the browser. `duplicate_of`
 * and `DUPLICATE_SUSPECT` are written by the ingestion path, which has the
 * payload hash and the full table; a similarity heuristic run over one visible
 * page would disagree with the registry and there would be no way to tell
 * which of the two an operator should believe.
 */
export function listMasterDuplicates(kind) {
  const template = '/api/masters/{kind_name}/duplicates';
  return firstAvailable([
    {
      source: 'masters',
      template,
      call: () => masters.get(`/${encodeURIComponent(kind)}/duplicates`),
    },
  ], MASTERS_NOTE);
}

/* ------------------------------------------------------------------ *
 * SCR-36 / SCR-37 — the verified endpoint inventory
 * ------------------------------------------------------------------ */

const INVENTORY_NOTE = 'The verified endpoint inventory is not mounted in this build. It is the '
  + 'only evidenced description of what the destination API accepts, and nothing here substitutes '
  + 'for it: a field list written from memory would be a claim about somebody else\'s API that no '
  + 'specification backs.';

/** SCR-36 / SCR-37: every module in the verified inventory, with its scopes. */
export function getInventorySummary() {
  return firstAvailable([
    { source: 'inventory', template: '/api/zoho/inventory', call: () => zoho.get('/inventory') },
  ], INVENTORY_NOTE);
}

/**
 * SCR-36 / SCR-37: the endpoints of ONE module, with the three capability
 * flags that decide what a mapping and a sync can actually do.
 *
 *   `custom_field_support` — whether a custom-field mapping is possible at all;
 *   `filter_support`       — whether an incremental (delta) read is possible;
 *   `pagination_support`   — whether a page size is a meaningful control.
 *
 * Each row also carries `verification_status`, which is
 * CONFIRMED-BY-VENDOR-SPECIFICATION for every row in the inventory. That word
 * is about the SPECIFICATION, not about a live call, and every screen that
 * renders it says so — no live Zoho call has been made by this application.
 */
export function listModuleEndpoints(module) {
  return firstAvailable([
    {
      source: 'inventory',
      template: '/api/zoho/inventory/{module}',
      call: () => zoho.get(`/inventory/${encodeURIComponent(module)}`),
    },
  ], INVENTORY_NOTE);
}

/**
 * SCR-36: the transaction / custom-field mapping REGISTRY.
 *
 * THERE IS NO SUCH ROUTE IN THIS BUILD, AND THAT IS WHAT THIS FUNCTION SAYS.
 *
 * The candidates below are probed against /openapi.json like every other call
 * in this file. None of them is mounted, so `firstAvailable` raises
 * `EndpointUnavailableError` and SCR-36 renders the unavailable state naming
 * them. They are listed rather than assumed because the probe is the honest
 * mechanism: if one of them lands tomorrow this function starts working with
 * no edit, and if none ever does, the screen keeps saying so.
 *
 * NOTHING IS FABRICATED IN ITS PLACE. A mapping table invented in the browser
 * would be a statement that field X of ours is written to field Y of theirs —
 * a claim about how money reaches a ledger — supported by nothing.
 */
export function listFieldMappings(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/field-mappings',
      call: () => root.get('/api/integrations/field-mappings', params),
    },
    {
      source: 'wave5',
      template: '/api/integrations/mappings/fields',
      call: () => root.get('/api/integrations/mappings/fields', params),
    },
  ], 'No route in this build serves a transaction or custom-field mapping registry, so there is '
    + 'nothing to list and nothing to activate. The destination capability shown below IS real — '
    + 'it comes from the verified endpoint inventory — but which of our fields is written to '
    + 'which of theirs is not recorded anywhere this screen can read.');
}

/**
 * SCR-37: the per-object sync direction and schedule CONFIGURATION.
 *
 * Absent for the same reason and stated the same way. The consequence is
 * sharper here than on SCR-36: an operator looking at a schedule screen
 * reasonably assumes the schedule shown is the schedule running. Nothing on
 * SCR-37 is presented as a running schedule unless a mounted route said so.
 */
export function listSyncConfiguration(params) {
  return firstAvailable([
    {
      source: 'wave5',
      template: '/api/integrations/sync-configuration',
      call: () => root.get('/api/integrations/sync-configuration', params),
    },
    {
      source: 'wave5',
      template: '/api/integrations/schedules',
      call: () => root.get('/api/integrations/schedules', params),
    },
  ], 'No route in this build serves a per-object sync schedule, so no schedule, look-back window '
    + 'or page size on this screen is a configured value that something is running to. The '
    + 'watermarks and circuit states shown alongside ARE measured, and are labelled separately.');
}

/* ------------------------------------------------------------------ *
 * SCR-40 — connector audit and credential activity
 * ------------------------------------------------------------------ */

/**
 * The `object_type` values connector activity is recorded under.
 *
 * Read out of app/backend/api/integrations.py, which appends audit entries for
 * `integration_connection` (authorisation and organisation mapping),
 * `integration_inbox` / `integration_outbox` (a manual retry) and
 * `integration_inbox` (a discard). Listed explicitly so the screen can offer a
 * filter that names every connector stream rather than only the ones that
 * happen to have entries today — an empty stream and an unlisted stream are
 * different facts.
 */
export const CONNECTOR_OBJECT_TYPES = Object.freeze([
  'integration_connection', 'integration_inbox', 'integration_outbox',
]);

/**
 * SCR-40: the hash-chained audit entries for connector activity.
 *
 * `/api/audit/entries` is mounted and gated on `audit.read` at the router, so
 * the permission check is the server's and a refusal arrives as one. It
 * returns actor, action, at, object_type, object_id, detail, correlation_id
 * and entry_hash — exactly the six fields SCR-40 is specified to show, and no
 * credential field of any kind.
 *
 * A CREDENTIAL VALUE CANNOT ARRIVE HERE. `audit_log.detail` is a string
 * written by the backend and the response passes through `scrub()` regardless;
 * if a future backend change ever wrote a token into an audit detail, the
 * screen renders the redaction warning rather than the token.
 */
export function listConnectorAudit(params) {
  return firstAvailable([
    {
      source: 'audit',
      template: '/api/audit/entries',
      call: () => audit.get('/entries', params),
    },
  ], 'The audit trail is not mounted in this build, so connector and credential activity cannot '
    + 'be read. Nothing is reconstructed from the event log in its place: the event log is not '
    + 'hash-chained and an activity record that cannot be verified is not an audit record.');
}

/**
 * SCR-40: chain integrity for one stream.
 *
 * A credential activity log whose integrity is unverified is a log, not an
 * audit trail. This is the call that lets the screen say which it is showing.
 */
export function verifyAuditChain(streamKey) {
  return firstAvailable([
    {
      source: 'audit',
      template: '/api/audit/chain/verify',
      call: () => audit.get('/chain/verify', streamKey ? { stream_key: streamKey } : undefined),
    },
  ], 'Chain verification is not mounted in this build, so the entries shown cannot be confirmed '
    + 'unbroken. They are presented as a log, not as a verified audit trail.');
}

/* ------------------------------------------------------------------ *
 * The acting principal
 * ------------------------------------------------------------------ */

/**
 * The permissions this session holds, for PRESENTATIONAL gating only.
 *
 * SCR-40 collapses non-secret operational detail behind a reveal, and the
 * reveal is offered only to a principal holding the permission. That gate is
 * a courtesy, not a control: the server has already decided what to put in the
 * response, `/api/audit/entries` refuses a caller without `audit.read` before
 * the handler runs, and nothing this function returns can widen what arrived.
 * A screen that hid a field the server chose to send would be pretending to a
 * security property it does not have — so the reveal hides only clutter.
 *
 * Failure is reported as NO permissions, which is the safe direction: it
 * withholds a disclosure control rather than offering one.
 *
 * @returns {Promise<{permissions: Set<string>, actor: string|null, read: boolean}>}
 */
export async function getPrincipal() {
  /* The shell has already read this. app.js is a classic script and its
     top-level `const S` lives in the global lexical environment, which is on
     the scope chain of a module evaluated in the same realm — so the shell's
     own answer is preferred over a second /api/bootstrap, which returns every
     project, entity and user in the installation to answer a question about
     permissions. Guarded, because a module must not require the shell to
     exist: the VRT suite mounts these screens through app.js, but a future
     harness might not. */
  try {
    // eslint-disable-next-line no-undef
    const shell = typeof S !== 'undefined' ? S : null;
    if (shell && shell.perms && typeof shell.perms.has === 'function' && shell.perms.size) {
      return {
        permissions: new Set([...shell.perms].map(String)),
        actor: (shell.me && (shell.me.user_id || shell.me.name)) || null,
        read: true,
      };
    }
  } catch { /* fall through to the request */ }
  try {
    const body = await root.get('/api/bootstrap');
    const list = Array.isArray(body && body.permissions) ? body.permissions : [];
    const me = (body && body.me) || {};
    return {
      permissions: new Set(list.map(String)),
      actor: me.user_id || me.name || null,
      read: true,
    };
  } catch {
    return { permissions: new Set(), actor: null, read: false };
  }
}
