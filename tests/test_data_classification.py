"""Regulated tax identity: masked by default, revealed on permission, NEVER hashed.

PLAN SECTION 10.4, and the specific mistake it withdrew. Version 1.0 of this
system stored GSTIN and PAN as `sha256(value)[:16]` -- recorded in
`migrations/pg/010_integration.sql`, which says so in its own header. A hash
is a one-way function, so the moment it was written the numbers needed for
vendor matching, for GST return filing and for statutory reporting were gone,
and no amount of later access control could bring them back. 10.4 forbids
irreversibly hashing anything needed for matching, compliance or reporting.

The codebase already carries three guards against a RECURRENCE, and each is
worth exactly as much as its scope:

* `tests/test_pg_masters.py` forbids hashlib/hmac in `pg/masters.py`;
* the same file forbids `digest(gst_no` in `005_master_data.sql`;
* `tests/test_observability.py` forbids a hex token in a log line.

Three modules and one migration, out of forty-odd modules and twenty-four
migrations. The defect they guard against is not a property of `masters.py`;
it is a property of the SYSTEM, and it would return through whichever file
nobody thought to scan. This module asks the question of the whole repository
at once, and asks the other half of it too: is the PLAINTEXT still there?
Deleting the column would satisfy every never-hash assertion ever written.

Nothing here needs a database.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "app" / "backend"
PG_MIGRATIONS = PROJECT_ROOT / "migrations" / "pg"

#: Every spelling of the two regulated identifiers that appears in this
#: codebase, plus the ones a new module might reasonably invent.
TAX_IDENTITY_TOKENS = (
    "gst_no", "gstno", "gstin", "gst_number", "gstnumber",
    "pan_no", "panno", "pan_number", "pannumber", "pan",
)

#: Anything that turns a value into a digest. `crypt` and `digest` are
#: pgcrypto's; the rest are Python's.
HASHING_TOKENS = (
    "sha1", "sha224", "sha256", "sha384", "sha512", "md5", "blake2b", "blake2s",
    "hashlib", "hmac", "digest(", "crypt(", "encode(", "pbkdf2",
)


def _executable_python(path: Path) -> str:
    """The module's code with every docstring removed.

    Comments never survive `ast.unparse`, and docstrings are dropped
    explicitly, because several modules DOCUMENT the withdrawn approach at
    length -- `observability.py` explains why a GSTIN is removed from a log
    rather than hashed -- and a text search would report the explanation as
    the offence.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _executable_sql(path: Path) -> str:
    """SQL with `--` comments stripped, for the same reason.

    `010_integration.sql` records the v1.0 defect in its header, in the exact
    words a scanner would look for.
    """
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split("--", 1)[0]
        if code.strip():
            lines.append(code)
    return "\n".join(lines)


#: Whole words only. `pan` is a substring of "expansion", which appears in the
#: demo seed's project names, and a scanner that fired on it would be turned
#: off within a week.
_TAX_IDENTITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in TAX_IDENTITY_TOKENS) + r")\b")


def _hash_near_tax_identity(text: str) -> list[str]:
    """Statements mentioning a tax identity within reach of a hash call."""
    lowered = text.lower()
    hits = []
    for match in _TAX_IDENTITY_RE.finditer(lowered):
        window = lowered[max(0, match.start() - 160): match.end() + 160]
        if any(token in window for token in HASHING_TOKENS):
            hits.append(window.strip())
    return hits


# ======================================================================
# The whole repository, not one module
# ======================================================================
PYTHON_MODULES = sorted(BACKEND.rglob("*.py"))
SQL_MIGRATIONS = sorted(PG_MIGRATIONS.glob("*.sql"))


@pytest.mark.parametrize("path", PYTHON_MODULES,
                         ids=lambda p: str(p.relative_to(BACKEND)))
def test_no_backend_module_hashes_a_tax_identity(path):
    """Plan 10.4, asked of every module in the backend.

    A hash of a GSTIN is not a safer GSTIN. It is a correlatable
    pseudo-identifier that cannot be filed, cannot be matched against a vendor
    master, and cannot be reversed when somebody needs the number back.
    """
    hits = _hash_near_tax_identity(_executable_python(path))
    assert not hits, (
        f"{path.relative_to(PROJECT_ROOT)} appears to hash a regulated tax "
        f"identity, which plan 10.4 withdrew:\n" + "\n".join(hits[:3]))


