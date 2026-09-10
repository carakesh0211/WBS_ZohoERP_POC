"""Original budget creation, end to end, against a LIVE PostgreSQL (Fable 5.1).

Skipped without CAPEX_DB_URL, and a skip is not a pass. What only a server can
answer, and what the corrective brief demands as proof:

* create -> submit -> approve by a DIFFERENT identity through the approval
  engine -> RELEASED, writing kind='ORIGINAL' budget_line rows that carry the
  category, creating and classifying the cells, capturing the approving
  authority;
* the maker cannot approve their own budget (engine contributor filter);
* after release the document, its lines, the ORIGINAL lines and the cell's
  category are immutable AT THE DATABASE LEVEL (triggers, not discipline);
* a revision after release changes the current budget and leaves the
  original untouched;
* Budget CATEGORY and Budget HEAD are separate dimensions: the category
  filter and the head filter select different row sets over the same data;
* a second original grant on a funded cell is refused, including in the
  race the cell lock serialises;
* an out-of-scope principal gets 404, never a 403 that confirms existence;
* the CSV import path refuses row-level problems with row and column
  references, creates nothing on a problem, and creates one draft when clean;
* the audit stream of the document verifies.
"""
from __future__ import annotations

import sys
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

from psycopg.types.json import Jsonb                          # noqa: E402

from app.backend.pg import approval_rules as rules            # noqa: E402
from app.backend.pg import approvals as engine                # noqa: E402
from app.backend.pg import approval_writeback as wb           # noqa: E402
from app.backend.pg import audit as audit_mod                 # noqa: E402
from app.backend.pg import budget as budget_mod               # noqa: E402
from app.backend.pg import original_budget as ob              # noqa: E402
from app.backend.pg.engine import Scope                       # noqa: E402

pytestmark = pytest.mark.pg


# ----------------------------------------------------------------- helpers
def _scope(entity: str | None = None, user: str = "U-TEST-OB") -> Scope:
    if entity is None:
        return Scope(user_id=user, principal_kind="USER", read_all=True)
    return Scope(user_id=user, principal_kind="USER", read_all=False,
                 entity_ids=frozenset({entity}))


def _seed(con, suffix, *, heads=2, wbs_count=2, funded=False):
    """An entity with a project, `wbs_count` WBS elements, `heads` budget
    heads and two categories. No control cells unless `funded`."""
    org, ent, prj = f"O_{suffix}", f"E_{suffix}", f"P_{suffix}"
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,'Org','t','t')", (org, f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,'Entity','t','t')", (ent, org, f"EC_{suffix}"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,'Project','t','t')", (prj, ent, f"C_{suffix}"))
    head_ids = []
    for i in range(heads):
        h = f"H{i}_{suffix}"
        ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'t','t')", (h, ent, f"HC{i}_{suffix}", f"Head {i}"))
        head_ids.append(h)
    wbs_ids = []
    for i in range(wbs_count):
        w = f"W{i}_{suffix}"
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
           "created_by, updated_by) VALUES (%s,%s,%s,'n',%s,'t','t')", (w, prj, w, w))
        wbs_ids.append(w)
    cats = []
    for code in ("CIVIL", "PLANT"):
        c = f"BC_{code}_{suffix}"
        ex("INSERT INTO budget_category (category_id, code, name, created_by, updated_by) "
           "VALUES (%s,%s,%s,'t','t')", (c, f"{code}-{suffix.upper()}", code))
        cats.append(c)
    ex("INSERT INTO budget_category (category_id, code, name, active, created_by, updated_by) "
       "VALUES (%s,%s,'Retired',false,'t','t')", (f"BC_DEAD_{suffix}", f"DEAD-{suffix.upper()}"))
    if funded:
        for w in wbs_ids:
            for h in head_ids:
                ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
                   "VALUES (%s,%s,%s,'t')", (w, h, 1_000_000))
                ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by) VALUES (%s,%s,'t')",
                   (w, h))
                ex("INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, kind, amount_paise, "
                   "effective_from, status, created_by, updated_by) "
                   "VALUES (%s,%s,%s,'ORIGINAL',1000000,DATE '2020-01-01','Approved','t','t')",
                   (f"BL_{w}_{h}", w, h))
    con.commit()
    return {"entity": ent, "project": prj, "wbs": wbs_ids, "heads": head_ids, "cats": cats}


