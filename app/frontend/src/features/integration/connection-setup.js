/* app/frontend/src/features/integration/connection-setup.js
   SCR-31 — Zoho ERP Connection Setup Wizard.

   What this screen does NOT do, and why that is the design
   --------------------------------------------------------
   It does not choose a product for you, and it does not pretend the product is
   settled. D-14 is unresolved: the target product is PROVISIONAL, and
   WAVE5_CONTRACTS.md is explicit that "nothing may hardcode an ERP base URL,
   scope string or endpoint". So the product is a field with two values, both
   labelled with what they cost, and the screen says in as many words that the
   decision has not been taken.

   It does not set a mode. `integration_connection.mode` defaults to MOCK in
   the schema (C2) and §11.9 requires explicit authorisation before any live
   call. A create form that could set LIVE_WRITE would be a form that starts
   live traffic; there is no such control here, and the screen says why.

   It does not accept a client secret. See oauth-consent.js (SCR-32) — the
   secret never reaches the browser at all, on any of these screens.

   The data centre list matters more than it looks
   -----------------------------------------------
   Only the IN data centre is documented in Zoho's own ERP material; the other
   five are evidenced from CRM documentation. app/backend/zoho.py records that
   distinction per row (`erp_documented`), and this screen renders it rather
   than flattening six equal-looking options — choosing an undocumented DC is a
   decision an operator should make knowingly.
*/

import { h, text } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, keyValues, modeBadge, modeBanner, replace, selectInput, textInput,
} from './integration-kit.js';
import {
  adoptOrders, createConnection, drainOutbox, getGlobalMode, listConnections, sweepConnection,
} from './integration-api.js';

/** LIVE_READ and LIVE_WRITE are the only modes that reach a real tenant, and
 * therefore the only modes where "Sweep now" / "Adopt orders" / "Drain
 * outbox" mean anything: MOCK and SANDBOX have no live connection to act on.
 */
function isLiveMode(mode) {
  const key = String(mode || '').trim().toUpperCase();
  return key === 'LIVE_READ' || key === 'LIVE_WRITE';
}

const PRODUCTS = [
  ['ERP', 'Zoho ERP — provisional (D-14)'],
  ['BOOKS_INVENTORY', 'Zoho Books + Inventory — provisional (D-14)'],
];

/* Fallback only. The live list comes from the connections response's
   `data_centres`, which app/backend/zoho.py serves from the verified
   inventory; this is what the field offers before that response lands, and it
   carries the same documented/undocumented distinction rather than a flat
   list. */
const FALLBACK_DCS = [
  { code: 'IN', erp_documented: true },
  { code: 'US', erp_documented: false },
  { code: 'EU', erp_documented: false },
  { code: 'AU', erp_documented: false },
  { code: 'JP', erp_documented: false },
  { code: 'CA', erp_documented: false },
];

function dcOptions(list) {
  return (list && list.length ? list : FALLBACK_DCS).map((dc) => [
    dc.code,
    dc.erp_documented
      ? `${dc.code} — documented in Zoho's ERP material`
      : `${dc.code} — evidenced from CRM documentation only`,
  ]);
}

