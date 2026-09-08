/* app/frontend/src/core/api-client.js
   ONE fetch client for every SCR-nn feature module, parameterised by base
   path.

   Why this module exists
   ----------------------
   core/api.js was written hard-wired to '/api/audit'. When the budget screens
   needed the same session handling, the same X-Correlation-Id, the same
   RFC-7807 parsing and the same not-found-over-forbidden rule, the module
   could not be reused, so the whole pattern was copied into
   features/budget/budget-api.js. When the settings screens needed it, it was
   copied a third time into features/settings/api.js. Three copies means three
   places for a rule to drift — and it already had: only two of the three
   copies knew that this API has two error envelopes.

   Everything here is behaviour that was previously duplicated. The three
   modules above are now thin contract surfaces over this one client; they
   declare their base path, their error class and their wording, and nothing
   else.

   Session
   -------
   app.js authenticates every /api call with a server-issued session held in
   sessionStorage (never localStorage) under 'capex.session_id' and sent as
   X-Session. This module reuses that same key so a tab that already signed in
   through the main shell keeps working, and never creates or reads any other
   storage.

   Content-Security-Policy
   -----------------------
   Nothing in this module touches the DOM, so the `style-src 'self'` /
   `script-src 'self'` policy is not a concern here; it is enforced at the
   rendering layer by core/dom.js.
*/

const SESSION_KEY = 'capex.session_id';

/**
 * Base class for every feature client's error. Each feature subclasses it so
 * `err instanceof BudgetApiError` keeps discriminating between families, while
 * `err instanceof ApiClientError` catches all of them.
 */
export class ApiClientError extends Error {
  /**
   * @param {string} message - safe to show a user directly.
   * @param {Object} [opts]
   * @param {number} [opts.status] - HTTP status, or 0 for a network failure.
   * @param {string|null} [opts.code]
   * @param {string|null} [opts.messageId]
   * @param {string|null} [opts.correlationId]
   * @param {'network'|'auth'|'notfound'|'forbidden'|'validation'|'conflict'|'error'} [opts.kind]
   *   lets callers pick a rendering without re-deriving it from `status`.
   * @param {*} [opts.body] - the parsed response body, for diagnostics only.
   */
  constructor(message, {
    status = 0, code = null, messageId = null, correlationId = null,
    kind = 'error', body = null,
  } = {}) {
    super(message);
    this.name = 'ApiClientError';
    this.status = status;
    this.code = code;
    this.messageId = messageId;
    this.correlationId = correlationId;
    this.kind = kind;
    this.body = body;
  }
}

export function getSession() {
  try { return sessionStorage.getItem(SESSION_KEY) || ''; } catch { return ''; }
}

export function newCorrelationId() {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch { /* fall through to the manual id below */ }
  return 'cid-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

/**
 * Serialise a parameter object into a query string.
 *
 * THE EMPTY ARRAY WAS THE BUG, and it inverted the one distinction the backend
 * takes trouble to preserve. `[] !== ''`, so an empty array passed the skip
 * test above and reached `URLSearchParams.set`, which stringifies it to the
 * empty string: `buildQuery({entity_ids: []})` emitted `?entity_ids=`. FastAPI
 * parses that as `['']` — a filter restricting the result to records whose
 * entity id is the empty string, which is none of them. So "the caller chose
 * nothing" became "restrict to nothing", and a screen that meant "do not
 * filter on entity" got an empty table.
 *
 * `exports.py::serialise_scope` and `FilterSet._tuple` exist to keep three
 * states apart — `None` (do not filter), `()` (an explicit empty selection)
 * and a populated tuple — and a query string cannot express the middle one at
 * all: there is no way to send a repeated key zero times. The honest encoding
 * of "no value chosen" is therefore NO PARAMETER, which is what this now does.
 *
 * A POPULATED ARRAY IS REPEATED KEYS, not a comma-join. `set(k, ['A','B'])`
 * emitted `k=A%2CB` — ONE value, the five-character string "A,B" — which
 * FastAPI parses as a single id literally named "A,B", matching no row and
 * returning an empty result. Every non-analytics call site is scalar today, so
 * this corrects no live defect; it removes a trap that fires silently the
 * first time one of them grows a list. `analytics-api.js::toSearch` already
 * encodes lists this way and now agrees with this function rather than
 * working around it.
 *
 * SCALAR BEHAVIOUR IS UNCHANGED. `Object.entries` yields each key once, so
 * `append` and `set` are identical for a scalar; the skip test for undefined,
 * null and the empty string is untouched.
 */
export function buildQuery(params) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item === undefined || item === null || item === '') continue;
        q.append(key, String(item));
      }
      continue;
    }
    q.set(key, value);
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

/**
 * Read an error body.
 *
 * THIS API HAS TWO ERROR ENVELOPES, and a client that knows only one of them
 * silently loses the machine-readable `code` on roughly half of all failures:
 *
 *   A. the services.BusinessError handler emits `code` at the TOP LEVEL:
 *        {"code": "BUDGET_EXCEEDED", "message": "…", "message_id": "MSG-BUD-001"}
 *
 *   B. a router raising HTTPException(detail={...}) NESTS it under `detail`:
 *        {"detail": {"code": "VERSION_CONFLICT", "message": "…"}}
 *
 * Both are read here, top level first, because a BusinessError that also
 * carries a detail object should be described by its own code. Two further
 * shapes arrive from the framework rather than from our code and are handled
 * for completeness:
 *
 *   C. FastAPI's own {"detail": "a plain string"}
 *   D. FastAPI request validation {"detail": [{"loc": [...], "msg": "..."}]},
 *      which the two surviving copies of this logic collapsed to null — the
 *      user then saw only the generic fallback for a fixable typo.
 *
 * Plus RFC-7807's `title` as a last resort. Never throws.
 *
 * @returns {{detail: string|null, code: string|null, messageId: string|null, raw: *}}
 */
