# Adversarial review — `fable-5.1/full-app-hardening-uat`, commits `c51c2fe..HEAD`

Scope confirmed via `git log --oneline c51c2fe..HEAD` (38 commits, ending at
`6a3987c`). Read-only review: no edits, no deploys, no Playwright. Targeted
pytest runs only (`tests/test_live_transport_fable51.py` — 19 passed).

---

## FINDINGS

### P1 — Rate/budget control is bypassed for the exact call it exists to bound
**File:** `app/backend/integration/sweeps.py:939-950` (`SweepPoAnchored.resume`),
cross-referenced with `app/backend/integration/erp.py:632-650` (`receives_for_po`)
and `app/backend/integration/jobs.py:390-405` (`JobContext.charge`).

`receives_for_po`'s own docstring states its cost precisely:
> "Cost is therefore `1 + len(receives)` calls per open PO"

But the caller only ever asks the budget for 1:
```python
if not ctx.charge(1):
    return
receives = _adapter_call(self.adapter, "receives_for_po")(po_external_id=po.external_id)
```
`JobContext.charge`'s own docstring states the invariant this breaks:
> "Called BEFORE the call is made, never after: a budget consulted after the
> fact cannot prevent the request that breaches the daily ceiling"

Every receive-detail fetch inside `receives_for_po` (one GET per
`receive_external_ids` entry) happens with **zero budget authorization** —
`ctx.charge` is never called again for them. On ERP Standard's 2,000
calls/day ceiling (the single most binding constraint called out repeatedly
in project memory), a PO with several receives can silently spend far more
of the daily quota than the persisted `integration_rate_budget` ledger
records, so `/health`'s "remaining" figure and any daily-ceiling alerting
built on it become **optimistic fiction** — the app can walk straight into
Zoho's real 429/45 daily-limit lockout without its own governance predicting
it, at whatever hour that happens to matter.

**This is a known, unfixed, self-acknowledged gap** — the test suite says so
in its own words, in `tests/test_live_sweep_fable51.py:649-660`:
```python
# REPORTED, NOT SMOOTHED OVER. Seven requests left the machine and SIX
# were charged: `SweepPoAnchored` asks the budget for one call per open
# PO, but `ErpAdapter.receives_for_po` spends `1 + len(receives)` -- ...
# so the POLLING lane's daily figure under-counts by one per receive. ...
# A finding for stream 1 / stream 4; the transport's own sliding-minute
# ceiling counts every request regardless.
assert len(fake.requests) == 7
assert result["budget"]["windows"]["DAY"]["used"] == 6
```
Confirmed as fact, not speculation: the test itself proves 7 real calls
against 6 charged units.

**Minimal fix:** charge `1 + len(po.receive_external_ids)` — but that count
isn't known until `get_purchase_order` returns, so the shape has to change:
either (a) split `receives_for_po` so the caller charges 1 for the PO
detail, inspects `receive_external_ids`, charges `len(...)` before issuing
the per-receive fetches (return early / checkpoint if that second charge is
refused), or (b) have `receives_for_po` accept a `charge` callback invoked
between the PO-detail fetch and each receive fetch. Note the transport's own
in-process 100/min sliding window (`LiveTransport._spend_one`) is NOT
affected by this — it counts every real HTTP call regardless of what the
job's budget believed — so the exposure is specifically to the **persisted,
cross-request DAILY ceiling**, not to per-minute throttling within one
sweep call.

---

### P2 — `LiveTransport` is rebuilt from scratch on every HTTP request; the documented org-wide rate budget and token cache are not actually shared
**File:** `app/backend/api/integrations.py:314-339` (`_live_transport_for`)

```python
def _live_transport_for(connection: dict):
    ...
    try:
        return LiveTransport()
    except _ad.IntegrationError as exc:
        raise _live_error_to_http(...)
```
No caching, no singleton, no process-level reuse — confirmed by grep, the
only two call sites that construct `LiveTransport()` are here and
`app/backend/integration/live_sweep.py:500`, and there is no
`lru_cache`/module-level cache anywhere in `integrations.py`,
`integrations_live.py`, or `live_sweep.py`. Every call to
`GET /organizations`, `POST /validate`, `GET /scopes`, or `GET /health`
constructs a brand-new `LiveTransport`, which:
1. Mints a fresh access token from the refresh token on first use inside
   that call (`LiveTransport._bearer` → `_mint`) — defeating the documented
   "re-mint two minutes before expiry" cache; every route hit is a full
   OAuth token-refresh round trip to `accounts.zoho.in`, not just the first.
