-- 017_reporting.sql
-- Saved views for the reporting layer: a stored `FilterSet`, a per-user
-- default, and the sharing rule that governs who may load someone else's.
--
-- WHAT THIS DOES NOT DO, AND WHY THAT MATTERS MOST
--
-- It creates NO materialised aggregate, NO summary table and NO cached total.
-- Every figure `app/backend/pg/reporting.py` serves is computed at read time
-- from the document tables 013/014/015 created, with the arithmetic
-- transcribed from `domain.compute_ledger` -- the frozen `C5_formulas.json`
-- registry in code. A stored total is a total that can be stale, and a stale
-- total is indistinguishable on screen from a current one. `docs/WAVE7_CONTRACT.md`
-- requires zero data, denied scope, stale data and service failure to stay
-- four distinct states; a summary table quietly collapses two of them.
--
-- So the only thing worth persisting is the QUESTION, never the answer. That
-- is what a saved view is here: a `FilterSet` and a grouping, stored as jsonb,
-- re-executed under the CALLER'S OWN SCOPE every time it is opened.
--
--
-- THE SHARING RULE, WHICH IS NOT A DATA-ACCESS RULE
--
-- A shared view is readable by anyone whose scope reaches its entity. That is
-- safe for a reason worth writing down rather than assuming: a saved view
-- carries no data. Opening one runs the same scoped query the opener would
-- have built by hand, so a Kolhapur-plant controller who opens a view saved by
-- a Pune-plant controller sees KOLHAPUR rows -- the same view, honestly
-- narrower, never an error and never the author's numbers.
--
-- What a saved view CAN leak is the filter itself: a project id or a vendor id
-- the reader has no grant for, sitting in `definition`. That is why these
-- tables carry `entity_id` and a real RLS policy rather than being treated as
-- user preferences. `reporting.load_view` additionally strips any filter value
-- outside the opener's resolved scope before executing -- narrowing, never
-- widening, per the contract's first non-negotiable.
--
--
-- ONE DEFAULT PER (USER, REPORT), AS A TABLE AND NOT A FLAG
--
-- The obvious shape is `report_saved_view.is_default boolean`, and it is
-- wrong in a way a partial unique index cannot fix. "Default" is a property of
-- the READER, not of the view: two users may each want a different default,
-- and both may want a SHARED view -- one row, authored by someone else -- as
-- that default. A flag on the view row can express at most one of those
-- choices, and whichever user sets it last silently retracts the other's.
--
-- `report_view_default` therefore keys on (user_id, report_key) and points at
-- a view. The primary key IS the "one default per report per user" rule, so it
-- cannot be violated by a service forgetting to check.
--
--
-- WHY `report_key` IS TEXT AND NOT A FOREIGN KEY
--
-- The reports are screens (C8: SCR-01, SCR-02, SCR-23, SCR-24, SCR-25 and the
-- rest), and there is no table of screens in this schema. Inventing one here
-- would make the migration the registry of the UI, which is a contract this
-- file is not entitled to freeze. `ck_report_saved_view_key_shape` constrains
-- it to a non-blank lower-snake token so it cannot become free text, and
-- `reporting.REPORT_KEYS` is the application-side allow-list. A key outside
-- that list is refused by the service, not by the database -- the same
-- posture `procurement_policy` takes toward a new policy key.

BEGIN;

