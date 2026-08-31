"""Tests for `app.backend.api.settings` -- the organisation-hierarchy CRUD
router (organisations, entities, divisions, branches, zones, plants,
locations, departments).

Same two-layer split as `tests/test_pg_masters.py`: collection-registry
shape and the masking formula are pure logic; everything touching a real
row (optimistic concurrency, masking + reveal + audit, deactivate-not-
delete, referential integrity) needs a live PostgreSQL and is gated below.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import os  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.backend.api import settings as settings_api  # noqa: E402
from app.backend.pg import engine as pg_engine  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# ============================================================================
# Pure logic
# ============================================================================
class TestCollectionRegistry:
    def test_every_contract_collection_is_registered(self):
        expected = {"organisations", "entities", "divisions", "branches", "zones",
                    "plants", "locations", "departments"}
        assert set(settings_api.COLLECTIONS) == expected

    def test_spec_for_unknown_collection_raises_404(self):
        with pytest.raises(Exception) as exc_info:
            settings_api.spec_for("bogus")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "UNKNOWN_COLLECTION"

    def test_only_entities_carries_masked_fields(self):
        for name, spec in settings_api.COLLECTIONS.items():
            if name == "entities":
                assert spec.masked_fields == ("gst_no", "pan_no")
            else:
                assert spec.masked_fields == ()

    def test_render_masks_entity_tax_identity_by_default(self):
        spec = settings_api.COLLECTIONS["entities"]
        row = {"entity_id": "ENT-1", "gst_no": "27ABCDE1234A1Z5", "pan_no": "ABCDE1234F"}
        out = settings_api._render(spec, row, reveal=False)
        assert out["gst_no"] == "27ABCDE****1Z5"
        assert out["pan_no"] == "ABCDE****F"

    def test_render_reveals_entity_tax_identity_on_request(self):
        spec = settings_api.COLLECTIONS["entities"]
        row = {"entity_id": "ENT-1", "gst_no": "27ABCDE1234A1Z5", "pan_no": "ABCDE1234F"}
        out = settings_api._render(spec, row, reveal=True)
        assert out["gst_no"] == "27ABCDE1234A1Z5"
        assert out["pan_no"] == "ABCDE1234F"

    def test_render_leaves_unmasked_collections_alone(self):
        spec = settings_api.COLLECTIONS["departments"]
        row = {"department_id": "DEPT-1", "code": "FIN", "name": "Finance", "cost_centre": "CC-1"}
        assert settings_api._render(spec, row, reveal=False) == dict(row)


# ============================================================================
# Live PostgreSQL
# ============================================================================
@pytest.fixture()
def client(pg_database):
    """A minimal FastAPI app carrying ONLY this stream's settings router --
    `app/backend/main.py` does not mount it yet (the lead mounts routers only
    after every Wave 2 stream lands), so tests build their own app rather
    than waiting on that. Restores whatever database was configured before
    the test, same discipline as test_pg_audit_api_e2e.py's `pg_backed_app`."""
    previous = None
    try:
        previous = pg_engine.get_database()
    except RuntimeError:
        previous = None
    pg_engine.set_database(pg_database)

    app = FastAPI()
    app.include_router(settings_api.router)
    try:
        yield TestClient(app)
    finally:
        pg_engine.set_database(previous)


HEADERS = {"X-Actor-Id": "tester"}


