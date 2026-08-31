/* app/frontend/src/features/settings/collections.js
   Structural metadata for the organisation-hierarchy admin screens — NOT
   data. Every value here is a label or an id-field NAME, never a row; the
   actual rows always come from GET /api/settings/{collection} (see api.js).

   docs/WAVE2_CONTRACTS.md fixes the collection set and the id-field
   convention ("<id field>" per item, matching the item_id/vendor_id pattern
   already used for masters) but does not enumerate area-specific fields
   beyond code/name/is_active/audit columns/version_no. This screen is
   therefore deliberately generic: the fixed columns below are the ones the
   contract guarantees for every collection, and org-hierarchy.js renders any
   further keys a row actually carries without assuming their names — see
   the "extra attributes" handling there.

   Location is intentionally listed as its own collection, not folded into
   plant or zone — REQ-RPT-010 in WAVE2_CONTRACTS.md's traceability table
   treats location as a first-class dimension, and this admin screen mirrors
   that: selecting "Locations" filters and edits nothing but location rows.
*/

export const COLLECTIONS = [
  { key: 'organisations', label: 'Organisations', singular: 'organisation', idField: 'organisation_id' },
  { key: 'entities', label: 'Entities', singular: 'entity', idField: 'entity_id' },
  { key: 'divisions', label: 'Divisions', singular: 'division', idField: 'division_id' },
  { key: 'branches', label: 'Branches', singular: 'branch', idField: 'branch_id' },
  { key: 'zones', label: 'Zones', singular: 'zone', idField: 'zone_id' },
  { key: 'plants', label: 'Plants', singular: 'plant', idField: 'plant_id' },
  { key: 'locations', label: 'Locations', singular: 'location', idField: 'location_id' },
  { key: 'departments', label: 'Departments', singular: 'department', idField: 'department_id' },
];

export function collectionByKey(key) {
  return COLLECTIONS.find((c) => c.key === key) || null;
}

/**
 * Fields guaranteed by the frozen contract for every /api/settings/{collection}
 * row, in addition to the collection's own id field. Anything else present on
 * a row is area-specific and rendered generically (see org-hierarchy.js).
 */
export const AUDIT_FIELD_KEYS = ['created_at', 'created_by', 'updated_at', 'updated_by', 'version_no'];
export const CORE_FIELD_KEYS = ['code', 'name', 'is_active'];