def _users(con, users):
    con.execute("INSERT INTO app_user (user_id, email, display_name, principal_kind, created_by, updated_by) "
                "VALUES ('SYSTEM','system@example.test','System','SERVICE','t','t') "
                "ON CONFLICT (user_id) DO NOTHING")
    for user_id, roles in users.items():
        con.execute("INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
                    "VALUES (%s,%s,%s,'t','t') ON CONFLICT (user_id) DO NOTHING",
                    (user_id, f"{user_id}@example.test", user_id))
        for role in roles:
            con.execute("INSERT INTO role_grant (user_id, role, granted_by) VALUES (%s,%s,'t') "
                        "ON CONFLICT (user_id, role) DO NOTHING", (user_id, role))
    con.commit()


def _definition(con, suffix, *, entity):
    definition_id = f"AD_{suffix}_OB"
    con.execute("INSERT INTO approval_definition (definition_id, object_type, code, version, status, "
                "entity_id, effective_from, created_by, activated_at, activated_by) "
                "VALUES (%s,'ORIGINAL_BUDGET',%s,1,%s,%s,%s,'TEST',now(),'TEST')",
                (definition_id, f"OB_{suffix}", rules.DEF_ACTIVE, entity, date(2020, 1, 1)))
    con.execute("INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, created_by) "
                "VALUES (%s,%s,10,%s,'TEST')", (f"AR_{suffix}_OB", definition_id, Jsonb({"op": "true"})))
    stage_id = f"AS_{suffix}_OB_1"
    con.execute("INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, parallel_group, "
                "quorum_type, quorum_n, applies_when, sla_hours, escalate_after_hours, escalate_to, "
                "allow_delegation, requires_reason, created_by) "
                "VALUES (%s,%s,1,'Finance',NULL,%s,NULL,NULL,NULL,NULL,NULL,true,false,'TEST')",
                (stage_id, definition_id, rules.QUORUM_ANY))
    con.execute("INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, approver_ref, scope_expr) "
                "VALUES (%s,1,%s,'Finance',NULL)", (stage_id, rules.APPROVER_ROLE))
    con.commit()


def _lines(ids, *, amounts=(150_000_00, 250_000_00), cat_index=(0, 1)):
    return [{"wbs_id": ids["wbs"][i % len(ids["wbs"])], "budget_head_id": ids["heads"][i % len(ids["heads"])],
             "budget_category_id": ids["cats"][cat_index[i % len(cat_index)]],
             "amount_paise": amounts[i % len(amounts)], "justification": f"line {i}"}
            for i in range(len(amounts))]


def _create(pg_database, ids, *, actor="U-MAKER", lines=None, fy="2026-27", scope=None):
    with pg_database.session(scope or _scope()) as session:
        return ob.create_draft(session, actor=actor, project_id=ids["project"], fiscal_year=fy,
                               title="FY27 original budget", lines=lines or _lines(ids))


def _submit(pg_database, budget_id, *, actor="U-MAKER"):
    with pg_database.session(_scope()) as session:
        return ob.submit(session, actor=actor, budget_id=budget_id, business_date=date(2026, 6, 1))


def _decide(pg_database, instance_id, *, actor, action=None):
    with pg_database.session(_scope()) as session:
        return engine.decide(session, instance_id=instance_id, actor_user_id=actor,
                             action=action or rules.ACTION_APPROVE,
                             idempotency_key=f"k-{uuid.uuid4().hex[:8]}", object_version=1)


def _writeback_if_needed(pg_database, instance_id):
    with pg_database.session(_scope()) as session:
        instance = engine.get_instance(session, instance_id)
        wb.apply_outcome(session, instance)


def _released(pg_database, ids, suffix, con):
    _users(con, {"U-MAKER": ["Requestor"], "U-FIN": ["Finance"]})
    _definition(con, suffix, entity=ids["entity"])
    doc = _create(pg_database, ids)
    sub = _submit(pg_database, doc["budget_id"])
    assert sub["submitted"] and sub["status"] == "SUBMITTED"
    _decide(pg_database, sub["approval_instance_id"], actor="U-FIN")
    _writeback_if_needed(pg_database, sub["approval_instance_id"])
    with pg_database.session(_scope()) as session:
        return ob.get_budget(session, doc["budget_id"])


