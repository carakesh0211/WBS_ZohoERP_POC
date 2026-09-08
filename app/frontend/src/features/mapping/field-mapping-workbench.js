/* app/frontend/src/features/mapping/field-mapping-workbench.js
   SCR-36 — Transaction Field Mapping Workbench.

   THE HONEST SHAPE OF THIS SCREEN
   -------------------------------
   A field mapping has two halves: WHAT THE DESTINATION ACCEPTS, and WHICH OF
   OUR FIELDS IS WRITTEN TO WHICH OF THEIRS. This build serves the first and
   does not serve the second, and the screen is built along that seam rather
   than papering over it.

     * The destination half IS real. `/api/zoho/inventory/{module}` returns the
       verified endpoint inventory: every operation on the module, the params
       it requires, and — the flag that decides whether a custom-field mapping
       is possible at all — `custom_field_support`. That is 869 rows read from
       Zoho's own OpenAPI document, sha-pinned, with a verification status on
       every row.

     * The registry half IS NOT SERVED. No route in this build lists a
       transaction or custom-field mapping, and none accepts one. So the screen
       probes for the routes that would, finds none, and renders the
       UNAVAILABLE state naming them — not an empty table, which would say "no
       mappings are configured" and invite somebody to configure one.

   WHY NOT JUST SHOW A BLANK GRID AND LET SOMEBODY FILL IT IN
   ----------------------------------------------------------
   Because a mapping typed into a grid that cannot be saved is worse than no
   grid. The operator does the work, presses the button, and either nothing
   happens or — the version that actually ships — a local copy is kept and the
   screen looks configured while nothing is running. A field mapping decides
   which value lands in which column of a financial document; a screen that
   can appear configured without being configured is the wrong place to be
   optimistic.

   ACTIVATION IS GOVERNED AND AUDITED, AND THAT IS WHY IT IS NOT OFFERED
   --------------------------------------------------------------------
   The brief requires mapping activation to be governed and audited. Both
   halves of that are server work: a permission check that runs before the
   handler, and an entry in the hash-chained audit trail. Neither exists for
   field mappings in this build. The activation control is therefore rendered,
   disabled, with that stated — the same discipline SCR-37 applies to an
   unsupported incremental control, and for the same reason: the reason is the
   deliverable.

   VALIDATION ERRORS
   -----------------
   The errors this screen CAN state are the ones the specification supports:
   a destination module that accepts no custom field cannot carry a custom-field
   mapping, and a required parameter that no source field supplies is a mapping
   that will fail on first use. Both are computed from the inventory, both name
   the row they came from, and neither is a guess about a live tenant.
*/

import { h, text } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from '../integration/integration-screen.js';
import { getInventorySummary, listFieldMappings, listModuleEndpoints } from './mapping-api.js';
import {
  capabilityChip, card, createAnnouncer, disabledFieldset, field, identifier, keyValues,
  queryParam, selectInput, specificationNotice, textInput,
} from './mapping-kit.js';

/**
 * The modules whose FIELDS this application actually maps.
 *
 * A transaction field mapping is about a transaction. The inventory carries
 * every module Zoho publishes, including masters and settings; offering all of
 * them here would bury the four that carry money behind ninety that do not.
 * The list is the transaction objects named in the connector's own
 * REQUIRED_MODULES, and the module picker below still offers every module the
 * inventory returns — this is the DEFAULT, not a restriction.
 */
const TRANSACTION_MODULES = ['purchase-order', 'bills', 'purchasereceives', 'journals'];

