/* app/frontend/src/features/integration/integration-kit.js
   The parts every integration screen needs and none of them should re-invent:
   the mode indicator, the operational status chip, the business-visible
   `integration_state` badge, the source line, and the two states the shared
   approval state host does not model — "this route is not mounted" and "a
   server returned a secret".

   WHY THE MODE INDICATOR LIVES HERE AND NOT IN app.js
   ---------------------------------------------------
   §11.9: "`zoho.MODE` extends to MOCK | SANDBOX | LIVE_READ | LIVE_WRITE per
   connection. app.js's existing mockBadge() / mockBanner() honesty indicators
   become real mode indicators rather than being deleted."

   app.js's two functions are string-returning helpers in a classic script that
   knows exactly one mode, read from /api/health, for the whole application.
   They stay exactly as they are — the seventeen approved shell views render
   them and they have pixel baselines. `modeBadge()` and `modeBanner()` below
   are the EXTENSION §11.9 asks for: same class names, same wording discipline,
   same `.mock-chip` styling from the byte-frozen stylesheet, but built as real
   nodes, aware of all four modes, and taking the mode from the CONNECTION
   rather than from a global.

   The honesty rule is the one that carried over unchanged, and it is the whole
   point of the extension: A SCREEN SHOWING MOCK DATA MUST SAY SO. Three of the
   four modes are unverified against a live tenant and every one of them says
   so on the badge itself. Only LIVE_READ and LIVE_WRITE drop "NOT VERIFIED",
   and reaching either requires an authorisation this project has not been
   given (Phase 0B has not cleared), so in practice nothing renders as verified
   today — which is the correct state of the world, not a limitation of this
   code.

   THE INTEGRATION STATE BADGE IS NOT A BUSINESS STATUS
   ----------------------------------------------------
   C16's `business_visible_bridge`: an object keeps its C3 business status and
   carries a SEPARATE `integration_state` badge (QUEUED | SENT | FAILED),
   "rather than adding a 22nd business status. This is precisely why the
   registries are separated." `integrationStateBadge()` renders that badge and
   refuses anything that is not one of the three values — including, explicitly,
   any of C3's twenty-one codes, so the two namespaces cannot be mixed by a
   caller that has a status string and is not sure which kind it is.

   Content-Security-Policy: every node here is built with core/dom.js's h(),
   which throws on a `style` attribute and puts every string through a Text
   node. Nothing in this file can emit a style attribute or an unescaped
   interpolation.
*/

import { h, text, clear } from '../../core/dom.js';
import { statusChip } from '../../components/capex-statuschip.js';

/* ------------------------------------------------------------------ *
 * Mode
 * ------------------------------------------------------------------ */

/**
 * The four modes of §11.9, with what each one actually means for the reader.
 *
 * `verified` is the field that matters. It is false for three of the four, and
 * it drives the "NOT VERIFIED" suffix on the badge and the warning banner on
 * the screen. Nothing here can render a screen as verified without the mode
 * saying so, and only an authorised live connection produces such a mode.
 */
export const MODES = {
  MOCK: {
    verified: false,
    tone: 'neutral',
    summary: 'No network call is made. Requests are constructed from the verified '
      + 'endpoint specification and logged; responses are synthesised locally.',
  },
  SANDBOX: {
    verified: false,
    tone: 'progress',
    summary: 'Calls go to an authorised sandbox tenant. Sandbox data is not production '
      + 'data and a sandbox result is not evidence about production.',
  },
  LIVE_READ: {
    verified: true,
    tone: 'progress',
    summary: 'Read-only calls are made against the live tenant. Nothing is written.',
  },
  LIVE_WRITE: {
    verified: true,
    tone: 'warning',
    summary: 'Documents are created and updated in the live tenant. Every emission is '
      + 'idempotency-keyed and recorded in the outbox.',
  },
};

/** Normalise anything the API might return into one of the four, or UNKNOWN. */
export function normaliseMode(value) {
  const key = String(value || '').trim().toUpperCase();
  return Object.prototype.hasOwnProperty.call(MODES, key) ? key : 'UNKNOWN';
}

