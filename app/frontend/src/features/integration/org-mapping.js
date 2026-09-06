/* app/frontend/src/features/integration/org-mapping.js
   SCR-33 — Zoho Organisation Selection and Mapping.

   Every Zoho API call carries an organization_id, and getting it wrong is not
   a cosmetic error: it writes purchase orders into the wrong company's books.
   So this screen does two things and refuses to be clever about either.

   1. IT LISTS WHAT THE AUTHORISED CREDENTIAL CAN SEE, AND NOTHING ELSE.
      There is no free-text organisation id field. An operator who could type
      an id could type one the credential has no access to, and the failure
      would surface hours later as a 401 inside a background job rather than
      here, in front of the person who made the choice.

   2. IT MAKES THE UNIQUENESS CONSTRAINT VISIBLE BEFORE IT IS VIOLATED.
      C2 freezes `UNIQUE (entity_id, organization_id)` on
      integration_connection. An organisation already bound to another
      connection for this entity is shown as such and cannot be selected — the
      server would refuse it anyway, and a client that lets a user press a
      button that is certain to fail is wasting their time and hiding the rule.

   The mapping is a single explicit action with a confirmation of what it will
   do, because it is the point at which "this connection" and "that company's
   ledger" become the same thing.
*/

import { h, text } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, keyValues, modeBadge, modeBanner, queryParam, selectInput,
} from './integration-kit.js';
import {
  getGlobalMode, listConnections, listOrganisations, mapOrganisation,
} from './integration-api.js';

