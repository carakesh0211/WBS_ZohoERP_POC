"""Bill lines cite receives (migration 033, Fable 5.1, 2026-09-13).

VERIFIED LIVE 2026-09-12 on DEMO WBS: once an order has a receive, a bill
against it must cite that receive line by line (`receive_item_id`). Held here
on the live schema:

* a bill line citing a mirrored receive line resolves to that `grn_line`
  ON THE SAME purchase-order line, and the citation is kept verbatim;
* a citation of a receive line the ledger does not hold (or none at all)
  still attributes the bill line to its PO line -- the citation refines, it
  never gates;
* a citation that names a receive line of a DIFFERENT PO line does not
  re-attribute anything: the PO line wins and no grn_line is linked;
* billing beyond the cited receive raises BILL_EXCEEDS_RECEIVE on the
  grn_line with the excess in `local_paise`, visible and unclamped; a credit
  note that brings the sum back within the receipt retracts it;
* the kind is in the CHECK (033) and in the module constant.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    ScopedRoleDatabase, pg_admin_connection, pg_app_database, pg_connection,
    pg_database, pg_disposable_db_name, pg_scope, pg_template, pg_url,
)
from test_pg_fable51_inbound_ledger import (  # noqa: E402
    ACTOR, ENTITY, HEAD, PG, PO_DATE, PO_INR, POL_INR, SOURCE_LABEL, T0, WBS_A,
    _receive, _seed, _session,
)

import pytest  # noqa: E402

from app.backend.integration import sweeps  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import procurement  # noqa: E402

pytestmark = [pytest.mark.pg, PG]

KIND = "BILL_EXCEEDS_RECEIVE"


@pytest.fixture()
def seeded(pg_connection):
    _seed(pg_connection)
    # A second INR line on the same order, so a citation can be tested
    # ACROSS lines: the receive on line 1, a bill citing it from line 2.
    pg_connection.execute(
        "INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id,"
        " budget_head_id, quantity, rate_paise, amount_paise, line_external_id,"
        " created_by, updated_by)"
        " VALUES ('POL-F51-INR-2', 'PO-F51-INR', 2, 'PRJ-F51', %s, %s, 1, 300000,"
        " 300000, 'ZPOL-F51-INR-2', 'T', 'T')", (WBS_A, HEAD))
    pg_connection.commit()
    return pg_connection


def _bill(con, *, external_id, lines, doc_type="BILL"):
    return procurement.mirror_bill(
        _session(con), external_source=SOURCE_LABEL, external_id=external_id,
        bill_number=f"BN-{external_id}", vendor_name="Vendor INR",
        bill_date=PO_DATE, po_external_id=PO_INR, lines=lines,
        doc_type=doc_type, actor=ACTOR)


def _line(paise, *, line_id, po_line="ZPOL-F51-INR", receive_line=None):
    row = {"line_total_paise": paise, "bill_line_external_id": line_id,
           "purchase_order_line_external_id": po_line, "quantity": "1"}
    if receive_line is not None:
        row["receive_line_external_id"] = receive_line
    return row


def _bill_line_link(con, external_line_id):
    return con.execute(
        "SELECT receive_line_external_id, grn_line_id, po_line_id FROM bill_line"
        " WHERE external_line_id = %s", (external_line_id,)).fetchone()


def _open(con, kind=KIND):
    return con.execute(
        "SELECT object_type, object_id, local_paise, status FROM"
        " reconciliation_exception WHERE kind = %s ORDER BY raised_at", (kind,)).fetchall()


def test_the_kind_is_in_the_check_and_in_the_constants(seeded):
    body = seeded.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conname = 'ck_reconciliation_exception_kind'").fetchone()[0]
    assert f"'{KIND}'" in body
    assert KIND in store.EXCEPTION_KINDS and KIND in sweeps.EXCEPTION_KINDS
    assert sweeps.KIND_BILL_EXCEEDS_RECEIVE == procurement.KIND_BILL_EXCEEDS_RECEIVE == KIND
    cols = {r[0] for r in seeded.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'bill_line'").fetchall()}
    assert {"receive_line_external_id", "grn_line_id"} <= cols


def test_a_bill_line_citing_a_mirrored_receive_line_links_to_it(seeded):
    _receive(seeded, po_line_id="POL-F51-INR", receive_id="RCV-1", line_id="RCVL-1", paise=600_000)
    seeded.commit()
    grn_line_id = seeded.execute(
        "SELECT grn_line_id FROM grn_line WHERE line_external_id = 'RCVL-1'").fetchone()[0]
    out = _bill(seeded, external_id="ZB-1",
                lines=[_line(400_000, line_id="BL-1", receive_line="RCVL-1")])
    seeded.commit()
    assert out["attributed"] == 1 and out["quarantined"] == 0
    assert _bill_line_link(seeded, "BL-1") == ("RCVL-1", grn_line_id, "POL-F51-INR")
    assert _open(seeded) == [], "within the receipt: nothing raised"


def test_a_citation_the_ledger_does_not_hold_or_none_at_all_still_posts_the_line(seeded):
    out = _bill(seeded, external_id="ZB-2",
                lines=[_line(100_000, line_id="BL-2", receive_line="RCVL-NOT-HERE"),
                       _line(100_000, line_id="BL-3")])
    seeded.commit()
    assert out["attributed"] == 2 and out["quarantined"] == 0
    assert _bill_line_link(seeded, "BL-2") == ("RCVL-NOT-HERE", None, "POL-F51-INR")
    assert _bill_line_link(seeded, "BL-3") == (None, None, "POL-F51-INR")
    assert _open(seeded) == []


def test_a_citation_of_another_lines_receive_never_re_attributes_the_bill_line(seeded):
    _receive(seeded, po_line_id="POL-F51-INR", receive_id="RCV-1", line_id="RCVL-1", paise=600_000)
    seeded.commit()
    # The bill line names PO line 2 but cites line 1's receive: the PO line
    # wins, the receive is not on that line, nothing links.
    out = _bill(seeded, external_id="ZB-3",
                lines=[_line(50_000, line_id="BL-4", po_line="ZPOL-F51-INR-2", receive_line="RCVL-1")])
    seeded.commit()
    assert out["attributed"] == 1
    assert _bill_line_link(seeded, "BL-4") == ("RCVL-1", None, "POL-F51-INR-2")


def test_billing_beyond_the_cited_receipt_is_held_and_a_credit_note_retracts_it(seeded):
    _receive(seeded, po_line_id="POL-F51-INR", receive_id="RCV-1", line_id="RCVL-1", paise=600_000)
    seeded.commit()
    grn_line_id = seeded.execute(
        "SELECT grn_line_id FROM grn_line WHERE line_external_id = 'RCVL-1'").fetchone()[0]
    first = _bill(seeded, external_id="ZB-4", lines=[_line(400_000, line_id="BL-5", receive_line="RCVL-1")])
    seeded.commit()
    assert first["exception_ids"] == () and _open(seeded) == []
    second = _bill(seeded, external_id="ZB-5", lines=[_line(300_000, line_id="BL-6", receive_line="RCVL-1")])
    seeded.commit()
    # 400,000 + 300,000 billed against 600,000 received: 100,000 over, on the grn_line.
    assert len(second["exception_ids"]) == 1
    held = _open(seeded)
    assert held == [("grn_line", grn_line_id, 100_000, "Open")]
    # Both bill lines are posted: over-billing is visible, never clamped.
    billed = seeded.execute(
        "SELECT SUM(amount_paise) FROM bill_line WHERE grn_line_id = %s", (grn_line_id,)).fetchone()[0]
    assert int(billed) == 700_000
    # A credit note citing the same receipt brings the sum back within it.
    credit = _bill(seeded, external_id="ZB-6", doc_type="CREDIT_NOTE",
                   lines=[_line(-100_000, line_id="BL-7", receive_line="RCVL-1")])
    seeded.commit()
    assert credit["attributed"] == 1
    status = seeded.execute(
        "SELECT status FROM reconciliation_exception WHERE kind = %s AND object_id = %s",
        (KIND, grn_line_id)).fetchall()
    assert status and all(s[0] != "Open" for s in status), status
