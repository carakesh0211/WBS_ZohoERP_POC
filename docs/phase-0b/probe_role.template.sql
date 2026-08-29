-- Phase 0B Stage 1 -- ephemeral probe role.
--
-- TEMPLATE. Run this in the Supabase SQL Editor of the NEW throwaway project
-- only, after replacing both placeholders below.
--
-- The operator replaces the placeholders. Claude does not read this file's
-- edited contents, does not read the SQL Editor afterwards, and never learns
-- the password -- it verifies the role by ATTRIBUTE only (see the final query).
--
-- Replace, exactly:
--   <<<REPLACE-WITH-HIGH-ENTROPY-PASSWORD>>>
--   <<<REPLACE-WITH-UTC-TIMESTAMP-2H-FROM-NOW>>>     e.g. 2026-08-29 09:30:00+00
--
-- Do not paste the edited version into chat, a commit, or a screenshot.
--
-- Open a NEW query tab before pasting. A previous attempt hit a merged
-- statement because Ctrl+A did not select inside the editor, and the resulting
-- syntax error left nothing applied -- harmless that time, but only by luck.

-- 1. The role. LOGIN only: no CREATEDB, no CREATEROLE, no SUPERUSER, and a
--    hard expiry so it dies on its own even if teardown were to fail.
CREATE ROLE probe_ephemeral
    LOGIN
    PASSWORD '<<<REPLACE-WITH-HIGH-ENTROPY-PASSWORD>>>'
    VALID UNTIL '<<<REPLACE-WITH-UTC-TIMESTAMP-2H-FROM-NOW>>>'
    CONNECTION LIMIT 5
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

-- 2. Connect, and nothing else. The probe runs `SELECT 1` and three session
--    functions; it needs no table, no schema object and no data.
GRANT CONNECT ON DATABASE postgres TO probe_ephemeral;

-- 3. Remove the ambient PUBLIC grants, so the role cannot reach anything that
--    happens to exist. The database is empty, but an empty database is a
--    property of today, not a control.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM probe_ephemeral;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- 4. Verification -- attribute only, no secret returned.
--    Expect: rolcanlogin t | rolsuper f | rolcreatedb f | rolcreaterole f
--            rolconnlimit 5 | rolvaliduntil ~2h from now
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
       rolconnlimit, rolvaliduntil
  FROM pg_roles
 WHERE rolname = 'probe_ephemeral';

-- 5. Confirm the role owns nothing and can see nothing it should not.
--    Expect zero rows.
SELECT table_schema, table_name
  FROM information_schema.table_privileges
 WHERE grantee = 'probe_ephemeral';

-- Teardown is by project deletion, which removes the role with the database.
-- If the project ever had to be kept, the explicit form would be:
--   DROP ROLE IF EXISTS probe_ephemeral;
