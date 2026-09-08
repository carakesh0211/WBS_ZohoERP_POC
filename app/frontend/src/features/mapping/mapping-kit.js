/* app/frontend/src/features/mapping/mapping-kit.js
   The parts SCR-35, 36, 37 and 40 share, and none of them should re-invent.

   THE DISABLED CONTROL THAT STATES ITS REASON
   -------------------------------------------
   The brief for SCR-37 is explicit: an unsupported incremental control is
   DISABLED WITH THE DOCUMENTED REASON, not hidden. Hiding it is the tempting
   choice — a screen with fewer dead controls looks better — and it is the
   wrong one twice over.

     * A control that is absent teaches nothing. An operator who expected an
       incremental sync for Purchase Receives and cannot find the switch
       concludes the screen is incomplete, and goes looking for it somewhere
       else, or asks for it to be built.
     * The reason is the deliverable. "Zoho ERP publishes no list endpoint for
       Purchase Receives, so there is no delta feed to poll" is a fact about
       somebody else's API that this project paid to establish (OAS-02). A
       hidden control discards it.

   So `disabledControl()` renders the real control, disabled, with the reason
   attached to it — in the accessible name, in a visible note, and in the
   title. A screen-reader user gets the reason from the control itself rather
   than having to find a paragraph near it.

   NOTHING HERE STYLES A STATUS, A MESSAGE BOX OR A TABLE. Those come from the
   byte-frozen styles.css unchanged, exactly as integration.css leaves them.
*/

import { h, text } from '../../core/dom.js';
import { statusChip } from '../../components/capex-statuschip.js';

/* ------------------------------------------------------------------ *
 * Disabled controls, and the reason
 * ------------------------------------------------------------------ */

/**
 * Wrap a control so it is DISABLED and says why, permanently and visibly.
 *
 * @param {HTMLElement} control - the real control, rendered disabled.
 * @param {Object} opts
 * @param {string} opts.reason - a full sentence. Rendered visibly, put in the
 *   control's title, and joined to its accessible name so a screen-reader user
 *   hears the reason when they land on the control rather than only if they
 *   happen to read the paragraph beneath it.
 * @param {string} [opts.evidence] - the finding id or document that established
 *   the reason, e.g. 'OAS-02'. Rendered as a separate, quotable token.
 */
export function disabledControl(control, { reason, evidence } = {}) {
  const id = control.id ? `${control.id}Reason` : null;
  control.disabled = true;
  control.setAttribute('aria-disabled', 'true');
  control.title = reason;
  if (id) {
    control.setAttribute('aria-describedby',
      [control.getAttribute('aria-describedby'), id].filter(Boolean).join(' '));
  }
  return h('div', { class: 'mapping-disabled' }, [
    control,
    h('p', id ? { id, class: 'xs muted mapping-reason' } : { class: 'xs muted mapping-reason' }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '⊘'),
      text(' '),
      h('span', { class: 'sr-only' }, 'Unavailable, and this is why: '),
      text(reason),
      evidence ? text(' ') : null,
      evidence ? h('span', { class: 'mono mapping-evidence' }, evidence) : null,
    ].filter(Boolean)),
  ]);
}

/**
 * A whole GROUP of controls that cannot be operated, with one shared reason.
 *
 * Used where the reason is about the build rather than about one field: SCR-36
 * and SCR-37 have no route to write to, so every control that would save
 * something is disabled together, once, with one explanation. Repeating the
 * same sentence beside six controls reads as six problems.
 */
export function disabledFieldset(legend, children, { reason, evidence } = {}) {
  return h('fieldset', { class: 'mapping-fieldset', disabled: true, 'aria-disabled': 'true' }, [
    h('legend', { class: 'mapping-legend' }, legend),
    h('div', { class: 'msg msg-info mapping-governed', role: 'note' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '⊘'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'These controls are shown, disabled, rather than hidden.'),
        h('div', {}, reason),
        evidence ? h('div', { class: 'small mono' }, evidence) : null,
      ].filter(Boolean)),
    ]),
    h('div', { class: 'mapping-fieldset-body' }, children),
  ]);
}

/* ------------------------------------------------------------------ *
 * Status vocabularies
 * ------------------------------------------------------------------ */

const MAPPING_STATUS_TONE = {
  MAPPED: 'positive',
  UNMAPPED: 'neutral',
  NEEDS_REVIEW: 'warning',
  DUPLICATE_SUSPECT: 'negative',
};

const MAPPING_STATUS_TITLE = {
  MAPPED: 'This record is bound to an external record. The binding is recorded on the row, not '
    + 'inferred from a name match.',
  UNMAPPED: 'No external record is bound to this one. It is not a conflict and not an error: '
    + 'a locally created master starts here.',
  NEEDS_REVIEW: 'A human decision is required before this record can be treated as mapped. '
    + 'Nothing about the row is guessed at in the meantime.',
  DUPLICATE_SUSPECT: 'The registry flagged this row as a probable duplicate of another and '
    + 'recorded which one in duplicate_of. Posting against the wrong one of a duplicate pair '
    + 'splits a vendor ledger.',
};

