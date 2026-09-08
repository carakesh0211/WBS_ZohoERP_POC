/* app/frontend/src/features/analytics/analytics-kit.js
   The parts every analytics screen needs and none of them should re-invent:
   the source line, the freshness line, the metric card with its drill-down,
   the band bar, and the four blocks the shared state host cannot express —
   UNAVAILABLE, SCOPE DENIED, STALE and DATA QUALITY.

   THE FOUR STATES THAT MUST NEVER COLLAPSE
   ----------------------------------------
   Wave 2 gave every screen loading, empty, error and permission-denied.
   Reporting needs four MORE distinctions on top, and the Wave 7 contract names
   them because each one has been seen collapsed into another:

     no data       — the query ran, over a scope that reaches records, and
                     nothing matched. A clean bill of health.
     denied scope  — the query ran and the caller holds no grant that reaches
                     any of it. NOT a clean bill of health, and rendering it as
                     an empty table is the single most dangerous thing on a
                     financial control screen: it reads as "there are no
                     overruns" when it means "you cannot see the overruns".
     stale data    — figures ARE shown, and they are older than they should be.
                     A stale number rendered without its age is indistinguishable
                     from a current one.
     system failure— nothing was answered. Actionable, retryable, and never
                     dressed up as an empty result.

   Plus loading, plus success, plus UNAVAILABLE — the route is not mounted in
   this build — which is none of the seven above and which
   `features/integration/integration-screen.js` already established as a state
   in its own right.

   NOTHING HERE FABRICATES A NUMBER
   --------------------------------
   `metricCard()` renders `null` as "not reported", never as ₹0.00, and marks
   the card so a test can prove it. `freshnessLine()` renders "not reported"
   when a source sent no timestamp, never `new Date()`. `sourceLine()` refuses
   to render at all for a source it does not recognise, so a new source cannot
   reach the screen unlabelled.

   Content-Security-Policy: every node here is built with core/dom.js's h(),
   which throws on a `style` attribute and puts every string through a Text
   node. The one piece of computed geometry — the band bar's fill width — goes
   through setGeometry(), the CSSOM path CSP does not govern.
*/

import { h, text, clear, setGeometry } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { inr, bandOf, bpWidth, formatBp } from './analytics-metrics.js';
import { labelFor } from './analytics-filters.js';

/* ------------------------------------------------------------------ *
 * Source and freshness
 * ------------------------------------------------------------------ */

const SOURCE_TEXT = {
  reports: (label) => `Served by the reporting service (${label}). The filters on this screen were `
    + 'applied server-side, within your resolved access scope, and every total below was computed '
    + 'there — not in this browser.',
  /* The fallback, and its caveat has to be sharp. These figures are REAL —
     they come from `domain.compute_ledger`, the frozen C5 formula registry, so
     the arithmetic is the same arithmetic the reporting service will use. What
     they are not is FILTERED the way the filter bar implies: the ledger routes
     accept entity, plant and project and ignore the rest. The dropped
     dimensions are named individually beside this line rather than left for a
     reader to deduce. */
  /* "DID NOT ANSWER", not "is not mounted". The reporting route can be absent
     from the build OR mounted and unable to answer — /api/reports/metrics
     replies 503 DATABASE_NOT_CONFIGURED in any deployment still running on
     SQLite — and those are different facts with different remedies. The
     server's own reason is rendered beside this line when it gave one, so the
     generic sentence never has to guess which case it is in. */
  ledger: (label) => `The reporting endpoint for this screen did not answer. Shown `
    + `from this application's own ledger (${label}) instead. These are measured, server-computed `
    + 'figures from the same frozen formula registry — but the ledger routes narrow by entity, '
    + 'plant and project only, so any other filter you set was NOT applied to them.',
  exports: (label) => `Queued by the export service (${label}).`,
};

/**
 * The visible source line. Every screen renders one whenever it renders data.
 *
 * `ui-contract.md`: "No screen renders data without also rendering its source."
 * A fallback that did not name itself would let a reader take a
 * three-dimension answer for a nine-dimension one.
 */
export function sourceLine(result) {
  if (!result) return null;
  const build = SOURCE_TEXT[result.source];
  if (!build) return null;
  const compat = result.source === 'ledger';
  return h('p', {
    class: `xs muted analytics-source${compat ? ' analytics-source-compat' : ''}`,
    'data-source': result.source,
    'data-template': result.template || '',
  }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, compat ? '!' : '·'),
    text(' '),
    text(build(result.label || result.template)),
    /* The preferred source's OWN account of why it stood aside. Rendered
       verbatim and only when it gave one — this module knows that some source
       declined, and nothing about which half was missing. */
    result.declaredNote
      ? h('span', { class: 'analytics-declined' }, ` ${result.declaredNote}`)
      : null,
  ].filter(Boolean));
}

