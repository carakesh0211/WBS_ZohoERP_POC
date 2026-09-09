"""011's half of the `rls.py` registry handoff, which nothing enumerated.

`app.backend.pg.scope_inventory.Status` has a third value,
``"protected_pending_registry"``: the migration DOES enable, force and policy
the table, but `app.backend.pg.rls`'s registry -- lead-owned, and frozen for
whole waves at a time -- does not yet name it. Recording that in-between state
is honest, and its own docstring explains why neither of the other two labels
would be true.

WHAT THE STATUS ALSO DOES is remove the table from every check in
`tests/test_pg_rls_coverage.py`. The covered-vs-migration sweep skips it, the
still-a-gap sweep skips it, and
``set(covered_tables()) == set(rls.ALL_RLS_TABLES)`` -- the equality CI asserts
in both directions -- holds over it vacuously from both sides at once. A table
parked here is, in coverage terms, invisible.

Two migrations wrote themselves a test against exactly that:

  * 008 -- `test_pg_approval_schema.py::test_the_pending_registry_handoff_is_enumerable`
  * 010 -- `test_pg_integration_schema.py::test_the_rls_handoff_for_this_migration_is_enumerable`

Two did not. `018_export_jobs.sql` parked `export_job` and `export_job_chunk`
with no handoff test at all, and they stayed invisible for a whole wave --
deleting `ALTER TABLE export_job FORCE ROW LEVEL SECURITY` from 018 failed one
string grep in `tests/test_pg_exports.py` and nothing else, while in production
the migration-running identity would read every requester's captured
`scope_json` and every rendered CSV chunk with no policy, no error and no log
line. Those two are now `covered` and named by `rls.RLS_EXPORT_TABLE_COLUMNS`.

`011_reconciliation_exception.sql` is the one that remains, and this file is
its missing handoff test. It is a separate module rather than a block inside
`test_pg_rls_coverage.py` deliberately: the generic guard there
(`test_every_pending_registry_table_is_named_by_a_handoff_test`) excludes its
own file from the scan, because the first version did not and passed on a
deliberately parked `export_job` purely because that file's prose names it. A
guard its own explanatory docstrings can satisfy is not a guard.

Runs with no database, on the inventory alone.
"""
from __future__ import annotations

from app.backend.pg import rls, scope_inventory

#: The migration whose handoff this file owns.
RECONCILIATION_MIGRATION = "011_reconciliation_exception.sql"


def test_the_pending_registry_handoff_for_011_is_enumerable():
    """011's `reconciliation_exception` is protected by 011 and named by
    nothing in `rls.py`'s registry.

    When `rls.py` gains it, `pending` stops containing it, this test fails,
    and the inventory entry must be reclassified to ``status="covered"`` in
    the same commit -- at which point
    `test_rls_registry_and_the_independent_inventory_name_the_same_tables`
    keeps holding and this file can be deleted. That is the same contract
    008's and 010's handoff tests state for themselves.
    """
    pending = set(scope_inventory.pending_registry_tables())
    mine = set(scope_inventory.tables_for_migration(RECONCILIATION_MIGRATION))

    assert mine == {"reconciliation_exception"}, (
        f"the inventory no longer attributes exactly one table to "
        f"{RECONCILIATION_MIGRATION}: {sorted(mine)}")
    assert mine <= pending, (
        f"{sorted(mine - pending)} is no longer awaiting the rls.py registry. "
        f"If it has been added there, reclassify the scope_inventory entry to "
        f"status='covered' in the same commit and delete this file.")
    assert not (mine & set(rls.ALL_RLS_TABLES)), (
        "reconciliation_exception is in rls.ALL_RLS_TABLES while the inventory "
        "still calls it pending; the two halves have drifted apart, which is "
        "the state the third status exists to make impossible.")


def test_the_export_tables_are_no_longer_awaiting_the_registry():
    """The regression this whole file was written beside.

    `export_job` and `export_job_chunk` must never return to the pending
    status without a handoff test of their own -- which is what
    `test_pg_rls_coverage.py::test_every_pending_registry_table_is_named_by_a_handoff_test`
    would then demand. Asserted positively here so the reason 018 is absent
    from this module is on record rather than inferred from its absence.
    """
    pending = set(scope_inventory.pending_registry_tables())
    exports = set(scope_inventory.tables_for_migration("018_export_jobs.sql"))
    assert exports == {"export_job", "export_job_chunk"}, sorted(exports)
    assert not (exports & pending), (
        f"{sorted(exports & pending)} went back to protected_pending_registry; "
        f"that status excludes a table from every coverage check in "
        f"test_pg_rls_coverage.py")
    assert exports <= set(rls.ALL_RLS_TABLES)
    for table in sorted(exports):
        assert rls.RLS_MIGRATION_BY_TABLE[table] == "018_export_jobs.sql"
