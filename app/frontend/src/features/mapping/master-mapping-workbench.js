/* app/frontend/src/features/mapping/master-mapping-workbench.js
   SCR-35 — Master Data Mapping Workbench.

   WHAT THIS SCREEN IS FOR, IN ONE SENTENCE
   ----------------------------------------
   It says, for every vendor and item this application holds, WHICH external
   record it is bound to and how confident that binding is — because a purchase
   order posted against the wrong vendor is not a data-quality problem, it is
   money in somebody else's ledger.

   THREE RULES THIS SCREEN KEEPS
   -----------------------------
   1. THE REGISTRY DECIDES A CONFLICT, NOT THE BROWSER.
      `mapping_status` and `duplicate_of` are written by the ingestion path,
      which has the payload hash and the whole table. This screen renders them
      and computes nothing of its own. A similarity heuristic run over one
      visible page would produce a second opinion with no way to tell which one
      an operator should act on, and the one with a button next to it wins by
      default.

   2. NO TOTAL IS EVER SHOWN.
      `/api/masters/{kind}` is cursor-paginated and returns no count. "14
      unmapped vendors" derived from a fifty-row page reads as the size of the
      remaining work and would be wrong by an unknown amount. The screen says
      how many are on the page and says that the total is not reported.

   3. FILTERING HAPPENS SERVER-SIDE.
      For the same reason. `?mapping_status=NEEDS_REVIEW` asks the server for
      every row in that state; filtering the visible page would show a subset
      of a subset and present it as the answer to the question that was asked.

   ROLE AND SCOPE
   --------------
   `/api/masters/*` carries `masters.read` as a ROUTER-level dependency, so
   every route is gated before its handler runs and a refusal arrives as a
   refusal. Row-level scope is enforced in the same place, by the four-dimension
   scope predicate `repo.query()` compiles — this screen sends no scope of its
   own and could not widen one if it tried. The permission summary rendered
   here is a description of what the server did, not a second gate.
*/

import { h, text } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from '../integration/integration-screen.js';
import {
  MAPPING_STATUSES, MASTER_KINDS, getPrincipal, listMasterDuplicates, listMasters,
} from './mapping-api.js';
import {
  card, createAnnouncer, field, identifier, keyValues, mappingStatusChip, pageCount,
  queryParam, selectInput, textInput, timestamp,
} from './mapping-kit.js';

/** The page size asked for. The server caps it; this is a request, not a claim. */
const LIMIT = 50;

