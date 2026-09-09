"""Secrets stay where they were put: not in a response, a log, or a traceback.

Three separate properties, each with its own failure mode:

1. **The provider is concurrency-safe.** `pg/config` resolves the database
   password through a module-level provider, and `_from_url` used to do a
   READ-MODIFY-WRITE of that global -- read the current provider, reach into
   its PRIVATE `_values`, build a merged mapping, install a new chain. Two
   callers interleaving between the read and the write lost one password; the
   reach-in silently produced `{}` for any provider that is not a
   `MappingSecretProvider` (which is every real deployment); and each call
   nested another chain link, so the lookup path grew without bound.

2. **A response never carries a secret, and never grows one by accident.**
   `GET /api/zoho/connections` was `SELECT *` over the one table in the schema
   whose columns are secret-SHAPED. The three `_ref` columns it publishes are
   pointers, not values -- but a column named `client_secret` added tomorrow
   would have been published by the same wildcard, with no code change and no
   review.

3. **An exception handler emits a message we wrote.** `str(exc)` on a product
   error is a sentence an author chose. `str(exc)` on a driver error is
   whatever libpq felt like saying, which can name the host, the user, the
   database and the statement.

Nothing here needs a database.
"""
from __future__ import annotations

import ast
import re
import threading
from pathlib import Path

import pytest

from app.backend.pg import config as pg_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "app" / "backend"


# ======================================================================
# 1. The secret provider under concurrency
# ======================================================================
@pytest.fixture(autouse=True)
def _restore_provider():
    """The provider is a module global. Leaving it changed would leak."""
    original = pg_config.get_secret_provider()
    yield
    pg_config.set_secret_provider(original)


def test_concurrent_url_configuration_loses_no_password():
    """The read-modify-write race, driven hard enough to lose one.

    Each thread configures from a URL carrying its own password under its own
    secret name, and every one of them must be resolvable afterwards. Before
    the lock, two threads interleaving between `get_secret_provider()` and
    `set_secret_provider()` produced a chain in which one of the two was
    simply absent.
    """
    pg_config.set_secret_provider(pg_config.MappingSecretProvider({"BASE": "base"}))

    passwords = {f"pw{i}": f"postgresql://u:pw{i}@h:5432/db" for i in range(24)}
    barrier = threading.Barrier(len(passwords))
    errors: list[BaseException] = []

    def configure(url: str):
        try:
            barrier.wait(timeout=10)
            pg_config._from_url(url)
        except BaseException as exc:            # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=configure, args=(url,))
               for url in passwords.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"configuring concurrently raised: {errors[:3]}"

    provider = pg_config.get_secret_provider()
    resolved = provider.get("CAPEX_DB_URL_PASSWORD")
    assert resolved in passwords, (
        "the password resolvable after 24 concurrent configurations is not any "
        f"of the ones configured: {resolved!r}")
    assert provider.get("BASE") == "base", (
        "installing a URL password destroyed the provider it was chained in "
        "front of. The fallback is what makes every OTHER secret still "
        "resolvable.")


def test_repeated_configuration_does_not_grow_an_unbounded_provider_chain():
    """Each call used to wrap the previous provider in another chain link.

    Fifty calls meant fifty levels of `get()` delegation for every secret
    lookup for the life of the process -- and fifty objects each holding a
    password.
    """
    pg_config.set_secret_provider(pg_config.MappingSecretProvider({"BASE": "base"}))
    for i in range(50):
        pg_config._from_url(f"postgresql://u:pw{i}@h:5432/db")

    depth, node = 0, pg_config.get_secret_provider()
    while isinstance(node, pg_config._ChainedProvider):
        depth += 1
        node = node.second
    assert depth <= 2, (
        f"the provider chain is {depth} links deep after 50 configurations. "
        "Each link holds a password and delays every lookup.")
    assert pg_config.get_secret_provider().get("CAPEX_DB_URL_PASSWORD") == "pw49"
    assert pg_config.get_secret_provider().get("BASE") == "base"


def test_installing_a_provider_discards_the_modules_own_overlay():
    """A deployment installing its provider must not be shadowed by ours.

    If the overlay survived, a rotated secret in the real provider would keep
    losing to the stale copy this module lifted out of a URL at start-up --
    the hardest possible failure to diagnose, because everything looks
    configured.
    """
    pg_config._from_url("postgresql://u:lifted@h:5432/db")
    assert pg_config.get_secret_provider().get("CAPEX_DB_URL_PASSWORD") == "lifted"

    pg_config.set_secret_provider(pg_config.MappingSecretProvider({"OTHER": "x"}))

    assert pg_config.get_secret_provider().get("CAPEX_DB_URL_PASSWORD") is None, (
        "a password lifted from a URL survived the installation of a new "
        "provider.")
    assert pg_config._module_overlay is None