# ------------------------------------------------------------------ tests
def test_a_draft_is_numbered_validated_and_totalled_in_paise(pg_connection, pg_database):
    ids = _seed(pg_connection, "draft1", wbs_count=3)
    _users(pg_connection, {"U-MAKER": ["Requestor"]})
    doc = _create(pg_database, ids)
    assert doc["status"] == "DRAFT"
    assert doc["budget_number"].startswith("OB-") and doc["budget_number"].endswith("0001")
    assert doc["total_paise"] == 150_000_00 + 250_000_00
    assert isinstance(doc["total_paise"], int)
    assert [l["budget_category_id"] for l in doc["lines"]] == [ids["cats"][0], ids["cats"][1]]
    assert doc["lines"][0]["budget_head_id"] != doc["lines"][0]["budget_category_id"]
    # Numbers are collision-safe and never reused: a second draft gets 0002.
    doc2 = _create(pg_database, ids, lines=[{"wbs_id": ids["wbs"][2], "budget_head_id": ids["heads"][0],
                                            "budget_category_id": ids["cats"][0], "amount_paise": 1_00}])
    assert doc2["budget_number"].endswith("0002")


def test_line_validation_names_the_line_and_the_field(pg_connection, pg_database):
    ids = _seed(pg_connection, "val1", funded=True)
    _users(pg_connection, {"U-MAKER": ["Requestor"]})
    bad = [
        {"wbs_id": ids["wbs"][0], "budget_head_id": ids["heads"][0], "budget_category_id": f"BC_DEAD_val1",
         "amount_paise": 100},                                   # inactive category, funded cell
        {"wbs_id": ids["wbs"][0], "budget_head_id": ids["heads"][0], "budget_category_id": ids["cats"][0],
         "amount_paise": 100},                                   # duplicate cell in document
        {"wbs_id": "W-NOT-HERE", "budget_head_id": ids["heads"][1], "budget_category_id": ids["cats"][0],
         "amount_paise": 0},                                     # wrong WBS, zero amount
    ]
    with pytest.raises(ob.OriginalBudgetError) as exc:
        _create(pg_database, ids, lines=bad)
    assert exc.value.code == "BUDGET_LINES_INVALID" and exc.value.status == 422
    codes = {(p["line"], p["code"]) for p in exc.value.detail}
    assert (1, "CATEGORY_INACTIVE") in codes
    assert (1, "CELL_ALREADY_HAS_ORIGINAL") in codes
    assert (2, "DUPLICATE_CELL_IN_DOCUMENT") in codes
    assert (3, "WBS_NOT_IN_PROJECT") in codes
    assert (3, "INVALID_AMOUNT") in codes
    with pg_database.session(_scope()) as session:
        assert session.fetchone("SELECT COUNT(*) FROM original_budget")[0] == 0, "nothing was created"


def test_money_is_refused_unless_it_is_an_integer(pg_connection, pg_database):
    ids = _seed(pg_connection, "float1")
    _users(pg_connection, {"U-MAKER": ["Requestor"]})
    lines = _lines(ids, amounts=(100,), cat_index=(0,))
    lines[0]["amount_paise"] = 100.5
    with pytest.raises(ob.OriginalBudgetError) as exc:
        _create(pg_database, ids, lines=lines)
    assert any(p["code"] == "MONEY_NOT_INTEGER" for p in exc.value.detail)
    lines[0].pop("amount_paise")
    lines[0]["amount_rupees"] = "1234.56"
    doc = _create(pg_database, ids, lines=lines)
    assert doc["lines"][0]["amount_paise"] == 123456


def test_a_required_budget_custom_field_is_enforced_on_entry(pg_connection, pg_database):
    ids = _seed(pg_connection, "cf1")
    _users(pg_connection, {"U-MAKER": ["Requestor"]})
    pg_connection.execute(
        "INSERT INTO custom_field_def (field_def_id, code, label, data_type, select_options, is_required, "
        "created_by, updated_by) VALUES ('CF_cf1','ASSET_CLASS_cf1','Asset class','SELECT',%s,true,'t','t')",
        (Jsonb(["A", "B"]),))
    pg_connection.execute(
        "INSERT INTO custom_field_applicability (applicability_id, field_def_id, applies_to, created_by) "
        "VALUES ('CFA_cf1','CF_cf1','BUDGET','t')")
    pg_connection.commit()
    with pytest.raises(ob.OriginalBudgetError) as exc:
        _create(pg_database, ids)
    assert exc.value.code == "CUSTOM_FIELD_INVALID" and "ASSET_CLASS_cf1: required" in exc.value.message
    with pg_database.session(_scope()) as session:
        doc = ob.create_draft(session, actor="U-MAKER", project_id=ids["project"], fiscal_year="2026-27",
                              title="t", lines=_lines(ids), custom_fields={"ASSET_CLASS_cf1": "A"})
    assert doc["custom_fields"] == {"ASSET_CLASS_cf1": "A"}
    with pytest.raises(ob.OriginalBudgetError) as exc2:
        with pg_database.session(_scope()) as session:
            ob.create_draft(session, actor="U-MAKER", project_id=ids["project"], fiscal_year="2026-27",
                            title="t", lines=_lines(ids), custom_fields={"ASSET_CLASS_cf1": "Z"})
    assert exc2.value.code == "CUSTOM_FIELD_INVALID"