/**
 * The dimensions the answering source could not honour, named one by one.
 *
 * Returns null when nothing was dropped, so a screen on the reporting service
 * carries no noise at all.
 */
export function unappliedLine(unapplied) {
  if (!unapplied || !unapplied.length) return null;
  return h('div', { class: 'msg msg-warning analytics-unapplied', role: 'status' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
    h('div', { class: 'body' }, [
      h('strong', {}, `${unapplied.length} filter${unapplied.length === 1 ? ' was' : 's were'} not applied.`),
      h('div', {}, [
        text('The source that answered this screen cannot narrow by '),
        h('span', { class: 'mono' }, unapplied.map(labelFor).join(', ')),
        text('. The figures below are therefore WIDER than the filters above describe: they '
          + 'include rows those filters would have excluded. Do not read them as filtered totals.'),
      ]),
    ]),
  ]);
}

/**
 * The freshness line. Rendered next to the source line, always, for any screen
 * showing data that came from somewhere else.
 *
 * A source that reported no timestamp gets "not reported", explicitly. The
 * alternative — stamping the moment of the fetch — would be a number that is
 * always reassuring and never true: it would say when this browser asked, not
 * when the data was last correct.
 */
export function freshnessLine(freshness) {
  const f = freshness || {};
  if (!f.known) {
    return h('p', {
      class: 'xs muted analytics-freshness', 'data-freshness': 'unknown',
    }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'),
      text(' Freshness not reported by this source. The time shown by your browser is when this '
        + 'page asked, which is not the same as when these figures were last correct, so no '
        + 'timestamp is claimed here.'),
    ]);
  }
  const parts = [];
  if (f.sourceLabel) parts.push(`source ${f.sourceLabel}`);
  if (f.asOf) parts.push(`computed ${formatAuditTimestamp(f.asOf)}`);
  if (f.lastSyncAt) parts.push(`last synced ${formatAuditTimestamp(f.lastSyncAt)}`);

  /* THREE ANSWERS, THREE SENTENCES. `reporting.freshness()` distinguishes data
     that nothing syncs from data whose connector has never run, and both of
     them report a null `last_sync_at`. Printing only the timestamp would make
     the two identical on screen — and one of them is complete provenance while
     the other is a connector that has never delivered a row. */
  if (f.state === 'local') {
    parts.push('computed from documents raised in this application — no connector supplies any '
      + 'part of it, so there is no sync time to report and none is claimed');
  } else if (f.state === 'never_synced') {
    parts.push('a connector is configured for this data and has NEVER completed a poll, so any '
      + 'mirrored document is absent rather than out of date — this is not the same as up to date');
  }
  return h('p', {
    class: 'xs muted analytics-freshness',
    'data-freshness': f.stale ? 'stale' : 'fresh',
    'data-freshness-state': f.state || 'unknown',
  }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, f.stale ? '!' : '·'),
    text(` ${parts.join(' · ')}`),
  ]);
}

/**
 * STALE: the figures are shown AND marked. Not hidden, and not shown silently.
 *
 * Hiding stale data would leave the reader with nothing when something —
 * clearly labelled — is more useful. Showing it unmarked is the failure this
 * block exists to prevent.
 */
export function staleBlock(freshness) {
  const f = freshness || {};
  if (!f.stale) return null;
  return h('div', {
    class: 'msg msg-warning analytics-stale', role: 'status', 'data-state': 'stale',
  }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
    h('div', { class: 'body' }, [
      h('strong', {}, 'These figures are STALE.'),
      h('div', {}, [
        text('The source could not refresh them and has served what it last held'),
        f.asOf ? text(`, computed ${formatAuditTimestamp(f.asOf)}`) : null,
        text('. They are shown because a labelled old figure is more useful than a blank panel — '
          + 'but they are not current and must not be signed off as if they were.'),
      ].filter(Boolean)),
      f.staleReason ? h('div', { class: 'small' }, f.staleReason) : null,
    ].filter(Boolean)),
  ]);
}

/**
 * DENIED SCOPE: the query ran and the caller's resolved scope reaches nothing.
 *
 * Deliberately worded so it names no record and confirms no id — it is a
 * statement about the CALLER's grants, which the caller already knows, not
 * about what exists on the other side of them. That is what keeps it clear of
 * the existence-oracle rule that makes a refused read-by-id indistinguishable
 * from a miss.
 */