export function mountFieldMappingWorkbench(root) {
  if (!root) return;
  const announce = createAnnouncer('mappingLiveRegion');

  const state = {
    module: queryParam('module') || TRANSACTION_MODULES[0],
    modules: [],
    endpoints: [],
  };

  /* ---------------- module picker ---------------- */

  const moduleSelect = selectInput([[state.module, state.module]], {
    onChange: () => { state.module = moduleSelect.value; loadModule(); },
  });
  const moduleField = field('fmModule', 'Destination module', moduleSelect, {
    hint: 'Deep-linkable: /?module=purchase-order#mapping-fields.',
  });

  /* ---------------- the registry, which is absent ---------------- */

  const registryTable = createDataTable({
    caption: 'Configured transaction and custom-field mappings',
    emptyMessage: 'No field mapping is configured for this module.',
    columns: [
      { key: 'source_field', label: 'Source field (ours)', render: (r) => identifier(r.source_field) },
      { key: 'destination_field', label: 'Destination field (theirs)', render: (r) => identifier(r.destination_field) },
      { key: 'data_type', label: 'Type', render: (r) => text(r.data_type || 'not reported') },
      {
        key: 'required',
        label: 'Required',
        render: (r) => (r.required === true
          ? statusChip({ label: 'REQUIRED', tone: 'warning' })
          : r.required === false
            ? statusChip({ label: 'OPTIONAL', tone: 'neutral' })
            : statusChip({ label: 'NOT REPORTED', tone: 'neutral' })),
      },
      {
        key: 'active',
        label: 'Activation',
        render: (r) => (r.active === true
          ? statusChip({ label: 'ACTIVE', tone: 'positive' })
          : statusChip({ label: 'INACTIVE', tone: 'neutral' })),
      },
    ],
  });

  const registryLoader = createLoader({
    id: 'fmRegistryStatus',
    glyph: '⇄',
    what: 'The field-mapping registry',
    emptyMessage: 'No field mapping is configured for this module.',
    loadingMessage: 'Looking for a field-mapping registry…',
    onRetry: () => loadRegistry(),
    announce,
  });

  /* ---------------- the destination, which is real ---------------- */

  const endpointTable = createDataTable({
    caption: 'What the destination module accepts, from the verified endpoint inventory',
    emptyMessage: 'The inventory lists no endpoint for this module.',
    columns: [
      {
        key: 'operation',
        label: 'Operation',
        render: (r) => h('div', { class: 'mapping-idpair' }, [
          h('span', { class: 'mono' }, `${r.http_method || '?'} ${r.endpoint || '?'}`),
          h('span', { class: 'xs muted' }, String(r.summary || r.operation_id || '')),
        ]),
      },
      {
        key: 'required_params',
        label: 'Required parameters',
        render: (r) => {
          const params = Array.isArray(r.required_params) ? r.required_params : [];
          if (!params.length) {
            return h('span', { class: 'muted xs' }, 'none declared');
          }
          return h('ul', { class: 'mapping-params' },
            params.map((p) => h('li', {}, [
              h('span', { class: 'mono' }, String(p)),
              text(' '),
              statusChip({
                label: 'REQUIRED',
                tone: 'warning',
                title: 'Declared required by the vendor specification. A mapping that supplies no '
                  + 'value for it fails on first use rather than at configuration time.',
              }),
            ])));
        },
      },
      {
        key: 'custom_field_support',
        label: 'Custom fields',
        render: (r) => capabilityChip(r.custom_field_support, {
          yes: 'CUSTOM FIELDS ACCEPTED',
          no: 'NO CUSTOM FIELD',
          why: r.custom_field_support === false
            ? 'The specification declares no custom-field support on this operation. A custom-field '
              + 'mapping against it cannot be made to work by configuring it differently.'
            : 'The specification declares custom-field support on this operation.',
        }),
      },
      {
        key: 'verification_status',
        label: 'Evidence',
        render: (r) => h('div', { class: 'mapping-idpair' }, [
          h('span', { class: 'xs' }, String(r.verification_status || 'not reported')),
          h('span', { class: 'xs muted' }, String(r.verified_date || '')),
        ]),
      },
    ],
  });

  const endpointLoader = createLoader({
    id: 'fmEndpointStatus',
    glyph: '≣',
    what: 'The verified endpoint inventory for this module',
    emptyMessage: 'The verified inventory lists no endpoint for this module, so nothing can be '
      + 'mapped to it.',
    loadingMessage: 'Reading the verified endpoint inventory…',
    onRetry: () => loadModule(),
    announce,
  });

  /* ---------------- validation ---------------- */

  const validation = h('div', { id: 'fmValidation' });

  /**
   * The two errors this screen can state from evidence, and no others.
   *
   * Both are properties of the DESTINATION, so both are knowable without a
   * mapping registry — which is exactly why they are worth rendering on a
   * screen whose registry is absent. A third check ("this source field does
   * not exist") would need our own transaction schema as a served contract,
   * and inventing one would be the fabrication this file exists to avoid.
   */
  function renderValidation() {
    while (validation.firstChild) validation.removeChild(validation.firstChild);
    const rows = state.endpoints;
    if (!rows.length) return;

    const writes = rows.filter((r) => ['POST', 'PUT', 'PATCH'].includes(
      String(r.http_method || '').toUpperCase()));
    const customCapable = writes.filter((r) => r.custom_field_support === true);
    const requiredEverywhere = new Set();
    for (const r of writes) {
      for (const p of (Array.isArray(r.required_params) ? r.required_params : [])) {
        requiredEverywhere.add(String(p));
      }
    }

    const findings = [];

    if (writes.length && !customCapable.length) {
      findings.push(h('div', { class: 'msg msg-warning', role: 'note' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
        h('div', { class: 'body' }, [
          h('strong', {}, 'No write operation on this module accepts a custom field.'),
          text(` All ${writes.length} write operation${writes.length === 1 ? '' : 's'} in the `
            + 'verified inventory declare custom_field_support false. A custom-field mapping '
            + 'against this module cannot be made to work by configuring it differently — the '
            + 'destination has nowhere to put the value.'),
        ]),
      ]));
    }

    if (requiredEverywhere.size) {
      findings.push(h('div', { class: 'msg msg-info', role: 'note' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
        h('div', { class: 'body' }, [
          h('strong', {}, 'Every mapping to this module must supply these parameters.'),
          text(' A mapping that omits one is accepted at configuration time and fails on first '
            + 'use, which is the expensive place to find out: '),
          h('span', { class: 'mono' }, [...requiredEverywhere].sort().join(', ')),
          text('.'),
        ]),
      ]));
    }

    if (!findings.length) {
      findings.push(h('p', { class: 'muted small' },
        'The inventory declares no write-side constraint on this module that a mapping could '
        + 'violate. That is not a statement that a mapping would succeed — no call has been made.'));
    }

    for (const node of findings) validation.appendChild(node);
  }

  /* ---------------- activation, disabled and explained ---------------- */

  const activationSource = textInput({ placeholder: '' });
  const activationDest = textInput({ placeholder: '' });
  const activationType = selectInput([
    ['string', 'String'],
    ['number', 'Number'],
    ['date', 'Date'],
    ['boolean', 'Boolean'],
    ['reference', 'Reference'],
  ]);
  const activationRequired = h('input', { type: 'checkbox' });
  const activationButton = h('button', { type: 'button', class: 'btn-primary btn-sm' },
    'Activate this mapping');

  const activationSourceField = field('fmActSource', 'Source field (ours)', activationSource);
  const activationDestField = field('fmActDest', 'Destination field (theirs)', activationDest);
  const activationTypeField = field('fmActType', 'Type', activationType);
  const activationRequiredField = field('fmActRequired', 'Required', activationRequired);

  /* ---------------- layout ---------------- */

  root.appendChild(specificationNotice(
    'The registry half of this screen — which of our fields is written to which of theirs — is '
    + 'not served by any route in this build and is shown as unavailable rather than as empty.'));

  root.appendChild(card('fmModuleTitle', 'Destination module', [
    h('div', { class: 'toolbar mapping-toolbar' }, [moduleField.el]),
    h('p', { class: 'muted small' },
      'The four transaction modules are offered first because they are the ones that carry money. '
      + 'Every module in the verified inventory is selectable.'),
  ]));

  root.appendChild(card('fmRegistryTitle', 'Configured field mappings', [
    registryLoader.el,
    registryTable.el,
  ]));

  root.appendChild(card('fmEndpointTitle', 'What the destination accepts', [
    endpointLoader.el,
    endpointTable.el,
  ]));

  root.appendChild(card('fmValidationTitle', 'Validation', [validation]));

  root.appendChild(card('fmActivationTitle', 'Create and activate a mapping', [
    disabledFieldset('New field mapping', [
      h('div', { class: 'toolbar mapping-toolbar' }, [
        activationSourceField.el, activationDestField.el,
        activationTypeField.el, activationRequiredField.el,
      ]),
      h('div', { class: 'btn-row' }, [activationButton]),
    ], {
      reason: 'Activating a field mapping must be permission-checked before the handler runs and '
        + 'written to the hash-chained audit trail as a decision with an actor and a reason. Both '
        + 'are server work and neither exists for field mappings in this build, so there is '
        + 'nothing to activate against. The controls are shown disabled rather than hidden so it '
        + 'is clear that the capability is understood and absent, not forgotten.',
      evidence: 'REQ-INT-024 governs the credential; governance and audit of a mapping are '
        + 'separate and equally server-side.',
    }),
    h('p', { class: 'xs muted' },
      'Nothing typed into these controls is stored anywhere, and no local copy is kept. A mapping '
      + 'held only in a browser would make this screen look configured while nothing was running.'),
  ]));

  root.appendChild(card('fmContextTitle', 'Where these fields come from', [
    keyValues([
      ['Destination capability', text('/api/zoho/inventory/{module} — the verified endpoint '
        + 'inventory, read from Zoho\'s own OpenAPI document with a pinned sha256.')],
      ['Mapping registry', text('No route in this build. The probe against /openapi.json is what '
        + 'establishes that, so the day one is mounted this screen starts reading it with no edit.')],
      ['Verification', text('CONFIRMED-BY-VENDOR-SPECIFICATION is a claim about the specification. '
        + 'No live or sandbox call has been made by this application.')],
    ]),
  ]));

  /* ---------------- loading ---------------- */

  async function loadRegistry() {
    await registryLoader.run(() => listFieldMappings({ module: state.module }), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items : [];
        if (!items.length) { registryTable.renderRows([]); registryTable.el.hidden = true; return false; }
        registryTable.el.hidden = false;
        registryTable.renderRows(items);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') { registryTable.renderRows([]); registryTable.el.hidden = true; }
      },
    });
  }

  async function loadModule() {
    await endpointLoader.run(() => listModuleEndpoints(state.module), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items : [];
        state.endpoints = items;
        renderValidation();
        if (!items.length) { endpointTable.renderRows([]); endpointTable.el.hidden = true; return false; }
        endpointTable.el.hidden = false;
        endpointTable.renderRows(items);
        announce(`${items.length} endpoint${items.length === 1 ? '' : 's'} listed for ${state.module}.`);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') {
          state.endpoints = [];
          renderValidation();
          endpointTable.renderRows([]);
          endpointTable.el.hidden = true;
        }
      },
    });
    await loadRegistry();
  }

  async function loadModuleList() {
    try {
      const result = await getInventorySummary();
      const modules = (result.data && Array.isArray(result.data.modules))
        ? result.data.modules.map((m) => String(m.module)) : [];
      state.modules = modules;
      const ordered = [
        ...TRANSACTION_MODULES.filter((m) => modules.includes(m)),
        ...modules.filter((m) => !TRANSACTION_MODULES.includes(m)),
      ];
      const list = ordered.length ? ordered : TRANSACTION_MODULES;
      while (moduleSelect.firstChild) moduleSelect.removeChild(moduleSelect.firstChild);
      for (const m of list) moduleSelect.appendChild(h('option', { value: m }, m));
      if (!list.includes(state.module)) state.module = list[0];
      moduleSelect.value = state.module;
    } catch {
      /* The inventory summary is a convenience: it populates the picker. Its
         absence is reported by the endpoint loader below, which reads the same
         inventory and renders a real unavailable state naming the route. A
         second unavailable block for the same absence would say it twice. */
      while (moduleSelect.firstChild) moduleSelect.removeChild(moduleSelect.firstChild);
      for (const m of TRANSACTION_MODULES) moduleSelect.appendChild(h('option', { value: m }, m));
      moduleSelect.value = state.module;
    }
  }

  (async () => {
    await loadModuleList();
    await loadModule();
  })();
}
