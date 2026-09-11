"""The ERP v3 LIST row carries `total` and no `sub_total` (verified live on
the demo organisation, 2026-09-11); the DETAIL row carries both. The adapter
maps a list row's subtotal from the total it does state, and still refuses a
hydrated row that lacks `sub_total`. Database-free, network-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.integration import erp  # noqa: E402
from app.backend.integration.dto import DtoError  # noqa: E402

SOURCE = erp.ErpAdapter(organization_id="60074128927")._source("/bills")

LIST_BILL = {"bill_id": "1", "bill_number": "DEMO-1", "date": "2026-09-08",
             "last_modified_time": "2026-09-11T17:30:55+0530", "vendor_id": "V", "vendor_name": "v",
             "currency_code": "INR", "total": "2810000", "status": "open"}
LIST_PO = {"purchaseorder_id": "2", "purchaseorder_number": "PO-00001", "date": "2026-08-25",
           "last_modified_time": "2026-09-11T17:29:01+0530", "vendor_id": "V", "vendor_name": "v",
           "currency_code": "INR", "total": "5620000", "status": "open"}


def test_a_list_bill_without_sub_total_maps_from_its_stated_total():
    dto = erp._bill(LIST_BILL, SOURCE, hydrated=False)
    assert dto.subtotal_paise == 281_000_000 and dto.total_paise == 281_000_000
    assert dto.tax_paise == 0 and dto.lines_hydrated is False


def test_a_list_purchase_order_without_sub_total_maps_from_its_stated_total():
    dto = erp._purchase_order(LIST_PO, SOURCE, hydrated=False)
    assert dto.subtotal_paise == 562_000_000 and dto.lines_hydrated is False


@pytest.mark.parametrize("mapper, row", [(erp._bill, LIST_BILL), (erp._purchase_order, LIST_PO)])
def test_a_hydrated_row_without_sub_total_is_still_refused(mapper, row):
    with pytest.raises(DtoError) as exc:
        mapper(row, SOURCE, hydrated=True)
    assert "sub_total is missing" in str(exc.value)


def test_a_row_that_carries_sub_total_uses_it_not_the_total():
    row = {**LIST_BILL, "sub_total": "2800000", "tax_total": "10000"}
    dto = erp._bill(row, SOURCE, hydrated=False)
    assert (dto.subtotal_paise, dto.tax_paise, dto.total_paise) == (280_000_000, 1_000_000, 281_000_000)
