/* app/frontend/src/features/analytics/exception-monitor.js
   SCR-25 — Exception and Overrun Monitor.

   Named verbatim in research/30_contracts/C8_screens.json as "Exception and
   Overrun Monitor", so this screen carries its real number.

   THIS IS THE SCREEN THE FOUR-STATE RULE WAS WRITTEN FOR
   ------------------------------------------------------
   Every other screen in this feature would be merely wrong if it collapsed
   "you hold no grant" into "nothing matched". This one would be actively
   dangerous, because an empty exception monitor is read as GOOD NEWS. A
   controller who opens it, sees nothing, and closes it has concluded that
   there are no overruns — and if the reason it was empty is that their scope
   reaches none of the entities with overruns, they have concluded the opposite
   of the truth from a screen that told them nothing was wrong.

   So the four states are kept apart here with more care than anywhere else,
   and each one has its own words:

     NO DATA       every exception list was reported and every one was empty.
                   This is a measured "none" and the screen says "measured".
     DENIED SCOPE  the server declared the caller's resolved scope empty. The
                   screen says so and explicitly refuses to imply zero.
     STALE         the lists are shown and marked as older than they should be.
     FAILURE       nothing was answered; retry is offered.
     UNAVAILABLE   the route is not mounted; nothing is shown and it is named.

   `analytics-screen.js` stamps whichever one applies onto the loader as
   `data-analytics-state`, and the VRT suite asserts they are distinguishable
   rather than trusting the prose.

   FIVE LISTS, NEVER ONE COUNT
   ---------------------------
   A budget exception, an over-billed line, an unbilled receipt, a pending
   revision and a reconciliation exception are five different problems with
   five different owners and five different remedies. A single "12 issues"
   badge is a number nobody can act on and, worse, a number that hides which
   of the five it is made of. Each keeps its own count, its own definition and
   its own drill-down.

   THE OVERRUNS ARE COUNTED FROM THE ROWS, AND SAY SO
   ---------------------------------------------------
   "Projects over budget" is derived on this screen, from the project rows the
   server returned, by comparing exposure against budget in integer basis
   points. That is a departure from the rule that nothing is computed here, and
   it is labelled at the point of use: the card says it is a count of the rows
   returned rather than a portfolio statistic. Counting rows a server sent is
   not the same as inventing a total, but it is close enough to it that saying
   which one this is matters.
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  bandBar, bandChip, card, countCard, createAnnouncer, exportButton, metricCard,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, readFilters, screenHref, writeFilters,
} from './analytics-filters.js';
import { inr } from './analytics-metrics.js';
import { METRIC_LABEL, projectRow, readAlerts, readRows, readTotals } from './analytics-shapes.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import {
  exportAvailable, getExceptions, queueExport, REPORT_IDS,
} from './analytics-api.js';

const FILTER_FIELDS = [
  'entity_ids', 'plant_ids', 'location_ids', 'project_ids', 'budget_head_ids',
  'category_ids', 'vendor_ids', 'document_types', 'lifecycle_statuses',
  'approval_statuses', 'date_from', 'date_to', 'period_ids',
];

/** The five exception classes, each with what it MEANS and who owns it. */
const CLASSES = [
  {
    key: 'budget_exceptions',
    label: 'Budget exceptions',
    tone: 'negative',
    meaning: 'A purchase request that exceeds the available budget of the element it was raised '
      + 'against. It is held, not absorbed.',
    describe: (r) => `${r.pr_number || r.pr_id || 'A purchase request'} — `
      + `${r.exception_reason || 'exceeds available budget'}`,
    href: () => '#analytics-controller',
  },
  {
    key: 'over_billed_lines',
    label: 'Bills exceeding their order',
    tone: 'negative',
    meaning: 'A vendor bill whose value exceeds the ordered value of the line it was posted '
      + 'against. The excess is not a commitment and is not absorbed silently.',
    describe: (r) => `${r.po_number || '—'} (${r.wbs_code || '—'}): billed `
      + `${inr(r.billed_paise)} against ${inr(r.ordered_paise)} ordered`,
    href: () => '#integration-reconciliation',
  },
  {
    key: 'received_not_billed_lines',
    label: 'Received, not billed',
    tone: 'warning',
    meaning: 'Value received against an order and not yet invoiced. It is exposure today and '
      + 'converts to actual CWIP when the vendor bills.',
    describe: (r) => `${r.wbs_code || '—'} on ${r.po_number || '—'}: `
      + `${inr(r.received_not_billed_paise)} awaiting a vendor bill`,
    href: () => '#analytics-commitment-ageing',
  },
  {
    key: 'pending_revisions',
    label: 'Revisions awaiting approval',
    tone: 'progress',
    meaning: 'A budget revision or transfer that has been submitted and not yet decided. The '
      + 'budget it would change is still the pre-revision budget.',
    describe: (r) => `${r.revision_id || '—'} (${r.kind || 'revision'}) — `
      + `${r.reason || 'no reason recorded'}`,
    href: () => '#approval-inbox',
  },
  {
    key: 'reconciliation_exceptions',
    label: 'Reconciliation exceptions',
    tone: 'warning',
    meaning: 'A discrepancy between this application\'s ledger and its source that could not be '
      + 'resolved automatically. Triage is an operational act and lives in the exception queue.',
    describe: (r) => `${r.exception_id || '—'} (${r.kind || 'discrepancy'}) — `
      + `${r.detail || 'no detail recorded'}`,
    href: () => '#integration-exceptions',
  },
];

