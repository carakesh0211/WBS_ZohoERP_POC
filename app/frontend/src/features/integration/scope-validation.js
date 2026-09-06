/* app/frontend/src/features/integration/scope-validation.js
   SCR-34 — API Scope and Permission Validation.

   Three things this screen refuses to blur together, because blurring them is
   how a connector comes to look healthier than it is.

   1. A SCOPE THAT IS REQUIRED BUT NOT GRANTED IS A FAILURE, NOT A WARNING.
      Everything downstream of a missing scope fails at call time, in a
      background job, hours later. It is reported here as a failure with the
      module it breaks named next to it.

   2. A DOCUMENTED ABSENCE IS NOT A FAILURE OF THIS CONNECTOR.
      `hard_negatives` are things Zoho's own specification does not provide —
      most consequentially, that Zoho ERP Purchase Receives has NO list
      endpoint (WAVE5_CONTRACTS.md's first "most likely to be got wrong"). No
      scope grant fixes that, and no amount of retrying will. They are shown in
      their own section, described as design constraints, and they never count
      against the pass rate — because a pass rate that counted them would never
      reach 100% and would train the operator to ignore it.

   3. A PASS IN MOCK MODE IS NOT EVIDENCE.
      In MOCK no network call is made; a PASS means "the specification says
      this endpoint exists and the scope it needs has been granted". That is
      genuinely useful and it is NOT proof the tenant works. The banner says
      so, and the result table says so per row.
*/

import { h, text } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, modeBadge, modeBanner, normaliseMode, queryParam, selectInput,
} from './integration-kit.js';
import {
  getGlobalMode, listConnections, listScopes, validateScopes,
} from './integration-api.js';

const SEVERITY_TONE = { CRITICAL: 'negative', HIGH: 'negative', MEDIUM: 'warning', LOW: 'neutral' };

function resultChip(result) {
  const value = String(result || '').toUpperCase();
  if (value === 'PASS') return statusChip({ label: 'PASS', tone: 'positive' });
  if (value === 'FAIL') return statusChip({ label: 'FAIL', tone: 'negative' });
  if (value === 'NOT RUN') return statusChip({ label: 'NOT RUN', tone: 'neutral' });
  if (value === 'NOT AVAILABLE') {
    return statusChip({
      label: 'NOT AVAILABLE',
      tone: 'warning',
      title: 'No endpoint for this module exists in the official specification. This is a documented '
        + 'absence, not a connector failure — no scope grant fixes it.',
    });
  }
  return statusChip({ label: value || 'UNKNOWN', tone: 'neutral' });
}

