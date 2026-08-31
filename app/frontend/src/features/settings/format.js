/* app/frontend/src/features/settings/format.js
   Settings-feature formatting helpers. Re-exports the shared, lead-owned
   audit-timestamp formatter (core/format.js) under a name that fits this
   feature, and adds the plain "dd-MMM-yyyy" convention
   (research/30_contracts/C6_tokens.json number_and_date_format.date) for
   fields that are not audit timestamps and so carry no timezone label.
*/

import { formatAuditTimestamp } from '../../core/format.js';

/**
 * Settings/masters records carry created_at/updated_at/external_last_modified
 * as ISO-8601 timestamps. These are administrative record timestamps, not
 * the audit trail itself, but the same "explicit label, no ambiguity"
 * reasoning applies once more than one reviewer may be in a different
 * timezone, so this feature reuses the identical UTC-labelled rendering
 * rather than inventing a second convention.
 */
export const formatSettingsTimestamp = formatAuditTimestamp;

/** dd-MMM-yyyy only, per C6_tokens.json's general UI date convention. */
export function formatSettingsDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const mmm = MONTHS[d.getUTCMonth()];
  const yyyy = d.getUTCFullYear();
  return `${dd}-${mmm}-${yyyy}`;
}
