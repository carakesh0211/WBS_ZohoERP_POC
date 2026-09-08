/* app/frontend/src/features/mapping/sync-scheduling.js
   SCR-37 — Sync Direction and Scheduling Configuration.

   THE CONTROL THAT CANNOT WORK IS SHOWN, DISABLED, WITH THE REASON
   ----------------------------------------------------------------
   This is the screen the brief is most specific about, and the rule is worth
   restating because the tempting alternative looks tidier: an incremental-sync
   control for a module that cannot be incrementally synced is DISABLED AND
   EXPLAINED, never hidden.

   Hiding it loses the only thing on this screen that is expensive to know. It
   took a machine-readable read of Zoho's own OpenAPI document to establish
   that Purchase Receives has no list endpoint and that fixed assets carry no
   `last_modified_time`. An operator who cannot find the incremental switch
   concludes the screen is unfinished and asks for it to be built; an operator
   who finds it greyed out with the reason attached learns the constraint and
   stops asking. The reason is the deliverable.

   PURCHASE RECEIVES: PO-ANCHORED DISCOVERY, STATED ON THE SCREEN
   --------------------------------------------------------------
   Zoho ERP publishes FOUR operations for `purchasereceives`, and not one of
   them lists or searches. There is a POST, a GET by id, a PUT by id and a
   DELETE by id — so a receive can be read only if you already know its id.
   There is therefore NO DELTA FEED: nothing to poll, no watermark to advance
   against, no page to walk.

   The approved acquisition model is consequently PO-ANCHORED: open purchase
   orders are walked, and each one is asked for its own receives. This screen
   says that in those words, and offers NO polling control for the module —
   not a disabled one and not an enabled one — because a schedule control for a
   feed that does not exist is not a constraint to explain, it is a control
   with no referent. What IS offered, disabled, is the incremental switch,
   because that is the control an operator comes looking for.

   The evidence is OAS-03, whose impact text says it exactly: receipt lines
   "expose no purchaseorder_item_id and no project/tag/custom-field coding, and
   the module has no list or search endpoint." It is rendered verbatim from
   /api/zoho/scopes rather than paraphrased here.

   (A NOTE ON A MISCITATION THIS SCREEN DOES NOT REPEAT: several comments in
   the integration feature attribute the missing receives-list endpoint to
   OAS-02. OAS-02 is the finding that Purchase REQUEST does not exist in the
   specification at all. The receives finding is OAS-03. Reported; not fixed
   here, because those are not this stream's files.)

   WHAT IS MEASURED AND WHAT IS MERELY DECLARED, KEPT APART
   --------------------------------------------------------
   Two things on this screen look alike and are not:

     * CAPABILITY comes from the verified endpoint inventory. It is a claim
       about a specification. No live call has been made by this application.
     * WATERMARK, LOOK-BACK AND CIRCUIT STATE come from the connector health
       route and are measured facts about this deployment.

   They are in separate cards, with separate provenance lines, because a reader
   who conflates them believes a sync is running.
*/

import { h, text } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from '../integration/integration-screen.js';
import { modeBanner, operationalChip } from '../integration/integration-kit.js';
import {
  getHealth, getInventorySummary, listConnections, listModuleEndpoints, listScopes,
  listSyncConfiguration,
} from './mapping-api.js';
import {
  capabilityChip, card, createAnnouncer, disabledControl, disabledFieldset, field, identifier,
  keyValues, queryParam, selectInput, specificationNotice, timestamp, unavailableMetric,
} from './mapping-kit.js';

/**
 * The objects this connector synchronises, in the order they carry money.
 *
 * Each is a real `zoho_module` in the verified inventory; the capability of
 * each is read from that inventory rather than declared here. The list exists
 * because SCR-37 is a per-OBJECT screen and the inventory carries seventy-eight
 * modules, most of which this application never touches.
 */