-- ======================================================== report_saved_view
CREATE TABLE report_saved_view (
    view_id        text PRIMARY KEY,

    -- The scope dimension this row carries. NOT NULL deliberately: a view with
    -- no entity would be invisible to `capex_scope_permits` filtering on
    -- `entity_id`... or rather, WORSE than invisible -- `capex_dimension_permits`
    -- returns TRUE for a NULL row value, because it is built to waive a
    -- dimension the TABLE lacks. A nullable column is a different thing, and
    -- migration 012's whole subject is what that difference costs: every
    -- principal could read every unattributed row. Not repeated here.
    entity_id      text NOT NULL REFERENCES entity (entity_id),

    -- The screen this view belongs to. See the header for why it is not an FK.
    report_key     text NOT NULL,
    name           text NOT NULL,
    description    text,

    -- The author. Compared against `capex.user_id` by the policy below for a
    -- PRIVATE view, and it is the only thing that makes "private" true.
    owner_user_id  text NOT NULL REFERENCES app_user (user_id),

    visibility     text NOT NULL DEFAULT 'PRIVATE',

    -- The stored `FilterSet`, exactly as `reporting.FilterSet.to_json` renders
    -- it. jsonb and not text: the shape is validated on the way in by
    -- `FilterSet.from_json`, which REFUSES an unknown key rather than ignoring
    -- it -- a filter nobody applies is a filter the reader believes is applied.
    definition     jsonb NOT NULL,

    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,

    CONSTRAINT ck_report_saved_view_visibility CHECK (
        visibility IN ('PRIVATE', 'SHARED')
    ),
    -- A blank name is a view nobody can pick out of a list, and a name that is
    -- only whitespace renders as an empty row rather than as an error.
    CONSTRAINT ck_report_saved_view_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_report_saved_view_key_shape CHECK (
        report_key ~ '^[a-z][a-z0-9_]{2,63}$'
    ),
    -- The stored definition is an OBJECT. A bare array or scalar would parse
    -- and then fail deep inside `FilterSet.from_json`, at read time, on
    -- somebody's dashboard.
    CONSTRAINT ck_report_saved_view_definition_object CHECK (
        jsonb_typeof(definition) = 'object'
    )
);

-- One name per (owner, report). Two views called "My projects" on one screen
-- is a support call, not a feature. Scoped to the OWNER and not to the entity:
-- two people may legitimately each have a "My projects", and forbidding that
-- would make the first author's choice of a common word a land grab.
CREATE UNIQUE INDEX ux_report_saved_view_owner_name
    ON report_saved_view (owner_user_id, report_key, lower(btrim(name)));

CREATE INDEX ix_report_saved_view_owner
    ON report_saved_view (owner_user_id, report_key);

-- The listing query's index: shared views for one entity and one screen.
CREATE INDEX ix_report_saved_view_shared
    ON report_saved_view (entity_id, report_key)
    WHERE visibility = 'SHARED';


-- ====================================================== report_view_default
-- One default per (user, report). The primary key IS the rule -- see header.
CREATE TABLE report_view_default (
    user_id     text NOT NULL REFERENCES app_user (user_id),
    report_key  text NOT NULL,

    -- ON DELETE CASCADE, deliberately. A default pointing at a deleted view is
    -- a dashboard that opens on an error; there is nothing to preserve in the
    -- pointer once its target is gone, and the alternative (RESTRICT) makes
    -- one user's default able to veto another user's deletion of their own
    -- view.
    view_id     text NOT NULL REFERENCES report_saved_view (view_id)
                     ON DELETE CASCADE,

    set_at      timestamptz NOT NULL DEFAULT now(),
    set_by      text NOT NULL,

    CONSTRAINT pk_report_view_default PRIMARY KEY (user_id, report_key),
    CONSTRAINT ck_report_view_default_key_shape CHECK (
        report_key ~ '^[a-z][a-z0-9_]{2,63}$'
    )
);

CREATE INDEX ix_report_view_default_view ON report_view_default (view_id);


