-- 007_scope_sentinel.sql
-- Wave 3 stream 3: retire the `*` scope sentinel.
--
-- THE DEFECT THIS CLOSES
--
-- `Scope.as_settings()` rendered an unrestricted dimension as the literal
-- string `*`, and 004's `capex_dimension_permits` treated a setting value of
-- `*` as "unrestricted". A grant whose `scope_value` was literally `*` was
-- therefore stored as a RESTRICTION but read by RLS as NO restriction -- and
-- the two enforcement layers disagreed about it:
--
--   * `repo.compile_scope` treated `*` as an ordinary id, matching nothing;
--   * RLS treated `*` as a wildcard, matching everything.
--
-- One row, two opposite answers, with the database's answer being the wider
-- one. That is a privilege escalation wearing the costume of a restriction.
--
-- THE FIX (docs/WAVE3_CONTRACTS.md, contract 1 -- frozen; streams 1, 2 and 3
-- all depend on it)
--
-- Mode and identity become SEPARATE session settings. Per dimension
-- `d` in {entity, plant, project, location}:
--
--   capex.<d>_mode   'all' | 'none' | 'list'
--   capex.<d>_ids    comma-separated ids; meaningful only when mode = 'list'
--
-- There is now no value an id can take that means "unrestricted", so the
-- ambiguity is not merely rejected -- it is inexpressible.
--
--   p_value IS NULL                  -> true   (table shape waives this dim)
--   mode = 'all'                     -> true
--   mode = 'list'                    -> p_value = ANY(ids)
--   mode = 'none' | absent | unknown -> false  (FAIL CLOSED)
--
-- `capex_dimension_permits(text, text)` KEEPS ITS TWO-ARGUMENT SIGNATURE, so
-- 004's policies and stream 2's new policies in 006 keep calling it
-- unchanged; only the body is replaced. The mode key is derived inside the
-- function by replacing the `_ids` suffix of the setting key with `_mode`,
-- so no caller has to pass it and no policy has to be rewritten.
--
-- Fail-closed on an ABSENT mode is the property that matters most: a
-- connection that never went through `Database.session()` (capex_app used
-- directly, without `rls.assume_scoped_role`) sets no scope at all and must
-- therefore see NOTHING. A backstop that fails open on a forgotten
-- `SET LOCAL` protects nothing. That was 004's behaviour for '' and it
-- remains the behaviour here for an absent or unrecognised mode.
--
-- ROLLBACK:
--   ALTER TABLE user_scope_grant
--     DROP CONSTRAINT IF EXISTS user_scope_grant_value_not_sentinel;
--   -- and restore 004's wildcard-reading body:
--   CREATE OR REPLACE FUNCTION capex_dimension_permits(p_setting_key text, p_value text)
--   RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $ROLLBACK$
--       SELECT CASE
--           WHEN p_value IS NULL THEN true
--           WHEN COALESCE(current_setting(p_setting_key, true), '') = '*' THEN true
--           ELSE p_value = ANY(string_to_array(
--                    COALESCE(current_setting(p_setting_key, true), ''), ','))
--       END;
--   $ROLLBACK$;
--   -- NOTE: rolling back re-opens the defect above, and only works while the
--   -- application still renders the old single-setting format. Roll back the
--   -- application (engine.py `Scope.as_settings`) in the same step or the
--   -- `_mode` settings it emits will be read by nothing and every dimension
--   -- will read as restricted-to-nothing -- denial, not exposure.

-- ================================================ capex_dimension_permits
-- Signature unchanged; body replaced. Still `sql STABLE PARALLEL SAFE`, so
-- it remains inlinable into the policies that call it and costs no more to
-- evaluate per row than 004's version did.
--
-- `regexp_replace(p_setting_key, '_ids$', '_mode')` derives the mode key. A
-- key NOT ending in `_ids` is left unchanged, so `current_setting` looks up a
-- key that carries no mode, yields NULL -> '' -> the ELSE branch -> false.
-- A malformed call therefore denies rather than widening: fail closed even
-- on programmer error.
CREATE OR REPLACE FUNCTION capex_dimension_permits(p_setting_key text, p_value text)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE
        -- The row carries no value for this dimension at all -- the table or
        -- query shape waives it (mirrors `repo.compile_scope`'s
        -- `columns={"dim": None}` waiver). Only ever reached with a literal
        -- SQL NULL passed by a policy, never via an absent session setting.
        WHEN p_value IS NULL THEN true
        ELSE CASE COALESCE(
                 current_setting(regexp_replace(p_setting_key, '_ids$', '_mode'), true),
                 '')
            WHEN 'all'  THEN true
            WHEN 'list' THEN p_value = ANY(string_to_array(
                     COALESCE(current_setting(p_setting_key, true), ''), ','))
            -- 'none', '' (absent), or anything unrecognised: DENY.
            ELSE false
        END
    END;