2. Starts the 100-calls/60s sliding window (`self._calls: deque[float] = deque()`)
   **empty** on every request. The module docstring's claim —
   "The 101st call in any sliding minute (plan §11.6: 100/minute per
   organisation). Refused, never queued" — is enforced only *within* a
   single HTTP request's lifetime, not across the many separate requests an
   operator (or a script) can issue against these four routes. A caller
   hammering `/validate` (which itself makes up to ~7 probe calls per hit)
   in a loop is not actually rate-limited by this code at all; only Zoho's
   own server-side ceiling would eventually stop it.

Within one `POST /sweep` call this doesn't matter (one transport is built
and threaded through the whole sweep), but the four read-only routes in
`integrations.py` do not share that discipline.

**Minimal fix:** memoize a `LiveTransport` per process (or per connection_id)
with a short-lived cache, so the token and the sliding window are actually
shared across requests the way the module docstring claims.

---

### P2 — TOCTOU race in `_job_row_for` can create duplicate live job rows for the same (kind, connection_id)
**File:** `app/backend/integration/live_sweep.py:541-574`

```python
rows = repo.query(session, f"""
    SELECT job_id, state FROM {store.JOB}
    WHERE kind = %(kind)s AND connection_id = %(connection_id)s AND {{scope}}
    ORDER BY created_at
    """, ...)
dead = [r[0] for r in rows if r[1] == jobs.JOB_DEAD]
live = [r[0] for r in rows if r[1] in jobs.CLAIMABLE_STATES]
if live:
    return live[0], dead
job_id = store.enqueue_job(session, job_id=f"JOB-{uuid.uuid4().hex}", kind=kind, ...)
return job_id, dead
```
This is a plain `SELECT` with no `FOR UPDATE`, and I confirmed by reading
`migrations/pg/010_integration.sql:725-845` that the `job` table carries
**no unique constraint or partial unique index on `(kind, connection_id)`**
for non-terminal states — only non-unique indexes (`ix_job_claimable`,
`ix_job_connection`, etc.). Two concurrent `POST /sweep` calls on the same
connection can both run this SELECT, both find no live row, and both
`INSERT` a new job row for the same `kind` — producing two live job rows
for e.g. `poll_bills` on the same connection.

`ConnectionJobStore.claim_job`'s `FOR UPDATE SKIP LOCKED` (verified sound —
see below) prevents the *same* row from being claimed twice, but it does
nothing about two *different* rows for the same logical work existing and
being claimed independently by the two racing requests — each would then
poll the same module concurrently against the connection's single
`integration_watermark` row (`PRIMARY KEY (connection_id, module)`),
racing `set_watermark` writes and duplicating real Zoho API calls (which,
combined with the P1 finding above, makes the daily-budget undercount
worse under concurrent operator use).

I verified the missing constraint and the unguarded check-then-insert by
reading the code and schema; I did not reproduce the race with an actual
concurrent test run (out of scope — no live concurrency harness was run),
so this is reported as a structural gap proven by code/schema inspection,
not as an observed failure.

**Minimal fix:** either add a partial unique index
`UNIQUE (kind, connection_id) WHERE state IN (claimable states)` and let
the INSERT's conflict resolve to "read the winner," or take a
`pg_advisory_xact_lock` keyed on `(kind, connection_id)` around the
select-or-insert in `_job_row_for`.

---

### P2 — Stage B's "sslmode verify-full, never relaxed" is a default, not an enforced invariant
**File:** `tools/appsail/uat_main.py:88-100`

```python
on_catalyst = bool(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT"))
if on_catalyst and (os.environ.get("CAPEX_DB_HOST") or os.environ.get("CAPEX_DB_URL")):
    ca = os.path.join(BUNDLE, "ca-bundle.pem")
    os.environ.setdefault("CAPEX_DB_SSLMODE", "verify-full")
    if os.path.isfile(ca):
        os.environ.setdefault("CAPEX_DB_SSLROOTCERT", ca)
    elif os.environ.get("CAPEX_DB_SSLMODE", "verify-full") == "verify-full" \
            and not os.environ.get("CAPEX_DB_SSLROOTCERT"):
        _fail(...)
```
The module docstring claims: "the provider's CA at the archive root with
sslmode verify-full, **never relaxed**." But `os.environ.setdefault(...)`
only supplies a value when `CAPEX_DB_SSLMODE` is **absent**. If the
Catalyst console configuration (which the project memory confirms carries
eleven console-set env vars for Stage B) has `CAPEX_DB_SSLMODE` set to
anything else — `require`, `prefer`, or `disable`, whether by a copy/paste
mistake, a previous debugging session, or driftbetween the UAT and a future
prod console — this code does nothing to catch or refuse it; it silently
starts with whatever weaker mode was configured. The `_fail` branch only
fires when verify-full *is* the effective mode and no CA is available; it
never fires because a non-verify-full mode was explicitly set.

