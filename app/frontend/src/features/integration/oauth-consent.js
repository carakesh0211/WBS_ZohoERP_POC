/* app/frontend/src/features/integration/oauth-consent.js
   SCR-32 — Zoho OAuth Authorisation and Consent.

   REQ-INT-024 IS THE WHOLE SHAPE OF THIS SCREEN.
   ==============================================
   "The client secret is never returned by any API, never in the DOM, never
   logged." A screen that has to configure a connection is exactly where a
   secret field would normally go, so the requirement is not satisfied by
   remembering not to add one — it is satisfied by a flow in which the browser
   has no role in the secret at all:

     1. The operator installs the client secret ON THE SERVER, out of band,
        under a named reference. This screen tells them the reference name and
        nothing else about it.
     2. This screen reads `client_secret_present` — a BOOLEAN, never a value —
        and renders "installed" or "not installed".
     3. Authorisation is begun by asking the SERVER for an authorisation URL.
        The server holds the secret; the browser receives a URL and the state
        parameter, never a credential.
     4. The token exchange happens server-to-server. No access token, refresh
        token or secret is ever sent to this page.

   Every one of those four is a property a test can check, and
   tests/vrt/integration.spec.js checks all four against the running
   application:
     * no input on this screen accepts a secret (no password input, no field
       whose name, id or label matches /secret/);
     * the create and authorise request bodies carry no secret-shaped key,
       asserted by deep-scanning the intercepted request;
     * a response that DOES carry `client_secret` (a simulated backend
       regression) has that value nowhere in the rendered document, and the
       screen displays the redaction warning rather than swallowing it.

   The third of those is the one that matters most. The first two prove this
   code does not send a secret; the third proves that even a server that breaks
   the rule cannot get one into this DOM.

   `SUPPRESSED` FIELDS AND WHY THEY ARE LISTED
   -------------------------------------------
   integration-api.js's scrub() removes secret-shaped keys from every response.
   Removing them silently would be its own dishonesty: a backend that starts
   leaking a client secret would look, from this screen, exactly like one that
   does not. So the removal is REPORTED, in red, as a backend defect — which is
   what it is.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, keyValues, modeBadge, modeBanner, queryParam, selectInput,
} from './integration-kit.js';
import { beginAuthorisation, getGlobalMode, listConnections } from './integration-api.js';

/**
 * The secret reference an operator installs server-side. Naming it is the
 * whole of this screen's involvement with the secret: it tells the operator
 * WHERE to put it, and never touches the value.
 */
function secretReferenceName(connectionId) {
  return `ZOHO_CLIENT_SECRET__${String(connectionId || '<connection_id>').toUpperCase().replace(/[^A-Z0-9]+/g, '_')}`;
}

function presenceChip(present) {
  if (present === true) {
    return statusChip({
      label: 'Installed server-side',
      tone: 'positive',
      title: 'The server reports that a client secret is installed under this reference. Its value '
        + 'is not readable by any API and has never been sent to this browser.',
    });
  }
  if (present === false) {
    return statusChip({
      label: 'Not installed',
      tone: 'warning',
      title: 'No client secret is installed for this connection. Authorisation cannot complete '
        + 'until an operator installs one on the server.',
    });
  }
  return statusChip({
    label: 'Unknown',
    tone: 'neutral',
    title: 'This build does not report whether a secret is installed. Nothing is inferred.',
  });
}