/**
 * The `mapping_status` chip.
 *
 * An UNKNOWN value renders VERBATIM with a neutral tone and no invented
 * meaning — the same fail-closed rule integration-kit.js applies to a raw Zoho
 * status. The four known values are frozen by a database CHECK constraint, so
 * a fifth arriving means the constraint changed and this table is stale;
 * showing it as itself is how that becomes visible instead of being rounded to
 * the nearest familiar word.
 */
export function mappingStatusChip(value) {
  const state = String(value || '').trim().toUpperCase();
  if (!state) {
    return statusChip({
      label: 'NOT REPORTED',
      tone: 'neutral',
      title: 'The master registry returned no mapping_status for this row.',
    });
  }
  const tone = MAPPING_STATUS_TONE[state];
  return statusChip({
    label: state,
    tone: tone || 'neutral',
    title: tone ? MAPPING_STATUS_TITLE[state]
      : `${state} is not one of the four values migrations/pg/005_master_data.sql permits for `
        + 'mapping_status. It is shown verbatim rather than guessed at.',
  });
}

/**
 * A capability flag from the verified endpoint inventory, as a word.
 *
 * Never a tick or a cross alone: this is the value that decides whether an
 * incremental sync is offered at all, and a glyph that carries the meaning by
 * itself is lost to greyscale, to a screen reader that skips decorative
 * characters, and to anybody scanning a dense table quickly.
 */
export function capabilityChip(supported, { yes, no, why }) {
  if (supported === true) {
    return statusChip({ label: yes, tone: 'positive', title: why || '' });
  }
  if (supported === false) {
    return statusChip({ label: no, tone: 'warning', title: why || '' });
  }
  return statusChip({
    label: 'NOT REPORTED',
    tone: 'neutral',
    title: 'The inventory returned no value for this capability. It is not shown as "no", which '
      + 'would be a claim the specification does not make.',
  });
}

/* ------------------------------------------------------------------ *
 * Counts that are not totals
 * ------------------------------------------------------------------ */

/**
 * The number of rows ON THIS PAGE, said as such — never as a total.
 *
 * `/api/masters/{kind}` and `/api/audit/entries` are cursor-paginated and
 * neither returns a count of the whole set. "128 vendors" rendered from a
 * 50-row page is a fabricated total, and on a mapping workbench it is the
 * fabricated total that matters most: "12 unmapped" reads as the size of the
 * remaining work.
 *
 * @param {number} shown
 * @param {boolean} hasMore - the server's own `has_more`.
 * @param {string} noun - plural noun, e.g. 'vendors'.
 */
export function pageCount(shown, hasMore, noun) {
  return h('p', { class: 'xs muted mapping-count' }, [
    text(`${shown} ${noun} on this page.`),
    text(' '),
    hasMore
      ? h('span', { class: 'mapping-nototal' }, [
        h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'),
        text(' More pages follow. This endpoint reports no total, so the size of the whole set '
          + 'is not shown — a number derived from one page would read as one.'),
      ])
      : text('This is the last page.'),
  ]);
}

/**
 * A metric that CANNOT be stated because the source does not carry it.
 *
 * Distinct from zero and distinct from blank, for the same reason
 * integration-kit.js's `notMeasured()` is: on a tile row where the others show
 * numbers, a blank reads as none and a zero reads as a measurement.
 */
export function unavailableMetric(label, why) {
  return h('div', { class: 'tile mapping-tile-unavailable' }, [
    h('div', { class: 'k' }, label),
    h('div', { class: 'v mapping-tile-word' }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'),
      text(' not available'),
    ]),
    h('div', { class: 'sub' }, why),
  ]);
}

/* ------------------------------------------------------------------ *
 * Small shared blocks
 * ------------------------------------------------------------------ */

/**
 * The line every one of these four screens carries: NO LIVE ZOHO RESULT
 * APPEARS ANYWHERE IN THIS APPLICATION.
 *
 * The endpoint inventory is CONFIRMED-BY-VENDOR-SPECIFICATION, which is a
 * statement about a document, and the word "confirmed" beside a field list is
 * exactly where a reader starts believing a call was made. No sandbox
 * credential exists for this project and no live call has ever been made, so
 * every capability on SCR-36 and SCR-37 is a specification claim and says so.
 */
