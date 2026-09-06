/* app/frontend/src/features/integration/health-dashboard.js
   SCR-38 — Integration Health and API Usage Dashboard, including token health.

   THE DAILY CEILING IS THE BINDING ONE, AND THIS SCREEN LEADS WITH IT.
   WAVE5_CONTRACTS.md, on `integration_rate_budget`: "`window_kind ∈
   MINUTE|DAY`, because on ERP Standard the DAILY ceiling is the binding one,
   not the per-minute." A dashboard that led with calls-per-minute would show a
   comfortable number all day and then stop working at four in the afternoon.
   The daily budget is therefore the first tile, sized against
   `Capabilities.daily_call_ceiling` (assume 2000 on ERP Standard), and the
   per-minute figure sits beside it as the secondary fact it is.

   A CIRCUIT THAT IS OPEN BECAUSE OF A QUOTA IS NOT A CIRCUIT THAT IS OPEN
   BECAUSE ZOHO IS BROKEN. C16's rate_limit_classification distinguishes them:
   429/code 44 (our own throttle failed) and 429/code 45 (daily quota) do NOT
   count toward the breaker, while 5xx, timeouts and 401s do. The two states
   need different actions from an operator — wait, versus investigate — so the
   reason is rendered next to the state rather than folded into it.

   WATERMARKS ARE SHOWN WITH THEIR OVERLAP, BECAUSE A FILTER CANNOT PROVE IT
   RETURNED EVERYTHING. `last_modified_time` is filterable but NOT sortable on
   ERP/Books bills and POs, so a window can be selected but not walked as a
   stable keyset. Every poll therefore uses a bounded window with a 300-second
   overlap, and the completeness sweeps are retained. The high-water mark alone
   would read as "we are caught up to here", which is exactly the claim the
   contract says cannot be made; the overlap and the last sweep are shown next
   to it so the claim on screen matches the claim the design can support.

   TOKEN HEALTH IS A COUNTDOWN, NOT A TIMESTAMP. An access token lives one
   hour. "Expires 14:32 UTC" requires the reader to know what time it is in
   UTC; "expires in 8 minutes" does not, and the absolute time is kept beside
   it for the record.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { setGeometry } from '../../core/dom.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, keyValues, modeBadge, modeBanner, operationalChip,
  queryParam, selectInput,
} from './integration-kit.js';
import { getGlobalMode, getHealth, listConnections } from './integration-api.js';

/** ERP Standard's documented daily ceiling, used only when the server sends none. */
const ASSUMED_DAILY_CEILING = 2000;

/**
 * A usage meter.
 *
 * The width is assigned through the CSSOM (`setGeometry`), never as a markup
 * `style="…"` attribute: the CSP is `style-src 'self'` with no
 * 'unsafe-inline', so the attribute form is blocked outright while CSSOM
 * assignment is not governed by the policy at all. `enhance()` in app.js
 * depends on exactly this distinction for its bar geometry.
 *
 * The percentage is ALSO printed as text. A bar whose only signal is its
 * length is unreadable to a screen reader and unmeasurable in a screenshot.
 */
function meter(used, ceiling, { label }) {
  const cap = Number(ceiling) > 0 ? Number(ceiling) : 0;
  const value = Number(used) || 0;
  const pctValue = cap ? Math.min(100, (value / cap) * 100) : 0;
  const tone = pctValue >= 90 ? 'breach' : pctValue >= 70 ? 'watch' : 'safe';
  const fill = h('span', { class: `integration-meter-fill integration-meter-${tone}` });
  setGeometry(fill, { width: `${pctValue.toFixed(1)}%` });
  return h('div', { class: 'integration-meter' }, [
    h('div', {
      class: 'integration-meter-track',
      role: 'meter',
      'aria-label': label,
      'aria-valuenow': String(Math.round(pctValue)),
      'aria-valuemin': '0',
      'aria-valuemax': '100',
      'aria-valuetext': cap ? `${value} of ${cap} calls, ${pctValue.toFixed(1)} per cent` : `${value} calls, no ceiling reported`,
    }, [fill]),
    h('div', { class: 'xs' }, cap
      ? `${value} of ${cap} calls — ${pctValue.toFixed(1)}%`
      : `${value} calls — no ceiling reported`),
  ]);
}