const SYNC_OBJECTS = [
  { module: 'purchase-order', label: 'Purchase orders' },
  { module: 'purchasereceives', label: 'Purchase receives (GRN)' },
  { module: 'bills', label: 'Vendor bills' },
  { module: 'journals', label: 'Journals' },
  { module: 'fixed-assets', label: 'Fixed assets' },
  { module: 'contacts', label: 'Vendors' },
  { module: 'items', label: 'Items' },
];

/** OAS-08: the specification's own pagination ceiling. Not a guess. */
const DOCUMENTED_PAGE_MAX = 200;

/** A GET whose path carries no `{placeholder}` is a LIST operation. */
function isListRead(row) {
  return String(row.http_method || '').toUpperCase() === 'GET'
    && !String(row.endpoint || '').includes('{');
}

function isWrite(row) {
  return ['POST', 'PUT', 'PATCH'].includes(String(row.http_method || '').toUpperCase());
}

/**
 * Derive one object's sync capability from the inventory rows for its module.
 *
 * DERIVED, NOT DECLARED, and the difference matters: a hand-written table of
 * "which modules support incremental sync" is a claim that ages badly and
 * silently. This reads the same 869-row inventory the connector reads, so a
 * specification update that adds a list endpoint changes this screen without
 * anybody remembering to.
 */
function capabilityOf(rows) {
  const lists = rows.filter(isListRead);
  const writes = rows.filter(isWrite);
  const reads = rows.filter((r) => String(r.http_method || '').toUpperCase() === 'GET');
  const incremental = lists.some((r) => r.filter_support === true);
  const paged = lists.some((r) => r.pagination_support === true);
  return {
    rows: rows.length,
    lists: lists.length,
    reads: reads.length,
    writes: writes.length,
    enumerable: lists.length > 0,
    incremental,
    paged,
    listEndpoint: lists.length ? `${lists[0].http_method} ${lists[0].endpoint}` : null,
  };
}

/**
 * The reason an incremental control is disabled, in the module's own terms.
 *
 * Three distinct reasons, never collapsed into one sentence, because the
 * remedies differ: no feed at all, a feed that cannot be filtered, and a feed
 * that has no modification timestamp to filter on.
 */
function incrementalReason(module, cap) {
  if (!cap.rows) {
    return {
      reason: 'The verified endpoint inventory lists no operation for this module, so nothing is '
        + 'known about whether it can be synchronised at all.',
      evidence: null,
    };
  }
  if (!cap.enumerable) {
    return {
      reason: 'This module publishes no list or search operation, so there is no delta feed to '
        + 'poll: a record can be read only if its id is already known. Incremental synchronisation '
        + 'is not a setting that could be switched on — there is nothing to page through.',
      evidence: 'OAS-03',
    };
  }
  if (!cap.incremental) {
    if (module === 'fixed-assets') {
      return {
        reason: 'Fixed assets expose no created_time and no last_modified_time, so a window of '
          + '"everything changed since" cannot be expressed. This module is full-refresh only.',
        evidence: 'OAS-09',
      };
    }
    return {
      reason: 'The list operation for this module declares no filter support, so a request cannot '
        + 'be narrowed to what changed since the last sweep. A poll would return the whole set '
        + 'every time, which is a full refresh wearing an incremental label.',
      evidence: null,
    };
  }
  return null;
}

