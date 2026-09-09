# Runbook — encryption key rotation

**Status: `UNVERIFIED`.** No key has ever been rotated in this build, and there
is no key management system here to rotate one in. This is a design.

## The keys, and why they rotate differently

| Key | Protects | Rotation shape |
| --- | --- | --- |
| Database storage encryption | data at rest in the cluster | Platform-managed; re-encrypt in place. Application-transparent |
| Secret-store master key | the secrets behind `secretref://...` | Re-wrap the data keys. Secret **values** do not change |
| Backup encryption key | dump and WAL archive files | **Old backups stay encrypted under the old key** |
| Application field-level key | any column the application encrypts | Requires a re-encryption pass over stored rows |

## The rule that governs all four

**Never destroy the old key at cutover.** Retire it, keep it, and destroy it
only after everything encrypted under it has been re-encrypted *and* verified.

This bites hardest on backups. A backup encrypted under a key you have destroyed
is not a backup, and you discover that during an incident. The backup retention
window therefore sets the earliest date the old key may be destroyed — 30 days
under the proposed schedule in [`backup-restore.md`](backup-restore.md), which
is itself unconfigured.

## The audit-chain constraint

A field-level re-encryption pass **must not touch `audit_log`.**

`prev_hash` and `entry_hash` are computed over the *plaintext* payload
`prev|at|actor|action|type|id|detail`. Re-encrypting any of those columns and
writing back a different representation changes what a verifier reads and breaks
the chain — indistinguishably from tampering.

If a future design encrypts an audit column, the hash payload has to be defined
over the plaintext and computed before encryption, and
`app/backend/pg/audit.py` says in terms that the frozen payload format must not
be changed. Treat "encrypt an audit column" as a schema change needing its own
migration and its own proof, never as a step inside a key rotation.

The same holds in reverse: `audit_log` carries append-only triggers, so a
re-encryption pass that ran against it would be refused by the database. That is
the correct outcome and must not be worked around.

## Procedure (field-level key — the only one with an application step)

1. Introduce the new key **alongside** the old. Both must be readable.
2. Write with the new key, read with either — a key id per row, not a global
   switch.
3. Re-encrypt stored rows in batches, in a resumable job. **Exclude
   `audit_log`.**
4. Confirm no row remains under the old key id.
5. **Run the restore drill** (`tools/restore_drill.py`) against a restore taken
   *after* the pass. Its audit-chain check is what proves the pass did not
   disturb history.
6. Retire the old key. Destroy it only after the backup retention window has
   passed.

## After any rotation

* Run the restore drill. A key rotation is exactly the kind of change that
  produces a database which works today and cannot be restored tomorrow.
* Confirm `/readyz` is 200.
* Record the rotation in the audit log with the actor and both key ids.

## What is NOT decided

Which KMS, whether field-level encryption is used at all, key custody, and the
destruction schedule. There is no field-level encryption in the current schema,
so "Procedure" above describes a pass that would today have nothing to do — and
saying so is more useful than implying a control that is not there.