-- =============================================================== RLS
-- `report_saved_view` filters on `entity_id` directly, exactly as `entity`,
-- `division` and `branch` do -- the shape `rls.RLS_TABLE_COLUMNS` records as
-- {"entity": "entity_id", plant/location/project: None}.
--
-- THE POLICY IS NOT THE WHOLE CONTROL, AND SAYING SO HERE IS THE POINT.
-- `capex_scope_permits(entity_id, NULL, NULL, NULL)` decides which entity's
-- views a principal may see. It does NOT decide private from shared: a
-- colleague in the same entity passes that predicate, and must still not read
-- a PRIVATE view. That second half is the `visibility`/`owner_user_id`
-- disjunction below, in the SAME policy expression, so there is no window in
-- which one is enforced and the other is not.
ALTER TABLE report_saved_view ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_saved_view FORCE ROW LEVEL SECURITY;
CREATE POLICY report_saved_view_scope ON report_saved_view
    USING (
        capex_scope_permits(entity_id, NULL, NULL, NULL)
        AND (visibility = 'SHARED'
             OR owner_user_id = COALESCE(current_setting('capex.user_id', true), ''))
    )
    -- WITH CHECK is deliberately NARROWER than USING: a principal may READ a
    -- shared view somebody else authored, and may WRITE only their own. Without
    -- this asymmetry any principal in the entity could UPDATE a shared view --
    -- silently editing the filters under everyone who uses it.
    WITH CHECK (
        capex_scope_permits(entity_id, NULL, NULL, NULL)
        AND owner_user_id = COALESCE(current_setting('capex.user_id', true), '')
    );

-- `report_view_default` carries no dimension column of its own. It reaches one
-- through `view_id`, and the policy says so rather than defaulting to TRUE --
-- an all-NULL `capex_scope_permits` call would permit every row, which is the
-- fail-open shape 006's header names.
--
-- The EXISTS runs against `report_saved_view`, which is itself under RLS, so
-- the subquery sees only views this principal may already read. A default
-- pointing at a view they cannot see therefore reads as absent, never as an
-- error naming a view id they were not entitled to learn exists.
ALTER TABLE report_view_default ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_view_default FORCE ROW LEVEL SECURITY;
CREATE POLICY report_view_default_scope ON report_view_default
    USING (
        user_id = COALESCE(current_setting('capex.user_id', true), '')
        AND EXISTS (SELECT 1 FROM report_saved_view v
                     WHERE v.view_id = report_view_default.view_id)
    )
    WITH CHECK (
        user_id = COALESCE(current_setting('capex.user_id', true), '')
        AND EXISTS (SELECT 1 FROM report_saved_view v
                     WHERE v.view_id = report_view_default.view_id)
    );


-- ================================================== privileges for capex_app
-- Stated explicitly rather than relied upon, exactly as 011, 013 and 014 state
-- theirs: 004's ALTER DEFAULT PRIVILEGES attaches to the ROLE THAT ISSUED IT,
-- so a deployment whose 016 is applied by a different identity than its 004
-- would otherwise leave the application unable to read its own new tables.
--
-- DELETE IS GRANTED HERE, AND IT IS THE ONLY PLACE IN THIS SCHEMA IT IS.
-- Every other REVOKE in 004, 008, 013 and 014 protects a LEDGER: an audit row,
-- a reservation, a procurement document -- records whose history is the
-- control, where a deleted row destroys evidence. A saved view is none of
-- those. It is a user's own bookmark, it moves no money, it is not evidence of
-- anything, and a product that cannot delete a bookmark accumulates dead rows
-- until the list is unusable. Deleting one is recorded in the audit chain by
-- `api/reports.py`, which is where the trace belongs.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    report_saved_view, report_view_default
    TO capex_app;

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS: every saved view and every user default, with no
--   -- way to reconstruct them -- they are user-authored and exist nowhere
--   -- else. Take a dump of both tables first. Nothing ELSE breaks: reporting
--   -- itself is stateless, so `app/backend/pg/reporting.py` keeps serving
--   -- every ad-hoc query, and only the saved-view routes in
--   -- `app/backend/api/reports.py` stop working.
--   DROP POLICY IF EXISTS report_view_default_scope ON report_view_default;
--   ALTER TABLE report_view_default DISABLE ROW LEVEL SECURITY;
--   DROP POLICY IF EXISTS report_saved_view_scope ON report_saved_view;
--   ALTER TABLE report_saved_view DISABLE ROW LEVEL SECURITY;
--   DROP TABLE IF EXISTS report_view_default;
--   DROP TABLE IF EXISTS report_saved_view;
--   DELETE FROM schema_migrations WHERE version = '017';
--   COMMIT;
