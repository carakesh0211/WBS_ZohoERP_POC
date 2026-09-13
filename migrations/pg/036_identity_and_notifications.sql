-- 036_identity_and_notifications.sql
-- Durable identity state ("Continue with Zoho", "Sign in with WBS account",
-- forgot password) and the e-mail notification outbox.
--
-- THE DECISIONS THIS TRANSCRIBES (product owner, 2026-09-13, Streams D and E)
-- ==========================================================================
--
-- D. Two sign-in choices. "Continue with Zoho": OpenID Connect authorization
--    code with PKCE, a separate server-side client, the token's `sub` as the
--    identity, signature / issuer / audience / expiry / state / nonce all
--    validated, the subject MAPPED to an existing application user and its
--    roles -- never auto-provisioned and never auto-Administrator -- with a
--    domain policy and audited account linking. "Sign in with WBS account":
--    the local login, plus Forgot password with a generic response, a random
--    single-use token stored HASHED only, fifteen-minute expiry, rate limits,
--    every session revoked after a reset, a password policy and history, and
--    a secured break-glass Administrator.
--
-- E. A durable notification outbox with templates, retry with backoff, a
--    dead-letter state, per-user preferences, a delivery history, the mail
--    provider behind an adapter, no send inside a financial transaction,
--    deduplication and correlation ids.
--
-- WHY THESE TABLES ARE IN POSTGRESQL AND NOT IN THE SQLITE IDENTITY STORE
-- =========================================================================
--
-- The application's sessions, roles and credential hashes live in SQLite
-- (`app/backend/migrations/002_financial_controls.sql`), and on AppSail that
-- database is a PER-PROCESS SCRATCH COPY of a seed, re-provisioned from
-- `uat-credentials.json` at every boot (`tools/appsail/uat_main.py`). A
-- password a user changed, a Zoho identity an administrator linked or a
-- reset token in flight would all vanish at the next recycle. So the STATE
-- that must outlive a process is here: the durable credential override
-- (consulted before the seeded hash at login), the external-identity links,
-- the OIDC state/nonce/PKCE records, the single-use handoff codes, the reset
-- tokens (hash only), the password history, the rate-limit ledger, and the
-- outbox. Sessions stay in SQLite: a recycle signing everyone out is the
-- intended behaviour of an ephemeral session store.
--
-- ROW-LEVEL SECURITY. None of these tables is project-scoped. They are
-- SYSTEM tables read and written only by the identity and notification
-- services under a SERVICE principal (`Scope.system(...)`); every policy
-- below admits a SERVICE principal, and `notification_preference` also
-- admits the row's own user. A user session never queries them directly.
--
-- Nothing here edits a byte of 001..035.

BEGIN;

-- ===================================================================
-- D. Identity
-- ===================================================================