def test_submit_approve_by_another_identity_releases_and_writes_original_lines(pg_connection, pg_database):
    ids = _seed(pg_connection, "rel1")
    doc = _released(pg_database, ids, "rel1", pg_connection)
    assert doc["status"] == "RELEASED"
    assert doc["approving_authority"] == "U-FIN" and doc["released_at"]
    assert all(l["budget_line_id"] for l in doc["lines"])
    with pg_database.session(_scope()) as session:
        rows = session.fetchall(
            "SELECT bl.kind, bl.amount_paise, bl.budget_category_id, c.budget_category_id, c.budget_paise, "
            "lc.original_paise FROM budget_line bl "
            "JOIN budget_control_cell c ON (c.wbs_id, c.budget_head_id) = (bl.wbs_id, bl.budget_head_id) "
            "JOIN budget_ledger_cell lc ON (lc.wbs_id, lc.budget_head_id) = (bl.wbs_id, bl.budget_head_id) "
            "WHERE bl.budget_line_id = ANY(%s) ORDER BY bl.amount_paise",
            ([l["budget_line_id"] for l in doc["lines"]],))
    assert [r[0] for r in rows] == ["ORIGINAL", "ORIGINAL"]
    assert [r[1] for r in rows] == [150_000_00, 250_000_00]
    assert [r[2] for r in rows] == [ids["cats"][0], ids["cats"][1]], "line carries the category"
    assert [r[3] for r in rows] == [ids["cats"][0], ids["cats"][1]], "cell classified on release"
    assert [r[4] for r in rows] == [150_000_00, 250_000_00], "control cell budget derived from the line"
    assert [r[5] for r in rows] == [150_000_00, 250_000_00], "ledger original_paise"
    # audit stream: create, submit, release -- and the chain verifies
    with pg_database.session(_scope()) as session:
        actions = [e["action"] for e in ob.audit_history(session, budget_id=doc["budget_id"])]
        report = audit_mod.verify_chain(session, f"ORIGINAL_BUDGET:{doc['budget_id']}")
    assert actions[:2] == ["ORIGINAL_BUDGET_CREATE", "ORIGINAL_BUDGET_SUBMIT"]
    assert actions[-1] == "ORIGINAL_BUDGET_RELEASE"
    assert report["intact"] is True


def test_the_maker_cannot_approve_their_own_budget(pg_connection, pg_database):
    ids = _seed(pg_connection, "mc1")
    _users(pg_connection, {"U-MAKER": ["Requestor", "Finance"], "U-FIN": ["Finance"]})
    _definition(pg_connection, "mc1", entity=ids["entity"])
    doc = _create(pg_database, ids)
    sub = _submit(pg_database, doc["budget_id"])
    with pytest.raises(Exception) as exc:
        _decide(pg_database, sub["approval_instance_id"], actor="U-MAKER")
    assert "SELF" in str(exc.value).upper() or "CONTRIBUT" in str(exc.value).upper() or \
        "NOT_ASSIGNED" in str(exc.value).upper() or "ASSIGN" in str(exc.value).upper()
    with pg_database.session(_scope()) as session:
        assert ob.get_budget(session, doc["budget_id"])["status"] == "SUBMITTED"
        assert session.fetchone("SELECT COUNT(*) FROM budget_line WHERE kind='ORIGINAL'")[0] == 0
    # and the direct service path refuses too, independently of the engine
    with pytest.raises(ob.OriginalBudgetError) as exc2:
        with pg_database.session(_scope()) as session:
            ob.release(session, budget_id=doc["budget_id"], actor="U-MAKER", approval_instance_id="X")
    assert exc2.value.code == "SELF_APPROVAL" and exc2.value.status == 403