@pytest.mark.parametrize("path", SQL_MIGRATIONS, ids=lambda p: p.name)
def test_no_migration_hashes_a_tax_identity(path):
    """The same question of the schema, where v1.0 actually did it."""
    hits = _hash_near_tax_identity(_executable_sql(path))
    assert not hits, (
        f"{path.name} appears to hash a regulated tax identity in SQL:\n"
        + "\n".join(hits[:3]))


def test_the_scanner_catches_the_defect_it_is_written_against(tmp_path):
    """Mutation check. A scanner that matches nothing passes everything.

    The sample is the withdrawn v1.0 approach, written the way
    `010_integration.sql` records it.
    """
    module = tmp_path / "regression.py"
    module.write_text(
        "import hashlib\n"
        "def store(vendor):\n"
        "    gst_no = hashlib.sha256(vendor['gst_no'].encode()).hexdigest()[:16]\n"
        "    return gst_no\n",
        encoding="utf-8")
    assert _hash_near_tax_identity(_executable_python(module))

    sql = tmp_path / "regression.sql"
    sql.write_text(
        "ALTER TABLE vendor_master ADD COLUMN gst_no_hash text;\n"
        "UPDATE vendor_master SET gst_no_hash = digest(gst_no, 'sha256');\n",
        encoding="utf-8")
    assert _hash_near_tax_identity(_executable_sql(sql))


def test_the_scanner_does_not_fire_on_prose_about_the_defect(tmp_path):
    """The other half: a module explaining the rule must not fail the rule.

    Without this the cheapest way to make the scan pass would be to delete the
    explanation, which is the opposite of what anyone wants.
    """
    module = tmp_path / "prose.py"
    module.write_text(
        '"""Never hash a gst_no with hashlib.sha256 -- see plan 10.4."""\n'
        "def mask(value):\n"
        "    # hashing a gstin with sha256 was withdrawn in v1.2\n"
        "    return value[:7] + '****' + value[-3:]\n",
        encoding="utf-8")
    assert not _hash_near_tax_identity(_executable_python(module))


# ======================================================================
# The other half: the plaintext must still be there
# ======================================================================
def test_the_regulated_columns_are_stored_in_full_and_validated_as_such():
    """A digest column would satisfy every "never hashed" assertion.

    So would deleting the column. What proves the value is the real
    identifier is the CHECK constraint: a 15-character GSTIN shape and a
    10-character PAN shape cannot be satisfied by a hash, which is why the
    presence of those regexes is the assertion here.
    """
    schema = (PG_MIGRATIONS / "005_master_data.sql").read_text(encoding="utf-8")
    assert re.search(r"gst_no\s+text", schema)
    assert "[0-9]{2}[A-Z0-9]{13}" in schema, (
        "vendor_master.gst_no no longer validates the 15-character GSTIN "
        "shape. A column that accepts anything accepts a digest.")
    assert "[A-Z]{5}[0-9]{4}[A-Z]" in schema, (
        "vendor_master.pan_no no longer validates the PAN shape.")

    foundation = (PG_MIGRATIONS / "001_foundation.sql").read_text(encoding="utf-8")
    assert re.search(r"gst_no\s+text", foundation)
    assert re.search(r"pan_no\s+text", foundation)


def test_no_table_anywhere_carries_a_hashed_copy_of_a_tax_identity():
    """A second column holding the digest is the same defect in a new place."""
    offenders = []
    pattern = re.compile(
        r"\b(gst|gstin|pan)[a-z_]*(hash|digest|sha|fingerprint|token)\b", re.IGNORECASE)
    for path in SQL_MIGRATIONS:
        for match in pattern.finditer(_executable_sql(path)):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert not offenders, (
        "a column name suggests a hashed tax identity: " + ", ".join(offenders))


