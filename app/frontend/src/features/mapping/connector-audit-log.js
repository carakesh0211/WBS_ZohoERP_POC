/* app/frontend/src/features/mapping/connector-audit-log.js
   SCR-40 — Connector Audit and Credential Activity Log.

   THE ONE RULE THAT OUTRANKS EVERY OTHER RULE ON THIS SCREEN
   -----------------------------------------------------------
   NO SECRET, TOKEN OR CREDENTIAL VALUE IS RENDERED HERE. Not to an
   administrator, not behind a reveal, not in a tooltip, not in a data
   attribute, not in a title. REQ-INT-024: the client secret is never returned
   by any API, never in the DOM, never logged.

   This screen is named "Credential Activity Log" and that name is exactly why
   the rule needs restating at the top of the file rather than assumed. A
   reader arriving here may be looking for a credential; a developer extending
   this file may reasonably think "the administrator is allowed to see it".
   Neither is true. What this screen holds is the record of what was DONE with
   a credential — who, when, against which connection, with what result, under
   which correlation id. That is metadata about a credential and it is a
   different thing from a credential.

   THREE INDEPENDENT MEASURES, BECAUSE ONE OF THEM IS A PROMISE ABOUT SOMEBODY
   ELSE'S CODE
   ---------------------------------------------------------------------------
   1. The backend does not put one in the response. `/api/audit/entries`
      returns audit_id, stream_key, seq, at, actor, action, object_type,
      object_id, detail, correlation_id and entry_hash — there is no credential
      column in `audit_log` to leak.

   2. Every response passes through integration-api.js's `scrub()`, which
      deletes any key matching /secret|refresh_token|access_token|password|
      private_key|credential_value/i before it reaches this file, and reports
      what it deleted. A backend regression that started writing a token into
      an audit detail produces a visible defect warning here, not a token.

   3. This file renders a FIXED FIELD LIST. Nothing iterates the keys of a row
      and prints what it finds — which is the mechanism by which a new server
      field would otherwise appear on screen the day it was added, unreviewed.
      Every cell and every reveal names its field.

   THE PERMISSION-GATED REVEAL, AND WHAT IT HONESTLY IS
   -----------------------------------------------------
   The brief asks for a permission-gated reveal of NON-SECRET OPERATIONAL
   DETAIL, and that is what this is: correlation id, stream key, sequence
   number and chain hash, collapsed per row and offered to a principal holding
   `audit.read` — the permission the audit router itself enforces. See
   REVEAL_PERMISSION below for why it is not `connector.read`.

   It is stated on the screen as what it is — a disclosure of clutter, not a
   security boundary. The server decided the contents of the response before
   this code ran; hiding a field the server already sent would be theatre that
   anybody with a network tab sees through in a second, and pretending
   otherwise is worse than not having the control. The real gate is upstream:
   `/api/audit/entries` refuses a caller without `audit.read` at the router,
   before its handler runs.

   A LOG IS NOT AN AUDIT TRAIL UNTIL THE CHAIN IS VERIFIED
   -------------------------------------------------------
   `audit_log` is hash-chained, and this screen verifies the chain rather than
   assuming it. An unverified chain of credential activity is a list of claims;
   the difference matters most precisely here, where the claims are about who
   touched a credential. The verification result is rendered as its own
   statement, and a chain that could not be checked says so rather than
   defaulting to intact.
*/

import { h, text } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from '../integration/integration-screen.js';
import { operationalChip } from '../integration/integration-kit.js';
import {
  CONNECTOR_OBJECT_TYPES, getPrincipal, listConnectorAudit, listEvents, verifyAuditChain,
} from './mapping-api.js';
import {
  card, createAnnouncer, field, identifier, keyValues, noSecretsNotice, pageCount, queryParam,
  revealNote, selectInput, textInput, timestamp,
} from './mapping-kit.js';

const LIMIT = 50;

/**
 * The permission that opens the per-row operational detail. Presentational.
 *
 * `audit.read`, NOT `connector.read`, and the difference is the whole of
 * finding L2. The rows this screen shows come from `GET /api/audit/entries`
 * and `GET /api/audit/chain/verify`, and that router enforces `audit.read`
 * (`app/backend/api/audit.py`, the router-level `require_audit_read`
 * dependency). Gating the screen on a DIFFERENT permission than the API
 * enforces is a latent defect, not a live one: both are held today by exactly
 * `Administrator` and `Auditor` (`app/backend/auth.py`), so the two sets are
 * identical and `state.canReveal` is unconditionally true. The day somebody
 * grants `connector.read` to a third role — an integration operator, say —
 * this screen would offer a control the API refuses, and the user would meet
 * a 403 the interface had just told them they were entitled to.
 *
 * The screen now asks for the permission the server actually checks.
 */