export function mountMasterMappingWorkbench(root) {
  if (!root) return;
  const announce = createAnnouncer('mappingLiveRegion');

  const state = {
    kind: MASTER_KINDS.some((k) => k.kind === queryParam('kind'))
      ? queryParam('kind') : MASTER_KINDS[0].kind,
    q: queryParam('q') || '',
    mappingStatus: MAPPING_STATUSES.includes(String(queryParam('mapping_status') || '').toUpperCase())
      ? String(queryParam('mapping_status')).toUpperCase() : '',
    active: '',
    rows: [],
    hasMore: false,
    permissions: new Set(),
    principalRead: false,
  };

  /* ---------------- controls ---------------- */

  const kindSelect = selectInput(
    MASTER_KINDS.map((k) => [k.kind, k.label]),
    { onChange: () => { state.kind = kindSelect.value; load(); } },
  );
  kindSelect.value = state.kind;
  const kindField = field('mmKind', 'Master', kindSelect, {
    hint: 'Deep-linkable: /?kind=vendors&mapping_status=NEEDS_REVIEW#mapping-master.',
  });

  const searchInput = textInput({
    value: state.q,
    onChange: () => { state.q = searchInput.value; load(); },
  });
  const searchField = field('mmSearch', 'Code or name contains', searchInput, {
    hint: 'Sent to the server as ?q=. The whole set is searched, not the visible page.',
  });

  const statusSelect = selectInput([
    ['', 'Every mapping status'],
    ...MAPPING_STATUSES.map((s) => [s, s]),
  ], { onChange: () => { state.mappingStatus = statusSelect.value; load(); } });
  statusSelect.value = state.mappingStatus;
  const statusField = field('mmStatus', 'Match status', statusSelect, {
    hint: 'All four values are offered whether or not any row currently holds one — an empty '
      + 'bucket and an absent bucket are different answers.',
  });

  const activeSelect = selectInput([
    ['', 'Active and inactive'],
    ['true', 'Active only'],
    ['false', 'Inactive only'],
  ], { onChange: () => { state.active = activeSelect.value; load(); } });
  const activeField = field('mmActive', 'Record state', activeSelect);

  /* ---------------- table ---------------- */

  function idOf(row) {
    const spec = MASTER_KINDS.find((k) => k.kind === state.kind);
    return row[(spec && spec.idKey) || 'item_id'] || row.item_id || row.vendor_id || '';
  }

  const table = createDataTable({
    caption: 'Local master records, the external record each is bound to, and the state of that '
      + 'binding',
    emptyMessage: 'No master record matches these filters.',
    columns: [
      {
        key: 'source_id',
        label: 'Source identifier (ours)',
        render: (r) => h('div', { class: 'mapping-idpair' }, [
          identifier(r.code, { none: 'no code' }),
          h('span', { class: 'xs muted' }, String(idOf(r) || '')),
        ]),
      },
      { key: 'name', label: 'Name', render: (r) => text(r.name || '—') },
      {
        key: 'origin',
        label: 'Origin',
        render: (r) => {
          const source = String(r.source || '').toUpperCase();
          const truth = String(r.source_of_truth_status || '').toUpperCase();
          /* `source_of_truth_status` is the honesty column: LOCAL / MOCK /
             UNVERIFIED are all "nothing has confirmed this against a live
             tenant", and only LIVE / VERIFIED claim otherwise. The chip tone
             follows that split rather than following whether the row came from
             a Zoho-shaped import, which is a different question. */
          const verified = truth === 'LIVE' || truth === 'VERIFIED';
          return h('div', { class: 'mapping-origin' }, [
            statusChip({
              label: source || 'NOT REPORTED',
              tone: verified ? 'positive' : 'neutral',
              title: verified
                ? `source_of_truth_status is ${truth}.`
                : `source_of_truth_status is ${truth || 'not reported'} — this row has not been `
                  + 'confirmed against a live tenant.',
            }),
            h('span', { class: 'xs muted' }, truth || 'truth status not reported'),
          ]);
        },
      },
      {
        key: 'target',
        label: 'Target identifier (external)',
        render: (r) => {
          if (!r.external_id) {
            return h('span', { class: 'mapping-untargeted' }, [
              h('span', { class: 'sym', 'aria-hidden': 'true' }, '·' ),
              text(' no external record bound'),
            ]);
          }
          return h('div', { class: 'mapping-idpair' }, [
            identifier(r.external_id),
            h('span', { class: 'xs muted' }, String(r.external_source || 'source not reported')),
          ]);
        },
      },
      {
        key: 'external_last_modified',
        label: 'External last modified',
        render: (r) => timestamp(r.external_last_modified),
      },
      {
        key: 'mapping_status',
        label: 'Match status',
        render: (r) => mappingStatusChip(r.mapping_status),
      },
      {
        key: 'conflict',
        label: 'Conflict / manual review',
        render: (r) => {
          const status = String(r.mapping_status || '').toUpperCase();
          if (r.duplicate_of) {
            return h('div', { class: 'mapping-conflict' }, [
              statusChip({
                label: 'DUPLICATE OF',
                tone: 'negative',
                title: 'The registry recorded this row as a probable duplicate of the record '
                  + 'named beside it. Both may be posted against; that is the harm.',
              }),
              identifier(r.duplicate_of),
            ]);
          }
          if (status === 'NEEDS_REVIEW') {
            return h('span', { class: 'mapping-conflict' }, [
              statusChip({
                label: 'AWAITING A HUMAN DECISION',
                tone: 'warning',
                title: 'The registry could not settle this binding and will not guess. Nothing '
                  + 'about the row changes until somebody decides.',
              }),
            ]);
          }
          return h('span', { class: 'muted xs' }, 'none recorded');
        },
      },
      {
        key: 'is_active',
        label: 'Active',
        render: (r) => (r.is_active === false
          ? statusChip({ label: 'INACTIVE', tone: 'neutral', title: 'Deactivated in the master registry.' })
          : statusChip({ label: 'ACTIVE', tone: 'positive' })),
      },
    ],
  });

  const counts = h('div', { id: 'mmCounts' });

  const loader = createLoader({
    id: 'mmStatus',
    glyph: '⇵',
    what: 'The master mapping registry',
    emptyMessage: 'No master record matches these filters. This is a real, empty answer from the '
      + 'master registry — not a missing endpoint, which reads differently.',
    loadingMessage: 'Reading the master registry…',
    onRetry: () => load(),
    announce,
  });

  /* ---------------- duplicates ---------------- */

  const dupTable = createDataTable({
    caption: 'Duplicate candidates the master registry itself flagged',
    emptyMessage: 'The registry has flagged no duplicate candidate for this master.',
    columns: [
      { key: 'id', label: 'Record', render: (r) => identifier(r.id || r.item_id || r.vendor_id) },
      { key: 'code', label: 'Code', render: (r) => identifier(r.code, { none: 'no code' }) },
      { key: 'name', label: 'Name', render: (r) => text(r.name || '—') },
      { key: 'duplicate_of', label: 'Duplicate of', render: (r) => identifier(r.duplicate_of) },
      {
        key: 'reason',
        label: 'Why the registry flagged it',
        render: (r) => text(r.reason || 'The registry recorded no reason for this flag.'),
      },
    ],
  });

  const dupLoader = createLoader({
    id: 'mmDupStatus',
    glyph: '⚑',
    what: 'The duplicate-candidate list',
    emptyMessage: 'The registry has flagged no duplicate candidate for this master. That is the '
      + 'registry\'s answer, not an absence of checking.',
    loadingMessage: 'Reading duplicate candidates…',
    onRetry: () => loadDuplicates(),
    announce,
  });

  /* ---------------- permission summary ---------------- */

  const permissions = h('div', { id: 'mmPermissions' });

  function renderPermissions() {
    while (permissions.firstChild) permissions.removeChild(permissions.firstChild);
    const has = (p) => state.permissions.has(p);
    permissions.appendChild(keyValues([
      ['Acting permission', state.principalRead
        ? h('span', { class: 'mono' }, has('masters.read') ? 'masters.read held' : 'masters.read NOT held')
        : h('span', { class: 'muted' }, 'not established in this session — the server still decides')],
      ['Where the check runs', text('On the server, as a router-level dependency on every '
        + '/api/masters route. Nothing on this screen is a second gate, and nothing here can '
        + 'widen what the server returned.')],
      ['Row-level scope', text('Applied by the same server-side query as the permission: every '
        + 'column mapping names entity, plant, project and location, and waives a dimension only '
        + 'by explicit declaration. This screen sends no scope and cannot broaden one.')],
      ['Editing', text('This workbench reads. Creating a master, editing one and deactivating '
        + 'one are separate, separately permissioned routes and are not offered here.')],
    ]));
  }

  /* ---------------- layout ---------------- */

  root.appendChild(card('mmFilterTitle', 'What to show', [
    h('div', { class: 'toolbar mapping-toolbar' }, [
      kindField.el, searchField.el, statusField.el, activeField.el,
    ]),
    h('p', { class: 'muted small' },
      'Every filter is sent to the server. The response is cursor-paginated and carries no count, '
      + 'so no total appears anywhere on this screen.'),
  ]));

  root.appendChild(card('mmTableTitle', 'Source and target identifiers, and the state of each binding', [
    loader.el,
    counts,
    table.el,
  ]));

  root.appendChild(card('mmDupTitle', 'Conflicts the registry raised', [
    h('p', { class: 'muted small' },
      'These are the registry\'s own duplicate flags, read from /api/masters/{kind}/duplicates. '
      + 'Nothing on this screen compares two records itself: a browser-side similarity guess would '
      + 'be a second opinion with no way to tell which one to act on.'),
    dupLoader.el,
    dupTable.el,
  ]));

  root.appendChild(card('mmPermTitle', 'Role and scope', [permissions]));

  /* ---------------- loading ---------------- */

  function params() {
    const out = { limit: LIMIT };
    if (state.q) out.q = state.q;
    if (state.mappingStatus) out.mapping_status = state.mappingStatus;
    if (state.active) out.is_active = state.active;
    return out;
  }

  function renderCounts() {
    while (counts.firstChild) counts.removeChild(counts.firstChild);
    const spec = MASTER_KINDS.find((k) => k.kind === state.kind);
    counts.appendChild(pageCount(
      state.rows.length,
      !!state.hasMore,
      (spec ? spec.label : 'records').toLowerCase(),
    ));
  }

  async function load() {
    const result = await loader.run(() => listMasters(state.kind, params()), {
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
        announce(`${items.length} record${items.length === 1 ? '' : 's'} shown.`);
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
    await loadDuplicates();
    return result;
  }

  async function loadDuplicates() {
    await dupLoader.run(() => listMasterDuplicates(state.kind), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items : [];
        if (!items.length) { dupTable.renderRows([]); dupTable.el.hidden = true; return false; }
        dupTable.el.hidden = false;
        dupTable.renderRows(items);
        return true;
      },
      onState: (s) => { if (s !== 'ready') { dupTable.renderRows([]); dupTable.el.hidden = true; } },
    });
  }

  (async () => {
    const principal = await getPrincipal();
    state.permissions = principal.permissions;
    state.principalRead = principal.read;
    renderPermissions();
    await load();
  })();
}