export function mountSyncScheduling(root) {
  if (!root) return;
  const announce = createAnnouncer('mappingLiveRegion');

  const state = {
    connection: queryParam('connection') || '',
    connections: [],
    mode: 'MOCK',
    capabilities: new Map(),
    watermarks: [],
    circuits: [],
    healthRead: false,
  };

  const banner = h('div', { id: 'ssModeBanner' });

  const connectionSelect = selectInput([], {
    onChange: () => { state.connection = connectionSelect.value; loadHealth(); },
  });
  const connectionField = field('ssConnection', 'Connection profile', connectionSelect, {
    hint: 'Deep-linkable: /?connection=<id>#mapping-sync.',
  });

  /* ---------------- per-object capability ---------------- */

  const objectHost = h('div', { id: 'ssObjects', class: 'mapping-objects' });

  const capabilityLoader = createLoader({
    id: 'ssCapabilityStatus',
    glyph: '⇄',
    what: 'The verified endpoint inventory',
    emptyMessage: 'The verified inventory returned no module, so no object\'s sync capability can '
      + 'be stated.',
    loadingMessage: 'Reading the verified endpoint inventory…',
    onRetry: () => loadCapabilities(),
    announce,
  });

  /* ---------------- schedule, which is not configurable here ---------------- */

  const scheduleLoader = createLoader({
    id: 'ssScheduleStatus',
    glyph: '◷',
    what: 'The per-object sync schedule',
    emptyMessage: 'No sync schedule is configured.',
    loadingMessage: 'Looking for a sync schedule…',
    onRetry: () => loadSchedule(),
    announce,
  });

  const scheduleTable = createDataTable({
    caption: 'Configured sync schedules',
    emptyMessage: 'No sync schedule is configured.',
    columns: [
      { key: 'module', label: 'Object', render: (r) => identifier(r.module) },
      { key: 'direction', label: 'Direction', render: (r) => text(r.direction || 'not reported') },
      { key: 'cadence', label: 'Schedule', render: (r) => text(r.cadence || r.schedule || 'not reported') },
      { key: 'look_back_seconds', label: 'Look-back', render: (r) => text(
        r.look_back_seconds === null || r.look_back_seconds === undefined
          ? 'not reported' : `${r.look_back_seconds} s`) },
      { key: 'page_size', label: 'Page size', render: (r) => text(
        r.page_size === null || r.page_size === undefined ? 'not reported' : String(r.page_size)) },
      { key: 'status', label: 'Status', render: (r) => text(r.status || 'not reported') },
    ],
  });

  /* ---------------- measured state ---------------- */

  const watermarkTable = createDataTable({
    caption: 'High-water marks, look-back overlap and circuit state, measured for this connection',
    emptyMessage: 'This connection reports no watermark.',
    columns: [
      { key: 'module', label: 'Object', render: (r) => identifier(r.module) },
      { key: 'hwm', label: 'High-water mark', render: (r) => timestamp(r.hwm) },
      {
        key: 'overlap_seconds',
        label: 'Look-back overlap',
        render: (r) => (r.overlap_seconds === null || r.overlap_seconds === undefined
          ? h('span', { class: 'muted xs' }, 'not reported')
          : h('span', { class: 'mono' }, `${r.overlap_seconds} s`)),
      },
      { key: 'last_sweep_at', label: 'Last sweep', render: (r) => timestamp(r.last_sweep_at) },
      {
        key: 'circuit',
        label: 'Circuit',
        render: (r) => {
          const c = state.circuits.find((x) => String(x.module) === String(r.module));
          if (!c) return h('span', { class: 'muted xs' }, 'no circuit reported for this object');
          return h('div', { class: 'mapping-idpair' }, [
            operationalChip('circuit', c.state),
            c.reason ? h('span', { class: 'xs muted' }, String(c.reason)) : null,
          ].filter(Boolean));
        },
      },
    ],
  });

  const healthNotes = h('div', { id: 'ssHealthNotes' });

  /**
   * The two notes the connector health route returns verbatim, and — when the
   * answering source cannot carry a watermark — the explicit statement that it
   * cannot.
   *
   * `webhook_note` is the single most load-bearing sentence on this screen:
   * "No webhook or outbound-event framework is evidenced in the ERP
   * specification. The architecture is polling-first with a look-back window."
   * Every schedule and every look-back control here exists BECAUSE of that
   * finding, and a scheduling screen that did not say so would present polling
   * as a design choice rather than as the only option the API leaves.
   */
  function renderHealthNotes(data, result) {
    while (healthNotes.firstChild) healthNotes.removeChild(healthNotes.firstChild);
    const compat = result && result.source !== 'wave5';
    if (compat) {
      healthNotes.appendChild(h('div', { class: 'tiles mapping-tiles' }, [
        unavailableMetric('High-water marks',
          'The surface that answered is the Wave 4 connector, which carries no watermark. This is '
          + 'not a report that nothing has swept — it is a report that this source cannot say.'),
        unavailableMetric('Circuit state',
          'Circuit breakers belong to the integration platform. The connector surface has none to '
          + 'report, so none is shown as closed.'),
      ]));
    }
    for (const [key, label] of [
      ['webhook_note', 'Why this is a polling architecture'],
      ['rate_limit_note', 'What the rate limit actually documents'],
    ]) {
      const value = data && typeof data[key] === 'string' ? data[key].trim() : '';
      if (!value) continue;
      healthNotes.appendChild(h('div', { class: 'msg msg-info mapping-server-note', role: 'note' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
        h('div', { class: 'body' }, [h('strong', {}, label), text(' '), text(value)]),
      ]));
    }
  }

  const healthLoader = createLoader({
    id: 'ssHealthStatus',
    glyph: '◔',
    what: 'Measured sync state for this connection',
    emptyMessage: 'This connection reports no watermark and no circuit. Nothing has swept yet.',
    loadingMessage: 'Reading measured sync state…',
    idleMessage: 'Select a connection profile to read the watermarks and circuit states measured '
      + 'for it.',
    onRetry: () => loadHealth(),
    announce,
  });

  /* ---------------- hard negatives, verbatim ---------------- */

  const negatives = h('div', { id: 'ssNegatives' });

  const negativeLoader = createLoader({
    id: 'ssNegativeStatus',
    glyph: '⚑',
    what: 'The documented absences',
    emptyMessage: 'The connector reports no documented absence for these modules.',
    loadingMessage: 'Reading the documented absences…',
    onRetry: () => loadNegatives(),
    announce,
  });

  /* ---------------- layout ---------------- */

  root.appendChild(banner);

  root.appendChild(card('ssConnectionTitle', 'Connection', [
    h('div', { class: 'toolbar mapping-toolbar' }, [connectionField.el]),
  ]));

  root.appendChild(card('ssObjectTitle', 'Direction and supported sync mode, per object', [
    specificationNotice('Direction is derived from the operations the specification publishes: a '
      + 'module with a read operation can be read, a module with a write operation can be written. '
      + 'It is not a setting somebody chose.'),
    capabilityLoader.el,
    objectHost,
  ]));

  root.appendChild(card('ssNegativeTitle', 'The documented absences behind the disabled controls', [
    h('p', { class: 'muted small' },
      'Rendered verbatim from /api/zoho/scopes. These are the findings that establish why a '
      + 'control above cannot work; they are quoted rather than paraphrased so the wording that '
      + 'was evidenced is the wording that is read.'),
    negativeLoader.el,
    negatives,
  ]));

  root.appendChild(card('ssScheduleTitle', 'Schedule, look-back and page size', [
    scheduleLoader.el,
    scheduleTable.el,
    disabledFieldset('Edit this object\'s schedule', [
      h('div', { class: 'toolbar mapping-toolbar' }, [
        field('ssCadence', 'Schedule', selectInput([
          ['15m', 'Every 15 minutes'],
          ['1h', 'Hourly'],
          ['1d', 'Daily'],
        ])).el,
        field('ssLookback', 'Look-back window (seconds)', h('input', {
          type: 'number', min: '0', step: '60', value: '300',
        })).el,
        field('ssPageSize', 'Page size', h('input', {
          type: 'number', min: '1', max: String(DOCUMENTED_PAGE_MAX), value: '200',
        }), {
          hint: `The specification documents per_page with a maximum of ${DOCUMENTED_PAGE_MAX} `
            + '(OAS-08). A larger value is refused by the API, not by this control.',
        }).el,
      ]),
      h('div', { class: 'btn-row' }, [
        h('button', { type: 'button', class: 'btn-primary btn-sm' }, 'Save schedule'),
      ]),
    ], {
      reason: 'No route in this build accepts a sync schedule, so there is nothing to save to. '
        + 'These controls are shown disabled rather than hidden because the shape of the '
        + 'configuration — cadence, look-back window, page size — is settled and evidenced, and '
        + 'hiding it would suggest it is still an open question. Nothing typed here is stored, '
        + 'locally or otherwise.',
      evidence: 'Page-size ceiling: OAS-08, per_page maximum 200.',
    }),
  ]));

  root.appendChild(card('ssHealthTitle', 'Measured sync state for this connection', [
    h('p', { class: 'muted small' },
      'Everything in this card is measured by this deployment: a high-water mark that has moved, '
      + 'a look-back overlap that was applied, a circuit that opened. It is a different kind of '
      + 'fact from the capability table above, which describes a specification.'),
    healthLoader.el,
    healthNotes,
    watermarkTable.el,
  ]));

  /* ---------------- rendering ---------------- */

  function renderObject(spec) {
    const cap = state.capabilities.get(spec.module);
    const headingId = `ssObj-${spec.module}`;
    const known = !!cap;

    const directionValue = !known ? null
      : cap.reads && cap.writes ? 'Both — read from Zoho and written to Zoho'
        : cap.reads ? 'Inbound only — read from Zoho'
          : cap.writes ? 'Outbound only — written to Zoho'
            : 'Neither — the specification publishes no read and no write for this module';

    /* THE PO-ANCHORED CASE. Purchase Receives gets its own sentence, above the
       capability grid, because a reader who only reads one line on this row
       must read this one. */
    const anchored = spec.module === 'purchasereceives';

    const incremental = known ? incrementalReason(spec.module, cap) : {
      reason: 'The verified endpoint inventory could not be read for this module, so nothing is '
        + 'claimed about its sync mode in either direction.',
      evidence: null,
    };

    const toggle = h('input', {
      type: 'checkbox',
      id: `ssIncremental-${spec.module}`,
    });

    const incrementalBlock = incremental
      ? disabledControl(toggle, {
        ...incremental,
        labelText: 'Incremental (delta) synchronisation',
      })
      : h('div', { class: 'mapping-enabled-note' }, [
        statusChip({
          label: 'INCREMENTAL AVAILABLE',
          tone: 'positive',
          title: 'The list operation for this module declares filter support, so a window of '
            + '"changed since" can be expressed.',
        }),
        h('p', { class: 'xs muted' },
          'The switch itself is not offered here: no route in this build stores a sync mode, and '
          + 'a switch that saves nothing is worse than a statement of the capability.'),
      ]);

    return h('section', { class: 'card mapping-object', 'aria-labelledby': headingId }, [
      h('h3', { id: headingId, class: 'mapping-object-title' }, [
        text(spec.label),
        h('span', { class: 'spacer' }),
        h('span', { class: 'mono xs' }, spec.module),
      ]),
      h('div', { class: 'card-body' }, [
        anchored ? h('div', { class: 'msg msg-warning mapping-anchored', role: 'note' }, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
          h('div', { class: 'body' }, [
            h('strong', {}, 'Discovered by walking open purchase orders — there is no receives feed.'),
            text(' Zoho ERP publishes no list and no search operation for Purchase Receives, so '
              + 'there is no delta feed and no page to walk: a receive can be read only if its id '
              + 'is already known. Acquisition is therefore PO-ANCHORED — every open purchase '
              + 'order is walked and asked for its own receives. No polling control is offered '
              + 'for this object, because there is no feed for one to point at.'),
            h('div', { class: 'small mono' }, 'OAS-03'),
          ]),
        ]) : null,
        keyValues([
          ['Direction', known ? text(directionValue)
            : h('span', { class: 'muted' }, 'not established — the inventory could not be read')],
          ['Enumerable', capabilityChip(known ? cap.enumerable : null, {
            yes: 'LIST OPERATION PUBLISHED',
            no: 'NO LIST OR SEARCH OPERATION',
            why: known && cap.listEndpoint
              ? `The list operation is ${cap.listEndpoint}.`
              : 'Every published operation for this module addresses a single record by id.',
          })],
          ['Supported sync mode', known
            ? (cap.enumerable
              ? statusChip({
                label: cap.incremental ? 'INCREMENTAL OR FULL REFRESH' : 'FULL REFRESH ONLY',
                tone: cap.incremental ? 'positive' : 'warning',
                title: cap.incremental
                  ? 'The list operation declares filter support.'
                  : 'The list operation declares no filter support, so every sweep reads the '
                    + 'whole set.',
              })
              : statusChip({
                label: 'ANCHORED DISCOVERY ONLY',
                tone: 'warning',
                title: 'Nothing can be enumerated, so records are reached from a document that '
                  + 'references them.',
              }))
            : statusChip({ label: 'NOT ESTABLISHED', tone: 'neutral' })],
          ['Paging', capabilityChip(known ? cap.paged : null, {
            yes: `PAGED — maximum ${DOCUMENTED_PAGE_MAX} per page`,
            no: 'NO PAGING ON THE LIST OPERATION',
            why: `OAS-08 documents per_page with a maximum of ${DOCUMENTED_PAGE_MAX}.`,
          })],
          ['Operations published', known
            ? h('span', { class: 'mono' },
              `${cap.rows} total · ${cap.reads} read · ${cap.writes} write · ${cap.lists} list`)
            : h('span', { class: 'muted' }, 'not established')],
        ]),
        incrementalBlock,
      ].filter(Boolean)),
    ]);
  }

  function renderObjects() {
    while (objectHost.firstChild) objectHost.removeChild(objectHost.firstChild);
    for (const spec of SYNC_OBJECTS) objectHost.appendChild(renderObject(spec));
  }

  function renderBanner() {
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.connection),
    );
    banner.appendChild(modeBanner((row && row.mode) || state.mode, {
      extra: 'In MOCK no network call is made, so no watermark below has been advanced by a real '
        + 'sweep against a Zoho tenant.',
    }));
  }

  /* ---------------- loading ---------------- */

  async function loadCapabilities() {
    await capabilityLoader.run(async () => {
      /* One inventory summary to prove the route is mounted and to fail once
         if it is not, then one call per object. Seven small reads of a static,
         server-cached document; the alternative is one screen-wide failure
         mode that cannot say which module it could not read. */
      const summary = await getInventorySummary();
      const results = await Promise.all(SYNC_OBJECTS.map(async (spec) => {
        try {
          const r = await listModuleEndpoints(spec.module);
          const rows = Array.isArray(r.data) ? r.data
            : Array.isArray(r.data && r.data.items) ? r.data.items : [];
          return [spec.module, capabilityOf(rows)];
        } catch {
          /* A module the inventory does not carry is a real answer — the
             specification has no such module — and is rendered as "not
             established" rather than as a screen failure. */
          return [spec.module, null];
        }
      }));
      const map = new Map();
      for (const [module, cap] of results) if (cap) map.set(module, cap);
      return { ...summary, data: { modules: map } };
    }, {
      render: (data) => {
        state.capabilities = (data && data.modules) || new Map();
        renderObjects();
        if (!state.capabilities.size) return false;
        announce(`${state.capabilities.size} object capabilities established from the verified `
          + 'inventory.');
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') { state.capabilities = new Map(); renderObjects(); }
      },
    });
  }

  async function loadNegatives() {
    await negativeLoader.run(() => listScopes(state.connection || 'default'), {
      render: (data) => {
        const list = Array.isArray(data && data.hard_negatives) ? data.hard_negatives : [];
        while (negatives.firstChild) negatives.removeChild(negatives.firstChild);
        if (!list.length) return false;
        const ul = h('ul', { class: 'mapping-negatives' });
        for (const n of list) {
          ul.appendChild(h('li', {}, [
            h('div', { class: 'mapping-idpair' }, [
              statusChip({
                label: String(n.severity || 'NOTED'),
                tone: String(n.severity).toUpperCase() === 'HIGH' ? 'negative' : 'warning',
              }),
              h('span', { class: 'mono' }, String(n.id || '')),
            ]),
            h('div', { class: 'body-strong' }, String(n.title || '')),
            h('div', { class: 'small' }, String(n.impact || '')),
          ]));
        }
        negatives.appendChild(ul);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') while (negatives.firstChild) negatives.removeChild(negatives.firstChild);
      },
    });
  }

  async function loadSchedule() {
    await scheduleLoader.run(() => listSyncConfiguration(
      state.connection ? { connection_id: state.connection } : undefined,
    ), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items : [];
        if (!items.length) { scheduleTable.renderRows([]); scheduleTable.el.hidden = true; return false; }
        scheduleTable.el.hidden = false;
        scheduleTable.renderRows(items);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') { scheduleTable.renderRows([]); scheduleTable.el.hidden = true; }
      },
    });
  }

  async function loadHealth() {
    if (!state.connection) return;
    renderBanner();
    await healthLoader.run(() => getHealth(state.connection), {
      render: (data, result) => {
        state.watermarks = Array.isArray(data && data.watermarks) ? data.watermarks : [];
        state.circuits = Array.isArray(data && data.circuits) ? data.circuits : [];
        state.healthRead = true;
        renderHealthNotes(data, result);

        /* THE COMPATIBILITY SURFACE CARRIES NO WATERMARK, AND SAYING "NOTHING
           HAS SWEPT YET" WOULD BE A DIFFERENT CLAIM.

           /api/zoho/{id}/health answers a connector question — token, scopes,
           recent events — and knows nothing about the integration platform's
           high-water marks, because there is no integration platform behind
           it. Rendering its silence as an empty result would tell an operator
           that no sweep has run, which is a statement about this deployment's
           behaviour rather than about which endpoint answered. So the card
           reports the absence as an absence and stays in the ready state: the
           notes above it are real, and they are what this source can say. */
        if (result.source !== 'wave5') {
          watermarkTable.renderRows([]);
          watermarkTable.el.hidden = true;
          return true;
        }
        if (!state.watermarks.length) {
          watermarkTable.renderRows([]);
          watermarkTable.el.hidden = true;
          return false;
        }
        watermarkTable.el.hidden = false;
        watermarkTable.renderRows(state.watermarks);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') {
          state.watermarks = [];
          state.circuits = [];
          watermarkTable.renderRows([]);
          watermarkTable.el.hidden = true;
          while (healthNotes.firstChild) healthNotes.removeChild(healthNotes.firstChild);
        }
      },
    });
  }

  async function loadConnections() {
    try {
      const result = await listConnections();
      const items = Array.isArray(result.data) ? result.data
        : Array.isArray(result.data && result.data.connections) ? result.data.connections
          : Array.isArray(result.data && result.data.items) ? result.data.items : [];
      state.connections = items;
      if (result.data && result.data.mode) state.mode = result.data.mode;
      while (connectionSelect.firstChild) connectionSelect.removeChild(connectionSelect.firstChild);
      for (const row of items) {
        const id = String(row.connection_id || row.name || '');
        connectionSelect.appendChild(h('option', { value: id },
          row.connector_name ? `${row.connector_name} (${id})` : id));
      }
      if (items.length) {
        const wanted = items.some((c) => String(c.connection_id || c.name) === String(state.connection))
          ? state.connection
          : String(items[0].connection_id || items[0].name || '');
        state.connection = wanted;
        connectionSelect.value = wanted;
      }
    } catch {
      /* Reported by the health loader below, which reads a connection-scoped
         route and renders a real state naming it. */
      state.connections = [];
    }
  }

  (async () => {
    renderObjects();
    renderBanner();
    await loadConnections();
    renderBanner();
    await Promise.all([loadCapabilities(), loadNegatives(), loadSchedule()]);
    await loadHealth();
  })();
}