export function readProblem(body) {
  if (typeof body === 'string') {
    const s = body.trim();
    return { detail: s || null, code: null, messageId: null, raw: body };
  }
  if (!body || typeof body !== 'object') {
    return { detail: null, code: null, messageId: null, raw: body };
  }

  const d = body.detail;
  const nested = d && typeof d === 'object' && !Array.isArray(d) ? d : null;

  let detail = null;
  if (nested && typeof nested.message === 'string' && nested.message.trim()) {
    detail = nested.message;                                    // envelope B
  } else if (typeof d === 'string' && d.trim()) {
    detail = d;                                                 // envelope C
  } else if (Array.isArray(d)) {                                // envelope D
    const parts = d.map((e) => {
      const where = Array.isArray(e && e.loc)
        ? e.loc.filter((x) => x !== 'body').join('.')
        : '';
      return (where ? `${where}: ` : '') + ((e && e.msg) || 'is invalid');
    }).filter(Boolean);
    detail = parts.length ? parts.join('; ') : null;
  } else if (typeof body.message === 'string' && body.message.trim()) {
    detail = body.message;                                      // envelope A
  }
  if (!detail && typeof body.title === 'string' && body.title.trim()) {
    detail = body.title;                                        // RFC-7807
  }

  // Envelope A's code sits at the top level; envelope B's is nested. Read both.
  const code = body.code || (nested && nested.code) || null;
  const messageId = body.message_id
    || (nested && nested.message_id)
    || (Array.isArray(d) ? 'VALIDATION_ERROR' : null);

  return { detail: detail || null, code: code || null, messageId: messageId || null, raw: body };
}

/**
 * Map an HTTP status onto a rendering kind.
 *
 * THE NOT-FOUND-OVER-FORBIDDEN RULE. An out-of-scope READ must be
 * indistinguishable from a genuinely missing record: a 403 on an id is an
 * existence oracle, and so is a differently worded message for the two cases.
 * A 403 on a WRITE is a different matter — there is no id to leak that the
 * caller did not already name, and the user needs to know the action was
 * refused for permission rather than because the record vanished.
 *
 * @param {number} status
 * @param {string|null} code
 * @param {string} method
 */
export function classifyStatus(status, code, method) {
  if (status === 0) return 'network';
  if (status === 401) return 'auth';
  if (status === 404) return 'notfound';
  if (status === 403) return method === 'GET' || method === 'HEAD' ? 'notfound' : 'forbidden';
  if (status === 409 || code === 'VERSION_CONFLICT') return 'conflict';
  if (status === 422 || status === 400) return 'validation';
  return 'error';
}

/**
 * Build a client bound to one base path.
 *
 * @param {Object} config
 * @param {string} config.basePath - prefixed to every path, e.g. '/api/audit'.
 *   Pass '' for a client whose call sites supply absolute paths (the settings
 *   client spans two families, /api/settings and /api/masters).
 * @param {typeof ApiClientError} [config.ErrorClass] - the feature's error
 *   subclass, so `instanceof` keeps discriminating between families.
 * @param {Object} [config.messages] - per-kind fallback wording, used only
 *   when the server sent no readable detail. `error` may be a function of the
 *   HTTP status.
 * @returns {{request: Function, get: Function, post: Function, put: Function}}
 */
export function createApiClient({ basePath = '', ErrorClass = ApiClientError, messages = {} } = {}) {
  const fallback = {
    network: 'The service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to continue.',
    notfound: 'No data was found for these filters.',
    forbidden: 'You do not have permission to do this. Contact an administrator if you need access.',
    conflict: 'Someone else changed this record since it was loaded. Reload to see the latest version before trying again.',
    validation: 'The submitted values could not be accepted. Correct the highlighted fields and try again.',
    error: (status) => `The service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The service returned a response that could not be understood.',
    ...messages,
  };

  function wording(kind, status) {
    const w = fallback[kind];
    return typeof w === 'function' ? w(status) : w;
  }

  function fail(message, opts) {
    return new ErrorClass(message, opts);
  }

  async function request(method, path, { params, body, headers: extraHeaders } = {}) {
    const correlationId = newCorrelationId();
    const headers = {
      Accept: 'application/json',
      'X-Correlation-Id': correlationId,
      ...(extraHeaders || {}),
    };
    const sid = getSession();
    if (sid) headers['X-Session'] = sid;

    const init = { method, headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(basePath + path + buildQuery(params), init);
    } catch {
      throw fail(wording('network', 0), { status: 0, correlationId, kind: 'network' });
    }

    const txt = await response.text().catch(() => '');
    let parsed = null;
    let parsedOk = true;
    if (txt) {
      try { parsed = JSON.parse(txt); } catch { parsed = txt; parsedOk = false; }
    }

    if (!response.ok) {
      // An error body that is not JSON is discarded rather than shown: a proxy's
      // HTML error page is not a message for a user, and the per-kind fallback
      // wording below says something true instead.
      const { detail, code, messageId, raw } = readProblem(parsedOk ? parsed : null);
      const kind = classifyStatus(response.status, code, method);
      throw fail(detail || wording(kind, response.status), {
        status: response.status, code, messageId, correlationId, kind, body: raw,
      });
    }

    if (!parsedOk) {
      throw fail(wording('unreadable', response.status), {
        status: response.status, correlationId, kind: 'error',
      });
    }
    return parsed;
  }

  return {
    request,
    get: (path, params) => request('GET', path, { params }),
    post: (path, body, params) => request('POST', path, { body, params }),
    put: (path, body, params) => request('PUT', path, { body, params }),
  };
}