/**
 * The mode chip. This is app.js's `mockBadge()` grown up: same `.mock-chip`
 * class from the byte-frozen stylesheet, so it looks exactly like the honesty
 * indicator the client already approved, but it names the real mode and only
 * claims verification where verification is possible.
 *
 * An UNKNOWN mode reads as unverified, which is the safe direction to be wrong
 * in — it understates rather than overstates how live a connection is.
 */
export function modeBadge(mode, { note } = {}) {
  const key = normaliseMode(mode);
  const spec = MODES[key];
  const verified = !!(spec && spec.verified);
  const label = key === 'UNKNOWN' ? 'MODE UNKNOWN · NOT VERIFIED' : `${key}${verified ? '' : ' · NOT VERIFIED'}`;
  return h('span', {
    class: 'mock-chip',
    title: note || (spec ? spec.summary : 'The connector mode could not be read, so nothing on this screen is verified.'),
  }, label);
}

/**
 * The banner form — app.js's `mockBanner()`, extended the same way.
 *
 * Rendered on every screen that shows connector data, above the data, for the
 * same reason app.js renders it on the Zoho views: a reader who scrolls
 * straight to a table must not be able to mistake synthesised numbers for
 * measured ones.
 *
 * @param {string} mode
 * @param {Object} [opts]
 * @param {string} [opts.note] - the server's own mode note, rendered verbatim.
 * @param {string} [opts.extra] - screen-specific consequence of this mode.
 */
export function modeBanner(mode, { note, extra } = {}) {
  const key = normaliseMode(mode);
  const spec = MODES[key];
  const verified = !!(spec && spec.verified);
  const kind = verified ? 'info' : 'warning';
  const headline = key === 'UNKNOWN'
    ? 'Connector mode could not be read — treat everything on this screen as NOT VERIFIED.'
    : `Connector mode: ${key}${verified ? '.' : ' — NOT VERIFIED.'}`;
  return h('div', { class: `msg msg-${kind}` }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, verified ? '·' : '!'),
    h('div', { class: 'body' }, [
      h('strong', {}, headline),
      text(' '),
      text(note || (spec ? spec.summary : 'No mode was reported by the server.')),
      extra ? h('div', { class: 'small' }, extra) : null,
      h('div', { class: 'mid' }, `MODE=${key}`),
    ].filter(Boolean)),
  ]);
}

/* ------------------------------------------------------------------ *
 * Operational statuses (C16) and the business-visible bridge
 * ------------------------------------------------------------------ */

/**
 * C16 operational statuses to a non-colour tone. Every namespace is here in
 * ONE table because the same code means different things in different ones —
 * outbox PENDING is normal, job PENDING is normal, inbox has no PENDING at all
 * — so the namespace is a required argument rather than something inferred.
 */
const OPERATIONAL_TONE = {
  inbox: {
    RECEIVED: 'progress', PROCESSED: 'positive', QUARANTINED: 'warning',
    DISCARDED: 'neutral', DEAD: 'negative',
  },
  outbox: {
    PENDING: 'progress', SENT: 'positive', FAILED: 'warning', DEAD: 'negative',
  },
  job: {
    PENDING: 'neutral', CLAIMED: 'progress', CHECKPOINTED: 'progress',
    DONE: 'positive', FAILED: 'warning', DEAD: 'negative',
  },
  circuit: {
    CIRCUIT_CLOSED: 'positive', CIRCUIT_OPEN: 'negative', CIRCUIT_HALF_OPEN: 'warning',
  },
};

/**
 * An OPERATIONAL status chip, for SCR-26 / 38 / 39 only.
 *
 * C16's rule, verbatim: "Integration and job statuses are OPERATIONAL. They
 * are visible to administrators on SCR-26, SCR-38 and SCR-39. They must NEVER
 * be rendered on a business screen as if they were C3 business statuses." The
 * only enforcement this layer can offer is that the function lives in the
 * integration feature and takes a namespace — a business screen has no reason
 * to import it and no namespace to pass.
 *
 * An UNKNOWN code renders as itself with a neutral tone and never guesses a
 * meaning, which is the same fail-closed rule C17 applies to raw Zoho values.
 *
 * @param {'inbox'|'outbox'|'job'|'circuit'} namespace
 * @param {string} code
 */
