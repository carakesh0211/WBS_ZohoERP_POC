/* app/frontend/src/features/procurement/procurement-api.js
   The client for app/backend/api/procurement.py's purchase-order origination
   (Fable 5.1, migration 029), and the two shell reads the screen needs to
   fill its selects. Same shape as features/settings/fx-api.js: a base path,
   an error class and this family's wording; the session header, correlation
   id, RFC-7807 parsing and not-found-over-forbidden rule live once in
   core/api-client.js.

     POST /api/procurement/purchase-orders          (po.amend)

   THE PAYLOAD IS THE API MODEL, FIELD FOR FIELD (`_PurchaseOrderIn`,
   `extra="forbid"`): `project_id`, `vendor_name`, `lines`, optional
   `po_number`, and -- on a foreign-currency order only -- `currency`,
   `document_date`, `exchange_rate` (the exact decimal STRING the rate book
   answered), `rate_source` and `fx_rate_id`. A base-currency order carries
   NONE of the currency fields: `_po_basis` refuses an INR order that names
   a rate (PO_IDENTITY_TRANSLATION), and `_normalise_lines` refuses a
   foreign line that types `amount_paise` (PO_BASE_AMOUNT_ON_FOREIGN_ORDER)
   or a base line that carries `source_amount_minor`
   (PO_SOURCE_AMOUNT_ON_BASE_ORDER). The screen builds exactly one of the
   two line shapes, never both.

   The model's field is `currency`, not `currency_code` -- the response
   document and the emission DTO say `currency_code`; the request says
   `currency`. Sending the wrong spelling is a 422 under extra="forbid".

   ERRORS. The router answers RFC-7807: {"detail": {"type", "title",
   "status", "code", "detail": <sentence>, ...extra}}. core/api-client.js
   reads envelope B's `message`, which this envelope does not carry, so
   problemDetail() below reads `detail.detail` and `detail.code` the way
   fx-rates.js::problemDetail already does -- the coded refusal is shown
   VERBATIM, never paraphrased.
*/

import { ApiClientError, createApiClient } from '../../core/api-client.js';

export class ProcurementApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'ProcurementApiError';
  }
}

const client = createApiClient({
  basePath: '/api/procurement',
  ErrorClass: ProcurementApiError,
  messages: {
    network: 'The procurement service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to raise a purchase order.',
    notfound: 'The procurement service did not find what this request named.',
    forbidden: 'Your role cannot raise a purchase order. Contact an administrator if you need access.',
    error: (status) => `The procurement service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The procurement service returned a response that could not be understood.',
  },
});

/* The shell's own reads (app/backend/main.py): the project list and budget
   heads come from /api/bootstrap, the WBS tree with its per-node
   `allow_procurement` flag from /api/projects/{id}/wbs. */
const shellClient = createApiClient({
  basePath: '/api',
  ErrorClass: ProcurementApiError,
  messages: {
    network: 'The application could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to continue.',
  },
});

/** POST /api/procurement/purchase-orders (po.amend). `body` is the exact
 * `_PurchaseOrderIn` document; see the module comment. */
export function createPurchaseOrder(body) {
  return client.post('/purchase-orders', body);
}

/**
 * The shell's bootstrap answer -- projects, budget heads, permissions.
 * app.js is a classic script whose top-level `const S` is on the scope chain
 * of a module evaluated in the same realm (features/budget/budget-api.js::
 * getPrincipal gives the reasoning), so the answer the shell already holds
 * is read rather than fetched again; guarded so this module never REQUIRES
 * the shell to exist.
 * @returns {Promise<{projects: Array, budgetHeads: Array, permissions: Set<string>}>}
 */
export async function getShellBootstrap() {
  let boot = null;
  try {
    // eslint-disable-next-line no-undef
    const shell = typeof S !== 'undefined' ? S : null;
    if (shell && shell.boot && Array.isArray(shell.boot.projects)) boot = shell.boot;
  } catch { /* fall through to the request below */ }
  if (!boot) boot = await shellClient.get('/bootstrap');
  return {
    projects: Array.isArray(boot.projects) ? boot.projects : [],
    budgetHeads: Array.isArray(boot.budget_heads) ? boot.budget_heads : [],
    permissions: new Set(((boot && boot.permissions) || []).map(String)),
  };
}

/** GET /api/projects/{id}/wbs -- the tree, flattened to the nodes that
 * allow procurement, in tree order. */
export async function getProcurableWbs(projectId) {
  const data = await shellClient.get(`/projects/${encodeURIComponent(projectId)}/wbs`);
  const flat = [];
  const walk = (node) => { flat.push(node); (node.children || []).forEach(walk); };
  ((data && data.tree) || []).forEach(walk);
  return flat.filter((n) => n.allow_procurement);
}

/**
 * The coded refusal, verbatim. Reads the procurement router's RFC-7807
 * envelope ({"detail": {"code", "detail"}}) first, then the shapes
 * core/api-client.js already parsed into `err.code` / `err.message`.
 * @returns {{code: string|null, message: string, messageId: string|null, extra: Object|null}}
 */
export function problemDetail(err) {
  const raw = err && err.body;
  const nested = raw && typeof raw === 'object' && raw.detail && typeof raw.detail === 'object' && !Array.isArray(raw.detail) ? raw.detail : null;
  const message = (nested && typeof nested.detail === 'string' && nested.detail.trim())
    ? nested.detail
    : ((err && err.message) || 'This request could not be completed.');
  const code = (nested && nested.code) || (err && err.code) || null;
  let extra = null;
  if (nested) {
    const { type, title, status, code: _c, detail, message: _m, message_id, ...rest } = nested;
    extra = Object.keys(rest).length ? rest : null;
  }
  return { code, message, messageId: (err && err.messageId) || null, extra };
}
