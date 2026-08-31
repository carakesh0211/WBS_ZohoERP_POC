"""Proves the disposability guard itself (ADAPT-003).

``tests/conftest_pg.py::_assert_pg_disposable`` is the control that stops a
mis-set ``CAPEX_DB_URL`` - one that happens to point at a real, populated
database - from being dropped or rebuilt by the PostgreSQL fixture chain. It
is pure name-pattern logic and needs no live database, so this module does
not depend on ``pg_url`` and runs even when PostgreSQL is not configured.
"""

from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
#
# They deliberately do NOT live in a tests/pg/conftest.py: two conftest.py
# files in non-package directories both import under the bare module name
# `conftest`, and the subdirectory one shadows the root one -- which broke
# `from conftest import code_of, detail` in seven baseline test modules.
# Importing the fixtures by name into this module's namespace makes them
# available to pytest here, with no second conftest to collide.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import pytest

from conftest_pg import _assert_pg_disposable

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("name", [
    "capex_t1",
    "capex_t42",
    "capex_t123456789",
    "capex_t00042",
    "capex_tmpl_",
    "capex_tmpl_abc123",
    "capex_tmpl_" + "f" * 32,
])
def test_disposable_names_are_accepted(name):
    assert _assert_pg_disposable(name) == name


@pytest.mark.parametrize("name", [
    "capex",                 # the real database name a mis-set URL could carry
    "postgres",               # the maintenance database
    "capex_production",
    "capex_prod",
    "capex_t",                 # no digits - does not match ^capex_t\d+$
    "capex_test",              # SQLite's disposable pattern, not this one
    "capex_t1x",               # trailing non-digit
    "capex_t-1",
    "capex_tmpl",               # missing the trailing underscore
    "CAPEX_T1",                  # case matters - the regex is anchored and case-sensitive
    "capex_t1; DROP DATABASE capex",  # defence in depth against a crafted name
    "",
])
def test_non_disposable_names_are_refused(name):
    with pytest.raises(RuntimeError, match="Refusing to operate"):
        _assert_pg_disposable(name)


def test_refusal_names_the_offending_database():
    with pytest.raises(RuntimeError, match="capex_production"):
        _assert_pg_disposable("capex_production")
