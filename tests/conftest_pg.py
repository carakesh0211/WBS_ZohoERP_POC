"""Disposable-database fixtures for the PostgreSQL integration suite.

Mirrors the guarantees ``tests/conftest.py`` gives the SQLite suite, adapted to
PostgreSQL (ADAPT-003 in ``tests/ADAPTATIONS.md``):

    * one **template** database is built once per session, fully migrated with
      the product's own runner (``app.backend.pg.migrate_pg.upgrade``);
    * each test then gets its own database, ``CREATE DATABASE ... TEMPLATE``
      the session template - the PostgreSQL analogue of the SQLite suite's
      byte-copy-per-test.

There is no local PostgreSQL on the dev machine, so every fixture chain here
roots in :func:`pg_url`, which **skips the whole suite cleanly** - never
fails - when ``CAPEX_DB_URL`` is unset. CI supplies it from the ``postgres:16``
service container configured in ``.github/workflows/ci.yml``.

This file is deliberately named ``conftest_pg.py``, not ``conftest.py``: it is
not auto-discovered by pytest. ``tests/pg/conftest.py`` re-exports the fixtures
below into the collection tree pytest actually walks, and registers the
``pg`` marker.

Guard, mirrored from ``tests/conftest.py::_assert_disposable``
----------------------------------------------------------------
:func:`_assert_pg_disposable` refuses to operate on any database whose NAME
does not match ``^capex_t\\d+$`` (a per-test database) or ``^capex_tmpl_``
(a session template). This is the control that stops a mis-set
``CAPEX_DB_URL`` - one that happens to point at a real, populated database -
from being dropped or rebuilt by this fixture chain. It is asserted directly
by ``tests/pg/test_fixture_guard.py``.
"""
from __future__ import annotations

import itertools
import os
import re
import urllib.parse
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import sys  # noqa: E402

if str(PROJECT_ROOT) not in sys.path:                    # allow `import app.backend...`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend.pg import config as pg_config            # noqa: E402
from app.backend.pg import engine as pg_engine             # noqa: E402
from app.backend.pg import migrate_pg                       # noqa: E402

# ======================================================================== guard
_PER_TEST_DB_RE = re.compile(r"^capex_t\d+$")
_TEMPLATE_DB_RE = re.compile(r"^capex_tmpl_")


def _assert_pg_disposable(name: str) -> str:
    """Refuse to operate on any database whose name is not provably disposable.

    The PostgreSQL analogue of ``tests/conftest.py::_assert_disposable``: that
    function checks a SQLite file PATH never resolves into the application
    data directory; this one checks a PostgreSQL database NAME never falls
    outside the two patterns this fixture chain is allowed to create, template
    from, or drop (ADAPT-003).
    """
    if not (_PER_TEST_DB_RE.match(name) or _TEMPLATE_DB_RE.match(name)):
        raise RuntimeError(
            f"Refusing to operate on database {name!r}: PostgreSQL test "
            f"fixtures only ever create, template from, or drop databases "
            f"matching ^capex_t\\d+$ (per test) or ^capex_tmpl_ (session "
            f"template). This guard exists so a mis-set CAPEX_DB_URL can never "
            f"cause a real database to be dropped or rebuilt.")
    return name


