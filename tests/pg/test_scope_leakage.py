"""Row-level scope must never survive a pooled connection's return to the pool.

``app/backend/pg/engine.py`` states the guarantee directly in its module
docstring: scope is applied with ``SET LOCAL``, which is transaction-scoped
and reverts on commit or rollback. A plain ``SET`` would survive the
connection's return to the pool and leak one user's scope into the next
request's - "the single most dangerous mistake this layer can make."

This is the highest-value test in the PostgreSQL suite: a single missed
``LOCAL`` here is a cross-tenant data leak in a financial-controls product,
not a cosmetic bug.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest_pg import PROJECT_ROOT, _config_and_provider

pytestmark = pytest.mark.pg


# ------------------------------------------------------------- source-level guard
def test_no_bare_set_statement_in_pg_backend():
    """Every ``SET`` issued by ``app/backend/pg/`` must be ``SET LOCAL``.

    A regex over the SQL string literals themselves, not just a reading of
    engine.py today: this fires the moment a future edit (in ``config.py``,
    ``migrate_pg.py``, or a new module) introduces a bare ``SET`` anywhere in
    the package, before it ever reaches a running pool.

    Anchored on a quote character immediately followed by ``SET`` so prose
    like "Plain SET would leak..." in a docstring does not trip it - only the
    first token of an actual SQL string literal counts.
    """
    pg_dir = PROJECT_ROOT / "app" / "backend" / "pg"
    bare_set = re.compile(r"""(['"])SET(?!\s+LOCAL\b)\s""")

    offenders: list[str] = []
    for path in sorted(pg_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if bare_set.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()}")

    assert offenders == [], (
        "found a non-LOCAL SET statement in app/backend/pg/ - SET LOCAL is "
        "transaction-scoped and reverts on commit/rollback; a plain SET "
        "survives the connection's return to the pool and leaks scope into "
        "the next request:\n" + "\n".join(offenders)
    )


def test_scope_settings_are_always_applied_via_set_local():
    """Positive companion to the regex guard: the actual statement template
    ``Database._apply_scope`` builds is asserted directly, not just grepped
    for in the source text."""
    import inspect

    from app.backend.pg import engine as pg_engine

    source = inspect.getsource(pg_engine.Database._apply_scope)
    assert '"SET LOCAL {} = {}"' in source or "'SET LOCAL {} = {}'" in source, (
        "Database._apply_scope no longer builds its statement from a "
        "'SET LOCAL {} = {}' template - re-verify it still issues SET LOCAL, "
        "not a plain SET, for every scope key")


# --------------------------------------------------------------- low-level proof
def test_scope_does_not_leak_across_pool_reuse(pg_url, pg_disposable_db_name):
    """Reproduces the exact failure mode the guard exists to prevent, one
    layer below the public API.

    A pool of size 1 forces the second ``.connection()`` acquisition to reuse
    the SAME physical backend the first one used. If scope were applied with
    a plain ``SET`` instead of ``SET LOCAL``, ``capex.entity_ids`` would still
    read ``ENT-LEAK-A`` here, before any scope for the new borrower has been
    applied.
    """
    from app.backend.pg import engine as pg_engine

    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    database = pg_engine.Database(cfg, secret_provider=provider, min_size=1, max_size=1)
    try:
        scope_a = pg_engine.Scope(user_id="user-a", entity_ids=frozenset({"ENT-LEAK-A"}))

        with database._pool.connection() as con:
            database._apply_scope(con, scope_a)
            applied = con.execute(
                "SELECT current_setting('capex.entity_ids', true)").fetchone()[0]
            assert applied == "ENT-LEAK-A", "SET LOCAL did not even apply within its own transaction"
            con.commit()  # SET LOCAL's transaction ends here - the setting must revert

        # Same connection comes back out of a size-1 pool. Read the setting
        # BEFORE applying any new scope: this is the moment a plain SET would
        # still be showing user A's data.
        with database._pool.connection() as con2:
            leaked = con2.execute(
                "SELECT current_setting('capex.entity_ids', true)").fetchone()[0]
            assert leaked == "", (
                f"scope leaked across pool reuse: capex.entity_ids={leaked!r} was "
                "still set on a reused connection before any new scope was "
                "applied. SET LOCAL must revert on commit; a plain SET would not.")
            con2.rollback()
    finally:
        database.close()


# -------------------------------------------------------------- public API proof
def test_session_scope_does_not_leak_to_the_next_session(pg_url, pg_disposable_db_name):
    """The same guarantee, exercised entirely through the public
    ``Database.session`` context manager rather than pool internals."""
    from app.backend.pg import engine as pg_engine

    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    database = pg_engine.Database(cfg, secret_provider=provider, min_size=1, max_size=1)
    try:
        scope_a = pg_engine.Scope(
            user_id="user-a", entity_ids=frozenset({"ENT-SESSION-A"}))
        scope_b = pg_engine.Scope(user_id="user-b", entity_ids=frozenset({"ENT-SESSION-B"}))

        with database.session(scope_a) as session_a:
            assert session_a.current_setting("capex.entity_ids") == "ENT-SESSION-A"
            assert session_a.current_setting("capex.user_id") == "user-a"

        # A fresh session, same pooled connection (size 1). It must see ONLY
        # user B's scope - never a trace of A's, and never a leaked user_id.
        with database.session(scope_b) as session_b:
            assert session_b.current_setting("capex.entity_ids") == "ENT-SESSION-B"
            assert session_b.current_setting("capex.user_id") == "user-b"
            assert "ENT-SESSION-A" not in session_b.current_setting("capex.entity_ids")
    finally:
        database.close()


def test_session_rollback_also_clears_scope(pg_url, pg_disposable_db_name):
    """A session that raises must still leave no scope behind for the next
    borrower - the revert must not depend on the happy path."""
    from app.backend.pg import engine as pg_engine

    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    database = pg_engine.Database(cfg, secret_provider=provider, min_size=1, max_size=1)
    try:
        scope_a = pg_engine.Scope(
            user_id="user-a", entity_ids=frozenset({"ENT-ROLLBACK-A"}))

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom):
            with database.session(scope_a) as session_a:
                assert session_a.current_setting("capex.entity_ids") == "ENT-ROLLBACK-A"
                raise _Boom("simulated failure mid-transaction")

        with database._pool.connection() as con2:
            leaked = con2.execute(
                "SELECT current_setting('capex.entity_ids', true)").fetchone()[0]
            assert leaked == "", (
                f"a rolled-back session left scope behind: capex.entity_ids={leaked!r}")
            con2.rollback()
    finally:
        database.close()