export function mountOAuthConsent(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = { connections: [], selected: queryParam('connection') || '', mode: 'MOCK', modeNote: '' };

  const banner = h('div', { id: 'oauthModeBanner' });

  const connectionSelect = selectInput([], { onChange: () => { state.selected = connectionSelect.value; renderDetail(); } });
  const connectionField = field('oauthConnection', 'Connection profile', connectionSelect,
    { hint: 'Deep-linkable: /?connection=<id>#integration-oauth.' });

  const detail = h('div', { id: 'oauthDetail' });

  const authoriseButton = h('button', {
    type: 'button', class: 'btn-primary btn-sm', onClick: () => onAuthorise(),
  }, 'Request an authorisation URL');
  const authStatus = h('div', { id: 'oauthAuthStatus', 'aria-live': 'polite' });

  const listLoader = createLoader({
    id: 'oauthConnectionsStatus',
    glyph: '⚯',
    what: 'Connection profiles',
    emptyMessage: 'No connection profile exists yet. Create one on SCR-31 before authorising.',
    loadingMessage: 'Loading connection profiles…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(banner);

  root.appendChild(card('oauthSecretTitle', 'The client secret never reaches this browser', [
    h('p', {},
      'REQ-INT-024: the client secret is never returned by any API, never present in this '
      + 'document, and never written to a log. This screen therefore has no field that accepts '
      + 'one, and there is no flow in this application by which one could be typed here.'),
    h('ol', { class: 'integration-steps' }, [
      h('li', {}, [
        h('strong', {}, 'The operator installs the secret on the server, out of band. '),
        text('It is stored under a named reference; nothing in this application reads its value back.'),
      ]),
      h('li', {}, [
        h('strong', {}, 'This screen reads a boolean, not a value. '),
        text('`client_secret_present` says whether a secret is installed. That is the entire '
          + 'signal, and it is enough to tell the operator whether authorisation can proceed.'),
      ]),
      h('li', {}, [
        h('strong', {}, 'Authorisation is begun by the server. '),
        text('The browser asks for an authorisation URL and receives one. It never holds a '
          + 'credential, and the code-for-token exchange is server-to-server.'),
      ]),
      h('li', {}, [
        h('strong', {}, 'Responses are scrubbed on the way in. '),
        text('Any secret-shaped field in any response is discarded before it can reach the DOM, '
          + 'and the discard is reported as a backend defect rather than hidden.'),
      ]),
    ]),
  ]));

  root.appendChild(card('oauthConnectionTitle', 'Authorise a connection', [
    h('div', { class: 'toolbar integration-toolbar' }, [connectionField.el]),
    listLoader.el,
    detail,
    h('div', { class: 'btn-row' }, [authoriseButton]),
    authStatus,
  ]));

  function renderDetail() {
    while (detail.firstChild) detail.removeChild(detail.firstChild);
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.selected),
    );
    if (!row) {
      authoriseButton.disabled = true;
      return;
    }
    authoriseButton.disabled = false;
    const id = row.connection_id || row.name;
    const present = typeof row.client_secret_present === 'boolean' ? row.client_secret_present : null;
    detail.appendChild(keyValues([
      ['Connection', h('span', { class: 'mono' }, String(id))],
      ['Mode', modeBadge(row.mode || state.mode, { note: state.modeNote })],
      ['Product', text(row.product || 'ERP (assumed — D-14 unresolved)')],
      ['Data centre', h('span', { class: 'mono' }, String(row.dc || row.data_centre || '—'))],
      ['Client id', row.client_id
        ? h('span', { class: 'mono' }, String(row.client_id))
        : h('span', { class: 'muted' }, 'not set')],
      ['Client secret', h('span', { class: 'integration-secret' }, [
        presenceChip(present),
        h('span', { class: 'xs muted' },
          ' The value is never read by this application and is not present in this page.'),
      ])],
      ['Secret reference', h('span', { class: 'mono' }, secretReferenceName(id))],
      ['OAuth status', text(row.oauth_status || 'Not authorised')],
      ['Access token expiry', text(row.access_token_expiry
        ? formatAuditTimestamp(row.access_token_expiry)
        : 'no access token held')],
      ['Token last refreshed', text(row.token_last_refreshed
        ? formatAuditTimestamp(row.token_last_refreshed)
        : 'never')],
    ]));
  }

  async function onAuthorise() {
    if (!state.selected) return;
    authoriseButton.disabled = true;
    renderAuthStatus('info', 'Asking the server for an authorisation URL…');
    try {
      const result = await beginAuthorisation(state.selected);
      const data = result.data || {};
      const url = data.authorize_url || data.authorization_url || '';
      if (url) {
        renderAuthStatus('success', [
          h('div', {}, 'Open this URL to grant consent. The browser never receives a credential; '
            + 'the code it returns is exchanged for tokens by the server.'),
          h('div', { class: 'mono integration-authurl' }, String(url)),
          data.state ? h('div', { class: 'xs muted' }, `CSRF state parameter: ${String(data.state)}`) : null,
        ].filter(Boolean));
      } else {
        renderAuthStatus('success', 'The server accepted the authorisation request. '
          + 'It returned no URL, so the exchange is handled entirely server-side.');
      }
      announce('An authorisation URL was requested.');
    } catch (err) {
      if (err && err.name === 'EndpointUnavailableError') {
        renderAuthStatus('info',
          `Authorisation cannot be started in this build: it does not mount ${err.path}. `
          + 'Nothing was sent.');
      } else {
        renderAuthStatus('error', (err && err.message) || 'Authorisation could not be started.');
      }
      announce('Authorisation was not started.');
    } finally {
      authoriseButton.disabled = false;
    }
  }

  function renderAuthStatus(kind, body) {
    while (authStatus.firstChild) authStatus.removeChild(authStatus.firstChild);
    const glyph = kind === 'error' ? '✖' : kind === 'success' ? '✔' : '·';
    authStatus.appendChild(h('div', {
      class: `msg msg-${kind}`, role: kind === 'error' ? 'alert' : undefined,
    }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, glyph),
      h('div', { class: 'body' }, body),
    ]));
  }

  async function load() {
    await listLoader.run(listConnections, {
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
        if (!items.length) { renderDetail(); return false; }
        const wanted = items.some((c) => String(c.connection_id || c.name) === String(state.selected))
          ? state.selected
          : String(items[0].connection_id || items[0].name || '');
        state.selected = wanted;
        connectionSelect.value = wanted;
        renderDetail();
        return true;
      },
      onState: (s) => { if (s !== 'ready') { authoriseButton.disabled = true; } },
    });
    renderBanner();
  }

  function renderBanner() {
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    const row = state.connections.find(
      (c) => String(c.connection_id || c.name) === String(state.selected),
    );
    banner.appendChild(modeBanner((row && row.mode) || state.mode, {
      note: state.modeNote,
      extra: 'In MOCK the OAuth exchange is simulated: the state handling and storage shape are '
        + 'real, the network exchange is not. No consent screen is actually shown to anyone.',
    }));
  }

  authoriseButton.disabled = true;
  (async () => {
    const globalMode = await getGlobalMode();
    state.mode = globalMode.mode;
    state.modeNote = globalMode.note;
    renderBanner();
    await load();
  })();
}