export function operationalChip(namespace, code) {
  const value = String(code || '').trim().toUpperCase();
  if (!value) return statusChip({ label: 'UNKNOWN', tone: 'neutral', title: 'The server reported no status.' });
  const table = OPERATIONAL_TONE[namespace] || {};
  const tone = table[value];
  return statusChip({
    label: value,
    tone: tone || 'neutral',
    title: tone
      ? `${namespace} status ${value} (C16_integration_statuses.json).`
      : `${value} is not a declared ${namespace} status in C16_integration_statuses.json. `
        + 'It is shown verbatim rather than guessed at.',
  });
}

/**
 * The three business-visible bridge values from C16, and nothing else.
 *
 * These are the values an OBJECT carries alongside its C3 business status.
 * They are not statuses in the C3 sense and must never be substituted for one.
 */
export const INTEGRATION_STATES = Object.freeze(['QUEUED', 'SENT', 'FAILED']);

const INTEGRATION_STATE_TONE = { QUEUED: 'progress', SENT: 'positive', FAILED: 'negative' };

const INTEGRATION_STATE_TITLE = {
  QUEUED: 'An outbox row exists for this object in state PENDING. The business status is unchanged.',
  SENT: 'The outbox row is SENT and an external id was persisted. The business status is unchanged.',
  FAILED: 'The outbox row is FAILED or DEAD. This also sets the business status INTEGRATION_FAILED, '
    + 'which is a separate, C3 fact shown separately.',
};

/**
 * The `integration_state` badge.
 *
 * THIS IS NOT A TWENTY-SECOND BUSINESS STATUS, AND THE THROW IS THE PROOF.
 *
 * C16 exists because the temptation is to add QUEUED to C3 and be done with
 * it. A caller who has "a status string" and passes a C3 code here is making
 * exactly that mistake, and gets an exception rather than a badge that renders
 * an operational fact in the place a business status belongs. The frozen 21
 * are listed rather than imported because this module must not fetch a
 * contract file to validate an argument — and tests/test_contracts.py already
 * asserts C3 holds exactly these.
 */
const C3_BUSINESS_STATUSES = new Set([
  'DRAFT', 'SUBMITTED', 'UNDER_REVIEW', 'APPROVED', 'REJECTED', 'RETURNED', 'RELEASED',
  'PARTIALLY_COMMITTED', 'FULLY_COMMITTED', 'PARTIALLY_ACTUALISED', 'FULLY_ACTUALISED',
  'BUDGET_EXCEEDED', 'EXCEPTION_PENDING', 'TECHNICALLY_COMPLETED', 'FINANCIALLY_COMPLETED',
  'AWAITING_CAPITALISATION', 'CAPITALISED', 'CLOSED', 'REOPENED', 'INTEGRATION_FAILED',
  'RECONCILIATION_PENDING',
]);

export function integrationStateBadge(value) {
  const state = String(value || '').trim().toUpperCase();
  if (!INTEGRATION_STATES.includes(state)) {
    if (C3_BUSINESS_STATUSES.has(state)) {
      throw new Error(
        `integrationStateBadge(): "${state}" is a C3 business status, not an integration_state. `
        + 'C16 keeps these registries separate on purpose: an object carries its business status '
        + 'AND, separately, one of QUEUED | SENT | FAILED. Render the business status with the '
        + 'business status chip.',
      );
    }
    throw new Error(
      `integrationStateBadge(): "${state}" is not one of ${INTEGRATION_STATES.join(' | ')}.`,
    );
  }
  return h('span', { class: 'integration-state-badge' }, [
    h('span', { class: 'sr-only' }, 'Integration state: '),
    statusChip({
      label: state,
      tone: INTEGRATION_STATE_TONE[state],
      title: INTEGRATION_STATE_TITLE[state],
    }),
  ]);
}

/* ------------------------------------------------------------------ *
 * Source, unavailability, and leaked secrets
 * ------------------------------------------------------------------ */