# ======================================================================
# Masked by default, revealed only on permission, and audited
# ======================================================================
def test_masking_never_leaks_the_middle_of_the_value():
    """The mask is a display control; it must actually withhold something."""
    from app.backend.pg import masters

    gst = "27ABCDE1234A1Z5"
    pan = "ABCDE1234F"

    masked_gst = masters.mask_gst_no(gst)
    masked_pan = masters.mask_pan_no(pan)

    assert masked_gst != gst and masked_pan != pan
    assert "1234" not in masked_gst, (
        f"the masked GSTIN {masked_gst!r} still shows the registrant digits.")
    assert "1234" not in masked_pan
    assert masked_gst.startswith("27ABCDE") and masked_gst.endswith("1Z5"), (
        "the mask must keep enough for an operator to recognise the row; "
        "withholding everything makes the screen unusable and pushes people "
        "to reveal.")


@pytest.mark.parametrize("value", ["", "AB", "ABC", "1234567890123456789"])
def test_masking_a_degenerate_value_never_returns_it_unchanged(value):
    """A short or odd value must not fall through the mask.

    The dangerous shape here is a fallback that gives up and returns the
    input: it fires exactly on the values a test corpus does not contain.
    """
    from app.backend.pg import masters

    for mask in (masters.mask_gst_no, masters.mask_pan_no):
        masked = mask(value)
        if not value:
            assert masked is None, "an empty value must mask to None, not to a mask string"
        else:
            assert masked != value, (
                f"{mask.__name__}({value!r}) returned the value unchanged.")


def test_a_single_character_value_is_the_documented_limit_of_masking():
    """STATED, not smoothed over.

    `_mask` falls back to "first character, then asterisks" for a value too
    short to show both ends. For a ONE-character value that fallback is the
    value, so nothing is withheld. This is asserted rather than hidden because
    the mitigation is a schema constraint, not the masking function:
    `vendor_master.gst_no` and `pan_no` carry CHECK regexes pinning the full
    15- and 10-character shapes (`005_master_data.sql`), so a one-character
    value cannot be stored there at all.

    `entity.gst_no` / `entity.pan_no` (`001_foundation.sql`) carry NO such
    CHECK, so the case is reachable there in principle. It is recorded in this
    wave's report rather than fixed here: widening `_mask` would move
    `tests/test_pg_masters.py::test_mask_never_raises_on_a_too_short_value`,
    which this wave does not own.
    """
    from app.backend.pg import masters

    assert masters.mask_gst_no("A") == "A"
    assert masters.mask_pan_no("A") == "A"
    assert masters.mask_gst_no("AB") == "A*", (
        "two characters onwards the fallback does withhold something; if this "
        "changed, the single-character limit above may have been closed and "
        "this whole test should be revisited.")


def test_revealing_is_a_strictly_narrower_right_than_reading():
    """Holding read must never be enough to unmask."""
    from app.backend import auth

    for read, reveal in (("settings.read", "settings.tax_identity.reveal"),
                         ("masters.read", "masters.tax_identity.reveal")):
        holders_read = set(auth.PERMISSIONS[read])
        holders_reveal = set(auth.PERMISSIONS[reveal])
        assert holders_reveal < holders_read, (
            f"{reveal} is not a strict subset of {read} "
            f"({sorted(holders_reveal)} vs {sorted(holders_read)}). Masking "
            "would then be bypassable by holding read alone.")


def test_a_reveal_writes_an_audit_entry_from_the_service_not_the_router():
    """A router can forget. The service is the only place it cannot.

    Asserted on the executable source of the reveal function, so moving the
    audit call up into a route -- where the next route to be written will not
    have it -- fails here.
    """
    from app.backend.pg import masters

    module = ast.parse(Path(inspect.getfile(masters)).read_text(encoding="utf-8"))
    func = next(
        node for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "reveal_vendor_tax_identity")
    source = ast.unparse(func)
    calls = {
        getattr(node.func, "attr", getattr(node.func, "id", None))
        for node in ast.walk(func) if isinstance(node, ast.Call)
    }
    assert "append" in calls or "audit_append" in calls, (
        "reveal_vendor_tax_identity no longer writes an audit entry. An "
        "unaudited reveal of a regulated identifier is indistinguishable from "
        "no control at all.")
    assert "REVEAL_TAX_IDENTITY" in source, (
        "the reveal no longer names itself in the audit action code, so the "
        "entries cannot be found by anyone looking for them.")
