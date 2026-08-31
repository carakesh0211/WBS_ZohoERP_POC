"""Tests for `app.backend.pg.masters` and `app.backend.api.masters`.

Two layers, same split as `tests/test_pg_locking.py`:

  * Pure logic -- masking, normalisation, the never-hash regression guard,
    the shared-formatter behaviour -- needs no database and runs
    unconditionally.
  * Everything that needs a real row, a real lock, or real concurrency
    (optimistic concurrency, the ZOHO field-edit refusal, duplicate
    detection, numbering collision-safety, deactivate-not-delete, the
    reveal-writes-an-audit-entry guarantee) needs a live PostgreSQL and is
    gated below, exactly as `tests/test_pg_locking.py` gates its
    database-backed tests.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see
# tests/test_pg_locking.py's module docstring for why this cannot be a
# normal `conftest.py` auto-discovery.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import inspect  # noqa: E402
import os  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from pathlib import Path as _Path2  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg import masters  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

MASTERS_SQL = (_Path2(__file__).resolve().parents[1] / "migrations" / "pg"
               / "005_master_data.sql")


# ============================================================================
# Pure logic -- no database required
# ============================================================================
class TestMasking:
    """Matches docs/WAVE2_CONTRACTS.md's worked examples EXACTLY: `27ABCDE1234A1Z5`
    -> `27ABCDE****1Z5`; `ABCDE1234F` -> `ABCDE****F`."""

    def test_mask_gst_no_matches_contract_example(self):
        assert masters.mask_gst_no("27ABCDE1234A1Z5") == "27ABCDE****1Z5"

    def test_mask_pan_no_matches_contract_example(self):
        assert masters.mask_pan_no("ABCDE1234F") == "ABCDE****F"

    def test_mask_gst_no_none_passes_through(self):
        assert masters.mask_gst_no(None) is None
        assert masters.mask_gst_no("") is None

    def test_mask_pan_no_none_passes_through(self):
        assert masters.mask_pan_no(None) is None

    def test_mask_never_raises_on_a_too_short_value(self):
        # A malformed/short value must degrade safely, not crash the
        # formatter and not accidentally reveal the whole value.
        masked = masters.mask_gst_no("AB")
        assert masked != "AB"
        assert masked.startswith("A")

    def test_render_vendor_masks_by_default(self):
        row = {"vendor_id": "VEN-1", "gst_no": "27ABCDE1234A1Z5", "pan_no": "ABCDE1234F"}
        out = masters.render_vendor(row, reveal=False)
        assert out["gst_no"] == "27ABCDE****1Z5"
        assert out["pan_no"] == "ABCDE****F"
        assert out["tax_identity_revealed"] is False

    def test_render_vendor_reveals_on_request(self):
        row = {"vendor_id": "VEN-1", "gst_no": "27ABCDE1234A1Z5", "pan_no": "ABCDE1234F"}
        out = masters.render_vendor(row, reveal=True)
        assert out["gst_no"] == "27ABCDE1234A1Z5"
        assert out["pan_no"] == "ABCDE1234F"
        assert out["tax_identity_revealed"] is True

    def test_render_vendor_does_not_mutate_the_input_row(self):
        row = {"vendor_id": "VEN-1", "gst_no": "27ABCDE1234A1Z5", "pan_no": "ABCDE1234F"}
        masters.render_vendor(row, reveal=False)
        assert row["gst_no"] == "27ABCDE1234A1Z5", "masking must not mutate the caller's row"


class TestNeverHashed:
    """Regression guard against a mistake this project has already made
    once (see the header comment on entity.gst_no/pan_no in
    001_foundation.sql): gst_no/pan_no must NEVER be run through a hash
    function anywhere in this stream's code or schema."""

    def test_masters_module_never_imports_or_calls_a_hash_function(self):
        # Checks actual usage forms (an import, or an attribute call), not
        # the bare word -- this module's own docstrings and comments
        # legitimately discuss "hashlib"/"hmac" in prose while explaining
        # why they must never be used, and that prose must not trip this
        # regression guard.
        source = inspect.getsource(masters)
        for banned in ("import hashlib", "hashlib.", "import hmac", "hmac."):
            assert banned not in source, (
                f"{banned!r} must never appear in app/backend/pg/masters.py -- "
                f"gst_no/pan_no exist to be matched against statutory filings and "
                f"Zoho records, and hashing destroys that. See the module docstring.")

    def test_migration_never_hashes_gst_or_pan_columns(self):
        sql = MASTERS_SQL.read_text(encoding="utf-8")
        for banned in ("digest(gst_no", "digest(pan_no", "crypt(gst_no", "crypt(pan_no"):
            assert banned not in sql, (
                f"{banned!r} found in 005_master_data.sql -- gst_no/pan_no must "
                f"never be hashed, even at the schema level (pgcrypto digest/crypt).")


