# Runbook — health, readiness, and the 30-second cold-start budget

**Status: MIXED.** The two endpoints exist and their separation is enforced by
`tests/test_ops_readiness.py`, which runs everywhere. The **timing** claim
against a real cold start on the platform is `UNVERIFIED` — there is no
PostgreSQL and no AppSail deployment on this build host.

## Two endpoints, two genuinely different questions

| | `GET /healthz` | `GET /readyz` |
| --- | --- | --- |
| Question | Is the process up? | Is the database reachable and its schema current? |
| Touches a database | **Never** | Yes |
| When the database is down | still 200 | 503 |
| When nothing is configured | 200 | 503 `DatabaseNotConfigured` |

Conflating these is how a broken deployment reports healthy. `/healthz` must
answer **even when the database is completely unreachable**, because that is
exactly the moment an orchestrator needs to tell "the process crashed" from "the
process is fine, its dependency is down". `app/backend/api/health.py` implements
`/healthz` so that it imports, constructs and calls nothing that could touch a
database — not even to check that one is configured.

`tests/test_ops_readiness.py` asserts this by inspecting the handler's source,
because the distinction is a property of the handler rather than of a response
body: a `/healthz` that happened to touch the database would pass any
behavioural test run on a machine where the database was up.

## The information leak that is deliberately not there

`/readyz` reports an exception **class name** on failure, never the driver
message. A libpq error echoes host, user and database name. Nothing in
`health.py` reads a field from the database config or re-stringifies a caught
exception into the response, and that is a constraint on future edits rather
than an accident of the current one.

## The 30-second budget

The platform's request budget is 30 seconds, and both endpoints must answer
inside it on a cold start.

* `/healthz` returns a dict literal. It cannot exceed the budget without the
  process being unable to serve any request at all.
* `/readyz` takes a pooled connection and runs simple `SELECT`s: low
  milliseconds warm, a couple of seconds on a cold start while the pool opens
  its first connection.

**`UNVERIFIED`:** that second figure has not been measured against a real cold
start on the platform. It is an estimate from the shape of the work. The number
that matters comes from a deployed instance with a managed PostgreSQL behind it,
and until that measurement exists **do not quote a cold-start latency to a
client.**

What *is* checked here is that `/readyz` does no unbounded work — no full scan,
no per-table loop, no migration application — so the budget cannot be blown by
data volume. That is a source-level property and it is tested.

## Using them

* **Liveness probe → `/healthz`.** A liveness probe pointed at `/readyz`
  restarts the process every time the database blips, turning a dependency
  outage into a crash loop.
* **Readiness probe → `/readyz`.** Traffic should not reach an instance that
  cannot serve it.
* **Alerting:** `/readyz` 503 while `/healthz` 200 means the dependency, not the
  application. That pair is the diagnostic, and it is the whole reason for the
  split.