-- The durable local credential: consulted BEFORE the seeded SQLite hash at
-- login, so a password set through the reset flow survives a recycle.
CREATE TABLE identity_credential (
    user_id             text PRIMARY KEY,
    password_salt       text NOT NULL,
    password_hash       text NOT NULL,
    credential_version  integer NOT NULL DEFAULT 1,
    changed_at          timestamptz NOT NULL DEFAULT now(),
    changed_by          text NOT NULL,
    change_reason       text NOT NULL,
    CONSTRAINT ck_identity_credential_salt CHECK (password_salt ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_identity_credential_hash CHECK (password_hash ~ '^[0-9a-f]{64}$')
);

-- Every hash a user has had, so a reset cannot reuse a recent one.
CREATE TABLE identity_password_history (
    history_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        text NOT NULL,
    password_salt  text NOT NULL,
    password_hash  text NOT NULL,
    set_at         timestamptz NOT NULL DEFAULT now(),
    set_by         text NOT NULL
);
CREATE INDEX ix_identity_password_history_user ON identity_password_history (user_id, set_at DESC);

-- A forgot-password token: the HASH only, single-use, fifteen minutes.
CREATE TABLE identity_password_reset (
    reset_id        text PRIMARY KEY,
    user_id         text NOT NULL,
    token_hash      text NOT NULL,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    consumed_at     timestamptz,
    requested_from  text,
    correlation_id  text,
    CONSTRAINT ux_identity_password_reset_token UNIQUE (token_hash),
    CONSTRAINT ck_identity_password_reset_hash CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_identity_password_reset_window CHECK (expires_at > requested_at)
);
CREATE INDEX ix_identity_password_reset_user ON identity_password_reset (user_id, requested_at DESC);

-- The rate-limit ledger for the unauthenticated flows: one row per attempt,
-- counted over a window. Survives a recycle, unlike an in-memory counter.
CREATE TABLE identity_attempt (
    attempt_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        text NOT NULL,
    attempt_key text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_identity_attempt_kind CHECK (
        kind IN ('FORGOT', 'RESET', 'OIDC_START', 'OIDC_CALLBACK', 'LOGIN'))
);
CREATE INDEX ix_identity_attempt_key ON identity_attempt (kind, attempt_key, at DESC);

-- An external identity LINKED to an application user. The subject is the
-- provider's stable `sub`; e-mail is recorded for the audit trail and the
-- domain policy, never used as the identity.
CREATE TABLE identity_external_link (
    link_id         text PRIMARY KEY,
    provider        text NOT NULL,
    subject         text NOT NULL,
    user_id         text NOT NULL,
    email           text,
    email_verified  boolean NOT NULL DEFAULT false,
    linked_at       timestamptz NOT NULL DEFAULT now(),
    linked_by       text NOT NULL,
    link_method     text NOT NULL,
    revoked_at      timestamptz,
    revoked_by      text,
    revoke_reason   text,
    CONSTRAINT ck_identity_external_link_method CHECK (
        link_method IN ('ADMIN', 'EMAIL_MATCH', 'SELF')),
    CONSTRAINT ck_identity_external_link_revoke CHECK (
        (revoked_at IS NULL) = (revoked_by IS NULL))
);
-- One live link per subject, and one live link per user per provider.
CREATE UNIQUE INDEX ux_identity_external_link_subject
    ON identity_external_link (provider, subject) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX ux_identity_external_link_user
    ON identity_external_link (provider, user_id) WHERE revoked_at IS NULL;

-- One OIDC round trip: the state (the key), the nonce (hashed), the PKCE
-- verifier (server-side only; the challenge went to the provider), a short
-- expiry, consumed once.
CREATE TABLE identity_oidc_state (
    state           text PRIMARY KEY,
    provider        text NOT NULL,
    nonce_hash      text NOT NULL,
    code_verifier   text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    consumed_at     timestamptz,
    requested_from  text,
    correlation_id  text
);

-- The one-time handoff from the provider callback to the browser: the
-- session is created server-side and fetched once with a code that lives
-- for a minute, so no session id ever appears in a URL.
CREATE TABLE identity_handoff (
    code_hash    text PRIMARY KEY,
    user_id      text NOT NULL,
    session_id   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    consumed_at  timestamptz,
    CONSTRAINT ck_identity_handoff_hash CHECK (code_hash ~ '^[0-9a-f]{64}$')
);

-- ===================================================================
-- E. Notifications
-- ===================================================================

CREATE TABLE notification_template (
    template     text PRIMARY KEY,
    subject_tpl  text NOT NULL,
    text_tpl     text NOT NULL,
    html_tpl     text,
    description  text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL
);

CREATE TABLE notification_outbox (
    notification_id      text PRIMARY KEY,
    event                text NOT NULL,
    template             text NOT NULL REFERENCES notification_template (template),
    recipient_user_id    text,
    recipient_email      text NOT NULL,
    subject              text NOT NULL,
    body_text            text NOT NULL,
    body_html            text,
    dedupe_key           text NOT NULL,
    correlation_id       text,
    object_type          text,
    object_id            text,
    state                text NOT NULL DEFAULT 'QUEUED',
    attempts             integer NOT NULL DEFAULT 0,
    max_attempts         integer NOT NULL DEFAULT 5,
    next_attempt_at      timestamptz NOT NULL DEFAULT now(),
    last_error           text,
    provider             text,
    provider_message_id  text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL,
    sent_at              timestamptz,
    CONSTRAINT ux_notification_outbox_dedupe UNIQUE (dedupe_key),
    CONSTRAINT ck_notification_outbox_state CHECK (
        state IN ('QUEUED', 'SENDING', 'SENT', 'FAILED', 'DEAD', 'SUPPRESSED')),
    CONSTRAINT ck_notification_outbox_attempts CHECK (
        attempts >= 0 AND max_attempts >= 1)
);
CREATE INDEX ix_notification_outbox_due
    ON notification_outbox (next_attempt_at) WHERE state IN ('QUEUED', 'FAILED');
CREATE INDEX ix_notification_outbox_recipient
    ON notification_outbox (recipient_user_id, created_at DESC);

-- Append-only: one row per delivery attempt, whatever its outcome.
CREATE TABLE notification_delivery (
    delivery_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    notification_id  text NOT NULL REFERENCES notification_outbox (notification_id),
    attempt          integer NOT NULL,
    at               timestamptz NOT NULL DEFAULT now(),
    outcome          text NOT NULL,
    provider         text NOT NULL,
    detail           text,
    CONSTRAINT ck_notification_delivery_outcome CHECK (
        outcome IN ('SENT', 'RETRY', 'DEAD', 'SUPPRESSED', 'RECORDED'))
);
CREATE INDEX ix_notification_delivery_notification
    ON notification_delivery (notification_id, attempt);

CREATE TABLE notification_preference (
    user_id     text NOT NULL,
    event       text NOT NULL,
    channel     text NOT NULL DEFAULT 'email',
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, event, channel),
    CONSTRAINT ck_notification_preference_channel CHECK (channel IN ('email'))
);