$$;

GRANT EXECUTE ON FUNCTION capex_dimension_permits(text, text) TO PUBLIC;

-- ========================================== pre-existing offending rows
-- The CHECK constraint below is added to a table that may already hold data
-- (`migrations/pg/seed_parts/004_access.sql` is real seed data, and a
-- restored production dump is realer still). Two things are true and both
-- are handled rather than assumed:
--
--   1. NO SHIPPED ROW VIOLATES IT. Every `user_scope_grant` row in
--      `seed_parts/004_access.sql` carries a concrete id -- ENT-DM1,
--      PLT-DM1-A, PRJ-DM-001, PLT-DM2-A, ENT-DM2. U-NOGRANT, the
--      restricted-to-nothing user, deliberately has NO grant rows at all
--      (the empty set is recorded by `user_scope_restriction`, which is the
--      whole reason that second table exists). So on any database built from
--      this repository the constraint is satisfied the moment it is added.
--
--   2. A DATABASE THIS MIGRATION HAS NEVER SEEN MAY STILL VIOLATE IT -- a `*`
--      row inserted by hand, or by the pre-Wave-3 API before the write-path
--      guard existed. That row is exactly the defect being closed, so it must
--      NOT be silently deleted: dropping a grant row changes someone's access
--      without telling anyone, and doing it inside a migration hides it from
--      the audit trail entirely. The migration instead REFUSES, naming every
--      offending row, and an operator decides what each one was meant to say.
--      A `*` row's intent is genuinely ambiguous -- "unrestricted" (delete the
--      restriction row) or "nothing" (delete just the grant) -- and a
--      migration must not guess at a security boundary.
DO $$
DECLARE
    offending text;
    offending_count integer;
BEGIN
    SELECT count(*), string_agg(
               format('(user_id=%L, dimension=%L, scope_value=%L)',
                      user_id, dimension, scope_value),
               ', ' ORDER BY user_id, dimension, scope_value)
      INTO offending_count, offending
      FROM user_scope_grant
     WHERE scope_value = '*'
        OR btrim(scope_value) = ''
        OR position(',' in scope_value) > 0;

    -- Format and hint are each ONE string literal on ONE line, deliberately.
    -- PL/pgSQL requires RAISE's format to be a simple string literal, not an
    -- expression; splitting it across adjacent literals relies on lexer
    -- behaviour this migration cannot afford to be wrong about.
    IF offending_count > 0 THEN
        RAISE EXCEPTION 'migration 007 refuses to add user_scope_grant_value_not_sentinel: % pre-existing row(s) violate it: %', offending_count, offending
        USING HINT = 'Each row is the defect this migration closes. Decide per row what it was MEANT to say, then re-run: for "unrestricted", DELETE the user_scope_restriction row for that (user_id, dimension) and the grant cascades away with it; for "restricted to nothing", DELETE just the user_scope_grant row and KEEP the restriction row. This migration will not choose for you: the two mean opposite things.';
    END IF;
END $$;

-- ============================================ the third rejection layer
-- Layers one and two are `roles.set_scope` (the grant write path) and
-- `repo.compile_scope` (the repository boundary), both in Python. This is
-- the third, and the only one that also binds `psql`, a restored dump, a
-- future write path that forgets to call `set_scope`, and any code path
-- that has not been written yet.
--
-- Each rejected shape is a value some layer would MISREAD, not merely fail
-- to match:
--   `*`              the pre-Wave-3 wildcard; the whole point of this file
--   blank/whitespace indistinguishable from "no id" once comma-joined
--   contains a comma one grant would split into two on the wire, because
--                    `capex.<d>_ids` is a comma-separated list
--
-- NOT VALID is deliberately NOT used: the whole value of this constraint is
-- that it holds for rows already present, and the guard above has already
-- proven they do.
ALTER TABLE user_scope_grant
    ADD CONSTRAINT user_scope_grant_value_not_sentinel
    CHECK (
        scope_value <> '*'
        AND btrim(scope_value) <> ''
        AND position(',' in scope_value) = 0
    );

COMMENT ON CONSTRAINT user_scope_grant_value_not_sentinel ON user_scope_grant IS 'No grant id may be the * wildcard, blank, or comma-bearing. * was the pre-Wave-3 "unrestricted" sentinel that RLS and repo.compile_scope read in opposite directions; blank and comma-bearing ids corrupt the comma-joined capex.<d>_ids wire format. Unrestricted is expressed by the ABSENCE of a user_scope_restriction row, never by a magic id.';