export function specificationNotice(extra) {
  return h('div', { class: 'msg msg-warning mapping-spec-notice' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
    h('div', { class: 'body' }, [
      h('strong', {}, 'Read from the verified specification, not from a live tenant.'),
      text(' Every capability below is CONFIRMED-BY-VENDOR-SPECIFICATION — confirmed against '
        + 'Zoho\'s own OpenAPI document, which is a statement about that document. No credential '
        + 'has been issued to this project and no live or sandbox call has ever been made, so '
        + 'nothing here is verified against a running tenant.'),
      extra ? h('div', { class: 'small' }, extra) : null,
    ].filter(Boolean)),
  ]);
}

/**
 * REQ-INT-024, stated on the screen that most needs it.
 *
 * SCR-40 is a CREDENTIAL ACTIVITY log. A reader arriving at a screen with that
 * name may reasonably be looking for a credential, and must be told at the top
 * that they will not find one here and why that is deliberate rather than an
 * omission.
 */
export function noSecretsNotice() {
  return h('div', { class: 'msg msg-info mapping-no-secrets', role: 'note' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
    h('div', { class: 'body' }, [
      h('strong', {}, 'No credential value is shown on this screen, at any permission level.'),
      text(' REQ-INT-024: the client secret is never returned by any API, never rendered in the '
        + 'page and never logged. This is the record of what was DONE with a credential — who, '
        + 'when, against which connection, and with what result. There is no permission that '
        + 'reveals a secret here, because there is no secret here to reveal.'),
    ]),
  ]);
}

/**
 * A reveal control for NON-SECRET operational detail, gated on a permission.
 *
 * The gate is presentational and the code says so: the server decided what to
 * put in the response before this ran, and hiding a field the server sent
 * would be a security theatre that a reader of the network tab sees through in
 * a second. What the gate actually does is keep a dense operational payload —
 * correlation ids, entry hashes, stream keys — out of the way of a reader who
 * cannot act on it.
 *
 * @param {boolean} allowed
 * @param {string} permission - named on screen when the control is withheld.
 */
export function revealNote(allowed, permission) {
  if (allowed) {
    return h('p', { class: 'xs muted' }, [
      text('Operational detail is collapsed on each row. It carries correlation ids, stream keys '
        + 'and chain hashes — never a credential.'),
    ]);
  }
  return h('p', { class: 'xs muted mapping-reveal-withheld' }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, '⊘'),
    text(' Per-row operational detail is not offered to this session, which does not hold '),
    h('span', { class: 'mono' }, permission),
    text('. The rows themselves are unchanged: the server decides what it returns, and it '
      + 'returned no credential to either kind of reader.'),
  ]);
}

/**
 * A card, matching integration-kit.js's structure and its reasoning: the shell
 * renders the screen name as the page <h1>, so a card header must be an <h2>
 * or the heading outline skips a level and axe flags it.
 */
export function card(headingId, headingText, children, { actions = null } = {}) {
  return h('section', { class: 'card mapping-card', 'aria-labelledby': headingId }, [
    h('h2', { id: headingId, class: 'mapping-card-title' }, [
      headingText,
      actions ? h('span', { class: 'spacer' }) : null,
      actions,
    ].filter(Boolean)),
    h('div', { class: 'card-body' }, children),
  ]);
}

/** A definition list on the frozen `.kv` grid, as a real <dl>. */
export function keyValues(pairs) {
  const dl = h('dl', { class: 'kv mapping-kv' });
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
  if (hint) control.setAttribute('aria-describedby', `${id}Hint`);
  return {
    el: h('div', { class: 'field' }, [
      h('label', { for: id }, labelText),
      control,
      hint ? h('div', { id: `${id}Hint`, class: 'xs muted' }, hint) : null,
    ].filter(Boolean)),
    control,
  };
}

export function selectInput(options, attrs = {}) {
  return h('select', attrs, options.map(([value, label]) => h('option', { value }, label)));
}

export function textInput(attrs = {}) {
  return h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', ...attrs });
}

/** An announcer bound to a live region the HOST created. Never creates one. */
export function createAnnouncer(regionId) {
  const region = document.getElementById(regionId);
  return function announce(message) {
    if (region) region.textContent = String(message ?? '');
  };
}

/** Read a query parameter. The shell routes on the hash, so search survives. */
export function queryParam(name) {
  try { return new URLSearchParams(window.location.search).get(name); } catch { return null; }
}

/** A monospaced identifier, or an explicit "none" that cannot read as blank. */
export function identifier(value, { none = 'none' } = {}) {
  const s = value === null || value === undefined ? '' : String(value).trim();
  if (!s) return h('span', { class: 'muted mapping-none' }, none);
  return h('span', { class: 'mono' }, s);
}

/** An ISO timestamp rendered as itself. No relative time, no re-formatting. */
export function timestamp(value) {
  const s = value === null || value === undefined ? '' : String(value).trim();
  if (!s) return h('span', { class: 'muted mapping-none' }, 'not recorded');
  return h('time', { class: 'mono', datetime: s }, s);
}