-- The templates. Placeholders are `{name}` fields rendered by
-- `pg/notifications.py` with `str.format_map` over an escaped context; a
-- placeholder the context does not supply renders as "(not supplied)"
-- rather than raising inside a business transaction.
INSERT INTO notification_template
    (template, subject_tpl, text_tpl, html_tpl, description, updated_by)
VALUES
    ('PR_SUBMITTED',
     '[WBS] Purchase request {pr_number} awaits your approval',
     'Purchase request {pr_number} ({amount}) for project {project} was submitted by {requested_by} and awaits approval.\n\nOpen it: {link}',
     NULL, 'Sent to the approvers when a purchase request is submitted.', 'SYSTEM'),
    ('PR_APPROVED',
     '[WBS] Purchase request {pr_number} approved',
     'Purchase request {pr_number} ({amount}) was approved by {approver}.\n\nOpen it: {link}',
     NULL, 'Sent to the requester when their purchase request is approved.', 'SYSTEM'),
    ('IMR_APPROVED',
     '[WBS] Internal material request {imr_number} approved',
     'Internal material request {imr_number} for {quantity} on {wbs_code} was approved by {approver}.\n\nOpen it: {link}',
     NULL, 'Sent to the requester when their internal material request is approved.', 'SYSTEM'),
    ('ADMIN_SELF_APPROVAL_OVERRIDE',
     '[WBS] Administrator self-approval override on {object_label}',
     'Administrator {actor} approved {object_label}, which they raised themselves, with the reason: {reason}\n\nThis override is recorded in the audit trail (action ADMIN_SELF_APPROVAL_OVERRIDE, correlation {correlation_id}).',
     NULL, 'Sent to every other Administrator when an override is taken.', 'SYSTEM'),
    ('EXPORT_COMPLETED',
     '[WBS] Your export {dataset} is ready',
     'Export {export_job_id} ({dataset}, {rows} rows, {format}) is ready to download until {expires_at}.\n\nDownload: {link}',
     NULL, 'Sent to the requester when an export job succeeds.', 'SYSTEM'),
    ('PASSWORD_RESET_REQUESTED',
     '[WBS] Reset your WBS account password',
     'A password reset was requested for your WBS account {user_id}. If that was you, use this link within 15 minutes:\n\n{link}\n\nIf it was not you, ignore this message; your password is unchanged.',
     NULL, 'Sent to the account e-mail on a forgot-password request. Generic on the screen; specific here.', 'SYSTEM'),
    ('PASSWORD_CHANGED',
     '[WBS] Your WBS account password was changed',
     'The password for your WBS account {user_id} was changed at {changed_at}. Every earlier session was signed out. If this was not you, contact an Administrator immediately.',
     NULL, 'Sent after a password reset or change.', 'SYSTEM'),
    ('IDENTITY_LINKED',
     '[WBS] A Zoho identity was linked to your WBS account',
     'The Zoho identity {email} was linked to your WBS account {user_id} by {linked_by} ({method}). If this was not expected, contact an Administrator.',
     NULL, 'Sent when an external identity is linked.', 'SYSTEM'),
    ('EXCEPTION_RAISED',
     '[WBS] Reconciliation exception {kind} on {object_label}',
     'A reconciliation exception of kind {kind} was raised on {object_label}: {detail}\n\nOpen the exception queue: {link}',
     NULL, 'Sent to the finance and administrator roles when an exception opens.', 'SYSTEM');

