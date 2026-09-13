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
  createAnnouncer, card, field, keyValues, modeBadge, modeBanner, selectInput, textInput,
} from './integration-kit.js';
import { createConnection, getGlobalMode, listConnections } from './integration-api.js';

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
  const table = createDataTable({
    caption: 'Connection profiles this application holds, with the mode each one runs in',
    emptyMessage: 'No connection profile has been created yet.',
    columns: [
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
    ],
  });

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

  root.appendChild(banner);
  root.appendChild(card('setupExistingTitle', 'Existing connection profiles', [
    listLoader.el, table.el,
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
        if (!items.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.el.hidden = false;
        table.renderRows(items);
        announce(`${items.length} connection profile${items.length === 1 ? '' : 's'}.`);
        return true;
      },
      onState: (s) => { if (s !== 'ready') { table.renderRows([]); table.el.hidden = true; } },
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

  (async () => {
    const globalMode = await getGlobalMode();
    state.mode = globalMode.mode;
    state.modeNote = globalMode.note;
    renderBanner();
    await loadConnections();
  })();
}