export function mountScopeValidation(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    connections: [], selected: queryParam('connection') || '',
    mode: 'MOCK', modeNote: '', granted: new Set(),
  };

  const banner = h('div', { id: 'scopeModeBanner' });

  const connectionSelect = selectInput([], {
    onChange: () => { state.selected = connectionSelect.value; renderBanner(); loadScopes(); },
  });
  const connectionField = field('scopeConnection', 'Connection profile', connectionSelect,
    { hint: 'Deep-linkable: /?connection=<id>#integration-scopes.' });

  /* ---------------- required scopes -------------------------------------- */
  const scopeTable = createDataTable({
    caption: 'OAuth scopes this application requires, why each one is needed, and whether it is granted',
    emptyMessage: 'No required scope is declared.',
    columns: [
      { key: 'scope', label: 'Scope', render: (r) => h('span', { class: 'mono' }, String(r.scope ?? '—')) },
      {
        key: 'granted',
        label: 'Granted',
        render: (r) => (state.granted.size === 0
          ? statusChip({
            label: 'Not authorised',
            tone: 'neutral',
            title: 'This connection has not been authorised, so no scope has been granted yet.',
          })
          : state.granted.has(r.scope)
            ? statusChip({ label: 'Granted', tone: 'positive' })
            : statusChip({
              label: 'MISSING',
              tone: 'negative',
              title: 'Every call needing this scope will fail at run time, inside a background job.',
            })),
      },
      {
        key: 'why',
        label: 'What breaks without it',
        // `business_impact_if_missing` is the Wave 4 surface's key and it is
        // the best of the four: it names the CONSEQUENCE rather than the
        // purpose, which is what an operator deciding whether to chase a
        // missing grant actually needs.
        render: (r) => text(r.business_impact_if_missing || r.why || r.reason || r.purpose || '—'),
      },
      {
        key: 'modules',
        label: 'Modules',
        render: (r) => ((Array.isArray(r.modules) && r.modules.length)
          ? h('span', { class: 'mono xs' }, r.modules.join(', '))
          : text('—')),
      },
    ],
  });

  const scopeLoader = createLoader({
    id: 'scopeListStatus',
    glyph: '⊙',
    what: 'The required scope list',
    emptyMessage: 'No required scope is declared for this connection.',
    loadingMessage: 'Loading the required scopes…',
    onRetry: () => loadScopes(),
    announce,
  });

  /* ---------------- documented absences ---------------------------------- */
  const negatives = h('div', { id: 'scopeNegatives' });

  /* ---------------- validation run --------------------------------------- */
  const runButton = h('button', {
    type: 'button', class: 'btn-primary btn-sm', onClick: () => onValidate(),
  }, 'Validate scopes and connectivity');
  const summary = h('div', { id: 'scopeRunSummary' });

  const resultTable = createDataTable({
    caption: 'Module-by-module validation: the endpoint that would be called, the scope it needs, and the outcome',
    emptyMessage: 'Validation has not been run for this connection.',
    columns: [
      { key: 'module', label: 'Module', render: (r) => h('span', { class: 'mono' }, String(r.module ?? '—')) },
      { key: 'result', label: 'Result', render: (r) => resultChip(r.result) },
      {
        key: 'endpoint',
        label: 'Endpoint',
        render: (r) => (r.endpoint
          ? h('span', { class: 'mono xs' }, `${r.http_method || 'GET'} ${r.endpoint}`)
          : h('span', { class: 'muted' }, 'none in the specification')),
      },
      {
        key: 'scope',
        label: 'Scope',
        render: (r) => (r.scope ? h('span', { class: 'mono xs' }, String(r.scope)) : text('—')),
      },
      { key: 'why', label: 'Needed for', render: (r) => text(r.why || '—') },
      {
        key: 'detail',
        label: 'Detail',
        render: (r) => (r.error_message
          ? h('span', {}, [
            h('span', { class: 'mono xs' }, String(r.error_code || 'ERROR')),
            text(` — ${r.error_message}`),
          ])
          : h('span', { class: 'muted xs' }, normaliseMode(state.mode) === 'MOCK'
            ? 'no network call was made'
            : 'no error reported')),
      },
    ],
  });

  const runLoader = createLoader({
    id: 'scopeRunStatus',
    glyph: '⊛',
    what: 'Scope validation',
    emptyMessage: 'Validation returned no module results.',
    loadingMessage: 'Validating every required module…',
    // Nothing has been validated until the operator asks, so the host starts
    // with the prompt rather than with a spinner for a run nobody started.
    idleMessage: 'Validation has not been run for this connection. Use the button above.',
    onRetry: () => onValidate(),
    announce,
  });

  const connectionLoader = createLoader({
    id: 'scopeConnectionsStatus',
    glyph: '⚯',
    what: 'Connection profiles',
    emptyMessage: 'No connection profile exists yet. Create one on SCR-31.',
    loadingMessage: 'Loading connection profiles…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(banner);
  root.appendChild(card('scopeConnectionTitle', 'Connection', [
    h('div', { class: 'toolbar integration-toolbar' }, [connectionField.el]),
    connectionLoader.el,
  ]));
  root.appendChild(card('scopeRequiredTitle', 'Required OAuth scopes — least privilege', [
    h('p', { class: 'muted small' },
      'Each scope is requested because a specific module needs it. A scope that is required but '
      + 'not granted is reported as MISSING here rather than discovered at call time inside a '
      + 'background job.'),
    scopeLoader.el, scopeTable.el,
  ]));
  root.appendChild(card('scopeNegativesTitle', 'Documented absences — no scope grant fixes these', [
    h('p', { class: 'muted small' },
      "These are things Zoho's own published specification does not provide. They are design "
      + 'constraints on this integration, not failures of this connector, and they are deliberately '
      + 'excluded from the pass rate above.'),
    negatives,
  ]));
  root.appendChild(card('scopeRunTitle', 'Module-by-module validation', [
    h('div', { class: 'btn-row' }, [runButton]),
    summary, runLoader.el, resultTable.el,
  ]));

  resultTable.el.hidden = true;

  async function onValidate() {
    if (!state.selected) return;
    runButton.disabled = true;
    await runLoader.run(() => validateScopes(state.selected), {
      render: (data) => {
        const results = Array.isArray(data && data.results) ? data.results
          : Array.isArray(data && data.items) ? data.items : [];
        renderSummary(data, results);
        if (!results.length) { resultTable.renderRows([]); resultTable.el.hidden = true; return false; }
        resultTable.el.hidden = false;
        resultTable.renderRows(results);
        const passed = results.filter((r) => String(r.result).toUpperCase() === 'PASS').length;
        announce(`${passed} of ${results.length} modules passed.`);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') {
          resultTable.renderRows([]); resultTable.el.hidden = true;
          while (summary.firstChild) summary.removeChild(summary.firstChild);
        }
      },
    });
    runButton.disabled = false;
  }

  function renderSummary(data, results) {
    while (summary.firstChild) summary.removeChild(summary.firstChild);
    // NOT AVAILABLE rows are documented absences and are excluded from the
    // denominator: counting them would make a fully-correct connector report a
    // permanently sub-100% pass rate, which trains an operator to ignore it.
    const testable = results.filter((r) => String(r.result).toUpperCase() !== 'NOT AVAILABLE');
    const passed = testable.filter((r) => String(r.result).toUpperCase() === 'PASS').length;
    const absent = results.length - testable.length;
    const verified = ['LIVE_READ', 'LIVE_WRITE'].includes(normaliseMode(data && data.mode ? data.mode : state.mode));
    summary.appendChild(h('div', { class: 'tiles integration-tiles' }, [
      h('div', { class: `tile accent-${passed === testable.length && testable.length ? 'safe' : 'watch'}` }, [
        h('div', { class: 'k' }, 'Testable modules passing'),
        h('div', { class: 'v' }, `${passed} / ${testable.length}`),
        h('div', { class: 'sub' }, verified ? 'measured against the tenant' : 'not verified — no network call was made'),
      ]),
      h('div', { class: 'tile accent-info' }, [
        // NOT "Documented absences": that phrase names the `hard_negatives`
        // section above, which is a different and larger set. This tile counts
        // only the required modules for which the specification documents no
        // endpoint at all, which is why they cannot be tested and are excluded
        // from the denominator beside them.
        h('div', { class: 'k' }, 'Modules with no endpoint'),
        h('div', { class: 'v' }, String(absent)),
        h('div', { class: 'sub' }, 'untestable, and excluded from the pass rate'),
      ]),
      h('div', { class: 'tile accent-info' }, [
        h('div', { class: 'k' }, 'Mode'),
        h('div', { class: 'v integration-tile-badge' }, modeBadge(data && data.mode ? data.mode : state.mode, { note: state.modeNote })),
        h('div', { class: 'sub' }, verified ? 'live calls were made' : 'requests constructed and logged only'),
      ]),
    ]));
  }

  function renderNegatives(list) {
    while (negatives.firstChild) negatives.removeChild(negatives.firstChild);
    if (!list || !list.length) {
      negatives.appendChild(h('p', { class: 'muted' },
        'This build reports no documented absence for the target product.'));
      return;
    }
    negatives.appendChild(h('ul', { class: 'integration-negatives' }, list.map((n) => h('li', {}, [
      statusChip({
        label: String(n.severity || 'NOTE').toUpperCase(),
        tone: SEVERITY_TONE[String(n.severity || '').toUpperCase()] || 'neutral',
      }),
      text(' '),
      h('strong', {}, String(n.title || n.id || 'Documented absence')),
      n.id ? h('span', { class: 'mono xs muted' }, ` (${n.id})`) : null,
      n.impact ? h('div', { class: 'small' }, String(n.impact)) : null,
    ].filter(Boolean)))));
  }

  async function loadScopes() {
    if (!state.selected) return;
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.selected),
    );
    // `granted_scopes` is stored as a JSON string by the Wave 4 surface and as
    // an array by the Wave 5 contract. Both are read; neither is guessed at.
    let granted = (row && (row.granted_scopes || row.granted)) || [];
    if (typeof granted === 'string') {
      try { granted = JSON.parse(granted); } catch { granted = []; }
    }
    state.granted = new Set(Array.isArray(granted) ? granted : []);

    await scopeLoader.run(() => listScopes(state.selected), {
      render: (data) => {
        const required = Array.isArray(data && data.required) ? data.required
          : Array.isArray(data && data.scopes) ? data.scopes
            : Array.isArray(data) ? data : [];
        renderNegatives((data && data.hard_negatives) || []);
        if (!required.length) { scopeTable.renderRows([]); scopeTable.el.hidden = true; return false; }
        scopeTable.el.hidden = false;
        scopeTable.renderRows(required);
        const missing = required.filter((r) => state.granted.size && !state.granted.has(r.scope)).length;
        announce(missing
          ? `${missing} required scope${missing === 1 ? ' is' : 's are'} missing.`
          : `${required.length} required scopes listed.`);
        return true;
      },
      onState: (s) => { if (s !== 'ready') { scopeTable.renderRows([]); scopeTable.el.hidden = true; } },
    });
  }

  async function load() {
    await connectionLoader.run(listConnections, {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.connections) ? data.connections
            : Array.isArray(data && data.items) ? data.items : [];
        state.connections = items;
        if (data && data.mode) state.mode = data.mode;
        while (connectionSelect.firstChild) connectionSelect.removeChild(connectionSelect.firstChild);
        for (const row of items) {
          const id = String(row.connection_id || row.name || '');
          connectionSelect.appendChild(h('option', { value: id },
            row.connector_name ? `${row.connector_name} (${id})` : id));
        }
        if (!items.length) { runButton.disabled = true; return false; }
        const wanted = items.some((c) => String(c.connection_id || c.name) === String(state.selected))
          ? state.selected
          : String(items[0].connection_id || items[0].name || '');
        state.selected = wanted;
        connectionSelect.value = wanted;
        runButton.disabled = false;
        return true;
      },
      onState: (s) => { if (s !== 'ready') runButton.disabled = true; },
    });
    renderBanner();
    await loadScopes();
  }

  function renderBanner() {
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.selected),
    );
    banner.appendChild(modeBanner((row && row.mode) || state.mode, {
      note: state.modeNote,
      extra: 'A PASS in MOCK means the specification documents this endpoint and the scope it needs '
        + 'has been granted. It is not evidence that the tenant answered.',
    }));
  }

  runButton.disabled = true;
  (async () => {
    const globalMode = await getGlobalMode();
    state.mode = globalMode.mode;
    state.modeNote = globalMode.note;
    renderBanner();
    await load();
  })();
}