/** Minutes until an ISO instant, or null when it cannot be computed. */
function minutesUntil(iso) {
  if (!iso) return null;
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return null;
  return Math.round((at - Date.now()) / 60000);
}

function tokenChip(minutes) {
  if (minutes === null) {
    return statusChip({
      label: 'No token held',
      tone: 'neutral',
      title: 'This connection holds no access token. Authorise it on SCR-32.',
    });
  }
  if (minutes <= 0) {
    return statusChip({
      label: `Expired ${Math.abs(minutes)} min ago`,
      tone: 'negative',
      title: 'Every call will fail with 401 until the token is refreshed. A refresh token does not '
        + 'expire until revoked, so this is usually recoverable without re-consent.',
    });
  }
  if (minutes <= 10) {
    return statusChip({
      label: `Expires in ${minutes} min`,
      tone: 'warning',
      title: 'A long-running job started now may outlive this token. The throttle refreshes once on '
        + 'a 401; a second failure opens the circuit and raises a P1.',
    });
  }
  return statusChip({ label: `Expires in ${minutes} min`, tone: 'positive' });
}

/**
 * Refresh-token presence, as three states rather than two.
 *
 * `refresh_token_present` is a boolean when the server reports it and absent
 * when it does not. Collapsing "absent" into "held" would assert the one fact
 * on this panel an operator acts on — whether an expired access token can be
 * recovered without interactive re-consent — from no evidence at all.
 */
function refreshTokenChip(present) {
  if (present === true) {
    return statusChip({
      label: 'Held server-side',
      tone: 'positive',
      title: 'Refresh tokens do not expire until revoked, to a maximum of 20 per user. The value is '
        + 'never returned to this browser.',
    });
  }
  if (present === false) {
    return statusChip({
      label: 'Absent',
      tone: 'negative',
      title: 'Without a refresh token this connection needs interactive re-consent when the access '
        + 'token expires.',
    });
  }
  return statusChip({
    label: 'Not reported',
    tone: 'neutral',
    title: 'This build does not report whether a refresh token is held, so nothing is claimed about '
      + 'it. Do not assume the connection can recover from an expired access token.',
  });
}