export function mountOrgMapping(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    connections: [], organisations: [], selected: queryParam('connection') || '',
    chosen: null, mode: 'MOCK', modeNote: '',
  };

  const banner = h('div', { id: 'orgModeBanner' });

  const connectionSelect = selectInput([], {
    onChange: () => { state.selected = connectionSelect.value; state.chosen = null; renderCurrent(); loadOrganisations(); },
  });
  const connectionField = field('orgConnection', 'Connection profile', connectionSelect,
    { hint: 'Deep-linkable: /?connection=<id>#integration-organisation.' });

  const current = h('div', { id: 'orgCurrent' });

  /** Is this organisation already bound to a DIFFERENT connection in the same entity? */
  function takenBy(org) {
    const me = state.connections.find((c) => String(c.connection_id || c.name) === String(state.selected));
    const entity = me && (me.entity_id || me.entity);
    const id = String(org.organization_id || org.organisation_id || org.id || '');
    return state.connections.find((c) => {
      if (String(c.connection_id || c.name) === String(state.selected)) return false;
      if (entity && (c.entity_id || c.entity) && String(c.entity_id || c.entity) !== String(entity)) return false;
      return String(c.organization_id || c.zoho_org_id || '') === id;
    });
  }

  const table = createDataTable({
    caption: 'Organisations visible to the authorised credential, and which connection each is bound to',
    emptyMessage: 'The authorised credential can see no organisation.',
    columns: [
      {
        key: 'organization_id',
        label: 'Organisation id',
        render: (r) => h('span', { class: 'mono' },
          String(r.organization_id || r.organisation_id || r.id || '—')),
      },
      { key: 'name', label: 'Name', render: (r) => text(r.name || r.company_name || '—') },
      { key: 'currency', label: 'Base currency', render: (r) => text(r.currency_code || r.currency || '—') },
      {
        key: 'status',
        label: 'Availability',
        render: (r) => {
          const clash = takenBy(r);
          if (clash) {
            return statusChip({
              label: 'Bound elsewhere',
              tone: 'warning',
              title: `Already mapped to connection ${clash.connection_id || clash.name}. C2 freezes `
                + 'UNIQUE (entity_id, organization_id), so this entity cannot bind it twice.',
            });
          }
          const id = String(r.organization_id || r.organisation_id || r.id || '');
          const me = state.connections.find(
            (c) => String(c.connection_id || c.name) === String(state.selected),
          );
          if (me && String(me.organization_id || me.zoho_org_id || '') === id) {
            return statusChip({ label: 'Currently mapped', tone: 'positive' });
          }
          return statusChip({ label: 'Available', tone: 'neutral' });
        },
      },
      {
        key: 'choose',
        label: 'Select',
        render: (r) => {
          const clash = takenBy(r);
          const id = String(r.organization_id || r.organisation_id || r.id || '');
          const button = h('button', {
            type: 'button',
            class: 'btn-sm',
            disabled: !!clash || !id,
            onClick: () => { state.chosen = r; renderCurrent(); announce(`Selected organisation ${id}.`); },
          }, 'Select');
          if (clash) {
            button.title = 'This organisation is bound to another connection in the same entity. '
              + 'The server would refuse the mapping.';
          }
          return button;
        },
      },
    ],
  });

  const orgLoader = createLoader({
    id: 'orgListStatus',
    glyph: '⌾',
    what: 'Organisation discovery',
    emptyMessage: 'The authorised credential can see no organisation. Authorise the connection on '
      + 'SCR-32 first, then return here.',
    loadingMessage: 'Discovering organisations…',
    // Discovery needs a connection first, so this host waits rather than
    // spinning for a request that has not been made.
    idleMessage: 'Select a connection profile to discover the organisations its credential can see.',
    onRetry: () => loadOrganisations(),
    announce,
  });

  const connectionLoader = createLoader({
    id: 'orgConnectionsStatus',
    glyph: '⚯',
    what: 'Connection profiles',
    emptyMessage: 'No connection profile exists yet. Create one on SCR-31.',
    loadingMessage: 'Loading connection profiles…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(banner);
  root.appendChild(card('orgConnectionTitle', 'Connection', [
    h('div', { class: 'toolbar integration-toolbar' }, [connectionField.el]),
    connectionLoader.el,
    current,
  ]));
  root.appendChild(card('orgListTitle', 'Organisations visible to this credential', [
    h('p', { class: 'muted small' },
      'Only organisations the authorised credential can actually see are listed, and there is no '
      + 'free-text id field. An organisation id typed by hand would fail hours later inside a '
      + 'background job rather than here.'),
    orgLoader.el,
    table.el,
  ]));

  function renderCurrent() {
    while (current.firstChild) current.removeChild(current.firstChild);
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.selected),
    );
    if (!row) return;
    const id = row.connection_id || row.name;
    const mapped = row.organization_id || row.zoho_org_id || '';
    const chosenId = state.chosen
      ? String(state.chosen.organization_id || state.chosen.organisation_id || state.chosen.id || '')
      : '';

    const confirm = h('button', {
      type: 'button', class: 'btn-primary btn-sm', disabled: !chosenId,
      onClick: () => onMap(chosenId),
    }, chosenId ? `Map ${chosenId} to this connection` : 'Select an organisation below');

    current.appendChild(keyValues([
      ['Connection', h('span', { class: 'mono' }, String(id))],
      ['Entity', h('span', { class: 'mono' }, String(row.entity_id || row.entity || '—'))],
      ['Mode', modeBadge(row.mode || state.mode, { note: state.modeNote })],
      ['Currently mapped organisation', mapped
        ? h('span', { class: 'mono' }, String(mapped))
        : h('span', { class: 'muted' }, 'none — this connection cannot call Zoho until one is bound')],
      ['Selected', chosenId
        ? h('span', { class: 'mono' }, chosenId)
        : h('span', { class: 'muted' }, 'nothing selected')],
    ]));
    current.appendChild(h('div', { class: 'btn-row' }, [confirm]));
    current.appendChild(h('div', { id: 'orgMapStatus', 'aria-live': 'polite' }));
  }

  async function onMap(organizationId) {
    const host = document.getElementById('orgMapStatus');
    const say = (kind, message) => {
      if (!host) return;
      while (host.firstChild) host.removeChild(host.firstChild);
      host.appendChild(h('div', {
        class: `msg msg-${kind}`, role: kind === 'error' ? 'alert' : undefined,
      }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, kind === 'error' ? '✖' : kind === 'success' ? '✔' : '·'),
        h('div', { class: 'body' }, message),
      ]));
    };
    say('info', 'Binding the organisation…');
    try {
      await mapOrganisation(state.selected, organizationId);
      say('success', `Organisation ${organizationId} is bound to ${state.selected}.`);
      announce('The organisation was bound.');
      state.chosen = null;
      await load();
    } catch (err) {
      if (err && err.name === 'EndpointUnavailableError') {
        say('info', `The mapping cannot be saved in this build: it does not mount ${err.path}. `
          + 'Nothing was sent.');
      } else {
        say('error', (err && err.message) || 'The organisation could not be bound.');
      }
      announce('The organisation was not bound.');
    }
  }

  async function loadOrganisations() {
    if (!state.selected) return;
    await orgLoader.run(() => listOrganisations(state.selected), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.organizations) ? data.organizations
            : Array.isArray(data && data.items) ? data.items : [];
        state.organisations = items;
        if (!items.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.el.hidden = false;
        table.renderRows(items);
        announce(`${items.length} organisation${items.length === 1 ? '' : 's'} visible.`);
        return true;
      },
      onState: (s) => { if (s !== 'ready') { table.renderRows([]); table.el.hidden = true; } },
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
        renderCurrent();
        return true;
      },
    });
    renderBanner();
    await loadOrganisations();
  }

  function renderBanner() {
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.selected),
    );
    banner.appendChild(modeBanner((row && row.mode) || state.mode, {
      note: state.modeNote,
      extra: 'In MOCK the organisation list is synthesised locally. It is not evidence that any '
        + 'credential can see any Zoho organisation.',
    }));
  }

  (async () => {
    const globalMode = await getGlobalMode();
    state.mode = globalMode.mode;
    state.modeNote = globalMode.note;
    renderBanner();
    await load();
  })();
}