export function scopeDeniedBlock(err, { what }) {
  return h('div', {
    class: 'msg msg-info analytics-scope-denied', role: 'status', 'data-state': 'denied',
  }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
    h('div', { class: 'body' }, [
      h('strong', {}, `${what} is empty because your access scope reaches none of it.`),
      h('div', {}, 'This is NOT the same as "nothing matched". No figure is shown here, and no '
        + 'figure of zero should be inferred: there may well be records, and this account is '
        + 'not entitled to them. Ask your administrator for the scope grant you need.'),
      err && err.detail ? h('div', { class: 'small mono' }, err.detail) : null,
    ].filter(Boolean)),
  ]);
}

/**
 * UNAVAILABLE: the route this screen reads is not mounted in this build.
 *
 * Not the empty state — "nothing matched" would be false when nothing was
 * asked. Not the error state — nothing failed, and a Retry button would invite
 * an operator to retry something that cannot succeed until another agent's
 * work lands. It names the route and offers no retry.
 */
export function unavailableBlock(err, { what }) {
  /* TWO WAYS TO BE UNAVAILABLE, AND ONLY ONE OF THEM IS "NOT MOUNTED".
     A route can be absent from the build, OR mounted and lacking the
     capability the screen needs — the ageing screens reach
     /api/reports/dimensions successfully and find that this build defines no
     age band to group by. Telling an operator that a mounted, answering route
     "is not mounted" sends them looking for a deployment problem that does not
     exist, so a caller that knows better supplies `err.reason` and this block
     prints that instead of guessing. */
  const reason = err && err.reason;
  return h('div', {
    class: 'msg msg-info analytics-unavailable',
    role: 'status',
    'data-state': 'unavailable',
    'data-unavailable': reason ? 'capability' : 'route',
  }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
    h('div', { class: 'body' }, [
      h('strong', {}, `${what} is not available in this build.`),
      h('div', {}, reason
        ? [
          text('This screen reads '),
          h('code', { class: 'mono' }, err && err.path ? err.path : 'an endpoint'),
          text(`, which answers — and ${reason} No figure is shown, because there is nothing to `
            + 'compute one from: not because the result was empty, and not because it was zero.'),
        ]
        : [
          text('This screen reads '),
          h('code', { class: 'mono' }, err && err.path ? err.path : 'an endpoint'),
          text(', which this deployment does not mount. No figure is shown, because there is '
            + 'nothing to compute one from — not because the result was empty, and not because '
            + 'it was zero.'),
        ]),
      err && err.note ? h('div', { class: 'small' }, err.note) : null,
    ].filter(Boolean)),
  ]);
}

/**
 * UNAVAILABLE, BY FILTER: the route answered and refused one of the filters.
 *
 * Distinct from `unavailableBlock` because the cause is different and so is
 * the remedy. There the route is missing and nothing the reader does will
 * help; here the route is mounted, working, and has said — with a code and a
 * reason — that this build cannot express one of the dimensions the reader
 * asked for. Clearing that one filter makes the screen work, so the block
 * names the field rather than the route.
 *
 * NO FIGURE IS SHOWN. `reporting.UNSUPPORTED_FILTERS` is explicit that a
 * dropped filter is worse than a refused one — "the reader gets a total that
 * looks right, computed over a population they did not ask for, with nothing
 * on screen saying so" — so the refusal is carried through to the screen
 * rather than being retried without the offending field.
 */
export function filterUnavailableBlock(err, { what }) {
  const fields = (err && err.fields && err.fields.length)
    ? err.fields
    : [{ field: '', code: (err && err.code) || '', detail: (err && err.detail) || '' }];
  return h('div', {
    class: 'msg msg-warning analytics-filter-unavailable',
    role: 'alert',
    'data-state': 'unavailable',
    'data-unavailable': 'filter',
  }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
    h('div', { class: 'body' }, [
      h('strong', {}, `${what} was not computed, because this build cannot apply one of your `
        + 'filters.'),
      h('div', {}, 'The filter is well formed — there is nothing to correct above. This build '
        + 'has no column to bind it to, and a total computed with it silently dropped would look '
        + 'right while describing a population you did not ask for. Clear the filter named below '
        + 'to see a figure.'),
      ...fields.map((f) => h('div', { class: 'small' }, [
        f.field ? h('span', { class: 'mono' }, labelFor(f.field)) : null,
        f.field ? text(' — ') : null,
        f.code ? h('span', { class: 'mono' }, f.code) : null,
        f.code ? text(': ') : null,
        text(f.detail || ''),
      ].filter(Boolean))),
    ]),
  ]);
}