export function mountExceptionMonitor(root) {
  requireRoot(root, 'SCR-25 Exception and Overrun Monitor');
  const announce = createAnnouncer('analyticsLiveRegion');

  let filters = readFilters();

  const chips = h('div', { class: 'analytics-chip-host' });
  const tiles = h('div', { id: 'excTiles' });
  const lists = h('div', { class: 'analytics-exception-lists' });
  const exportHost = h('span', { class: 'analytics-export-host' });

  const overruns = createDataTable({
    caption: 'Projects whose exposure exceeds their approved budget, with the size of the overrun',
    emptyMessage: 'No project in these rows is over budget.',
    columns: [
      {
        key: 'capex_code',
        label: 'CAPEX code',
        render: (r) => h('a', {
          class: 'linkish mono',
          href: drilldownHref('analytics-project-object', filters, {
            dimension: 'project', narrowKey: 'project_ids', narrowValue: r.project_id,
          }),
        }, String(r.capex_code || r.project_id || '—')),
      },
      { key: 'name', label: 'Project', render: (r) => text(r.name || '—') },
      { key: 'plant', label: 'Plant', render: (r) => text(r.plant || '—') },
      {
        key: 'budget',
        label: METRIC_LABEL.budget,
        numeric: true,
        render: (r) => text(inr(r.budget)),
      },
      {
        key: 'exposure',
        label: METRIC_LABEL.exposure,
        numeric: true,
        render: (r) => text(inr(r.exposure)),
      },
      {
        key: 'available',
        label: 'Overrun',
        numeric: true,
        // `available` is budget − exposure and is NEGATIVE on an overrun. It is
        // rendered as the server sent it, and formatINR puts a negative in
        // parentheses, so the sign is never carried by colour alone.
        render: (r) => h('span', {
          title: 'Budget less exposure, as the server computed it. A negative figure is the size '
            + 'of the overrun and is shown in parentheses — the sign is never carried by colour '
            + 'alone.',
        }, inr(r.available)),
      },
      {
        key: 'band',
        label: 'Utilisation',
        render: (r) => h('div', { class: 'analytics-band-cell' }, [
          bandChip(r.utilisation_bp),
          bandBar({
            actualBp: r.actual_bp,
            commitmentBp: r.utilisation_bp,
            label: `Exposure against budget for ${r.capex_code || 'this project'}.`,
          }),
        ]),
      },
    ],
  });

  const filterBar = createFilterBar({
    idPrefix: 'exc',
    fields: FILTER_FIELDS,
    value: filters,
    onApply: (next) => apply(next),
  });

  const loader = createLoader({
    id: 'excStatus',
    glyph: '⚠',
    what: 'The exception and overrun monitor',
    /* THE EMPTY MESSAGE IS A MEASUREMENT, AND IT SAYS SO. It is reached only
       from a SUCCESSFUL response with no rows — never from a refusal, which
       `analytics-screen.js` routes to the denied or permission state instead. */
    emptyMessage: 'Every exception list was reported for these filters and every one is empty. '
      + 'This is a measured "none" — the lists were read and nothing was in them.',
    loadingMessage: 'Loading exceptions and overruns…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(card('excTitle', 'Exception and Overrun Monitor', [
    h('p', { class: 'muted small' },
      'An empty exception monitor reads as good news, which is why this screen is careful about '
      + 'why it is empty. "Nothing matched" and "your access scope reaches none of this" are '
      + 'rendered differently and never collapsed — the second is not a clean bill of health and '
      + 'must not look like one.'),
    filterBar.el,
    chips,
    loader.el,
    tiles,
    lists,
    h('h3', { class: 'analytics-subhead' }, 'Projects over budget'),
    h('p', { class: 'xs muted' },
      'Exposure against approved budget, from the rows the server returned. Exposure is open '
      + 'commitment plus actual CWIP plus PR reservations — shown as its own column so nobody has '
      + 'to add the right two.'),
    overruns.el,
    h('nav', { class: 'analytics-crosslinks', 'aria-label': 'Operational views' }, [
      h('span', { class: 'muted' }, 'Act on these in:'),
      h('a', { class: 'linkish', href: '#integration-exceptions' },
        'the reconciliation exception queue'),
      h('a', { class: 'linkish', href: '#integration-retry' }, 'the failed sync and retry queue'),
      h('a', { class: 'linkish', href: '#integration-reconciliation' },
        'commitment-to-actual reconciliation'),
      h('a', { class: 'linkish', href: '#approval-inbox' }, 'your approval inbox'),
    ]),
  ], { actions: exportHost }));

  function apply(next) {
    filters = next;
    const query = writeFilters(filters);
    try {
      window.history.replaceState(null, '', `/${query ? `?${query}` : ''}${window.location.hash}`);
    } catch { /* non-navigable context */ }
    load();
  }

  function renderChips(unapplied) {
    while (chips.firstChild) chips.removeChild(chips.firstChild);
    chips.appendChild(filterChips(filters, unapplied));
  }

  function renderCards(alerts, totals, breached, rowCount) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    const band = h('div', { class: 'tiles analytics-tiles' });

    for (const spec of CLASSES) {
      const rows = alerts[spec.key];
      band.appendChild(countCard({
        label: spec.label,
        // Not reported is distinct from zero, on every one of the five.
        count: alerts.present ? rows.length : null,
        sub: alerts.present ? spec.meaning
          : 'This source did not report this list, so no count is shown. That is not zero.',
        accent: rows.length ? (spec.tone === 'negative' ? 'breach' : 'watch') : 'safe',
        metric: spec.key,
        href: spec.href(),
      }));
    }

    band.appendChild(countCard({
      label: 'Projects over budget',
      count: breached.length,
      sub: `counted from the ${rowCount} project row(s) this source returned — a count of what is `
        + 'on screen, not a portfolio-wide statistic',
      accent: breached.length ? 'breach' : 'safe',
      metric: 'overrun_count',
    }));

    band.appendChild(metricCard({
      label: 'Available across these rows',
      paise: totals ? (totals.available ?? totals.available_paise ?? null) : null,
      sub: 'The server\'s own figure. A negative total here means the portfolio in this filter is '
        + 'collectively over budget.',
      accent: 'info',
      metric: 'available',
      href: screenHref('analytics-controller', filters),
    }));

    tiles.appendChild(band);
  }

  function renderLists(alerts) {
    while (lists.firstChild) lists.removeChild(lists.firstChild);
    if (!alerts.present) {
      lists.appendChild(h('div', { class: 'msg msg-warning', role: 'status' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
        h('div', { class: 'body' }, [
          h('strong', {}, 'This source reported no exception lists at all.'),
          h('div', {}, 'No count is shown for any of the five classes above, and none should be '
            + 'inferred. The absence of a list is not an empty list.'),
        ]),
      ]));
      return;
    }
    for (const spec of CLASSES) {
      const rows = alerts[spec.key];
      const section = h('section', { class: 'analytics-exception-class', 'data-class': spec.key }, [
        h('h3', { class: 'analytics-subhead' }, [
          text(spec.label),
          text(' '),
          statusChip({
            label: rows.length ? `${rows.length} OPEN` : 'NONE',
            tone: rows.length ? spec.tone : 'positive',
            title: rows.length
              ? `${rows.length} open item(s) in this class.`
              : 'This list was read for these filters and is empty.',
          }),
        ]),
        h('p', { class: 'xs muted' }, spec.meaning),
      ]);
      if (rows.length) {
        section.appendChild(h('ul', { class: 'analytics-exception-items' },
          rows.slice(0, 10).map((r) => h('li', {}, text(spec.describe(r))))));
        if (rows.length > 10) {
          section.appendChild(h('p', { class: 'xs muted' }, [
            text(`and ${rows.length - 10} more — `),
            h('a', { class: 'linkish', href: spec.href() }, 'open the queue for this class'),
          ]));
        }
      }
      lists.appendChild(section);
    }
  }

  async function wireExport() {
    const availability = await exportAvailable();
    while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
    exportHost.appendChild(exportButton({
      availability,
      reportId: REPORT_IDS.exceptions,
      onExport: async () => {
        const result = await queueExport(REPORT_IDS.exceptions, filters);
        announce(result.queued ? 'The export has been queued.'
          : 'This build mounts no export endpoint, so nothing was queued.');
      },
    }));
  }

  let loading = false;

  async function load() {
    if (loading) return;
    loading = true;
    overruns.el.hidden = false;
    overruns.renderSkeleton();
    renderChips([]);

    await loader.run(() => getExceptions(filters), {
      render: (data, result) => {
        renderChips(result.unapplied);
        const alerts = readAlerts(data);
        const { rows: raw } = readRows(data, ['projects']);
        const rows = raw.map(projectRow);
        const totals = readTotals(data);
        const breached = rows.filter((r) => r.utilisation_bp !== null && r.utilisation_bp > 10000);

        renderCards(alerts, totals, breached, rows.length);
        renderLists(alerts);

        if (breached.length) {
          overruns.el.hidden = false;
          overruns.renderRows(breached);
        } else {
          overruns.renderRows([]);
        }

        const openCount = alerts.present
          ? CLASSES.reduce((n, spec) => n + alerts[spec.key].length, 0) : 0;
        announce(`${openCount} open exception(s) across five classes, and ${breached.length} `
          + 'project(s) over budget.');

        /* EMPTY only when every list was reported AND every one was empty AND
           no project is over budget. A source that reported no lists at all is
           NOT empty — it is a source that answered a narrower question, and
           returning true here keeps the screen in `ready` so the warning above
           is what the reader sees rather than "nothing matched". */
        if (!alerts.present) return true;
        return openCount > 0 || breached.length > 0;
      },
      onState: (state) => {
        if (state !== 'ready' && state !== 'stale') {
          overruns.renderRows([]);
          overruns.el.hidden = true;
          while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
          while (lists.firstChild) lists.removeChild(lists.firstChild);
        }
      },
    });

    loading = false;
  }

  wireExport();
  load();
}