**Minimal fix:** after the `setdefault`, assert
`os.environ["CAPEX_DB_SSLMODE"] == "verify-full"` and `_fail` otherwise (or
whitelist only `verify-full`/`verify-ca` and refuse `require`/`prefer`/
`disable`/`allow` outright on Catalyst), so the claim in the docstring is
something the code actually enforces rather than merely defaults to.

---

### P3 — `build_uat_bundle.py`'s secret scanner has a narrow signature list
**File:** `tools/appsail/build_uat_bundle.py:92-94`

```python
SECRET_RE = re.compile(
    rb"(AKIA[0-9A-Z]{16}|gh[pos]_[A-Za-z0-9]{30,}|1000\.[0-9a-f]{32}\.[0-9a-f]{32}"
    rb"|-----BEGIN (?:RSA |EC )?PRIVATE KEY|xox[baprs]-[0-9A-Za-z-]{10,})")
```
This catches AWS access keys, GitHub tokens, one specific Zoho OAuth-token
shape, PEM private-key headers, and Slack tokens — five specific vendor
signatures. It would **not** catch a generic secret accidentally swept into
the archive by `copy_tree(REPO / "app", ...)` or `copy_tree(REPO /
"migrations", ...)` — e.g. a Postgres connection string with an embedded
password (`postgresql://user:S3cr3t@host/db`, the shape the project's own
Stage B `db.env` file carries), a bare API key, or a JWT. The module
docstring's own claim — "a byte sequence that looks like a token or private
key in a text file" is refused — overstates what the regex actually
detects; the only other defense is the explicit filename/directory
blocklist (`.env`, `settings.local.json`, `*.db`), which is name-based, not
content-based.

I did not find an active path in the current `build()` function that would
actually copy an ERP or DB credential file into the bundle (the ERP OAuth
credential file and the Stage B `db.env` are never referenced by
`build_uat_bundle.py`), so this is defense-in-depth hygiene rather than a
demonstrated live leak — flagged P3.

**Minimal fix:** widen `SECRET_RE` with a generic high-entropy match (e.g.
`://[^/\s:@]+:[^/\s:@]+@` for connection-string credentials, plus a
generic long base64/hex-run heuristic with an allowlist for known-benign
hashes), or scan `app/`/`migrations/` copies against the specific secret
files known to exist on the operator's machine.

---

## CHECKED AND FOUND SOUND

- **`live_transport.py` — token/secret never reaches a log, error body, or
  `describe()`.** Verified by reading every raise site: `_mint()`'s error
  messages carry only HTTP status and Zoho's own `error` field;
  `ZohoApiError` carries `status`/`path`/Zoho's `code`/`message` only;
  `describe()` returns product/domain/scopes/booleans/counts, never the
  token. Confirmed further by
  `tests/test_live_transport_fable51.py::test_no_failure_transcript_ever_contains_a_secret`,
  which asserts across 4 distinct failure paths that none of `SECRET`,
  `REFRESH`, `ACCESS` appear in the rendered transcript — this is a
  mutation-resistant assertion (it would catch any code path that formats
  the credential dict into a message).

- **GET-only gate is sound and not bypassable via `base_url`.** The write
  gate (`live_transport.py:261-265`) checks
  `zoho_mod.outbound_writes_enabled()` (`app/backend/zoho.py:75-82`, reads
  `CAPEX_ERP_OUTBOUND_WRITES` from the environment on every call, no
  header/caller-supplied override anywhere). India-host pin
  (`live_transport.py:135-141`) uses `urllib.parse.urlsplit(...).hostname`
  at credential-load time, not string matching, so the classic
  `www.zohoapis.in.evil.com` / `www.zohoapis.in@evil.com` (userinfo) tricks
  both correctly resolve to a hostname that fails
  `host.endswith(".zohoapis.in")` — verified by hand-tracing `urlsplit`
  semantics for both cases. Per-call `request()` re-checks `base_url`
  against the already-validated `self.api_domain` (string-prefix check,
  safe because the anchor was already host-parsed). Confirmed against
  `tests/test_live_transport_fable51.py::test_another_host_or_product_is_refused_per_call`
  (parametrized over another DC, Books, Inventory).

- **Sliding-minute budget arithmetic is correct within one transport
  instance.** `_spend_one` pops entries older than 60.0s before checking
  `len(self._calls) >= ceiling`, so it never admits more than `ceiling`
  calls in any strict 60s window. Verified against
  `test_the_hundred_and_first_call_in_a_minute_is_refused_not_queued`.
  (See P2 above for the separate issue that this budget doesn't persist
  across HTTP requests.)