class TestNormalisation:
    """Mirrors the SQL `capex_normalise_text()` generated-column expression
    used by item_master.normalised_code/name and vendor_master's -- keep the
    two in step (see the module docstring on `normalise_text`)."""

    @pytest.mark.parametrize("value,expected", [
        ("Steel Rod 12mm", "steelrod12mm"),
        ("Steel Rod - 12 MM", "steelrod12mm"),
        ("STL-ROD-12", "stlrod12"),
        ("  Spaces   Everywhere  ", "spaceseverywhere"),
        ("", ""),
    ])
    def test_normalise_text(self, value, expected):
        assert masters.normalise_text(value) == expected

    def test_a_duplicate_pair_normalises_identically(self):
        assert masters.normalise_text("Steel Rod 12mm") == \
            masters.normalise_text("Steel Rod - 12 MM")


class TestKinds:
    def test_kind_for_items_and_vendors(self):
        assert masters.kind_for("items") is masters.ITEM
        assert masters.kind_for("vendors") is masters.VENDOR

    def test_kind_for_unknown_raises(self):
        with pytest.raises(masters.MasterDataError) as exc_info:
            masters.kind_for("bogus")
        assert exc_info.value.status == 404
        assert exc_info.value.code == "UNKNOWN_MASTER_KIND"

    def test_item_and_vendor_business_fields_are_disjoint_from_governance_fields(self):
        for kind in (masters.ITEM, masters.VENDOR):
            assert not (set(kind.business_fields) & set(masters.GOVERNANCE_FIELDS))


class TestIngestGuards:
    def test_ingest_refuses_live_status(self):
        assert "LIVE" not in masters.ALLOWED_INGEST_TRUTH_STATUS
        assert "VERIFIED" not in masters.ALLOWED_INGEST_TRUTH_STATUS
        assert set(masters.ALLOWED_INGEST_TRUTH_STATUS) == {"MOCK", "UNVERIFIED"}


# ============================================================================
# Live PostgreSQL -- everything that needs a real row, a real lock, or real
# concurrency.
# ============================================================================
def _seed_series(pg_connection):
    """Numbering series aren't created by the migration itself (only the
    tables are) -- they come from seed_parts/005_masters.sql, which most of
    these tests don't need loaded in full. Insert just the two series a test
    needs directly, keeping each test's fixture minimal and legible."""
    pg_connection.execute(
        """
        INSERT INTO numbering_series
            (series_id, code, prefix, suffix, pad_width, created_by, updated_by)
        VALUES
            ('NS-T-ITEM', 'ITEM', 'ITM-', '', 6, 'TEST', 'TEST'),
            ('NS-T-VENDOR', 'VENDOR', 'VEN-', '', 6, 'TEST', 'TEST')
        """)
    pg_connection.commit()