/**
 * DATA QUALITY: the card's figure and the rows behind it do not agree.
 *
 * The Wave 7 contract requires the round trip to be asserted. This is what the
 * assertion looks like when it fails, and it is deliberately loud: a card that
 * quietly redrew itself as the sum of whatever rows arrived would erase the
 * evidence of the defect and leave a plausible number in its place.
 *
 * `checked: false` — the comparison could not run — is rendered as its own
 * outcome and never as a pass.
 *
 * @param {Object} result - from analytics-metrics.js::assertSumsBack.
 */
export function reconciliationBlock(result) {
  if (!result) return null;
  if (result.ok) {
    return h('p', {
      class: 'xs muted analytics-reconciled', 'data-reconciled': 'true',
    }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '✔'),
      text(` ${result.message}`),
    ]);
  }
  const kind = result.checked ? 'error' : 'warning';
  return h('div', {
    class: `msg msg-${kind} analytics-data-quality`,
    role: result.checked ? 'alert' : 'status',
    'data-state': 'data-quality',
    'data-reconciled': result.checked ? 'false' : 'unchecked',
  }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, result.checked ? '✖' : '?'),
    h('div', { class: 'body' }, [
      h('strong', {}, result.checked
        ? 'This figure does not equal the rows behind it.'
        : 'This figure could not be checked against the rows behind it.'),
      h('div', {}, result.message),
      result.label ? h('div', { class: 'small mono' }, result.label) : null,
    ].filter(Boolean)),
  ]);
}

/* ------------------------------------------------------------------ *
 * Metric cards, and the drill-down out of every one
 * ------------------------------------------------------------------ */

/**
 * A metric card.
 *
 * EVERY card is a link to its own source records. The Wave 7 contract requires
 * a drill-down "from EVERY metric", so the drill-down is not a property a
 * caller may forget to pass: a card built without an `href` renders its figure
 * inside a `<span>` carrying an explicit note that no drill-down exists for it,
 * which is visible in review and asserted by the suite. There is no silent
 * dead card.
 *
 * A `null` figure renders as "not reported" and NEVER as ₹0.00.
 *
 * @param {Object} config
 * @param {string} config.label
 * @param {number|null} config.paise - integer paise, or null for not reported.
 * @param {string} [config.sub] - the definition of the metric, in words.
 * @param {string} [config.href] - the drill-down.
 * @param {string} [config.accent] - safe | watch | breach | info.
 * @param {string} [config.metric] - the metric key, for tests and for the URL.
 * @param {Node} [config.figure] - an override node (a count, a chip) instead
 *   of a money figure.
 */
export function metricCard({
  label, paise: value, sub, href, accent = 'info', metric, figure = null,
}) {
  const reported = figure !== null || (value !== null && value !== undefined);
  const body = figure !== null
    ? figure
    : (reported
      ? text(inr(value))
      : h('span', {
        class: 'analytics-not-reported',
        title: 'The source that answered this screen did not report this figure. It is not zero: '
          + 'zero would be a measurement, and no measurement was returned.',
      }, [
        h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'),
        text(' not reported'),
      ]));

  const valueEl = href
    ? h('a', {
      class: 'linkish analytics-drill',
      href,
      'data-metric': metric || label,
      title: `Open the source records behind ${label}. The drill-down carries the same filters as `
        + 'this card, plus this dimension, so its rows sum back to the figure shown here.',
    }, body)
    : h('span', { class: 'analytics-no-drill', 'data-metric': metric || label }, [
      body,
      h('span', { class: 'sr-only' },
        ' — no drill-down is available for this metric from the source that answered.'),
    ]);

  return h('div', {
    class: `tile accent-${accent} analytics-tile`,
    'data-metric': metric || label,
    'data-reported': String(reported),
  }, [
    h('div', { class: 'k' }, label),
    h('div', { class: 'v' }, valueEl),
    sub ? h('div', { class: 'sub' }, sub) : null,
  ].filter(Boolean));
}

/** A card whose figure is a plain count rather than money. */
export function countCard({ label, count, sub, href, accent = 'info', metric }) {
  const reported = count !== null && count !== undefined;
  return metricCard({
    label,
    paise: null,
    sub,
    href,
    accent,
    metric,
    figure: reported
      ? text(String(count))
      : h('span', { class: 'analytics-not-reported' }, [
        h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'),
        text(' not reported'),
      ]),
  });
}

/**
 * The budget utilisation band bar, geometry set through the CSSOM.
 *
 * Widths come from integer basis points; nothing here divides in floating
 * point. `title` carries the numbers in words, and the band is ALSO named in
 * text beside the bar, because a bar that carries its meaning in colour alone
 * fails both the contract and greyscale.
 */