@pytest.mark.pg
@PG
class TestSettingsCrud:
    def test_create_and_list_an_organisation(self, client):
        resp = client.post("/api/settings/organisations", headers=HEADERS,
                            json={"code": "ORG-T1", "name": "Test Org One"})
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["code"] == "ORG-T1"
        assert created["is_active"] is True
        assert created["version_no"] == 1

        listed = client.get("/api/settings/organisations", headers=HEADERS)
        assert listed.status_code == 200
        assert any(item["code"] == "ORG-T1" for item in listed.json()["items"])

    def test_create_requires_a_parent_reference(self, client):
        resp = client.post("/api/settings/divisions", headers=HEADERS,
                            json={"code": "DIV-T1", "name": "Missing Parent Division"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "MISSING_FIELD"

    def test_create_refuses_an_invalid_parent_reference(self, client):
        resp = client.post("/api/settings/divisions", headers=HEADERS,
                            json={"code": "DIV-T2", "name": "Bad Parent Division",
                                  "entity_id": "ENT-DOES-NOT-EXIST"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "INVALID_REFERENCE"

    def test_create_requires_actor_header(self, client):
        resp = client.post("/api/settings/organisations",
                            json={"code": "ORG-NOACTOR", "name": "No Actor"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "ACTOR_REQUIRED"

    def test_unknown_collection_is_404(self, client):
        resp = client.get("/api/settings/not-a-real-collection", headers=HEADERS)
        assert resp.status_code == 404
        assert resp.json()["code"] == "UNKNOWN_COLLECTION"


@pytest.mark.pg
@PG
class TestSettingsOptimisticConcurrency:
    def test_stale_version_returns_409_and_row_is_unchanged(self, client):
        created = client.post("/api/settings/organisations", headers=HEADERS,
                               json={"code": "ORG-OCC", "name": "OCC Org"}).json()

        first_update = client.put(f"/api/settings/organisations/{created['organisation_id']}",
                                   headers=HEADERS,
                                   json={"name": "OCC Org Renamed", "version_no": 1})
        assert first_update.status_code == 200, first_update.text
        assert first_update.json()["version_no"] == 2

        stale_update = client.put(f"/api/settings/organisations/{created['organisation_id']}",
                                   headers=HEADERS,
                                   json={"name": "OCC Org Hijacked", "version_no": 1})
        assert stale_update.status_code == 409
        assert stale_update.json()["code"] == "VERSION_CONFLICT"

        current = client.get("/api/settings/organisations", headers=HEADERS).json()
        row = next(i for i in current["items"] if i["organisation_id"] == created["organisation_id"])
        assert row["name"] == "OCC Org Renamed"
        assert row["version_no"] == 2


@pytest.mark.pg
@PG
class TestSettingsDeactivate:
    def test_deactivate_does_not_delete(self, client):
        created = client.post("/api/settings/organisations", headers=HEADERS,
                               json={"code": "ORG-DEACT", "name": "Deactivate Org"}).json()

        resp = client.post(
            f"/api/settings/organisations/{created['organisation_id']}/deactivate",
            headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"id": created["organisation_id"], "is_active": False}

        listed = client.get("/api/settings/organisations",
                             params={"is_active": "false"}, headers=HEADERS).json()
        assert any(i["organisation_id"] == created["organisation_id"] for i in listed["items"])


@pytest.mark.pg
@PG
class TestEntityTaxIdentityMasking:
    def _create_entity(self, client, org_id: str, code: str = "ENT-T1"):
        return client.post("/api/settings/entities", headers=HEADERS, json={
            "organisation_id": org_id, "code": code, "name": "Test Entity",
            "gst_no": "27ABCDE1234A1Z5", "pan_no": "ABCDE1234F",
        }).json()

    def test_masked_by_default(self, client):
        org = client.post("/api/settings/organisations", headers=HEADERS,
                           json={"code": "ORG-MASK", "name": "Masking Org"}).json()
        entity = self._create_entity(client, org["organisation_id"])
        assert entity["gst_no"] == "27ABCDE****1Z5"
        assert entity["pan_no"] == "ABCDE****F"

        listed = client.get("/api/settings/entities", headers=HEADERS).json()
        row = next(i for i in listed["items"] if i["entity_id"] == entity["entity_id"])
        assert row["gst_no"] == "27ABCDE****1Z5"

    def test_reveal_without_permission_is_refused(self, client):
        org = client.post("/api/settings/organisations", headers=HEADERS,
                           json={"code": "ORG-MASK2", "name": "Masking Org 2"}).json()
        self._create_entity(client, org["organisation_id"], code="ENT-T2")

        resp = client.get("/api/settings/entities", headers=HEADERS,
                           params={"reveal": "true"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "REVEAL_PERMISSION_REQUIRED"

    def test_reveal_with_permission_but_no_reason_is_refused(self, client):
        headers = dict(HEADERS, **{"X-Permissions": settings_api.REVEAL_PERMISSION})
        resp = client.get("/api/settings/entities", headers=headers, params={"reveal": "true"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "REVEAL_REASON_REQUIRED"

    def test_reveal_with_permission_and_reason_unmasks_and_audits(self, client, pg_database, pg_scope):
        org = client.post("/api/settings/organisations", headers=HEADERS,
                           json={"code": "ORG-MASK3", "name": "Masking Org 3"}).json()
        entity = self._create_entity(client, org["organisation_id"], code="ENT-T3")

        headers = dict(HEADERS, **{
            "X-Permissions": settings_api.REVEAL_PERMISSION,
            "X-Reveal-Reason": "statutory filing verification",
        })
        resp = client.get("/api/settings/entities", headers=headers, params={"reveal": "true"})
        assert resp.status_code == 200
        row = next(i for i in resp.json()["items"] if i["entity_id"] == entity["entity_id"])
        assert row["gst_no"] == "27ABCDE1234A1Z5"
        assert row["pan_no"] == "ABCDE1234F"
        assert row["tax_identity_revealed"] is True

        from app.backend.pg.audit import verify_chain
        with pg_database.session(pg_scope) as session:
            result = verify_chain(session, f"entity:{entity['entity_id']}")
        # CREATE (1, written by create_collection_row) + two
        # REVEAL_TAX_IDENTITY entries (gst_no, pan_no) = 3.
        assert result["entries_checked"] == 3
        assert result["intact"]