export function mountConnectionSetup(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = { connections: [], dcs: [], mode: 'MOCK', modeNote: '' };

  /* ---------------- the mode banner, rendered before anything else -------- */
  const banner = h('div', { id: 'setupModeBanner' });

  /* ---------------- existing connections --------------------------------- */
  const BASE_COLUMNS = [
    {
      key: 'connection_id',
      label: 'Connection',
      render: (r) => h('span', { class: 'mono' }, String(r.connection_id ?? r.name ?? '—')),
    },
    { key: 'connector_name', label: 'Name', render: (r) => text(r.connector_name ?? r.name ?? '—') },
    { key: 'product', label: 'Product', render: (r) => text(r.product ?? 'ERP (assumed — D-14 unresolved)') },
    { key: 'dc', label: 'Data centre', render: (r) => h('span', { class: 'mono' }, String(r.dc ?? r.data_centre ?? '—')) },
    {
      key: 'organization_id',
      label: 'Organisation',
      render: (r) => (r.organization_id || r.zoho_org_id
        ? h('span', { class: 'mono' }, String(r.organization_id || r.zoho_org_id))
        : h('span', { class: 'muted' }, 'not mapped — see SCR-33')),
    },
    {
      key: 'mode',
      label: 'Mode',
      render: (r) => modeBadge(r.mode || state.mode, { note: state.modeNote }),
    },
    {
      key: 'next',
      label: 'Next step',
      render: (r) => {
        const id = r.connection_id || r.name;
        if (!id) return text('—');
        return h('a', {
          class: 'linkish integration-link',
          href: `?connection=${encodeURIComponent(id)}#integration-oauth`,
        }, 'Authorise (SCR-32)');
      },
    },
  ];

  /**
   * The "Actions" column, appended to the table only when at least one row is
   * LIVE_READ or LIVE_WRITE — never structurally present otherwise, so the
   * seeded MOCK profiles and every approved screenshot baseline (all of them
   * MOCK today; Phase 0B has not cleared a live authorisation) render exactly
   * as before. Within a table that DOES carry the column, a MOCK row's own
   * cell is still blank: the column applies to every row once it exists, and
   * only a live row has an action to offer.
   */
  const ACTIONS_COLUMN = { key: 'actions', label: 'Actions', render: (r) => renderActionsCell(r) };

  /* The table is rebuilt, not mutated, whenever whether any row is live
     changes — createDataTable() fixes its column list at construction, and
     there is no API to add or remove one afterwards. `tableSlot` is the DOM
     anchor `replace()` swaps the current table into. */
  const tableSlot = h('div');
  let table = null;
  let tableHasActions = false;
  function ensureTable(anyLive) {
    if (table && tableHasActions === anyLive) return table;
    table = createDataTable({
      caption: 'Connection profiles this application holds, with the mode each one runs in',
      emptyMessage: 'No connection profile has been created yet.',
      columns: anyLive ? [...BASE_COLUMNS, ACTIONS_COLUMN] : BASE_COLUMNS,
    });
    tableHasActions = anyLive;
    replace(tableSlot, table.el);
    return table;
  }

  /* Where a completed action's summary renders — sweep's per-module lines,
     adopt's five counts, drain's claimed/sent and per-row outcomes, or an
     RFC-7807 code and detail verbatim on failure. Present on every render of
     this screen, empty until an operator runs one of the three actions. */
  const actionResult = h('div', { id: 'connectionActionResult', 'data-testid': 'connection-action-result' });

  const listLoader = createLoader({
    id: 'setupConnectionsStatus',
    glyph: '⚯',
    what: 'Connection profiles',
    emptyMessage: 'No connection profile has been created yet. Create one below.',
    loadingMessage: 'Loading connection profiles…',
    onRetry: () => loadConnections(),
    announce,
  });

  /* ---------------- the create form -------------------------------------- */
  const nameField = field('setupName', 'Connector name', textInput({ required: true, maxlength: '64' }),
    { hint: 'A label for operators. It is never sent to Zoho.' });
  const entityField = field('setupEntity', 'Entity', textInput({ required: true, maxlength: '32' }),
    { hint: 'The legal entity this connection belongs to. One connection per (entity, organisation).' });
  const productField = field('setupProduct', 'Target product', selectInput(PRODUCTS),
    { hint: 'D-14 is unresolved. Both options are provisional; the adapter boundary (§11.10) is what '
      + 'makes changing this a configuration change rather than a rewrite.' });
  const dcSelect = selectInput(dcOptions(null));
  const dcField = field('setupDc', 'Data centre', dcSelect,
    { hint: "Only IN is documented in Zoho's own ERP material. The others are evidenced from CRM "
      + 'documentation and are labelled as such.' });
  const clientIdField = field('setupClientId', 'OAuth client id', textInput({ maxlength: '128' }),
    { hint: 'The client id is not a secret and is safe in the browser. The client SECRET is never '
      + 'entered here, never sent from here and never returned to here — see SCR-32.' });

  const submitButton = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Create connection profile');
  const formStatus = h('div', { id: 'setupFormStatus', 'aria-live': 'polite' });

  const form = h('form', { class: 'integration-form', novalidate: true, onSubmit: onCreate }, [
    h('div', { class: 'toolbar integration-toolbar' }, [
      nameField.el, entityField.el, productField.el, dcField.el, clientIdField.el,
    ]),
    h('div', { class: 'btn-row' }, [submitButton]),
    formStatus,
  ]);

  ensureTable(false);

  root.appendChild(banner);
  root.appendChild(card('setupExistingTitle', 'Existing connection profiles', [
    listLoader.el, tableSlot, actionResult,
  ]));
  root.appendChild(card('setupCreateTitle', 'Create a connection profile', [
    h('p', { class: 'muted small' },
      'A new profile is created in MOCK mode and stays there. Moving a connection to SANDBOX, '
      + 'LIVE_READ or LIVE_WRITE is a separate, authorised action: §11.9 requires explicit '
      + 'authorisation before any live call, and there is deliberately no control here that '
      + 'could start live traffic.'),
    form,
    keyValues([
      ['Mode at creation', modeBadge('MOCK')],
      ['Secret handling', text('The client secret is installed server-side and never transits this '
        + 'browser. This form has no secret field, and REQ-INT-024 is proven by test, not asserted '
        + 'by comment.')],
      ['Uniqueness', text('C2 freezes UNIQUE (entity_id, organization_id), so one entity may hold '
        + 'one connection per Zoho organisation.')],
    ]),
  ]));

  async function onCreate(event) {
    event.preventDefault();
    const missing = [];
    if (!nameField.control.value.trim()) missing.push('a connector name');
    if (!entityField.control.value.trim()) missing.push('an entity');
    if (missing.length) {
      renderFormStatus('error', `Enter ${missing.join(' and ')}.`);
      return;
    }
    submitButton.disabled = true;
    renderFormStatus('info', 'Creating the connection profile…');
    try {
      const result = await createConnection({
        entity_id: entityField.control.value.trim(),
        product: productField.control.value,
        dc: dcSelect.value,
        connector_name: nameField.control.value.trim(),
        client_id: clientIdField.control.value.trim(),
      });
      const id = (result.data && (result.data.connection_id || result.data.id)) || '';
      renderFormStatus('success', id
        ? `Connection profile ${id} created in MOCK mode. Authorise it on SCR-32.`
        : 'Connection profile created in MOCK mode. Authorise it on SCR-32.');
      announce('Connection profile created.');
      form.reset();
      await loadConnections();
    } catch (err) {
      if (err && err.name === 'EndpointUnavailableError') {
        renderFormStatus('info',
          `Connection profiles cannot be created in this build: it does not mount ${err.path}. `
          + 'Nothing was sent.');
      } else {
        renderFormStatus('error', (err && err.message) || 'The connection profile could not be created.');
      }
      announce('The connection profile was not created.');
    } finally {
      submitButton.disabled = false;
    }
  }

  function renderFormStatus(kind, message) {
    while (formStatus.firstChild) formStatus.removeChild(formStatus.firstChild);
    const glyph = kind === 'error' ? '✖' : kind === 'success' ? '✔' : '·';
    formStatus.appendChild(h('div', {
      class: `msg msg-${kind}`, role: kind === 'error' ? 'alert' : undefined,
    }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, glyph),
      h('div', { class: 'body' }, message),
    ]));
  }

  async function loadConnections() {
    await listLoader.run(listConnections, {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.connections) ? data.connections
            : Array.isArray(data && data.items) ? data.items : [];
        state.connections = items;
        state.dcs = (data && data.data_centres) || [];
        if (state.dcs.length) {
          const chosen = dcSelect.value;
          while (dcSelect.firstChild) dcSelect.removeChild(dcSelect.firstChild);
          for (const [value, label] of dcOptions(state.dcs)) {
            dcSelect.appendChild(h('option', { value }, label));
          }
          if (chosen) dcSelect.value = chosen;
        }
        if (data && data.mode) state.mode = data.mode;
        const activeTable = ensureTable(items.some((r) => isLiveMode(r.mode || state.mode)));
        if (!items.length) { activeTable.renderRows([]); activeTable.el.hidden = true; return false; }
        activeTable.el.hidden = false;
        activeTable.renderRows(items);
        announce(`${items.length} connection profile${items.length === 1 ? '' : 's'}.`);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') { const t = ensureTable(false); t.renderRows([]); t.el.hidden = true; }
      },
    });
    renderBanner();
  }

  function renderBanner() {
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    banner.appendChild(modeBanner(state.mode, {
      note: state.modeNote,
      extra: 'Every connection profile below runs in its own mode. A profile in MOCK constructs the '
        + 'request it would issue and synthesises the response; it is not evidence that a Zoho '
        + 'tenant works.',
    }));
    // The operational warning (product-owner decision 2026-09-13): while any
    // profile is in LIVE_WRITE, an emission from this application creates a
    // real purchase order in the Zoho ERP DEMO WBS tenant. Shown only then,
    // so the approved screens keep their baselines in every other mode.
    const writing = state.connections.filter((c) => String(c.mode || '').toUpperCase() === 'LIVE_WRITE');
    if (writing.length) {
      banner.appendChild(h('div', { class: 'msg msg-warning', 'data-testid': 'live-write-warning', role: 'alert' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
        h('div', { class: 'body' }, [
          h('strong', {}, 'Outbound writes are ENABLED on ' + writing.map((c) => c.connection_id).join(', ') + '.'),
          text(' Purchase orders emitted from this application are created in the Zoho ERP DEMO WBS tenant '
            + '(organisation 60074128927) and are real records there. Only purchase-order creation is granted; '
            + 'bills, receives, payments, vendors, items, taxes and banking are never written. '
            + 'Every emission carries cf_capex_ref, cf_wbs_code and cf_budget_head, and an identical retry '
            + 'never creates a second order.'),
        ]),
      ]));
    }
  }

  /* ---------------- per-row operator actions: sweep / adopt / drain ------- */

  /**
   * The Actions cell for one row. Blank for anything not LIVE_READ or
   * LIVE_WRITE — including a MOCK row sharing a table with a live one, where
   * the column exists but this row has nothing to offer.
   */
  function renderActionsCell(row) {
    const mode = String(row.mode || state.mode || '').toUpperCase();
    if (!isLiveMode(mode)) return h('span', { class: 'muted' }, '—');
    const connectionId = row.connection_id || row.name;
    const buttons = [
      actionButton(connectionId, 'Sweep now', 'Sweeping…', onSweep),
      actionButton(connectionId, 'Adopt orders', 'Adopting…', onAdopt),
    ];
    if (mode === 'LIVE_WRITE') {
      buttons.push(actionButton(connectionId, 'Drain outbox', 'Draining…', onDrain));
    }
    return h('div', { class: 'btn-row' }, buttons);
  }

  /** One action button. Disabled the instant its own request is in flight;
   * it does not touch its row's other buttons, which stay independently
   * clickable. */
  function actionButton(connectionId, label, busyLabel, handler) {
    const button = h('button', {
      type: 'button',
      class: 'btn-sm',
      disabled: !connectionId,
      onClick: () => handler(connectionId, button, busyLabel, label),
    }, label);
    return button;
  }

  async function onSweep(connectionId, button, busyLabel, idleLabel) {
    if (!connectionId || button.disabled) return;
    button.disabled = true;
    button.textContent = busyLabel;
    renderActionInfo(`Sweeping ${connectionId}…`);
    try {
      const result = await sweepConnection(connectionId, {});
      renderSweepResult(connectionId, result.data);
      announce(`Sweep of ${connectionId} completed.`);
    } catch (err) {
      renderActionFailure(connectionId, 'sweep', err);
      announce(`Sweep of ${connectionId} failed.`);
    } finally {
      button.disabled = false;
      button.textContent = idleLabel;
    }
  }

  async function onAdopt(connectionId, button, busyLabel, idleLabel) {
    if (!connectionId || button.disabled) return;
    button.disabled = true;
    button.textContent = busyLabel;
    renderActionInfo(`Adopting tenant orders for ${connectionId}…`);
    try {
      const result = await adoptOrders(connectionId);
      renderAdoptResult(connectionId, result.data);
      announce(`Adopt orders for ${connectionId} completed.`);
    } catch (err) {
      renderActionFailure(connectionId, 'adopt-orders', err);
      announce(`Adopt orders for ${connectionId} failed.`);
    } finally {
      button.disabled = false;
      button.textContent = idleLabel;
    }
  }

  async function onDrain(connectionId, button, busyLabel, idleLabel) {
    if (!connectionId || button.disabled) return;
    // eslint-disable-next-line no-alert
    const proceed = window.confirm(
      'This sends every pending outbox row to Zoho ERP DEMO WBS (60074128927) as real purchase '
      + 'orders. Continue?');
    if (!proceed) {
      announce('The outbox drain was cancelled. Nothing was sent.');
      renderActionInfo('The outbox drain was cancelled. Nothing was sent.');
      return;
    }
    button.disabled = true;
    button.textContent = busyLabel;
    renderActionInfo(`Draining the outbox for ${connectionId}…`);
    try {
      const result = await drainOutbox(connectionId);
      renderDrainResult(connectionId, result.data);
      announce(`Outbox drain for ${connectionId} completed.`);
    } catch (err) {
      renderActionFailure(connectionId, 'drain-outbox', err);
      announce(`Outbox drain for ${connectionId} failed.`);
    } finally {
      button.disabled = false;
      button.textContent = idleLabel;
    }
  }

  /** A single-line interim message: cancelled, or in progress. */
  function renderActionInfo(message) {
    replace(actionResult, h('div', { class: 'msg msg-info' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, message),
    ]));
  }

  function renderSweepResult(connectionId, data) {
    const modules = (data && data.modules && typeof data.modules === 'object') ? data.modules : {};
    const moduleLines = Object.entries(modules).map(([name, m]) => h('div', {},
      `${name}: ${(m && m.state) || 'UNKNOWN'} `
      + `(${(m && m.records_seen) || 0} seen, ${(m && m.inbox_created) || 0} new)`));
    const receiveLines = (data && data.receive_lines_recorded) || 0;
    const exceptionsRaised = (data && data.exceptions && data.exceptions.raised) || 0;
    replace(actionResult, h('div', { class: 'msg msg-info' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, `Sweep of ${connectionId} completed.`),
        ...moduleLines,
        h('div', {}, `Receive lines recorded: ${receiveLines}`),
        h('div', {}, `Exceptions raised: ${exceptionsRaised}`),
      ]),
    ]));
  }

  function renderAdoptResult(connectionId, data) {
    const d = data || {};
    replace(actionResult, h('div', { class: 'msg msg-info' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, `Adopt orders for ${connectionId} completed.`),
        h('div', {}, `adopted ${d.adopted ?? 0}, linked ${d.linked ?? 0}, skipped ${d.skipped ?? 0}, `
          + `exceptions ${d.exceptions ?? 0}, calls ${d.calls ?? 0}`),
      ]),
    ]));
  }

  function renderDrainResult(connectionId, data) {
    const d = data || {};
    const results = Array.isArray(d.results) ? d.results : [];
    const resultLines = results.map((r) => {
      const outboxId = (r && r.outbox_id) || '—';
      if (r && r.sent) return h('div', { class: 'mono' }, `${outboxId} → ${r.external_id}`);
      const code = (r && (r.refused || r.message)) || 'REFUSED';
      return h('div', { class: 'mono' }, `${outboxId} refused ${code}`);
    });
    replace(actionResult, h('div', { class: 'msg msg-info' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, `Outbox drain for ${connectionId} completed.`),
        h('div', {}, `claimed ${d.claimed ?? 0}, sent ${d.sent ?? 0}`),
        ...resultLines,
      ]),
    ]));
  }

  /**
   * The RFC-7807 code and detail, verbatim, from `err.body.detail`.
   *
   * `readProblem()` in core/api-client.js reads a nested `.message`, not a
   * nested `.detail` — the field these three routes actually send — so
   * `err.message` here is generic conflict/validation wording, not what the
   * server said. `err.code` DOES resolve correctly (readProblem reads
   * `nested.code`), so only the detail text needs reaching for directly.
   */
  function rfc7807Of(err) {
    const nested = err && err.body && typeof err.body === 'object' ? err.body.detail : null;
    const code = (nested && nested.code) || (err && err.code) || 'ERROR';
    const detail = (nested && typeof nested.detail === 'string' && nested.detail)
      || (err && err.message) || 'The request failed.';
    return { code, detail };
  }

  function renderActionFailure(connectionId, action, err) {
    if (err && err.name === 'EndpointUnavailableError') {
      renderActionInfo(`This action is not available in this build: it does not mount ${err.path}. `
        + 'Nothing was sent.');
      return;
    }
    const { code, detail } = rfc7807Of(err);
    replace(actionResult, h('div', { class: 'msg msg-error', role: 'alert' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
      h('div', { class: 'body' }, [
        h('strong', {}, `${connectionId}: ${action} failed.`),
        h('div', { class: 'mono' }, `${code}: ${detail}`),
      ]),
    ]));
  }

  (async () => {
    const globalMode = await getGlobalMode();
    state.mode = globalMode.mode;
    state.modeNote = globalMode.note;
    renderBanner();
    await loadConnections();
  })();
}
