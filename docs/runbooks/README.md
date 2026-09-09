# Runbooks

Operational procedures for the CAPEX & WBS Control Hub.

## The rule this directory is held to

**A runbook nobody has executed is a comment.** Every procedure below carries a
verification status, and the statuses are not decoration — `tests/test_ops_readiness.py`
reads them and fails the build if a runbook claims a status it has not earned,
or if a step described as "verified" has no evidence recorded against it.

| Status | Means |
| --- | --- |
| `VERIFIED-LOCAL` | Executed on a workstation against a disposable database created by the procedure itself. The command and its real output are recorded in the runbook. |
| `VERIFIED-CI` | Executed in CI against the `postgres:16` service container. Green on a named job. |
| `UNVERIFIED` | Never executed anywhere. It needs managed PostgreSQL, PITR, a cloud project or a billable resource, none of which exist for this build. **Read these as a design, not as a working procedure.** |

`UNVERIFIED` is not a defect to be hidden. It is the honest label for a
procedure that has been designed and written down but not run, and this project
has been bitten repeatedly by claims that were true of the intent and false of
the code.

## Index

| Runbook | Covers | Status |
| --- | --- | --- |
| [`poc-to-postgres-migration.md`](poc-to-postgres-migration.md) | Export, preflight, dry run, import, reconciliation, audit-chain preservation, rollback | Mixed — per step |
| [`backup-restore.md`](backup-restore.md) | Backup, point-in-time recovery, the restore drill and its audit-chain gate | Mixed — mostly `UNVERIFIED` |
| [`alerts.md`](alerts.md) | The six alert conditions, their thresholds, and the response to each | `VERIFIED-LOCAL` for the definitions; firing is `UNVERIFIED` |
| [`credential-rotation.md`](credential-rotation.md) | Database passwords, Zoho OAuth refresh tokens, application secrets | `UNVERIFIED` |
| [`encryption-key-rotation.md`](encryption-key-rotation.md) | Data-at-rest and secret-store key rotation | `UNVERIFIED` |
| [`health-and-readiness.md`](health-and-readiness.md) | `/healthz` vs `/readyz`, and the 30-second cold-start budget | Mixed |

`alert-definitions.json` beside these files is the machine-readable form of
`alerts.md`. It is bound to `app/backend/observability.py::ALERT_CONDITIONS` by
a test, so a condition cannot exist in code without a response written down,
and a response cannot be written down for a condition that does not exist.