def test_the_provider_globals_are_guarded_by_one_lock():
    """Structural: both globals move together, or the pairing is meaningless."""
    source = Path(pg_config.__file__).read_text(encoding="utf-8")
    for function in ("def set_secret_provider", "def get_secret_provider",
                     "def _install_secret"):
        body = source.split(function, 1)[1].split("\ndef ", 1)[0]
        assert "with _LOCK" in body, f"{function} does not take _LOCK"


def test_no_module_reaches_into_a_providers_private_values():
    """The reach-in was silent, not loud: it produced `{}` and carried on."""
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'getattr\([^)]*,\s*[\'"]_values[\'"]', text):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {match.group(0)}")
    assert not offenders, (
        "a module reads another object's private `_values`: " + "; ".join(offenders))


# ======================================================================
# 2. Responses
# ======================================================================
def test_the_connector_route_names_its_columns_instead_of_selecting_everything():
    """`SELECT *` over `zoho_connection` publishes whatever it grows next."""
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert "SELECT * FROM zoho_connection" not in source, (
        "GET /api/zoho/connections is selecting every column of the one table "
        "whose column names are secret-shaped. Name the columns, so adding one "
        "is a decision somebody makes.")
    assert "client_secret_ref" in source, (
        "precondition: the route still publishes the secret REFERENCES, which "
        "the legacy connector screen renders and test_connector.py pins.")


def test_the_connector_response_carries_references_and_never_values(admin):
    """Behavioural. Every credential-shaped field must be a pointer."""
    body = admin.get("/api/zoho/connections").json()
    assert body["connections"], "precondition: the demo dataset has a connection"

    for connection in body["connections"]:
        for key, value in connection.items():
            if not isinstance(value, str):
                continue
            if key.endswith("_ref"):
                assert value.startswith("secretref://"), (
                    f"{key} is not a secret reference: {value!r}")
                continue
            assert not re.fullmatch(r"[0-9a-f]{32,}", value), (
                f"{key} looks like a raw token: {value!r}")
            assert "Bearer " not in value and "oauthtoken" not in value.lower(), (
                f"{key} carries credential material: {value!r}")


def test_no_route_response_key_is_a_bare_secret_name():
    """The names, not the values: a key called `client_secret` is the defect.

    Scanned over the SQL the legacy module actually issues, so a column added
    to a SELECT list is caught even before any row carries a value.
    """
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    statements = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "SELECT" in node.value.upper()
    ]
    forbidden = re.compile(
        r"\b(client_secret|refresh_token|access_token|password|password_hash|"
        r"private_key|api_key)\b(?!_ref)", re.IGNORECASE)
    offenders = [s for s in statements if forbidden.search(s)]
    assert not offenders, (
        "a SELECT in main.py names a secret-bearing column: "
        + "; ".join(o.strip()[:120] for o in offenders))


# ======================================================================
# 3. Exception handlers
# ======================================================================
#: `ValueError` and friends are raised BY this codebase with authored
#: messages at every site that stringifies one. Everything else that is not a
#: class defined under `app/backend` carries text we do not control.
EXPLICITLY_ALLOWED = {"ValueError", "TypeError", "KeyError"}

#: Sites that stringify an exception this codebase did not author, WITH the
#: reason each is still open. Kept as data, generated against, and asserted in
#: BOTH directions below, so it cannot rot into a permanent excuse.
#:
#: None of these reaches an HTTP response by way of the handler itself -- they
#: write a database column or a scrubbed log line. Two of them (`jobs.py`,
#: `outbound.py`) write `integration_outbox.last_error` / the job's error,
#: which `GET /api/integrations/outbox` then SERVES UNSCRUBBED, and that is a
#: real finding this wave reports rather than patches: those modules belong to
#: the integration stream. The fix is one of `observability.scrub_text` on the
#: way in, or on the way out in `api/integrations.py`.
STRINGIFY_OPEN_FINDINGS = {
    "app/backend/integration/jobs.py": (
        "bare `except Exception` -> `error = f'{type(exc).__name__}: {exc}'`, "
        "stored on the job and logged. The log is scrubbed; the column is not. "
        "Owner: integration stream."),
    "app/backend/integration/outbound.py": (
        "three bare `except Exception` handlers writing "
        "`integration_outbox.last_error` from the raw exception text, which "
        "`api/integrations.py::_outbox_row` returns to the caller unscrubbed. "
        "Owner: integration stream."),
    "app/backend/pg/integration_store.py": (
        "`ZoneInfoNotFoundError` -> an authored IntegrationStoreError whose "
        "message interpolates the exception. The text is a timezone name the "
        "caller supplied; benign, and left alone rather than widened."),
    "app/backend/pg/migrate_pg.py": (
        "a duplicate-object error during ADOPTION, stringified into a "
        "MigrationError raised on a CLI path that never reaches a client."),
}