@pytest.mark.pg
@PG
class TestCreateAndSource:
    def test_create_local_refuses_a_payload_that_tries_to_set_source(self, pg_database, pg_scope):
        """A caller cannot create a ZOHO-sourced row through this path, and is
        told so rather than having the field quietly dropped."""
        with pg_database.session(pg_scope) as session:
            with pytest.raises(masters.MasterDataError) as exc:
                masters.create_local(
                    session, masters.ITEM, actor="tester",
                    payload={"code": "ITM-A", "name": "Widget A", "source": "ZOHO"})
        assert exc.value.status == 422
        assert "source" in str(exc.value)

    def test_create_local_sets_source_local_itself(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            row = masters.create_local(
                session, masters.ITEM, actor="tester",
                payload={"code": "ITM-A", "name": "Widget A"})
        assert row["source"] == "LOCAL"
        assert row["source_of_truth_status"] == "LOCAL"
        assert row["mapping_status"] == "UNMAPPED"
        assert row["is_active"] is True
        assert row["version_no"] == 1

    def test_create_local_writes_an_audit_entry(self, pg_database, pg_scope):
        from app.backend.pg.audit import verify_chain
        with pg_database.session(pg_scope) as session:
            row = masters.create_local(session, masters.ITEM, actor="tester",
                                        payload={"code": "ITM-AUD", "name": "Audited Widget"})
            result = verify_chain(session, f"item_master:{row['item_id']}")
        assert result["intact"]
        assert result["entries_checked"] == 1

    def test_ingest_from_adapter_creates_a_zoho_row(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            row = masters.ingest_from_adapter(
                session, masters.VENDOR, actor="adapter",
                external_source="ZOHO_ERP", external_id="ZE-1",
                payload={"code": "VEN-Z1", "name": "Zoho Vendor One"},
                payload_sha="sha256:test")
        assert row["source"] == "ZOHO"
        assert row["source_of_truth_status"] == "MOCK"
        assert row["external_source"] == "ZOHO_ERP"

    def test_ingest_from_adapter_refuses_live_or_verified_status(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            for bad_status in ("LIVE", "VERIFIED"):
                with pytest.raises(masters.MasterDataError) as exc_info:
                    masters.ingest_from_adapter(
                        session, masters.VENDOR, actor="adapter",
                        external_source="ZOHO_ERP", external_id=f"ZE-BAD-{bad_status}",
                        payload={"code": f"VEN-{bad_status}", "name": "Bad Status Vendor"},
                        payload_sha="sha256:test", source_of_truth_status=bad_status)
                assert exc_info.value.code == masters.INVALID_SOURCE_OF_TRUTH


@pytest.mark.pg
@PG
class TestOptimisticConcurrency:
    def test_stale_version_returns_409_and_row_is_provably_unchanged(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            created = masters.create_local(session, masters.ITEM, actor="tester",
                                            payload={"code": "ITM-OCC", "name": "OCC Widget"})
        item_id = created["item_id"]

        # A legitimate update from version 1 succeeds and moves the row to
        # version 2 -- establishing the "someone else" the stale caller below
        # collides with.
        with pg_database.session(pg_scope) as session:
            updated = masters.update_master(
                session, masters.ITEM, actor="tester-a", id_value=item_id,
                expected_version=1, payload={"name": "OCC Widget Renamed"})
        assert updated["version_no"] == 2
        assert updated["name"] == "OCC Widget Renamed"

        # A second caller, still holding the stale version_no=1, must be
        # refused -- and the row must be PROVABLY unchanged by the attempt:
        # no UPDATE is issued once the mismatch is detected under the lock.
        with pg_database.session(pg_scope) as session:
            with pytest.raises(masters.MasterDataError) as exc_info:
                masters.update_master(
                    session, masters.ITEM, actor="tester-b", id_value=item_id,
                    expected_version=1, payload={"name": "OCC Widget Hijacked"})
        assert exc_info.value.status == 409
        assert exc_info.value.code == masters.VERSION_CONFLICT

        with pg_database.session(pg_scope) as session:
            current = masters._select_row(session, masters.ITEM, item_id)
        current_dict = masters.row_to_dict(masters.ITEM, current)
        assert current_dict["name"] == "OCC Widget Renamed", \
            "the rejected stale update must not have changed the row at all"
        assert current_dict["version_no"] == 2


@pytest.mark.pg
@PG
class TestZohoFieldEditRefusal:
    def test_zoho_sourced_row_refuses_a_business_field_edit(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            row = masters.ingest_from_adapter(
                session, masters.ITEM, actor="adapter",
                external_source="ZOHO_ERP", external_id="ZE-ITEM-1",
                payload={"code": "ITM-ZFE", "name": "Zoho Mirrored Item"},
                payload_sha="sha256:test")

        with pg_database.session(pg_scope) as session:
            with pytest.raises(masters.MasterDataError) as exc_info:
                masters.update_master(
                    session, masters.ITEM, actor="tester", id_value=row["item_id"],
                    expected_version=row["version_no"], payload={"name": "Locally Renamed"})
        assert exc_info.value.status == 409
        assert exc_info.value.code == masters.ZOHO_FIELD_READONLY

        with pg_database.session(pg_scope) as session:
            current = masters.row_to_dict(
                masters.ITEM, masters._select_row(session, masters.ITEM, row["item_id"]))
        assert current["name"] == "Zoho Mirrored Item", "the refused edit must not apply"

    def test_zoho_sourced_row_allows_a_governance_field_edit(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            local_row = masters.create_local(session, masters.ITEM, actor="tester",
                                              payload={"code": "ITM-GOV-BASE", "name": "Base Item"})
            zoho_row = masters.ingest_from_adapter(
                session, masters.ITEM, actor="adapter",
                external_source="ZOHO_ERP", external_id="ZE-ITEM-GOV",
                payload={"code": "ITM-GOV", "name": "Governance Test Item"},
                payload_sha="sha256:test")

        with pg_database.session(pg_scope) as session:
            updated = masters.update_master(
                session, masters.ITEM, actor="tester", id_value=zoho_row["item_id"],
                expected_version=zoho_row["version_no"],
                payload={"mapping_status": "MAPPED", "duplicate_of": local_row["item_id"]})
        assert updated["mapping_status"] == "MAPPED"
        assert updated["duplicate_of"] == local_row["item_id"]
        assert updated["name"] == "Governance Test Item", "business fields must be untouched"


@pytest.mark.pg
@PG
class TestDuplicateDetection:
    def test_duplicate_is_flagged_never_merged(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            first = masters.create_local(session, masters.ITEM, actor="tester",
                                          payload={"code": "ITM-DUP-1", "name": "Copper Wire 2.5mm"})
            second = masters.create_local(
                session, masters.ITEM, actor="tester",
                payload={"code": "ITM-DUP-2", "name": "Copper Wire - 2.5 MM"})

        assert second["duplicate_of"] == first["item_id"]
        assert second["mapping_status"] == "DUPLICATE_SUSPECT"
        # Never merged: both rows still exist, independently, with their own
        # data -- flagging is not deletion and not a foreign-key redirect of
        # anything but the flag itself.
        with pg_database.session(pg_scope) as session:
            first_after = masters.row_to_dict(
                masters.ITEM, masters._select_row(session, masters.ITEM, first["item_id"]))
            second_after = masters.row_to_dict(
                masters.ITEM, masters._select_row(session, masters.ITEM, second["item_id"]))
        assert first_after["duplicate_of"] is None, "the ORIGINAL row is never flagged against itself"
        assert first_after["name"] == "Copper Wire 2.5mm"
        assert second_after["name"] == "Copper Wire - 2.5 MM"

    def test_find_duplicate_returns_none_when_nothing_matches(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            row = masters.create_local(session, masters.ITEM, actor="tester",
                                        payload={"code": "ITM-UNIQ", "name": "Truly Unique Item"})
            duplicate = masters.find_duplicate(
                session, masters.ITEM, exclude_id=row["item_id"],
                code="ITM-UNIQ", name="Truly Unique Item")
        assert duplicate is None


@pytest.mark.pg
@PG
class TestDeactivate:
    def test_deactivate_does_not_delete(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            row = masters.create_local(session, masters.VENDOR, actor="tester",
                                        payload={"code": "VEN-DEACT", "name": "Deactivate Me"})
        with pg_database.session(pg_scope) as session:
            deactivated = masters.deactivate_master(session, masters.VENDOR, actor="tester",
                                                      id_value=row["vendor_id"])
        assert deactivated["is_active"] is False
        assert deactivated["version_no"] == 2

        with pg_database.session(pg_scope) as session:
            still_there = masters._select_row(session, masters.VENDOR, row["vendor_id"])
        assert still_there is not None, "deactivate must never hard-delete the row"
        assert masters.row_to_dict(masters.VENDOR, still_there)["name"] == "Deactivate Me"


@pytest.mark.pg
@PG
class TestRevealAudits:
    def test_reveal_writes_one_audit_entry_per_present_field(self, pg_database, pg_scope):
        from app.backend.pg.audit import verify_chain
        with pg_database.session(pg_scope) as session:
            row = masters.create_local(
                session, masters.VENDOR, actor="tester",
                payload={"code": "VEN-REV", "name": "Reveal Test Vendor",
                         "gst_no": "27ABCDE1234A1Z5", "pan_no": "ABCDE1234F"})

        with pg_database.session(pg_scope) as session:
            revealed = masters.reveal_vendor_tax_identity(
                session, row["vendor_id"], actor="auditor-1", reason="statutory filing check")
        assert revealed["gst_no"] == "27ABCDE1234A1Z5"
        assert revealed["pan_no"] == "ABCDE1234F"

        with pg_database.session(pg_scope) as session:
            result = verify_chain(session, f"vendor_master:{row['vendor_id']}")
        # CREATE (1) + two REVEAL_TAX_IDENTITY entries (gst_no, pan_no) = 3
        assert result["entries_checked"] == 3
        assert result["intact"]


@pytest.mark.pg
@PG
class TestNumbering:
    def test_issue_number_formats_with_prefix_and_padding(self, pg_connection, pg_database, pg_scope):
        _seed_series(pg_connection)
        with pg_database.session(pg_scope) as session:
            first = masters.issue_number(session, "ITEM", actor="tester")
            second = masters.issue_number(session, "ITEM", actor="tester")
        assert first["formatted_number"] == "ITM-000001"
        assert second["formatted_number"] == "ITM-000002"
        assert second["value"] == first["value"] + 1

    def test_issue_number_unknown_series_raises(self, pg_database, pg_scope):
        with pg_database.session(pg_scope) as session:
            with pytest.raises(masters.MasterDataError) as exc_info:
                masters.issue_number(session, "NO-SUCH-SERIES", actor="tester")
        assert exc_info.value.code == masters.NUMBERING_SERIES_NOT_FOUND

    def test_issued_numbers_are_immutable(self, pg_connection, pg_database, pg_scope):
        _seed_series(pg_connection)
        with pg_database.session(pg_scope) as session:
            issued = masters.issue_number(session, "ITEM", actor="tester")

        with pytest.raises(Exception) as exc_info:
            with pg_database.session(pg_scope) as session:
                session.execute("UPDATE numbering_issued SET value = 999999 WHERE issued_id = %s",
                                 (issued["issued_id"],))
        assert "append-only" in str(exc_info.value)

        with pytest.raises(Exception) as exc_info:
            with pg_database.session(pg_scope) as session:
                session.execute("DELETE FROM numbering_issued WHERE issued_id = %s",
                                 (issued["issued_id"],))
        assert "append-only" in str(exc_info.value)

    def test_numbering_is_collision_safe_under_concurrency(self, pg_connection, pg_database, pg_scope):
        """N workers race to issue a number from the SAME series/period.
        Collision-safety means: N issuances, N distinct values, no exception,
        no gap larger than the worker count would explain by rollback."""
        _seed_series(pg_connection)
        workers = 20

        def _issue(worker_index: int):
            scope = Scope(user_id=f"worker-{worker_index}", principal_kind="SERVICE")
            with pg_database.session(scope) as session:
                return masters.issue_number(session, "VENDOR", actor=f"worker-{worker_index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_issue, range(workers)))

        values = [r["value"] for r in results]
        formatted = [r["formatted_number"] for r in results]
        assert len(set(values)) == workers, \
            f"expected {workers} distinct values, got {sorted(values)}"
        assert len(set(formatted)) == workers, "formatted numbers must also all be distinct"

        with pg_database.session(pg_scope) as session:
            rows = session.fetchall(
                "SELECT COUNT(*) FROM numbering_issued WHERE series_id = "
                "(SELECT series_id FROM numbering_series WHERE code = 'VENDOR')")
        assert rows[0][0] == workers, "every issuance must have a corresponding issued record"