def _replace_dbname(url: str, dbname: str) -> str:
    """`url` re-pointed at a different database name; host, port and
    credentials are untouched."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parsed._replace(path="/" + dbname))


_db_name_counter = itertools.count(1)


def _next_test_db_name() -> str:
    """A fresh, guaranteed-disposable per-test database name.

    All-digits after the fixed prefix, matching ``_PER_TEST_DB_RE`` exactly:
    the process id keeps distinct test runs on the same host from colliding,
    the counter keeps two databases within one run from colliding.
    """
    return _assert_pg_disposable(f"capex_t{os.getpid()}{next(_db_name_counter):05d}")


def _config_and_provider(url: str, dbname: str) -> tuple[pg_config.DatabaseConfig, pg_config.SecretProvider]:
    """Build a ``DatabaseConfig``/``SecretProvider`` pair for `dbname`, from the
    host/credentials in `url`.

    Deliberately independent of ``app.backend.pg.config.from_env``/``_from_url``:
    those install their result into the module-level secret provider singleton,
    which would leak between tests. Every disposable database here gets its own
    provider instance instead.
    """
    parsed = urllib.parse.urlsplit(url)
    secret_name = "TEST_CAPEX_DB_PASSWORD"
    provider = pg_config.MappingSecretProvider(
        {secret_name: urllib.parse.unquote(parsed.password or "")})
    cfg = pg_config.DatabaseConfig(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        database=dbname,
        user=urllib.parse.unquote(parsed.username or "capex_app"),
        sslmode=os.environ.get("CAPEX_DB_SSLMODE", "prefer"),
        password_secret_name=secret_name,
        application_name="capex-pg-test-suite",
    )
    return cfg, provider


# ======================================================================= fixtures
@pytest.fixture(scope="session")
def pg_url() -> str:
    """The ``CAPEX_DB_URL`` the PostgreSQL suite should connect through.

    Skips the whole suite cleanly - never fails - when unset. There is no
    local PostgreSQL on the dev machine; CI's ``pg_tests`` job provides a
    ``postgres:16`` service container and sets this.
    """
    url = os.environ.get("CAPEX_DB_URL")
    if not url:
        pytest.skip(
            "CAPEX_DB_URL is not set: no PostgreSQL instance is configured for "
            "this run. Expected locally (no PostgreSQL on the dev machine); "
            "CI's `pg_tests` job provides a postgres:16 service container and "
            "sets CAPEX_DB_URL. Skipping the PostgreSQL integration suite.")
    return url


@pytest.fixture(scope="session")
def pg_admin_connection(pg_url):
    """Autocommit connection to the maintenance database.

    ``CREATE DATABASE`` / ``DROP DATABASE`` cannot run inside a transaction
    block, so this connection is never used for anything else.
    """
    conn = psycopg.connect(pg_url, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def pg_template(pg_url, pg_admin_connection) -> str:
    """One template database, migrated once with the product's own runner,
    shared read-only for the whole session.

    Every per-test database (:func:`pg_disposable_db_name`) is
    ``CREATE DATABASE ... TEMPLATE`` this one - the PostgreSQL analogue of the
    SQLite suite's template-database-per-session, byte-copy-per-test
    guarantee (ADAPT-003).
    """
    name = _assert_pg_disposable(f"capex_tmpl_{uuid.uuid4().hex[:16]}")
    pg_admin_connection.execute(
        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(_replace_dbname(pg_url, name), autocommit=False) as con:
            migrate_pg.upgrade(con)
            con.commit()
        yield name
    finally:
        pg_admin_connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(_assert_pg_disposable(name))))


@pytest.fixture()
def pg_disposable_db_name(pg_url, pg_template, pg_admin_connection) -> str:
    """A fresh, fully migrated, per-test database name. Dropped after the test,
    unconditionally - including on failure.

    A separate fixture from :func:`pg_connection`/:func:`pg_database` (which
    both depend on it and therefore share the same underlying database) so a
    test that needs more than one connection to the same disposable database -
    a concurrency test, for instance - can open them itself.
    """
    name = _next_test_db_name()
    pg_admin_connection.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(pg_template)))
    try:
        yield name
    finally:
        pg_admin_connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(_assert_pg_disposable(name))))


@pytest.fixture()
def pg_connection(pg_url, pg_disposable_db_name):
    """A raw psycopg connection to a disposable, per-test database."""
    conn = psycopg.connect(_replace_dbname(pg_url, pg_disposable_db_name), autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def pg_database(pg_url, pg_disposable_db_name):
    """A ``Database`` (pool) instance pointed at a disposable, per-test
    database - the same one :func:`pg_connection` would use, when both are
    requested by the same test."""
    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    database = pg_engine.Database(cfg, secret_provider=provider, min_size=1, max_size=5)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture()
def pg_scope() -> pg_engine.Scope:
    """A ``Scope`` for a single, deliberately RESTRICTED test user - not
    ``read_all`` - so scope-leakage and row-level-scope tests have something
    meaningful to assert against."""
    return pg_engine.Scope(
        user_id="U-PG-TEST",
        principal_kind="USER",
        entity_ids=frozenset({"ENT-TEST-A"}),
        plant_ids=frozenset({"PL-TEST-A"}),
        project_ids=None,
        location_ids=None,
        read_all=False,
    )