- **Single 401 re-mint loop terminates.** `_send`'s `retried` flag gates the
  one-shot re-mint; a second 401 falls through to the `status >= 400` branch
  and raises `ZohoApiError` rather than recursing again. Verified by
  `test_one_401_mints_once_and_retries_once_never_loops`, which asserts
  exactly 2 token mints on both the recover-on-retry and fail-twice cases.

- **`integrations.py` LIVE_READ routes: no organisation leak.**
  `list_organisations` fetches the full organisation list the credential
  can see (confirmed the transport does return all of them — the demo
  credential genuinely sees 21 other orgs per project memory) but filters
  to `pinned = [o for o in rows if organization_id == connection[...]]`
  before building the response; only the pinned organisation's fields are
  returned, others are reduced to a bare count
  (`other_organisations_visible`). Verified against
  `tests/test_integrations_live_routes_fable51.py::test_organisations_names_only_the_pinned_one_and_counts_the_rest`,
  which asserts `"OTHER CLIENT" not in r.text` against the full raw
  response body — a mutation-resistant assertion that would catch an
  accidental full-list echo, not just a targeted field check.

- **Existence-oracle rule (404 for both no-row and out-of-scope) holds.**
  `_require_visible_connection` (`integrations.py:754-770`) is called
  first, before any transport/adapter work, on every one of
  `list_organisations`, `validate_connection`, `list_scopes`,
  `begin_authorisation`, `map_organisation`, and the separate
  `integrations_live.py` sweep route (its own docstring states the refusal
  order explicitly: connection visibility is refusal #2, before the
  live-mode check or transport construction). A caller who cannot see a
  connection_id gets the identical 404 whether the row exists in another
  entity's estate or doesn't exist at all.

- **MOCK/SANDBOX connections never reach the network.** `_live_transport_for`
  returns `None` unless `product == "ERP"`, `mode` in `("LIVE_READ",
  "LIVE_WRITE")`, and `dc == "IN"` (case-normalized). Verified against
  `test_a_live_books_connection_gets_no_transport` and
  `test_a_non_live_connection_keeps_every_refusal`.