def test_a_released_budget_is_immutable_at_the_database_level(pg_connection, pg_database):
    ids = _seed(pg_connection, "imm1")
    doc = _released(pg_database, ids, "imm1", pg_connection)
    con = pg_connection
    for sql, params in (
        ("UPDATE original_budget SET title = 'edited' WHERE budget_id = %s", (doc["budget_id"],)),
        ("DELETE FROM original_budget WHERE budget_id = %s", (doc["budget_id"],)),
        ("UPDATE original_budget_line SET amount_paise = amount_paise + 1 WHERE budget_id = %s", (doc["budget_id"],)),
        ("DELETE FROM original_budget_line WHERE budget_id = %s", (doc["budget_id"],)),
        ("UPDATE budget_line SET amount_paise = 1 WHERE budget_line_id = %s", (doc["lines"][0]["budget_line_id"],)),
        ("DELETE FROM budget_line WHERE budget_line_id = %s", (doc["lines"][0]["budget_line_id"],)),
        ("UPDATE budget_control_cell SET budget_category_id = %s WHERE wbs_id = %s AND budget_head_id = %s",
         (ids["cats"][1], doc["lines"][0]["wbs_id"], doc["lines"][0]["budget_head_id"])),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            con.execute(sql, params)
        con.rollback()
    # the service refuses too, with a code, rather than reaching the trigger
    with pytest.raises(ob.OriginalBudgetError) as exc:
        with pg_database.session(_scope()) as session:
            ob.update_draft(session, actor="U-MAKER", budget_id=doc["budget_id"],
                            expected_version=doc["version_no"], title="edited")
    assert exc.value.code == "BUDGET_NOT_EDITABLE"


def test_a_revision_changes_the_current_budget_and_not_the_original(pg_connection, pg_database):
    ids = _seed(pg_connection, "rev1")
    doc = _released(pg_database, ids, "rev1", pg_connection)
    line = doc["lines"][0]
    with pg_database.session(_scope()) as session:
        rev = budget_mod.create_revision(session, wbs_id=line["wbs_id"], budget_head_id=line["budget_head_id"],
                                         delta_paise=50_000_00, effective_from=date(2026, 6, 1),
                                         justification="supplement", actor="U-MAKER")
    with pg_database.session(_scope()) as session:
        budget_mod.approve_revision(session, revision_id=rev["revision_id"], actor="U-FIN")
    with pg_database.session(_scope()) as session:
        cell = session.fetchone(
            "SELECT c.budget_paise, lc.original_paise, lc.revisions_paise, c.budget_category_id "
            "FROM budget_control_cell c JOIN budget_ledger_cell lc USING (wbs_id, budget_head_id) "
            "WHERE c.wbs_id = %s AND c.budget_head_id = %s", (line["wbs_id"], line["budget_head_id"]))
        originals = session.fetchone("SELECT COUNT(*), SUM(amount_paise)::bigint FROM budget_line "
                                     "WHERE kind='ORIGINAL' AND wbs_id=%s AND budget_head_id=%s",
                                     (line["wbs_id"], line["budget_head_id"]))
        rev_cat = session.fetchone("SELECT budget_category_id FROM budget_line WHERE kind='REVISION' "
                                   "AND wbs_id=%s AND budget_head_id=%s", (line["wbs_id"], line["budget_head_id"]))
    assert cell[0] == 150_000_00 + 50_000_00, "current budget = original + approved revision"
    assert cell[1] == 150_000_00 and originals == (1, 150_000_00), "original untouched"
    assert cell[2] == 50_000_00
    assert cell[3] == ids["cats"][0]
    # The revision leg is either classified to the cell's category or left NULL by
    # the pre-026 writer; it can never disagree with the cell.
    assert rev_cat is None or rev_cat[0] in (None, ids["cats"][0])


def test_category_and_head_filters_select_different_sets(pg_connection, pg_database):
    """Two heads x two categories over four cells. Filtering by category
    CIVIL must return rows across BOTH heads; filtering by head 0 must
    return rows across BOTH categories; the sets differ."""
    ids = _seed(pg_connection, "cat1", heads=2, wbs_count=4)
    lines = [
        {"wbs_id": ids["wbs"][0], "budget_head_id": ids["heads"][0], "budget_category_id": ids["cats"][0], "amount_paise": 1_00},
        {"wbs_id": ids["wbs"][1], "budget_head_id": ids["heads"][1], "budget_category_id": ids["cats"][0], "amount_paise": 2_00},
        {"wbs_id": ids["wbs"][2], "budget_head_id": ids["heads"][0], "budget_category_id": ids["cats"][1], "amount_paise": 4_00},
        {"wbs_id": ids["wbs"][3], "budget_head_id": ids["heads"][1], "budget_category_id": ids["cats"][1], "amount_paise": 8_00},
    ]
    _users(pg_connection, {"U-MAKER": ["Requestor"], "U-FIN": ["Finance"]})
    _definition(pg_connection, "cat1", entity=ids["entity"])
    doc = _create(pg_database, ids, lines=lines)
    sub = _submit(pg_database, doc["budget_id"])
    _decide(pg_database, sub["approval_instance_id"], actor="U-FIN")
    _writeback_if_needed(pg_database, sub["approval_instance_id"])
    with pg_database.session(_scope()) as session:
        by_cat = session.fetchall(
            "SELECT wbs_id, budget_head_id, budget_paise FROM budget_control_cell WHERE budget_category_id = %s "
            "ORDER BY wbs_id", (ids["cats"][0],))
        by_head = session.fetchall(
            "SELECT wbs_id, budget_head_id, budget_paise FROM budget_control_cell WHERE budget_head_id = %s "
            "AND wbs_id LIKE %s ORDER BY wbs_id", (ids["heads"][0], "%_cat1"))
        both = session.fetchall(
            "SELECT wbs_id FROM budget_control_cell WHERE budget_category_id = %s AND budget_head_id = %s",
            (ids["cats"][0], ids["heads"][0]))
    assert {r[1] for r in by_cat} == set(ids["heads"]), "category spans both heads"
    assert sum(r[2] for r in by_cat) == 3_00
    assert {r[0] for r in by_head} == {ids["wbs"][0], ids["wbs"][2]}, "head spans both categories"
    assert sum(r[2] for r in by_head) == 5_00
    assert {r[0] for r in by_cat} != {r[0] for r in by_head}
    assert [r[0] for r in both] == [ids["wbs"][0]], "composition narrows, never widens"


def test_a_second_original_on_a_funded_cell_is_refused_even_under_the_lock(pg_connection, pg_database):
    ids = _seed(pg_connection, "twice1", wbs_count=3)
    doc = _released(pg_database, ids, "twice1", pg_connection)
    # At validation time:
    with pytest.raises(ob.OriginalBudgetError) as exc:
        _create(pg_database, ids)
    assert any(p["code"] == "CELL_ALREADY_HAS_ORIGINAL" for p in exc.value.detail)
    # And under the lock, bypassing validation by monkeypatching it away --
    # the post-lock re-check must still refuse, which is what protects the
    # race between two releases the lock serialises.
    with pg_database.session(_scope()) as session:
        session.execute("UPDATE original_budget SET status='SUBMITTED', released_at=NULL, "
                        "approving_authority=NULL WHERE budget_id = %s AND false", (doc["budget_id"],))
    ids2 = dict(ids)
    other = _create(pg_database, ids2, lines=[{"wbs_id": ids["wbs"][2], "budget_head_id": ids["heads"][1],
                                              "budget_category_id": ids["cats"][0], "amount_paise": 7_00}])
    sub = _submit(pg_database, other["budget_id"])
    assert sub["submitted"]
    with pg_database.session(_scope()) as session:
        # Fund the cell behind this budget's back, as a racing release would.
        session.execute("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
                        "VALUES (%s,%s,0,'race') ON CONFLICT DO NOTHING", (ids["wbs"][2], ids["heads"][1]))
        session.execute("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by) "
                        "VALUES (%s,%s,'race') ON CONFLICT DO NOTHING", (ids["wbs"][2], ids["heads"][1]))
        session.execute("INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, kind, amount_paise, "
                        "effective_from, status, created_by, updated_by) "
                        "VALUES ('BL_RACE_twice1',%s,%s,'ORIGINAL',5,DATE '2026-04-01','Approved','race','race')",
                        (ids["wbs"][2], ids["heads"][1]))
    import unittest.mock as mock
    with pytest.raises(ob.OriginalBudgetError) as exc2:
        with pg_database.session(_scope()) as session:
            with mock.patch.object(ob, "_validate_lines", side_effect=lambda *a, **k: [
                    {**l, "line_no": i + 1, "custom_fields": {}, "division_id": None, "branch_id": None,
                     "zone_id": None, "plant_id": None, "location_id": None}
                    for i, l in enumerate(ob.get_budget(session, other["budget_id"])["lines"])]):
                ob.release(session, budget_id=other["budget_id"], actor="U-FIN", approval_instance_id="INST-X")
    assert exc2.value.code == "CELL_ALREADY_HAS_ORIGINAL" and exc2.value.status == 409
    with pg_database.session(_scope()) as session:
        assert session.fetchone("SELECT COUNT(*) FROM budget_line WHERE kind='ORIGINAL' AND wbs_id=%s "
                                "AND budget_head_id=%s", (ids["wbs"][2], ids["heads"][1]))[0] == 1


def test_an_out_of_scope_principal_gets_404_not_an_existence_oracle(pg_connection, pg_database):
    ids = _seed(pg_connection, "scope1")
    other = _seed(pg_connection, "scope2")
    _users(pg_connection, {"U-MAKER": ["Requestor"]})
    doc = _create(pg_database, ids)
    with pg_database.session(_scope(entity=other["entity"], user="U-OTHER")) as session:
        with pytest.raises(ob.OriginalBudgetError) as exc:
            ob.get_budget(session, doc["budget_id"])
        assert exc.value.status == 404 and exc.value.code == "BUDGET_NOT_FOUND"
        with pytest.raises(ob.OriginalBudgetError) as exc2:
            ob.submit(session, actor="U-OTHER", budget_id=doc["budget_id"])
        assert exc2.value.status == 404
        assert ob.list_budgets(session)["items"] == []
        # and a write into the other entity's project is refused before it exists
        with pytest.raises(ob.OriginalBudgetError) as exc3:
            ob.create_draft(session, actor="U-OTHER", project_id=ids["project"], fiscal_year="2026-27",
                            title="x", lines=_lines(ids))
        assert exc3.value.status == 404


def test_rejection_leaves_the_document_and_writes_nothing(pg_connection, pg_database):
    ids = _seed(pg_connection, "rej1")
    _users(pg_connection, {"U-MAKER": ["Requestor"], "U-FIN": ["Finance"]})
    _definition(pg_connection, "rej1", entity=ids["entity"])
    doc = _create(pg_database, ids)
    sub = _submit(pg_database, doc["budget_id"])
    with pg_database.session(_scope()) as session:
        engine.decide(session, instance_id=sub["approval_instance_id"], actor_user_id="U-FIN",
                      action=rules.ACTION_REJECT, idempotency_key="rej-1", object_version=1,
                      reason_text="not this year")
    _writeback_if_needed(pg_database, sub["approval_instance_id"])
    with pg_database.session(_scope()) as session:
        after = ob.get_budget(session, doc["budget_id"])
        assert after["status"] == "REJECTED"
        assert session.fetchone("SELECT COUNT(*) FROM budget_line WHERE kind='ORIGINAL'")[0] == 0
        assert session.fetchone("SELECT COUNT(*) FROM budget_control_cell")[0] == 0
        assert ob.audit_history(session, budget_id=doc["budget_id"])[-1]["action"] == "ORIGINAL_BUDGET_REJECTED"


def test_csv_import_refuses_with_row_and_column_and_creates_nothing(pg_connection, pg_database):
    ids = _seed(pg_connection, "imp1")
    _users(pg_connection, {"U-MAKER": ["Requestor"]})
    with pg_database.session(_scope()) as session:
        template = ob.import_template_csv(session)
    assert template.splitlines()[0].startswith("wbs_code,budget_head_code,category_code,amount_rupees")
    text = ("wbs_code,budget_head_code,category_code,amount_rupees,justification\n"
            f"{ids['wbs'][0]},HC0_imp1,CIVIL-IMP1,1000.50,ok\n"
            f"NOPE,HC1_imp1,PLANT-IMP1,12.34,bad wbs\n"
            f"{ids['wbs'][1]},HC1_imp1,DEAD-IMP1,abc,bad category and amount\n")
    with pg_database.session(_scope()) as session:
        preview = ob.import_preview(session, project_id=ids["project"], text=text)
    assert preview["valid"] is False and preview["rows"] == 3
    problems = {(p["row"], p["column"], p["code"]) for p in preview["problems"]}
    assert (3, "wbs_code", "WBS_NOT_IN_PROJECT") in problems
    assert (4, "category_code", "CATEGORY_NOT_FOUND") in problems
    assert (4, "amount_rupees", "INVALID_AMOUNT") in problems
    with pytest.raises(ob.OriginalBudgetError) as exc:
        with pg_database.session(_scope()) as session:
            ob.import_commit(session, actor="U-MAKER", project_id=ids["project"], fiscal_year="2026-27",
                             title="import", text=text)
    assert exc.value.code == "IMPORT_INVALID" and exc.value.status == 422
    with pg_database.session(_scope()) as session:
        assert session.fetchone("SELECT COUNT(*) FROM original_budget")[0] == 0
    good = ("wbs_code,budget_head_code,category_code,amount_rupees,justification\n"
            f"{ids['wbs'][0]},HC0_imp1,CIVIL-IMP1,1000.50,ok\n"
            f"{ids['wbs'][1]},HC1_imp1,PLANT-IMP1,12.34,ok\n")
    with pg_database.session(_scope()) as session:
        doc = ob.import_commit(session, actor="U-MAKER", project_id=ids["project"], fiscal_year="2026-27",
                               title="import", text=good)
    assert doc["status"] == "DRAFT" and doc["total_paise"] == 100050 + 1234
    assert doc["import"]["rows"] == 2 and len(doc["import"]["content_sha256"]) == 64
    with pg_database.session(_scope()) as session:
        assert [e["action"] for e in ob.audit_history(session, budget_id=doc["budget_id"])] == \
            ["ORIGINAL_BUDGET_CREATE", "ORIGINAL_BUDGET_IMPORT"]


def test_selectors_are_scoped_and_wbs_needs_a_project(pg_connection, pg_database):
    ids = _seed(pg_connection, "sel1")
    other = _seed(pg_connection, "sel2")
    with pg_database.session(_scope(entity=ids["entity"])) as session:
        projects = ob.search_selector(session, kind="project", q="")
        assert {p["id"] for p in projects} == {ids["project"]}, "the other entity's project is invisible"
        with pytest.raises(ob.OriginalBudgetError) as exc:
            ob.search_selector(session, kind="wbs", q="")
        assert exc.value.code == "SELECTOR_NEEDS_PROJECT"
        wbs = ob.search_selector(session, kind="wbs", q="W1", project_id=ids["project"])
        assert [w["id"] for w in wbs] == [ids["wbs"][1]]
        cats = ob.search_selector(session, kind="budget_category", q="", entity_id=ids["entity"])
        assert f"BC_DEAD_sel1" not in {c["id"] for c in cats}, "inactive categories are not offered"
        with pytest.raises(ob.OriginalBudgetError):
            ob.search_selector(session, kind="not-a-kind")


def test_category_master_is_governed(pg_connection, pg_database):
    _seed(pg_connection, "cm1")
    with pg_database.session(_scope()) as session:
        parent = ob.create_category(session, actor="U-ADM", code="mach-cm1", name="Machinery")
        assert parent["code"] == "MACH-CM1" and parent["active"]
        child = ob.create_category(session, actor="U-ADM", code="MACH-CM1-CONV", name="Conveyors",
                                   parent_category_id=parent["category_id"])
        with pytest.raises(ob.OriginalBudgetError) as exc:
            ob.create_category(session, actor="U-ADM", code="MACH-CM1", name="dup")
        assert exc.value.code == "CATEGORY_CODE_EXISTS"
        with pytest.raises(ob.OriginalBudgetError) as exc2:
            ob.deactivate_category(session, actor="U-ADM", category_id=parent["category_id"])
        assert exc2.value.code == "CATEGORY_HAS_ACTIVE_CHILDREN"
        with pytest.raises(ob.OriginalBudgetError) as exc3:
            ob.update_category(session, actor="U-ADM", category_id=parent["category_id"],
                               expected_version=parent["version_no"], parent_category_id=child["category_id"])
        assert exc3.value.code == "CATEGORY_CYCLE"
        with pytest.raises(ob.OriginalBudgetError) as exc4:
            ob.update_category(session, actor="U-ADM", category_id=parent["category_id"],
                               expected_version=99, name="stale")
        assert exc4.value.code == "CATEGORY_STALE"
        ob.deactivate_category(session, actor="U-ADM", category_id=child["category_id"])
        assert not ob.deactivate_category(session, actor="U-ADM", category_id=parent["category_id"])["active"]