const SOURCE_TEXT = {
  wave5: (template) => `Served by the Wave 5 integration platform (${template}).`,
  'wave4-compat': (template) => `The Wave 5 endpoint for this screen is not mounted in this build. `
    + `Shown from the Wave 4 connector surface (${template}) instead, which answers the same `
    + `question from the verified endpoint inventory — it is NOT the integration platform and `
    + `carries no inbox, outbox, job or rate-budget state.`,
  /* The THIRD source, and the one whose caveat is sharpest. The Wave 4
     connector surface at least answers a connector question. This one answers
     a LEDGER question — what this application itself holds — and is being read
     on a screen whose subject is what reached Zoho. Every screen that renders
     it also states, in its own words, which question it cannot answer from
     here; this line states the general form of that limit so a reader who
     skims only the source line still gets it. */
  'ledger-compat': (template) => `The Wave 5 endpoint for this screen is not mounted in this build. `
    + `Shown from this application's own ledger (${template}) instead. That is OUR record of these `
    + `documents, not Zoho's: it says what exists here and says NOTHING about what was sent, `
    + `received, queued, retried or acknowledged. No integration state on this screen is measured; `
    + `the columns that would carry it are marked as not available rather than filled in.`,
};

/**
 * The visible source line. Every screen renders one whenever it renders data.
 *
 * A fallback that did not say it was a fallback would let an operator read
 * Wave-4 mock output as Wave-5 platform state — the same class of dishonesty
 * as rendering an absent endpoint as an empty list.
 */
export function sourceLine(result) {
  if (!result) return null;
  const build = SOURCE_TEXT[result.source];
  if (!build) return null;
  const compat = result.source !== 'wave5';
  return h('p', {
    class: `xs muted integration-source${compat ? ' integration-source-compat' : ''}`,
    'data-source': result.source,
  }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, compat ? '!' : '·'),
    text(' '),
    text(build(result.template)),
  ]);
}

/**
 * A cell whose value CANNOT BE MEASURED from the source that answered.
 *
 * The three transaction-queue screens fall back to this application's own
 * ledger, which knows every purchase order, receipt and bill it holds and
 * knows NOTHING about whether any of them reached Zoho. The integration-state
 * columns on those screens are therefore not empty and not zero — either would
 * read as a measurement — but explicitly unmeasurable, with the reason
 * attached.
 *
 * An em-dash alone was rejected for exactly this reason: on a table where
 * other rows DO carry a value, a dash reads as "this one has none", which is a
 * different and false claim.
 *
 * @param {string} why - the reason, rendered in the title and to a screen
 *   reader. Written as a full sentence.
 */
export function notMeasured(why) {
  return h('span', { class: 'integration-unmeasured', title: why }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'),
    text(' '),
    h('span', { class: 'xs muted' }, 'not measured'),
    h('span', { class: 'sr-only' }, ` — ${why}`),
  ]);
}

/**
 * The state the shared approval state host cannot express: THIS ROUTE IS NOT
 * MOUNTED.
 *
 * It is deliberately not the empty state and not the error state. "No records
 * were found" would be false, and an error box with a Retry button would
 * invite the operator to retry something that cannot succeed until a different
 * stream lands. This says what is true, names the route, and offers no retry.
 */
export function unavailableBlock(err, { what }) {
  return h('div', { class: 'msg msg-info integration-unavailable', role: 'status' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
    h('div', { class: 'body' }, [
      h('strong', {}, `${what} is not available in this build.`),
      h('div', {}, [
        text('This screen reads '),
        h('code', { class: 'mono' }, err && err.path ? err.path : 'an endpoint'),
        text(', which this deployment does not mount. Nothing is shown here because there is '
          + 'nothing to show — not because the queue is empty.'),
      ]),
      err && err.note ? h('div', { class: 'small' }, err.note) : null,
    ].filter(Boolean)),
  ]);
}

/**
 * REQ-INT-024's visible half: a server that returned a secret-shaped field.
 *
 * integration-api.js's `scrub()` removed the value before it could reach the
 * DOM, so nothing is leaked by rendering this. Reporting it is the point: a
 * silent drop would let a backend regression that leaks a client secret ship
 * unnoticed, because the screen would look exactly the same.
 */
