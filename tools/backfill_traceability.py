"""Migrate the CLIENT-PDF requirements from C1_req_ids.json into C14_traceability.json.

Closes CONTRACT_GAPS.md GAP-05 in part: the 107 client-PDF requirements lived
only in C1 with no traceability - no screens, APIs, tables, services, tests or
acceptance criteria.

What this does and does not do
------------------------------
It migrates each requirement with its REAL metadata from C1 - id, description,
source section, page, business purpose, actor - and sets

    traceability_status = "PENDING"

It does NOT invent screens, tables or test names. Fabricating a traceability
row would be worse than an empty one: an empty row is visibly unfinished, while
a plausible-looking wrong one silently reports coverage that does not exist.

Tracing each requirement is analysis work, done per phase as the requirement is
implemented. The ratchet in tests/test_contracts.py asserts the PENDING count
only ever falls.

Usage
-----
    python tools/backfill_traceability.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "research" / "30_contracts"
C1 = CONTRACTS / "C1_req_ids.json"
C14 = CONTRACTS / "C14_traceability.json"

TRACED_FIELDS = ("screens", "api_routes", "tables", "services", "tests")


def main() -> int:
    c1 = json.loads(C1.read_text(encoding="utf-8"))
    c14 = json.loads(C14.read_text(encoding="utf-8"))

    existing = {r["requirement_id"] for r in c14["requirements"]}
    added = 0

    for req in c1["requirements"]:
        rid = req["requirement_id"]
        if rid in existing:
            continue  # already traced by hand - never overwrite analysis
        c14["requirements"].append({
            "requirement_id": rid,
            "provenance": "CLIENT-PDF",
            "source_ref": (
                f"Atha Group requirements PDF, "
                f"{req.get('source_section', 'section not recorded')}"
                + (f", page {req['pdf_page']}" if req.get("pdf_page") else "")
            ),
            "requirement": req.get("requirement_description", ""),
            "business_purpose": req.get("business_purpose", ""),
            "actor": req.get("actor", ""),
            "screens": [],
            "api_routes": [],
            "tables": [],
            "services": [],
            "tests": [],
            "acceptance_criteria": "",
            "traceability_status": "PENDING",
            "traceability_note": (
                "Migrated from C1_req_ids.json by tools/backfill_traceability.py. "
                "Screens, tables, services, tests and acceptance criteria are "
                "deliberately empty rather than invented - they are completed as "
                "analysis when the requirement enters its phase."
            ),
            "phase": None,
            "status": "NOT_STARTED",
        })
        added += 1

    for r in c14["requirements"]:
        r.setdefault(
            "traceability_status",
            "TRACED" if all(r.get(f) for f in ("tests", "acceptance_criteria")) else "PENDING",
        )

    pending = sum(1 for r in c14["requirements"] if r["traceability_status"] == "PENDING")
    traced = sum(1 for r in c14["requirements"] if r["traceability_status"] == "TRACED")

    c14["counts"] = {
        "CLIENT-PDF": sum(1 for r in c14["requirements"] if r["provenance"] == "CLIENT-PDF"),
        "CLIENT-NOTES-2026-08-28": sum(
            1 for r in c14["requirements"] if r["provenance"] == "CLIENT-NOTES-2026-08-28"),
        "PRODUCT-OWNER-REQUEST-2026-08-28": sum(
            1 for r in c14["requirements"] if r["provenance"] == "PRODUCT-OWNER-REQUEST-2026-08-28"),
        "total": len(c14["requirements"]),
        "traced": traced,
        "pending_traceability": pending,
        "ratchet_note": (
            "pending_traceability must never increase. tests/test_contracts.py "
            "asserts it against MAX_PENDING_TRACEABILITY. Lower that constant as "
            "requirements are traced."
        ),
    }

    C14.write_text(json.dumps(c14, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Added {added} CLIENT-PDF requirement(s).")
    print(f"Total {len(c14['requirements'])} - traced {traced}, pending {pending}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