export function mountHealthDashboard(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    connections: [], selected: queryParam('connection') || '', mode: 'MOCK', modeNote: '',
  };

  const banner = h('div', { id: 'healthModeBanner' });

  const connectionSelect = selectInput([], {
    onChange: () => { state.selected = connectionSelect.value; renderBanner(); loadHealth(); },
  });
  const connectionField = field('healthConnection', 'Connection profile', connectionSelect,
    { hint: 'Deep-linkable: /?connection=<id>#integration-health.' });

  const tiles = h('div', { id: 'healthTiles' });
  const tokenPanel = h('div', { id: 'healthToken' });
  const circuitPanel = h('div', { id: 'healthCircuits' });

  const watermarkTable = createDataTable({
    caption: 'Per-module high-water marks, the overlap each poll re-reads, and the last completeness sweep',
    emptyMessage: 'No watermark has been recorded for this connection.',
    columns: [
      { key: 'module', label: 'Module', render: (r) => h('span', { class: 'mono' }, String(r.module ?? '—')) },
      { key: 'hwm', label: 'High-water mark', render: (r) => text(formatAuditTimestamp(r.hwm || r.high_water_mark)) },
      {
        key: 'overlap_seconds',
        label: 'Overlap re-read',
        numeric: true,
        render: (r) => h('span', {
          title: '`last_modified_time` is filterable but NOT sortable, so a window can be selected '
            + 'but not walked as a stable keyset. Every poll re-reads this many seconds behind the '
            + 'mark; a filter cannot prove it returned everything.',
        }, `${r.overlap_seconds ?? 300} s`),
      },
      {
        key: 'last_sweep_at',
        label: 'Last completeness sweep',
        render: (r) => (r.last_sweep_at
          ? text(formatAuditTimestamp(r.last_sweep_at))
          : statusChip({
            label: 'Never swept',
            tone: 'warning',
            title: 'The sweeps are what make the poll trustworthy. A module that has never been '
              + 'swept has no evidence of completeness at all.',
          })),
      },
      {
        key: 'discovery',
        label: 'Discovery',
        render: (r) => (r.discovery || (String(r.module || '').includes('receive')
          ? h('span', {
            title: 'Zoho ERP Purchase Receives has NO list endpoint. PO-anchored discovery is the '
              + 'sole acquisition mechanism, and a receive against a PO we do not know about is '
              + 'undiscoverable until its bill arrives.',
          }, 'PO-anchored (no list endpoint)')
          : text('delta window'))),
      },
    ],
  });

  const loader = createLoader({
    id: 'healthStatus',
    glyph: '◔',
    what: 'Integration health',
    emptyMessage: 'This connection has recorded no health data yet.',
    loadingMessage: 'Loading integration health…',
    onRetry: () => loadHealth(),
    announce,
  });

  const connectionLoader = createLoader({
    id: 'healthConnectionsStatus',
    glyph: '⚯',
    what: 'Connection profiles',
    emptyMessage: 'No connection profile exists yet. Create one on SCR-31.',
    loadingMessage: 'Loading connection profiles…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(banner);
  root.appendChild(card('healthConnectionTitle', 'Connection', [
    h('div', { class: 'toolbar integration-toolbar' }, [connectionField.el]),
    connectionLoader.el,
  ]));
  root.appendChild(card('healthUsageTitle', 'API usage against the binding ceiling', [
    h('p', { class: 'muted small' },
      'On ERP Standard the DAILY ceiling is the binding one, not the per-minute — which is why the '
      + 'daily budget leads here. A dashboard that led with calls-per-minute would look comfortable '
      + 'all day and then stop working in the afternoon.'),
    loader.el, tiles,
  ]));
  root.appendChild(card('healthTokenTitle', 'Token health', [tokenPanel]));
  root.appendChild(card('healthCircuitTitle', 'Circuit breakers, per module', [
    h('p', { class: 'muted small' },
      'A circuit opened by an exhausted daily quota needs an operator to wait; one opened by 5xx '
      + 'responses or a rejected token needs an operator to investigate. The reason is shown next '
      + 'to the state rather than folded into it.'),
    circuitPanel,
  ]));
  root.appendChild(card('healthWatermarkTitle', 'Watermarks and completeness', [watermarkTable.el]));

  function renderUsage(data) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    const budgets = Array.isArray(data && data.rate_budgets) ? data.rate_budgets : [];
    const day = budgets.find((b) => String(b.window_kind).toUpperCase() === 'DAY')
      || (data && data.daily) || {};
    const minute = budgets.find((b) => String(b.window_kind).toUpperCase() === 'MINUTE')
      || (data && data.per_minute) || {};
    const dayCeiling = day.ceiling ?? day.limit ?? (data && data.daily_call_ceiling) ?? ASSUMED_DAILY_CEILING;
    const assumed = !(day.ceiling ?? day.limit ?? (data && data.daily_call_ceiling));

    tiles.appendChild(h('div', { class: 'tiles integration-tiles' }, [
      h('div', { class: 'tile accent-info integration-tile-wide' }, [
        h('div', { class: 'k' }, 'Daily call budget — the binding ceiling'),
        h('div', { class: 'v' }, String(day.used ?? 0)),
        meter(day.used ?? 0, dayCeiling, { label: 'Daily call budget used' }),
        h('div', { class: 'sub' }, assumed
          ? `ceiling assumed at ${ASSUMED_DAILY_CEILING} (ERP Standard) — the server reported none`
          : `window opened ${formatAuditTimestamp(day.window_start)}`),
      ]),
      h('div', { class: 'tile accent-info integration-tile-wide' }, [
        h('div', { class: 'k' }, 'Per-minute budget — secondary'),
        h('div', { class: 'v' }, String(minute.used ?? 0)),
        meter(minute.used ?? 0, minute.ceiling ?? minute.limit ?? 0, { label: 'Per-minute call budget used' }),
        h('div', { class: 'sub' }, 'exceeding this checkpoints and resumes; it does not open the circuit'),
      ]),
      h('div', { class: 'tile accent-info' }, [
        h('div', { class: 'k' }, 'Mode'),
        h('div', { class: 'v integration-tile-badge' }, modeBadge((data && data.mode) || state.mode, { note: state.modeNote })),
        h('div', { class: 'sub' }, 'per connection, per §11.9'),
      ]),
    ]));
  }

  function renderToken(data) {
    while (tokenPanel.firstChild) tokenPanel.removeChild(tokenPanel.firstChild);
    const expiry = (data && (data.access_token_expires_at || data.access_token_expiry)) || null;
    const minutes = minutesUntil(expiry);
    tokenPanel.appendChild(keyValues([
      ['Access token', tokenChip(minutes)],
      ['Expires at', expiry ? text(formatAuditTimestamp(expiry)) : h('span', { class: 'muted' }, '—')],
      ['Last refreshed', (data && data.token_last_refreshed)
        ? text(formatAuditTimestamp(data.token_last_refreshed))
        : h('span', { class: 'muted' }, 'never')],
      // Three-way, not two. A server that said nothing about the refresh token
      // must not be rendered as "held": that is the one claim on this panel an
      // operator would act on, and asserting it from an absent field is how a
      // connection comes to look recoverable when it needs re-consent.
      ['Refresh token', refreshTokenChip(data && data.refresh_token_present)],
      ['Secret handling', text('REQ-INT-024: no token or secret value is returned by any API or '
        + 'present in this page. Only these expiry facts are.')],
    ]));
  }

  function renderCircuits(data) {
    while (circuitPanel.firstChild) circuitPanel.removeChild(circuitPanel.firstChild);
    const circuits = Array.isArray(data && data.circuits) ? data.circuits : [];
    if (!circuits.length) {
      circuitPanel.appendChild(h('p', { class: 'muted' },
        'No circuit breaker has tripped or been recorded for this connection.'));
      return;
    }
    circuitPanel.appendChild(h('ul', { class: 'integration-circuits' }, circuits.map((c) => h('li', {}, [
      h('span', { class: 'mono' }, String(c.module || '—')),
      text(' '),
      operationalChip('circuit', c.state || c.status),
      c.reason ? h('div', { class: 'small' }, String(c.reason)) : null,
      c.next_probe_at
        ? h('div', { class: 'xs muted' }, `next probe ${formatAuditTimestamp(c.next_probe_at)}`)
        : null,
      c.counts_toward_circuit === false
        ? h('div', { class: 'xs muted' },
          'This class of failure does not count toward the breaker — it is our own throttle or a '
          + 'quota, not a vendor fault.')
        : null,
    ].filter(Boolean)))));
  }

  async function loadHealth() {
    if (!state.selected) return;
    await loader.run(() => getHealth(state.selected), {
      render: (data) => {
        if (!data || typeof data !== 'object') return false;
        renderUsage(data);
        renderToken(data);
        renderCircuits(data);
        const marks = Array.isArray(data.watermarks) ? data.watermarks : [];
        if (marks.length) {
          watermarkTable.el.hidden = false;
          watermarkTable.renderRows(marks);
        } else {
          watermarkTable.renderRows([]);
          watermarkTable.el.hidden = true;
        }
        announce('Integration health loaded.');
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') {
          while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
          while (tokenPanel.firstChild) tokenPanel.removeChild(tokenPanel.firstChild);
          while (circuitPanel.firstChild) circuitPanel.removeChild(circuitPanel.firstChild);
          watermarkTable.renderRows([]);
          watermarkTable.el.hidden = true;
        }
      },
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
        if (!items.length) return false;
        const wanted = items.some((c) => String(c.connection_id || c.name) === String(state.selected))
          ? state.selected
          : String(items[0].connection_id || items[0].name || '');
        state.selected = wanted;
        connectionSelect.value = wanted;
        return true;
      },
    });
    renderBanner();
    await loadHealth();
  }

  function renderBanner() {
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.selected),
    );
    banner.appendChild(modeBanner((row && row.mode) || state.mode, {
      note: state.modeNote,
      extra: 'In MOCK no call is made, so every usage figure below counts requests that were '
        + 'constructed and logged. They are not measurements of a Zoho tenant.',
    }));
  }

  watermarkTable.el.hidden = true;
  (async () => {
    const globalMode = await getGlobalMode();
    state.mode = globalMode.mode;
    state.modeNote = globalMode.note;
    renderBanner();
    await load();
  })();
}
