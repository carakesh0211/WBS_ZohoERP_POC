/* app/frontend/src/components/export-button.js
   ONE reusable export control (Stream C), mounted on every register, report,
   dashboard, reconciliation and tabular screen this stream touches.

   Contract this codes against (app/backend/api/exports.py, app/backend/pg/
   exports.py — read in full before changing this file):

     GET  /api/exports/datasets                 -> {items:[{dataset, formats,
                                                     filters_supported, ...}], ...}
     POST /api/exports  {dataset, filters?, format?, chunk_rows?, ttl_hours?}
       -> 202 {export_job_id, state, progress, result, poll:{...}, ...}
     GET  /api/exports/{id}                      -> the same job shape
     POST /api/exports/{id}/advance              -> drives ONE bounded chunk;
       {export_job_id, state, more, rows_written(_now), rows_total?}. THE SPA
       DRIVES ITS OWN JOB: nothing on the server advances a job on its own
       (see exports.py's module docstring), so this component calls /advance
       in a loop until `state` is terminal.
     POST /api/exports/{id}/retry                -> re-queues a FAILED job
     POST /api/exports/{id}/cancel               -> honoured at the next
       chunk boundary, i.e. the NEXT /advance this component makes
     GET  /api/exports/{id}/result               -> the rendered file. CSV is
       text/csv; XLSX is binary. Both are fetched as a Blob and handed to the
       browser as a download named from the Content-Disposition header,
       because a `<script>`-visible text body would corrupt an .xlsx.

   WHY THIS IS ONE FILE AND NOT FOURTEEN
   --------------------------------------
   Every screen this mounts on differs only in `dataset` and in what its OWN
   filter state looks like. `filtersProvider()` is the seam: it returns the
   CALLING SCREEN's current filters in the export API's OWN field names (see
   FILTER_FIELDS in app/backend/pg/exports.py), and this module intersects
   that against the dataset's OWN `filters_supported` (from the datasets
   route) before ever building a request — a field the dataset does not
   support is dropped here, silently to the network call and visibly to the
   operator (a tooltip on the export controls), never sent and refused by
   the server as UNKNOWN_FILTER.

   MONEY, ROWS, FILENAMES: rendered exactly as the server sent them. No paise
   arithmetic, no row-count estimate, no filename invented — see the module
   docstring on `list_datasets` for why the refusals are published too.
*/

import { h, clear } from '../core/dom.js';
import { ApiClientError, createApiClient, getSession } from '../core/api-client.js';

export class ExportApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'ExportApiError';
  }
}