- **`live_sweep.py`: connection-bound claim is real, not cosmetic.**
  `_bind_claim_to_connection` inserts `AND connection_id = %(connection_id)s`
  into the `claimable` CTE's `WHERE kind = %(kind)s` clause (verified by
  reading `jobs.CLAIM_JOB_SQL` at `app/backend/integration/jobs.py:725-754`
  — the anchor sits inside the CTE that feeds `FOR UPDATE SKIP LOCKED`, so
  the connection filter narrows the *candidate set before locking*, not
  just the final row). A defensive assertion (`statement.count(anchor) != 1`)
  fails the whole module at import time if the framework's SQL shape
  changes underneath it. Cross-connection isolation is further backed by
  `known_purchase_order`/`open_purchase_orders` both filtering on
  `entity_id = self.entity_id` (the connection's own entity) in addition to
  `{scope}` — scope can only narrow, never widen, so no cross-entity read is
  possible through this path.

- **20s soft deadline is honoured.** `run_inbound_sweep`'s per-module loop
  computes `remaining = int(deadline_at - clock.now().timestamp())`, skips
  the module if `remaining <= 0`, and passes
  `min(remaining, jobs.SOFT_DEADLINE_SECONDS)` into `jobs.run_job` — the
  budget shrinks correctly across modules within one call.

- **No non-GET request is possible from `live_sweep.py`.** Every sweep
  function (`poll_contacts`/`poll_items`/`poll_purchaseorders`/`poll_bills`/
  `SweepPoAnchored`) calls only `adapter.list_*`, `get_purchase_order`, or
  `receives_for_po`, all of which route through `ErpAdapter._get` (GET
  only). Verified against the live pg test's own assertion
  `assert fake.non_get == []`.

- **Unsanctioned-commitment control (`_custom_field`, item 4) — sound for
  the cases actually tested, one theoretical gap noted.**
  `_custom_field` (`erp.py:933-952`) correctly treats a **blank value**
  as absent in all three shapes (top-level, `custom_field_hash`, and the
  `custom_fields` list — the last via `_opt_str`, which maps `""` to
  `None`), so "key present but blank" does **not** create a false negative
  (a blank field asserts no claim in the first place, so there is nothing
  to under-detect). I confirmed the inbox idempotency key
  (`uq_integration_inbox_idempotency UNIQUE (connection_id, module,
  external_id, payload_sha)`, `migrations/pg/010_integration.sql:520-521`)
  includes `payload_sha`, computed over the **raw, unredacted** payload
  (`record_inbound`'s docstring: "hashed BEFORE it is redacted") — so a
  tenant editing a PO after first sight (e.g. adding `cf_capex_ref` later)
  produces a new inbox row and `_accept` returns `inserted=True` again,
  correctly re-arming the `PollPurchaseOrders._accept` check
  (`sweeps.py:840-857`) rather than silently missing the addition forever.
  One theoretical, unproven gap: `_custom_field`'s top-level and
  `custom_field_hash` branches do exact, case-sensitive key lookups
  (`row.get(api_name)`), so a hypothetical differently-cased key from Zoho
  would be missed — I found no evidence this occurs in practice (the
  module's own "VERIFIED LIVE" comment describes testing against the real
  DEMO WBS tenant) and no way for a tenant user to change a custom field's
  `api_name` casing through normal UI use, so this is noted but not raised
  as an exploitable finding. A second, narrower hygiene note: the
  `custom_fields`-list branch (`erp.py:949-951`) assumes a list of dict-like
  objects and would raise `AttributeError` rather than degrade to `None` if
  Zoho ever returned that field as a non-list shape — a crash, not a silent
  bypass, and not something I could confirm as a live Zoho behaviour.

- **Money: no float in the diff's touched files.** `grep -n "float("` across
  `live_transport.py`, `integrations.py`, `live_sweep.py`,
  `integrations_live.py`, `erp.py`, `sweeps.py`, `uat_main.py`,
  `build_uat_bundle.py` returns nothing. `LiveTransport._send` parses wire
  JSON with `parse_float=str` specifically so money never becomes a Python
  float (verified against
  `test_a_decimal_in_the_wire_body_arrives_as_a_string_never_a_float`).
  Currency handling in `erp.py`'s `_purchase_order`/`_bill` derives
  `minor_exponent` per-document from `minor_exponent_of(currency_code)`
  (erp.py:707, 738), not a hardcoded INR/paise exponent — so a JPY or KWD
  document is not booked at INR's 2-decimal face value. This fix predates
  `c51c2fe` (commit `5acb270`) and remains intact in the current code; no
  regression found in the reviewed range.

- **`build_uat_bundle.py` / `uat_main.py`, other than the sslmode gap
  above:** Stage A correctly strips `CAPEX_DB_URL`/`CAPEX_DB_HOST` from the
  process environment whenever it is not genuinely on Catalyst with a DB
  host/URL configured (`uat_main.py:101-103`), and `build_seed` pops the
  same two variables before invoking the seed migration
  (`build_uat_bundle.py:167-170`), so a developer's local Postgres export
  cannot leak into either the seed build or a local run. The archive
  assembly never references the ERP OAuth credential file or the Stage B
  `db.env`; `app-config.json.env_variables` is hardcoded to `{}` and
  `verify_archive` refuses a non-empty value as a second check. The CA
  bundle, if supplied, is verified to contain `BEGIN CERTIFICATE` and
  refused if it contains `PRIVATE KEY`.

---

## OVERALL VERDICT

The live-transport and LIVE_READ-route hardening in this range is careful
and, on the specific attacks the brief asked about, largely sound: no path
leaks the token/secret/refresh-token (proven by both code reading and a
transcript-scanning test), the write gate and India/ERP host pin resist the
base_url tricks tried, the 401 retry terminates, the organisation-leak and
existence-oracle rules hold under test, and the unsanctioned-commitment
custom-field control correctly treats blanks as absent and re-fires on a
later edit. The real, verified gaps are all on the **budget/reliability**
side of the picture rather than a credential leak or a scope escalation:
the receive-detail fetches inside `receives_for_po` spend real Zoho API
calls with zero budget authorization (a self-acknowledged, unfixed defect
that directly undermines the one constraint — ERP Standard's 2,000
calls/day — this whole architecture is built around); the persisted
per-connection rate budget and token cache are not actually shared across
the four read-only LIVE routes because a fresh `LiveTransport` is
constructed on every request; and a plain check-then-insert in the sweep's
job-row lookup has no database constraint stopping two concurrent sweep
calls from creating duplicate job rows against a schema that was otherwise
careful to use `FOR UPDATE SKIP LOCKED` everywhere else. None of these four
findings is an credential/authorization bypass; all four are places where
the system's own stated invariants ("budget consulted before, never after,"
"100/minute per organisation," "sslmode verify-full, never relaxed") are
weaker in practice than the comments and docstrings claim, in each case
because the enforcement point stops one edge short of where the invariant
actually needs to hold.