def _authored_exception_names() -> set[str]:
    """Every class defined under `app/backend`.

    Definition by DEFINITION SITE, not by name shape: `OperationalError` ends
    in "Error" and is psycopg's, which is exactly the class of message that
    must not reach a caller.
    """
    names: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    return names


def _stringified_exceptions(paths) -> dict[str, list[str]]:
    """{module: [handler, ...]} for `except X as e: ... str(e) ...`."""
    authored = _authored_exception_names()
    offenders: dict[str, list[str]] = {}
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for handler in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]:
            if handler.name is None:
                continue
            node_type = handler.type
            if node_type is None:
                caught = ["Exception"]
            else:
                parts = node_type.elts if isinstance(node_type, ast.Tuple) else [node_type]
                caught = [getattr(p, "attr", getattr(p, "id", "?")) for p in parts]
            if all(name in authored or name in EXPLICITLY_ALLOWED for name in caught):
                continue
            body = "\n".join(ast.unparse(node) for node in handler.body)
            name = re.escape(handler.name)
            if re.search(rf"\bstr\(\s*{name}\s*\)", body) \
                    or re.search(rf"\brepr\(\s*{name}\s*\)", body) \
                    or re.search(rf"\{{{name}\}}", body):
                key = path.relative_to(PROJECT_ROOT).as_posix()                     if PROJECT_ROOT in path.parents else path.as_posix()
                offenders.setdefault(key, []).append(
                    f"except {caught} -> {body[:160]}")
    return offenders


RESPONSE_BUILDING_MODULES = [BACKEND / "main.py"] + sorted((BACKEND / "api").glob("*.py"))


def test_no_response_builder_stringifies_an_exception_it_did_not_author():
    """A libpq message can echo the host, the user, the database and the SQL.

    Product errors carry a `.message` an author wrote and a code a client can
    branch on. Everything else must be redacted to a static sentence plus a
    correlation id, which is what `main.py`'s catch-all does.

    Scanned over the modules that BUILD RESPONSES, because that is where the
    property has to hold absolutely.
    """
    offenders = _stringified_exceptions(RESPONSE_BUILDING_MODULES)
    assert not offenders, (
        "a route handler stringifies an exception this codebase did not "
        f"author:\n{offenders}\nIf the text is genuinely ours, give the "
        "exception an authored type; if it is not, emit a static message and "
        "log the detail.")


def test_every_other_stringifying_handler_is_a_recorded_open_finding():
    """The rest of the backend, held to a register rather than to silence.

    Both directions. A NEW site fails because it is not registered; a
    registered site that has been FIXED fails because the register is now
    excusing something that no longer exists. Neither can rot.
    """
    offenders = _stringified_exceptions(BACKEND.rglob("*.py"))
    found = set(offenders)
    registered = set(STRINGIFY_OPEN_FINDINGS)

    assert found - registered == set(), (
        "a new site stringifies an unauthored exception and is not in "
        f"STRINGIFY_OPEN_FINDINGS: {sorted(found - registered)}")
    assert registered - found == set(), (
        "STRINGIFY_OPEN_FINDINGS names a site that no longer offends; delete "
        f"the entry: {sorted(registered - found)}")


def test_the_handler_scanner_catches_the_shape_it_is_written_against(tmp_path):
    """Mutation check: a scanner matching nothing would pass everything."""
    module = tmp_path / "leaky.py"
    module.write_text(
        "import psycopg\n"
        "def route():\n"
        "    try:\n"
        "        pass\n"
        "    except psycopg.OperationalError as exc:\n"
        "        return {'detail': str(exc)}\n",
        encoding="utf-8")

    offenders = _stringified_exceptions([module])
    assert offenders, (
        "the scanner did not flag a route stringifying a driver exception, so "
        "the two tests above prove nothing.")
    assert "OperationalError" not in _authored_exception_names(), (
        "psycopg's OperationalError is being treated as a class this codebase "
        "authored, which would exempt exactly the messages that matter.")


def test_the_unhandled_error_response_is_a_fixed_sentence(monkeypatch, requestor):
    """The catch-all must not describe what went wrong.

    A stack trace, a SQL fragment or a driver message in a 500 body is a free
    map of the system for anyone who can provoke one.
    """
    from app.backend import main

    def explode(*_args, **_kwargs):
        raise RuntimeError("secret-token-abcdef0123456789 SELECT * FROM app_credential")

    monkeypatch.setattr(main.services, "verify_audit_chain", explode, raising=False)
    monkeypatch.setattr(main.domain, "reconciliation", explode, raising=False)

    resp = requestor.get("/api/reconciliation")

    assert resp.status_code == 500, resp.text
    text = resp.text
    assert "secret-token-abcdef0123456789" not in text
    assert "SELECT" not in text.upper()
    assert "RuntimeError" not in text
    assert "Traceback" not in text
    assert resp.headers.get("X-Correlation-Id"), (
        "the response withholds the detail and offers no way to find it "
        "either; the correlation id is what makes redaction acceptable.")