export function bandBar({ actualBp, commitmentBp, label }) {
  const band = bandOf(commitmentBp === null ? actualBp : commitmentBp);
  const bar = h('span', {
    class: `bar${band === 'breach' ? ' breach' : ''} analytics-bar`,
    role: 'img',
    'aria-label': label || `Utilisation ${formatBp(commitmentBp === null ? actualBp : commitmentBp)}`,
    title: label || '',
  });
  const commit = h('i', { class: 'commit' });
  const actual = h('i', { class: 'actual' });
  setGeometry(commit, { width: bpWidth(commitmentBp) });
  setGeometry(actual, { width: bpWidth(actualBp) });
  bar.appendChild(commit);
  bar.appendChild(actual);
  return bar;
}

/** The band as a non-colour status chip, for the cell beside the bar. */
export function bandChip(bp) {
  const band = bandOf(bp);
  const spec = {
    safe: { tone: 'positive', label: 'WITHIN BUDGET' },
    watch: { tone: 'warning', label: 'APPROACHING LIMIT' },
    breach: { tone: 'negative', label: 'OVER BUDGET' },
    unknown: { tone: 'neutral', label: 'NO BUDGET SET' },
  }[band];
  return statusChip({
    label: `${spec.label} ${formatBp(bp)}`,
    tone: spec.tone,
    title: band === 'unknown'
      ? 'No budget is set against this row, so it has no utilisation. That is not 0% and it is '
        + 'not 100%.'
      : `Utilisation is ${formatBp(bp)} of the approved budget.`,
  });
}

/* ------------------------------------------------------------------ *
 * Small shared layout — matching the approved structure
 * ------------------------------------------------------------------ */

/**
 * A titled card with an <h2> heading.
 *
 * The shell renders the screen name as the page <h1> and the frozen
 * stylesheet's card header is `.card > h3`, so reusing it directly would skip a
 * heading level: axe flags it and a screen-reader user loses the outline.
 * `.analytics-card-title` reproduces `.card > h3` from the same tokens, so the
 * card is visually identical and only the element name differs. budget.css,
 * approvals.css and integration.css all solve it the same way.
 */
export function card(headingId, headingText, children, { actions = null } = {}) {
  return h('section', { class: 'card analytics-card', 'aria-labelledby': headingId }, [
    h('h2', { id: headingId, class: 'analytics-card-title' }, [
      headingText,
      actions ? h('span', { class: 'spacer' }) : null,
      actions,
    ].filter(Boolean)),
    h('div', { class: 'card-body' }, children),
  ]);
}

/** A definition list using the frozen `.kv` grid, as a real <dl>. */
export function keyValues(pairs) {
  const dl = h('dl', { class: 'kv analytics-kv' });
  for (const [label, value] of pairs) {
    if (value === null || value === undefined) continue;
    dl.appendChild(h('dt', {}, label));
    dl.appendChild(h('dd', {}, value));
  }
  return dl;
}

/** An announcer bound to a live region the HOST created. Never creates one. */
export function createAnnouncer(regionId) {
  const region = document.getElementById(regionId);
  return function announce(message) {
    if (region) region.textContent = String(message ?? '');
  };
}

/** Replace a host's children with one node. */
export function replace(host, node) {
  clear(host);
  if (node) host.appendChild(node);
}

/**
 * The export control.
 *
 * Disabled, with the reason ON the control, when no export route is mounted.
 * A button that looks live and does nothing is worse than one that says why it
 * cannot: the first teaches an operator that the application is unreliable,
 * the second tells them what is missing.
 */
export function exportButton({ availability, onExport, report }) {
  /* The DATASET, not a report id. An export is a request for a named dataset
     under the caller's own scope — `budget_ledger_cells`, `wbs_elements`,
     `purchase_order_lines` — and naming it is what lets an operator check the
     export catalogue at /api/exports/datasets for the columns they will get. */
  const dataset = (report && report.dataset) || 'this dataset';
  if (availability === true) {
    return h('button', {
      type: 'button', class: 'btn-sm analytics-export', 'data-export': 'available',
      onClick: () => onExport(),
    }, 'Export these rows');
  }
  const unknown = availability === null;
  return h('button', {
    type: 'button',
    class: 'btn-sm analytics-export',
    disabled: true,
    'data-export': unknown ? 'unknown' : 'unavailable',
    title: unknown
      ? 'This build\'s API schema could not be read, so whether an export endpoint exists is '
        + 'unknown. The control is disabled rather than offered on a guess.'
      : `No export endpoint is mounted in this build, so ${dataset} cannot be queued. `
        + 'The control is disabled rather than failing when pressed.',
  }, 'Export unavailable');
}