const client = createApiClient({
  basePath: '/api/exports',
  ErrorClass: ExportApiError,
  messages: {
    network: 'The export service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to continue.',
    notfound: 'That export job could not be found.',
    forbidden: 'You do not have permission to create an export.',
    validation: 'The export could not be started. Correct the highlighted filters and try again.',
    error: (status) => `The export service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The export service returned a response that could not be understood.',
  },
});

/* The catalogue rarely changes within a session and every mounted button
   needs it, so it is fetched ONCE and shared — not once per screen, which
   is what a naive per-instance fetch would do on a page carrying several of
   these controls (e.g. a screen with both a lines table and a header
   export). Reset on failure so a later mount can retry after a transient
   network error instead of being stuck on a rejected promise forever. */
let datasetsPromise = null;
function loadDatasets() {
  if (!datasetsPromise) {
    datasetsPromise = client.get('/datasets').catch((err) => {
      datasetsPromise = null;
      throw err;
    });
  }
  return datasetsPromise;
}

const TERMINAL_STATES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED']);
const ACTIVE_STATES = new Set(['QUEUED', 'RUNNING']);
/* exports.py's own advertised interval (api/exports.py::_POLL_AFTER_SECONDS)
   — this component is the poller the module docstring says the SPA must be,
   so it waits the same interval between /advance calls. */
const POLL_MS = 2000;

function sleep(ms) {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

function sanitiseFilters(rawFilters, supportedFields) {
  const out = {};
  const dropped = [];
  for (const [key, value] of Object.entries(rawFilters || {})) {
    if (value === undefined || value === null || value === '') continue;
    if (Array.isArray(value) && value.length === 0) continue;
    if (supportedFields && !supportedFields.has(key)) { dropped.push(key); continue; }
    out[key] = value;
  }
  return { filters: out, dropped };
}

function filenameFromContentDisposition(headerValue, fallback) {
  if (headerValue) {
    const starred = /filename\*=UTF-8''([^;]+)/i.exec(headerValue);
    if (starred) { try { return decodeURIComponent(starred[1]); } catch { /* fall through */ } }
    const plain = /filename="?([^";]+)"?/i.exec(headerValue);
    if (plain) return plain[1];
  }
  return fallback || 'export';
}

/** Trigger a browser download of an already-fetched Blob without navigating. */
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/**
 * Fetch GET /api/exports/{id}/result and save it as a file. Not routed
 * through core/api-client.js: that module always parses the body as JSON
 * text, which would corrupt a binary .xlsx. A raw fetch() with the SAME
 * session header is the whole difference.
 */
async function downloadResult(jobId, fallbackFilename) {
  const headers = {};
  const sid = getSession();
  if (sid) headers['X-Session'] = sid;
  let res;
  try {
    res = await fetch(`/api/exports/${encodeURIComponent(jobId)}/result`, { headers });
  } catch {
    throw new ExportApiError('The export file could not be reached. Check your connection and try again.',
      { kind: 'network' });
  }
  if (!res.ok) {
    let detail = null;
    try { detail = await res.json(); } catch { /* not JSON: fall through to the generic message */ }
    const message = (detail && detail.detail) || `The export file could not be downloaded (HTTP ${res.status}).`;
    throw new ExportApiError(typeof message === 'string' ? message : JSON.stringify(message),
      { status: res.status, kind: 'error' });
  }
  const blob = await res.blob();
  const filename = filenameFromContentDisposition(res.headers.get('Content-Disposition'), fallbackFilename);
  saveBlob(blob, filename);
}

/**
 * Mount a reusable Export CSV / Export XLSX control onto `host`.
 *
 * @param {Object} opts
 * @param {HTMLElement} opts.host - an element this owns exclusively; its
 *   previous contents (if any — a re-mount on a re-rendered screen) are
 *   cleared first.
 * @param {string} opts.dataset - one of GET /api/exports/datasets' names.
 * @param {() => Object} [opts.filtersProvider] - returns the CALLING
 *   screen's CURRENT filters, in the export API's own field names. Called
 *   fresh at the moment an export is started, never cached, so the export
 *   always reflects what is on screen right now. Defaults to `() => ({})`
 *   for a screen with no filters of its own (e.g. a fixed dataset dump).
 * @param {string} [opts.label] - the dataset's human name, used in status
 *   text; defaults to `dataset` itself.
 * @returns {{ destroy: () => void }} destroy stops any in-flight polling —
 *   call it if `host` is about to be removed from the document.
 */
export function mountExportButton({ host, dataset, filtersProvider, label }) {
  if (!host) return { destroy() {} };
  clear(host);

  const state = { job: null, driving: false, destroyed: false, datasetMeta: null };
  const title = label || dataset;

  const csvBtn = h('button', { type: 'button', class: 'btn-sm', disabled: true }, 'Export CSV');
  const xlsxBtn = h('button', { type: 'button', class: 'btn-sm', disabled: true, hidden: true }, 'Export XLSX');
  const cancelBtn = h('button', { type: 'button', class: 'btn-sm', hidden: true }, 'Cancel');
  const retryBtn = h('button', { type: 'button', class: 'btn-sm', hidden: true }, 'Retry');
  const statusEl = h('span', { class: 'export-status', role: 'status' }, '');
  const noteHost = h('span', {});
  const resultHost = h('span', {});

  host.appendChild(h('div', { class: 'export-actions' }, [
    csvBtn, xlsxBtn, cancelBtn, retryBtn, statusEl, noteHost, resultHost,
  ]));

  function setButtonsForIdle() {
    const xlsxAllowed = !!(state.datasetMeta && state.datasetMeta.formats.includes('xlsx'));
    csvBtn.disabled = false;
    csvBtn.hidden = false;
    xlsxBtn.hidden = !xlsxAllowed;
    xlsxBtn.disabled = !xlsxAllowed;
    cancelBtn.hidden = true;
    retryBtn.hidden = true;
  }

  function setButtonsForActive() {
    csvBtn.disabled = true;
    xlsxBtn.disabled = true;
    cancelBtn.hidden = false;
    retryBtn.hidden = true;
  }

  loadDatasets().then((res) => {
    if (state.destroyed) return;
    const items = (res && res.items) || [];
    state.datasetMeta = items.find((d) => d.dataset === dataset) || null;
    if (!state.datasetMeta) {
      statusEl.classList.add('is-error');
      statusEl.textContent = `"${dataset}" is not an export dataset this build knows.`;
      return;
    }
    setButtonsForIdle();
  }).catch((err) => {
    if (state.destroyed) return;
    statusEl.classList.add('is-error');
    statusEl.textContent = (err && err.message) || 'The export catalogue could not be loaded.';
  });

  function renderProgress() {
    const job = state.job;
    if (!job) return;
    statusEl.classList.toggle('is-error', job.state === 'FAILED');
    clear(resultHost);
    // setButtonsForIdle() BEFORE the state-specific branches below: it
    // unconditionally hides Retry/Cancel and re-enables Export CSV/XLSX, and
    // FAILED's own branch un-hides Retry immediately afterward. Calling it
    // AFTER those branches (as an earlier version of this file did) silently
    // hid the Retry button the instant a job failed, by re-hiding what the
    // FAILED branch had just shown.
    if (TERMINAL_STATES.has(job.state)) setButtonsForIdle();
    if (ACTIVE_STATES.has(job.state)) {
      const pct = job.progress && job.progress.percent;
      statusEl.textContent = pct !== null && pct !== undefined
        ? `${title}: ${job.state.toLowerCase()} (${pct.toFixed ? pct.toFixed(0) : pct}%, ${job.progress.rows_written} rows so far)…`
        : `${title}: ${job.state.toLowerCase()} (${(job.progress && job.progress.rows_written) || 0} rows so far)…`;
    } else if (job.state === 'SUCCEEDED') {
      const result = job.result || {};
      statusEl.textContent = `${title}: ready — ${result.rows ?? '?'} rows`
        + (result.expires_at ? `, available until ${result.expires_at}` : '') + '.';
      resultHost.appendChild(h('button', {
        type: 'button', class: 'btn-sm btn-primary',
        onClick: () => triggerDownload(),
      }, `Download ${result.filename || 'file'}`));
    } else if (job.state === 'FAILED') {
      statusEl.textContent = `${title}: failed`
        + (job.error && job.error.detail ? ` — ${job.error.detail}` : job.error && job.error.code ? ` (${job.error.code})` : '') + '.';
      retryBtn.hidden = false;
    } else if (job.state === 'CANCELLED') {
      statusEl.textContent = `${title}: cancelled.`;
    } else if (job.state === 'EXPIRED') {
      statusEl.textContent = `${title}: the result has expired and can no longer be downloaded.`;
    }
  }

  async function triggerDownload() {
    const job = state.job;
    if (!job) return;
    try {
      await downloadResult(job.export_job_id, (job.result && job.result.filename) || undefined);
    } catch (err) {
      statusEl.classList.add('is-error');
      statusEl.textContent = (err && err.message) || 'The export file could not be downloaded.';
    }
  }

  async function driveJob(jobId) {
    state.driving = true;
    setButtonsForActive();
    try {
      for (;;) {
        if (state.destroyed) return;
        const advanceRes = await client.post(`/${encodeURIComponent(jobId)}/advance`, {});
        const outcome = advanceRes || {};
        state.job = { ...state.job, ...outcome,
          progress: { ...(state.job && state.job.progress), rows_written: outcome.rows_written,
                     rows_total: outcome.rows_total ?? (state.job && state.job.progress && state.job.progress.rows_total) } };
        renderProgress();
        if (TERMINAL_STATES.has(outcome.state)) break;
        await sleep(POLL_MS);
      }
      if (state.destroyed) return;
      // The final GET carries `result` (filename, sha256, expires_at) and
      // `error`, neither of which /advance's own bounded response repeats.
      const full = await client.get(`/${encodeURIComponent(jobId)}`);
      if (state.destroyed) return;
      state.job = full;
      renderProgress();
    } catch (err) {
      if (state.destroyed) return;
      statusEl.classList.add('is-error');
      statusEl.textContent = (err && err.message) || `${title}: the export could not be completed.`;
      setButtonsForIdle();
    } finally {
      state.driving = false;
    }
  }

  async function startExport(format) {
    if (state.driving) return;
    statusEl.classList.remove('is-error');
    clear(noteHost);
    clear(resultHost);
    setButtonsForActive();
    statusEl.textContent = `${title}: starting…`;
    try {
      const supported = state.datasetMeta ? new Set(state.datasetMeta.filters_supported) : null;
      const raw = typeof filtersProvider === 'function' ? (filtersProvider() || {}) : {};
      const { filters, dropped } = sanitiseFilters(raw, supported);
      if (dropped.length) {
        noteHost.appendChild(h('span', {
          class: 'export-unavailable-note',
          title: `This screen's own filters do not all apply to this export — ${dropped.join(', ')} `
            + 'could not be sent, so the export covers a broader set than the screen shows.',
        }, ' (some filters not applied ℹ)'));
      }
      const created = await client.post('', { dataset, filters, format });
      state.job = created;
      renderProgress();
      driveJob(state.job.export_job_id);
    } catch (err) {
      statusEl.classList.add('is-error');
      statusEl.textContent = (err && err.message) || `${title}: the export could not be started.`;
      setButtonsForIdle();
    }
  }

  csvBtn.addEventListener('click', () => startExport('csv'));
  xlsxBtn.addEventListener('click', () => startExport('xlsx'));

  cancelBtn.addEventListener('click', async () => {
    if (!state.job) return;
    cancelBtn.disabled = true;
    try {
      await client.post(`/${encodeURIComponent(state.job.export_job_id)}/cancel`, {});
      statusEl.textContent = `${title}: cancelling — this takes effect at the next chunk boundary.`;
    } catch (err) {
      statusEl.classList.add('is-error');
      statusEl.textContent = (err && err.message) || 'The export could not be cancelled.';
    } finally {
      cancelBtn.disabled = false;
    }
  });

  retryBtn.addEventListener('click', async () => {
    if (!state.job) return;
    retryBtn.disabled = true;
    try {
      const retried = await client.post(`/${encodeURIComponent(state.job.export_job_id)}/retry`, {});
      state.job = retried;
      renderProgress();
      driveJob(state.job.export_job_id);
    } catch (err) {
      statusEl.classList.add('is-error');
      statusEl.textContent = (err && err.message) || 'The export could not be retried.';
    } finally {
      retryBtn.disabled = false;
    }
  });

  return {
    destroy() { state.destroyed = true; },
  };
}