export function redactionWarning(redacted) {
  if (!redacted || !redacted.length) return null;
  return h('div', { class: 'msg msg-error integration-redaction', role: 'alert' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
    h('div', { class: 'body' }, [
      h('strong', {}, 'The server returned a field that must never leave it.'),
      h('div', {}, [
        text('REQ-INT-024: the client secret is never returned by any API. '),
        text(`${redacted.length} secret-shaped field${redacted.length === 1 ? '' : 's'} `),
        text('arrived in this response and '),
        text(redacted.length === 1 ? 'was' : 'were'),
        text(' discarded before rendering: '),
        h('span', { class: 'mono' }, redacted.join(', ')),
        text('. Report this as a backend defect; the value is not shown and was not stored.'),
      ]),
    ]),
  ]);
}

/* ------------------------------------------------------------------ *
 * Small shared layout
 * ------------------------------------------------------------------ */

/**
 * A titled card, matching the approved `.card` structure with an <h2> heading.
 *
 * The same reasoning as the approval screens' `card()`: the shell renders the
 * screen name as the page <h1>, and the frozen stylesheet's card header is
 * `.card > h3`, so reusing it directly would skip a heading level. axe's
 * heading-order flags that and a screen-reader user loses the outline.
 * `.integration-card-title` in integration.css reproduces `.card > h3` from
 * the same tokens, so the card is visually identical and only the element name
 * and the document outline differ.
 */
export function card(headingId, headingText, children, { actions = null } = {}) {
  return h('section', { class: 'card integration-card', 'aria-labelledby': headingId }, [
    h('h2', { id: headingId, class: 'integration-card-title' }, [
      headingText,
      actions ? h('span', { class: 'spacer' }) : null,
      actions,
    ].filter(Boolean)),
    h('div', { class: 'card-body' }, children),
  ]);
}

/** A definition list using the frozen `.kv` grid, as a real <dl>. */
export function keyValues(pairs) {
  const dl = h('dl', { class: 'kv integration-kv' });
  for (const [label, value] of pairs) {
    if (value === null || value === undefined) continue;
    dl.appendChild(h('dt', {}, label));
    dl.appendChild(h('dd', {}, value));
  }
  return dl;
}

/** A labelled control. `label` is a real <label for>, never a placeholder. */
export function field(id, labelText, control, { hint } = {}) {
  control.id = id;
  if (hint) {
    control.setAttribute('aria-describedby', `${id}Hint`);
  }
  return {
    el: h('div', { class: 'field' }, [
      h('label', { for: id }, labelText),
      control,
      hint ? h('div', { id: `${id}Hint`, class: 'xs muted' }, hint) : null,
    ].filter(Boolean)),
    control,
  };
}

export function textInput(attrs = {}) {
  return h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', ...attrs });
}

export function selectInput(options, attrs = {}) {
  return h('select', attrs, options.map(([value, label]) => h('option', { value }, label)));
}

/** An announcer bound to a live region the HOST created. Never creates one. */
export function createAnnouncer(regionId) {
  const region = document.getElementById(regionId);
  function announce(message) {
    announce.calls += 1;
    if (region) region.textContent = String(message ?? '');
  }
  /* See the twin in analytics-kit.js. A live region holds one message, so
     `integration-screen.js::run` counts the writes across the render call and
     announces its own default only when the screen announced nothing — a
     generic sentence must never replace a screen's row count. */
  announce.calls = 0;
  return announce;
}

/** Replace a host's children with one node. */
export function replace(host, node) {
  clear(host);
  if (node) host.appendChild(node);
}

/**
 * Read a query parameter from the current URL.
 *
 * The shell routes on the HASH, so the search string survives a hash change —
 * which is what makes `/?connection=C1#integration-health` the deep link that
 * works, exactly as the approval screens use `/?instance=X#approval-request`.
 */
export function queryParam(name) {
  try { return new URLSearchParams(window.location.search).get(name); } catch { return null; }
}