-- ------------------------------------------------------------- privileges
GRANT SELECT, INSERT, UPDATE ON identity_credential TO capex_app;
GRANT SELECT, INSERT ON identity_password_history TO capex_app;
GRANT SELECT, INSERT, UPDATE ON identity_password_reset TO capex_app;
GRANT SELECT, INSERT ON identity_attempt TO capex_app;
GRANT SELECT, INSERT, UPDATE ON identity_external_link TO capex_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON identity_oidc_state TO capex_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON identity_handoff TO capex_app;
GRANT SELECT, INSERT, UPDATE ON notification_template TO capex_app;
GRANT SELECT, INSERT, UPDATE ON notification_outbox TO capex_app;
GRANT SELECT, INSERT ON notification_delivery TO capex_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON notification_preference TO capex_app;
REVOKE DELETE ON identity_credential, identity_password_history,
                 identity_password_reset, identity_attempt,
                 identity_external_link, notification_template,
                 notification_outbox, notification_delivery FROM capex_app;

-- ------------------------------------------------------------------- RLS
-- System tables: a SERVICE principal (the identity and notification
-- services run under Scope.system) reads and writes them; a USER session
-- sees nothing -- except a user's own notification preferences.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['identity_credential', 'identity_password_history',
                             'identity_password_reset', 'identity_attempt',
                             'identity_external_link', 'identity_oidc_state',
                             'identity_handoff', 'notification_template',
                             'notification_outbox', 'notification_delivery']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (COALESCE(current_setting(''capex.principal_kind'', true), '''') = ''SERVICE'') '
            'WITH CHECK (COALESCE(current_setting(''capex.principal_kind'', true), '''') = ''SERVICE'')',
            t || '_service', t);
    END LOOP;
END $$;

ALTER TABLE notification_preference ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_preference FORCE ROW LEVEL SECURITY;
CREATE POLICY notification_preference_own ON notification_preference
    USING (COALESCE(current_setting('capex.principal_kind', true), '') = 'SERVICE'
           OR user_id = COALESCE(current_setting('capex.user_id', true), ''))
    WITH CHECK (COALESCE(current_setting('capex.principal_kind', true), '') = 'SERVICE'
                OR user_id = COALESCE(current_setting('capex.user_id', true), ''));

-- Migration 036 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   DROP TABLE IF EXISTS notification_preference;
--   DROP TABLE IF EXISTS notification_delivery;
--   DROP TABLE IF EXISTS notification_outbox;
--   DROP TABLE IF EXISTS notification_template;
--   DROP TABLE IF EXISTS identity_handoff;
--   DROP TABLE IF EXISTS identity_oidc_state;
--   DROP TABLE IF EXISTS identity_external_link;
--   DROP TABLE IF EXISTS identity_attempt;
--   DROP TABLE IF EXISTS identity_password_reset;
--   DROP TABLE IF EXISTS identity_password_history;
--   -- identity_credential holds passwords users SET; dropping it reverts them
--   -- to the seeded hashes. Export it before dropping.
--   DROP TABLE IF EXISTS identity_credential;
--   DELETE FROM schema_migrations WHERE version = '036';
--   COMMIT;
