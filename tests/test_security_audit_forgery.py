"""An append-only log defends against edits. The attack is an INSERT.

`audit_log`'s triggers block UPDATE and DELETE (`002_financial_controls.sql`),
so a tamperer does not edit a row — they add one. `verify_audit_chain` skipped
every row whose `entry_hash` was NULL, on the reasoning that pre-migration rows
carry none. True, and unbounded: a row inserted TODAY with no hash was skipped
on the same grounds, not chained over, and reported intact.

An adversarial review demonstrated it over HTTP — a fabricated `PR_APPROVED`
inserted by hand left `GET /api/audit/verify` returning
`{"entries":4,"broken":[],"intact":true}` with the forged row sitting in the
trail. Nothing in the suite covered it: `tests/test_audit.py` asserts UPDATE
and DELETE tampering only, which the triggers already prevent.

The repair is a watermark derived from the data — the id below the first hashed
row. Below it, an unhashed row is history. Above it, an unhashed row was
written outside `audit()` and is a break.

These live in their own file rather than in `tests/test_audit.py`, which is
inside the 220-function baseline the manifest holds fixed.
"""
import pytest

from app.backend import services


def _forge(con, actor="U-ATTACKER", object_id="PR-FORGED"):
    """The attack, verbatim: a plain INSERT with no `prev_hash`, no `entry_hash`."""
    con.execute(
        "INSERT INTO audit_log (at,actor,action,object_type,object_id,detail)"
        " VALUES ('2020-01-01T00:00:00Z',?,'PR_APPROVED','PurchaseRequest',?,"
        "         'fabricated approval')",
        (actor, object_id))
    con.commit()


def test_an_inserted_unhashed_row_is_reported_as_a_break(raw_con):
    """The finding itself."""
    services.audit(raw_con, "U-ADM", "PR_APPROVED", "PurchaseRequest",
                   "PR-REAL", "genuine")
    raw_con.commit()
    assert services.verify_audit_chain(raw_con)["intact"] is True

    _forge(raw_con)
    result = services.verify_audit_chain(raw_con)
    assert result["intact"] is False, (
        "a row inserted with no entry_hash was reported as intact -- the "
        "append-only triggers block UPDATE and DELETE, so INSERT is the whole "
        "attack surface")
    assert result["broken"], "the forged row was not named"


def test_the_forged_row_is_named_not_merely_counted(raw_con):
    """An auditor needs the id, not a boolean."""
    services.audit(raw_con, "U-ADM", "PR_APPROVED", "PurchaseRequest",
                   "PR-REAL", "genuine")
    raw_con.commit()
    before = {r["audit_id"] for r in
              raw_con.execute("SELECT audit_id FROM audit_log")}
    _forge(raw_con)
    after = {r["audit_id"] for r in
             raw_con.execute("SELECT audit_id FROM audit_log")}
    forged_id = (after - before).pop()

    assert forged_id in services.verify_audit_chain(raw_con)["broken"]


def test_genuine_pre_migration_rows_are_still_exempt(raw_con):
    """The skip existed for a real reason and must survive.

    A seeded database legitimately holds unhashed rows. Failing them would make
    every install report broken, which is the failure mode that would get this
    check switched off.
    """
    result = services.verify_audit_chain(raw_con)
    assert result["intact"] is True, (
        f"a freshly seeded database reports tampering: {result}")


def test_a_wholly_legacy_log_is_not_reported_as_wholly_forged(raw_con):
    """The case the first version of this fix got backwards.

    With NO hashed row anywhere, the whole log is pre-migration and every row
    is legitimately unhashed. Returning a watermark of -1 there marks every row
    a break — which is exactly what happened, and `tests/test_audit.py` caught
    it within a minute.
    """
    rows = raw_con.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entry_hash IS NOT NULL"
    ).fetchone()["n"]
    if rows:
        pytest.skip("the seeded log already contains a hashed row")
    result = services.verify_audit_chain(raw_con)
    assert result["intact"] is True, (
        f"a wholly pre-migration log was reported as tampered: {result}")


def test_the_watermark_moves_with_the_first_hashed_row(raw_con):
    """Derived from the data, so no migration and no backfill is needed."""
    before = services._unhashed_watermark(raw_con)
    services.audit(raw_con, "U-ADM", "PR_APPROVED", "PurchaseRequest",
                   "PR-1", "genuine")
    raw_con.commit()
    after = services._unhashed_watermark(raw_con)
    assert after <= before or before == -1, (
        f"the watermark rose after a hashed row was appended "
        f"({before} -> {after}); it must only ever pin the legacy boundary")

    # And a second genuine entry must not move it again.
    services.audit(raw_con, "U-ADM", "PR_APPROVED", "PurchaseRequest",
                   "PR-2", "genuine")
    raw_con.commit()
    assert services._unhashed_watermark(raw_con) == after


def test_verify_reports_where_verifiable_history_begins(raw_con):
    """The unhashed rows are reported, not silently skipped.

    Migration 017.2 of the plan requires the unhashed-row count to reach the
    operator: an auditor is entitled to know where verifiable history starts
    rather than being told a number that quietly excludes some rows.
    """
    result = services.verify_audit_chain(raw_con)
    assert "unhashed_legacy_rows" in result
    assert "unhashed_watermark" in result


def test_the_connector_no_longer_writes_an_unhashed_row(raw_con):
    """`zoho.authorise` bypassed `audit()` with its own INSERT.

    That produced a row the verifier skipped AND hard-coded 'U-ADM' as the
    actor while the route passed the real session identity — so with two
    administrators every OAuth authorisation was attributed to the wrong
    person. Routing it through `audit()` fixes both.
    """
    from app.backend import zoho

    row = raw_con.execute(
        "SELECT connection_id FROM zoho_connection LIMIT 1").fetchone()
    if row is None:
        pytest.skip("the seeded estate has no Zoho connection")

    zoho.authorise(raw_con, row["connection_id"], actor="U-SOMEONE-ELSE")
    entry = raw_con.execute(
        "SELECT actor, entry_hash FROM audit_log"
        " WHERE action = 'OAUTH_AUTHORISED' ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()

    assert entry is not None, "authorise wrote no audit entry"
    assert entry["entry_hash"] is not None, (
        "the connector still writes an unhashed row, which the verifier would "
        "skip and a forger could hide behind")
    assert entry["actor"] == "U-SOMEONE-ELSE", (
        f"the audit entry names {entry['actor']}, not the acting session "
        "identity; a hard-coded actor misattributes every authorisation")
    assert services.verify_audit_chain(raw_con)["intact"] is True