const REVEAL_PERMISSION = 'audit.read';

/**
 * The result of one recorded action, read from the fields the server sent.
 *
 * `audit_log` records what HAPPENED, so an entry existing is itself the
 * outcome — there is no failure row to find, because a refused action never
 * reached the append. That is stated on the chip rather than dressed up as a
 * measured PASS: "RECORDED" is true and "SUCCEEDED" would be an inference.
 */
function resultChip(row) {
  const action = String(row.action || '').trim();
  if (!action) {
    return statusChip({
      label: 'NOT REPORTED',
      tone: 'neutral',
      title: 'The audit entry carries no action.',
    });
  }
  return statusChip({
    label: 'RECORDED',
    tone: 'positive',
    title: 'An audit entry exists for this action, which means the action completed far enough to '
      + 'be appended to the chain. It is not a separate success flag: an action refused before it '
      + 'ran leaves no entry, so absence is the failure signal here, not a FAILED row.',
  });
}

export function mountConnectorAuditLog(root) {
  if (!root) return;
  const announce = createAnnouncer('mappingLiveRegion');

  const state = {
    objectType: CONNECTOR_OBJECT_TYPES.includes(queryParam('object_type'))
      ? queryParam('object_type') : '',
    objectId: queryParam('object_id') || '',
    rows: [],
    hasMore: false,
    canReveal: false,
    principalRead: false,
  };

  /* ---------------- filters ---------------- */

  const typeSelect = selectInput([
    ['', 'Every connector stream'],
    ...CONNECTOR_OBJECT_TYPES.map((t) => [t, t]),
  ], { onChange: () => { state.objectType = typeSelect.value; load(); } });
  typeSelect.value = state.objectType;
  const typeField = field('calType', 'Connector stream', typeSelect, {
    hint: 'Every connector stream is offered whether or not it currently has entries — an empty '
      + 'stream and an unlisted stream are different facts.',
  });

  const objectInput = textInput({
    value: state.objectId,
    onChange: () => { state.objectId = objectInput.value; load(); },
  });
  const objectField = field('calObject', 'Connection or row id', objectInput, {
    hint: 'Deep-linkable: /?object_type=integration_connection&object_id=CONN-01#connector-audit.',
  });

  /* ---------------- the log ---------------- */

  /**
   * THE REVEAL IS DECIDED AT CONSTRUCTION, NOT TOGGLED AFTERWARDS.
   *
   * createDataTable renders a per-row Details button if and only if it was
   * given a `renderRowDetail` when it was built. So the table is built AFTER
   * the acting principal is known, and a session without the permission gets
   * no control at all rather than one that opens an empty panel — a control
   * that never does anything reads as a fault in the screen.
   */
  let table = null;
  const tableHost = h('div', { id: 'calTableHost' });

  function buildTable(withDetail) {
    return createDataTable({
    caption: 'Connector and credential activity: actor, action, time, connection, result and '
      + 'correlation id',
    emptyMessage: 'No connector activity has been recorded for these filters.',
    renderRowDetail: withDetail ? rowDetail : undefined,
    columns: [
      { key: 'at', label: 'Timestamp', render: (r) => timestamp(r.at) },
      {
        key: 'actor',
        label: 'Actor',
        render: (r) => identifier(r.actor, { none: 'no actor recorded' }),
      },
      {
        key: 'action',
        label: 'Action',
        render: (r) => h('div', { class: 'mapping-idpair' }, [
          text(String(r.action || 'not reported')),
          h('span', { class: 'xs muted' }, String(r.object_type || '')),
        ]),
      },
      {
        key: 'connection',
        label: 'Connection / object',
        render: (r) => identifier(r.object_id, { none: 'no object recorded' }),
      },
      { key: 'result', label: 'Result', render: (r) => resultChip(r) },
      {
        key: 'correlation_id',
        label: 'Correlation id',
        render: (r) => identifier(r.correlation_id, { none: 'not correlated' }),
      },
      {
        key: 'detail',
        label: 'What was recorded',
        /* `detail` is a STRING written by the backend, rendered through a Text
           node by h(). It is never parsed, never interpolated into markup, and
           never scanned for something to turn into a control. */
        render: (r) => text(r.detail || 'The entry carries no detail.'),
      },
    ],
    });
  }

  const counts = h('div', { id: 'calCounts' });
  const reveal = h('div', { id: 'calReveal' });

  const loader = createLoader({
    id: 'calStatus',
    glyph: '⧉',
    what: 'The connector and credential activity log',
    emptyMessage: 'No connector activity has been recorded for these filters. This is the audit '
      + 'trail\'s own answer — an action refused before it ran leaves no entry, so an empty result '
      + 'here is not evidence that nothing was attempted.',
    loadingMessage: 'Reading the audit trail…',
    onRetry: () => load(),
    announce,
  });

  /* ---------------- chain verification ---------------- */

  const chain = h('div', { id: 'calChain' });

  const chainLoader = createLoader({
    id: 'calChainStatus',
    glyph: '⛓',
    what: 'Chain verification',
    emptyMessage: 'The verifier returned no result for this stream.',
    loadingMessage: 'Verifying the hash chain…',
    onRetry: () => loadChain(),
    announce,
  });

  /* ---------------- the connector event log ---------------- */

  const eventTable = createDataTable({
    caption: 'Connector transport events for the same correlation ids',
    emptyMessage: 'No connector transport event is available.',
    columns: [
      { key: 'at', label: 'Timestamp', render: (r) => timestamp(r.at) },
      { key: 'module', label: 'Module', render: (r) => identifier(r.module) },
      {
        key: 'direction',
        label: 'Direction',
        render: (r) => text(r.direction || 'not reported'),
      },
      {
        key: 'state',
        label: 'Result',
        render: (r) => operationalChip(
          String(r.queue || '').toLowerCase() === 'outbox' ? 'outbox' : 'inbox',
          r.state,
        ),
      },
      {
        key: 'correlation_id',
        label: 'Correlation id',
        render: (r) => identifier(r.correlation_id, { none: 'not correlated' }),
      },
    ],
  });

  const eventLoader = createLoader({
    id: 'calEventStatus',
    glyph: '⇄',
    what: 'The connector transport event log',
    emptyMessage: 'The connector transport event log holds no event.',
    loadingMessage: 'Reading the connector transport event log…',
    onRetry: () => loadEvents(),
    announce,
  });

  /* ---------------- layout ---------------- */

  root.appendChild(noSecretsNotice());

  root.appendChild(card('calFilterTitle', 'What to show', [
    h('div', { class: 'toolbar mapping-toolbar' }, [typeField.el, objectField.el]),
  ]));

  root.appendChild(card('calChainTitle', 'Is this an audit trail, or a list?', [
    h('p', { class: 'muted small' },
      'The entries below are hash-chained. Until the chain is verified they are a list of claims '
      + 'about who touched a credential, which is not the same thing as a record of it.'),
    chainLoader.el,
    chain,
  ]));

  root.appendChild(card('calTableTitle', 'Connector and credential activity', [
    loader.el,
    reveal,
    counts,
    tableHost,
  ]));

  root.appendChild(card('calEventTitle', 'Transport events for the same correlation ids', [
    h('p', { class: 'muted small' },
      'The transport event log is NOT hash-chained and is not an audit record. It is shown '
      + 'alongside because the correlation id is the same, so a recorded decision above can be '
      + 'traced to the call it caused — and because a transport event exists for attempts that '
      + 'never reached the audit trail at all.'),
    eventLoader.el,
    eventTable.el,
  ]));

  root.appendChild(card('calRuleTitle', 'What this screen will not show', [
    keyValues([
      ['Client secret', text('Never. REQ-INT-024: it is not returned by any API, so it is not in '
        + 'the response this screen renders, and every response is additionally scrubbed of any '
        + 'secret-shaped key before it reaches the renderer.')],
      ['Access or refresh token', text('Never, for the same reason and by the same mechanism. '
        + 'Token EXPIRY and LAST-REFRESHED times are operational metadata and appear on the '
        + 'connection health screen; the token values appear nowhere.')],
      ['Any reveal that would show one', text('There is none. The reveal on this screen discloses '
        + 'correlation ids, stream keys, sequence numbers and chain hashes. No permission in this '
        + 'application returns a credential value to a browser.')],
      ['If a server ever returned one', text('A visible defect warning is rendered above the data '
        + 'naming the field, and the value is discarded before it reaches the page. The warning is '
        + 'never suppressed: a silent drop would let the regression ship unnoticed.')],
    ]),
  ]));

  /* ---------------- loading ---------------- */

  function params() {
    const out = { limit: LIMIT };
    if (state.objectType) out.object_type = state.objectType;
    if (state.objectId) out.object_id = state.objectId;
    return out;
  }

  /**
   * The per-row operational detail. NON-SECRET BY CONSTRUCTION: it names four
   * fields and reads only those four.
   */
  function rowDetail(row) {
    return keyValues([
      ['Correlation id', identifier(row.correlation_id, { none: 'not correlated' })],
      ['Audit stream', identifier(row.stream_key, { none: 'no stream recorded' })],
      ['Sequence in stream', row.seq === null || row.seq === undefined
        ? h('span', { class: 'muted' }, 'not reported')
        : h('span', { class: 'mono' }, String(row.seq))],
      ['Entry hash', identifier(row.entry_hash, { none: 'no hash on this entry' })],
    ]);
  }

  function renderReveal() {
    while (reveal.firstChild) reveal.removeChild(reveal.firstChild);
    reveal.appendChild(revealNote(state.canReveal, REVEAL_PERMISSION));
  }

  function renderCounts() {
    while (counts.firstChild) counts.removeChild(counts.firstChild);
    counts.appendChild(pageCount(state.rows.length, !!state.hasMore, 'entries'));
  }

  async function load() {
    if (!table) return;
    await loader.run(() => listConnectorAudit(params()), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items : [];
        state.rows = items;
        state.hasMore = !!(data && data.has_more);
        if (!items.length) {
          table.renderRows([]);
          table.el.hidden = true;
          while (counts.firstChild) counts.removeChild(counts.firstChild);
          return false;
        }
        table.el.hidden = false;
        table.renderRows(items);
        renderCounts();
        announce(`${items.length} recorded action${items.length === 1 ? '' : 's'} shown.`);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') {
          table.renderRows([]);
          table.el.hidden = true;
          while (counts.firstChild) counts.removeChild(counts.firstChild);
        }
      },
    });
  }

  async function loadChain() {
    await chainLoader.run(() => verifyAuditChain(state.objectId || ''), {
      render: (data) => {
        while (chain.firstChild) chain.removeChild(chain.firstChild);
        if (!data || typeof data !== 'object') return false;
        const intact = data.intact === true;
        const found = data.stream_found !== false;
        chain.appendChild(keyValues([
          ['Verdict', !found
            ? statusChip({
              label: 'STREAM NOT FOUND',
              tone: 'neutral',
              title: 'The verifier has no such stream. That is not a broken chain and is not an '
                + 'intact one.',
            })
            : statusChip({
              label: intact ? 'CHAIN INTACT' : 'CHAIN BROKEN',
              tone: intact ? 'positive' : 'negative',
              title: intact
                ? 'Every entry hashes onto its predecessor over the range checked.'
                : 'An entry does not hash onto its predecessor. The entries below are a list, not '
                  + 'an audit trail, until this is explained.',
            })],
          ['Entries checked', data.entries_checked === null || data.entries_checked === undefined
            ? h('span', { class: 'muted' }, 'not reported')
            : h('span', { class: 'mono' }, String(data.entries_checked))],
          ['First break at sequence', data.first_break_seq === null
            || data.first_break_seq === undefined
            ? h('span', { class: 'muted' }, 'none')
            : h('span', { class: 'mono' }, String(data.first_break_seq))],
          ['Sequence contiguous', data.sequence_contiguous === undefined
            ? h('span', { class: 'muted' }, 'not reported')
            : statusChip({
              label: data.sequence_contiguous ? 'CONTIGUOUS' : 'GAP IN SEQUENCE',
              tone: data.sequence_contiguous ? 'positive' : 'warning',
            })],
          ['Verified at', timestamp(data.verified_at)],
          ['Truncation', data.whole_stream_truncation_note
            ? text(String(data.whole_stream_truncation_note))
            : null],
        ]));
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') while (chain.firstChild) chain.removeChild(chain.firstChild);
      },
    });
  }

  async function loadEvents() {
    await eventLoader.run(() => listEvents({ limit: LIMIT }), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items : [];
        if (!items.length) { eventTable.renderRows([]); eventTable.el.hidden = true; return false; }
        eventTable.el.hidden = false;
        eventTable.renderRows(items);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') { eventTable.renderRows([]); eventTable.el.hidden = true; }
      },
    });
  }

  (async () => {
    const principal = await getPrincipal();
    state.principalRead = principal.read;
    state.canReveal = principal.permissions.has(REVEAL_PERMISSION);
    table = buildTable(state.canReveal);
    table.el.hidden = true;
    tableHost.appendChild(table.el);
    renderReveal();
    await Promise.all([loadChain(), load(), loadEvents()]);
  })();
}
