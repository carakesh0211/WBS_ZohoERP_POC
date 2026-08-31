"""Auto-loaded pytest conftest for the PostgreSQL integration suite.

``tests/conftest_pg.py`` is deliberately not named ``conftest.py`` - see its
module docstring - so it is not picked up by pytest's automatic conftest
discovery. This file is, and does two things:

    1. registers the ``pg`` marker (``pytest.ini`` is owned by another agent,
       so registration happens here via ``pytest_configure`` instead);
    2. re-exports the fixtures from ``tests/conftest_pg.py`` by importing them
       into this module's namespace, which is how every test under
       ``tests/pg/`` can request ``pg_url``, ``pg_template``, ``pg_connection``,
       ``pg_database`` and ``pg_scope`` by name.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection,
    pg_connection,
    pg_database,
    pg_disposable_db_name,
    pg_scope,
    pg_template,
    pg_url,
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "pg: PostgreSQL integration test - needs CAPEX_DB_URL, skips cleanly without it",
    )
