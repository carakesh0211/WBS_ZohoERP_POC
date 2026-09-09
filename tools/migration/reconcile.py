"""Source-vs-target reconciliation. One paisa of difference is a failure.

Three checks, and the third is the one that catches the interesting bugs
-----------------------------------------------------------------------
**Row counts, per table.** Catches a truncated batch and a resumed job that
re-inserted. Necessary, and on its own almost worthless: it cannot see a row
that arrived with the wrong amount.

**Paisa totals, per table per money column.** Catches a lost row, a doubled
row, and a value damaged in transit. Still not enough on its own: two errors
that cancel -- a hundred rupees moved from one project to another -- leave every
table total identical.

**Paisa totals, per DIMENSION.** ``SUM(amount_paise) GROUP BY project_id``, and
the same for entity and WBS. This is the check that sees value moving between
owners while the estate total stays put. It is the reason the reconciliation is
not one ``SELECT SUM``.

Why integers all the way down
-----------------------------
Every total here is a Python ``int``. Python integers are arbitrary precision,
so no total can overflow and no sum can drift. Nothing in this module accepts,
produces or compares a float, and :func:`_as_paise` REFUSES one rather than
coercing it -- a reconciliation that rounds is a reconciliation that passes when
it should not.

``Decimal`` is refused too, and less obviously. psycopg returns PostgreSQL
``numeric`` as ``Decimal``, and a money column that has become ``numeric``
somewhere in the schema is a defect this check should REPORT rather than absorb
by calling ``int()`` on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

#: Columns to group money by when the table has them. Ordered, so a report
#: generated twice lists its dimensions the same way.
DIMENSION_COLUMNS = ("entity_id", "project_id", "wbs_id", "budget_head_id")

MONEY_SUFFIX = "_paise"


class ReconciliationError(RuntimeError):
    """A difference was found. The migration does not proceed."""


def _as_paise(table: str, column: str, value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReconciliationError(
            f"{table}.{column} contributed {value!r} ({type(value).__name__}) to a "
            "paisa total. Money is an integer count of paise. A float or a "
            "Decimal here means the value has been through a representation "
            "that cannot be trusted to the paisa, and the reconciliation "
            "refuses to average over it."
        )
    return value


def money_columns(columns: Iterable[str]) -> list[str]:
    return [c for c in columns if c.endswith(MONEY_SUFFIX)]


def dimension_columns(columns: Iterable[str]) -> list[str]:
    present = set(columns)
    return [c for c in DIMENSION_COLUMNS if c in present]


@dataclass
class TableTotals:
    """Everything reconciled about one table."""
    table: str
    rows: int = 0
    #: {money column: total paise}
    totals: dict[str, int] = field(default_factory=dict)
    #: {dimension column: {dimension value: {money column: total paise}}}
    by_dimension: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "totals": dict(sorted(self.totals.items())),
            "by_dimension": {
                dim: {str(k): dict(sorted(v.items())) for k, v in sorted(values.items(),
                                                                        key=lambda kv: str(kv[0]))}
                for dim, values in sorted(self.by_dimension.items())
            },
        }


def totals_from_records(table: str, columns: Sequence[str],
                        records: Iterable[Mapping[str, Any]]) -> TableTotals:
    """Totals over an in-memory row source -- the exported NDJSON, typically."""
    money = money_columns(columns)
    dims = dimension_columns(columns)
    out = TableTotals(table=table)
    out.totals = {c: 0 for c in money}
    out.by_dimension = {d: {} for d in dims}

    for record in records:
        out.rows += 1
        for column in money:
            out.totals[column] += _as_paise(table, column, record.get(column))
        for dim in dims:
            key = record.get(dim)
            key = "\x00NULL" if key is None else str(key)
            bucket = out.by_dimension[dim].setdefault(key, {c: 0 for c in money})
            for column in money:
                bucket[column] += _as_paise(table, column, record.get(column))
    return out


def totals_from_sql(table: str, columns: Sequence[str],
                    query: Callable[[str, tuple], list[tuple]]) -> TableTotals:
    """Totals computed BY THE SERVER, for the target side.

    `query` runs one statement and returns rows -- a two-line adapter over a
    psycopg cursor, kept as a callable so this module needs no driver and the
    arithmetic can be exercised against a recorded query log.

    The money sums are cast to ``bigint``. PostgreSQL's ``SUM(bigint)`` returns
    ``numeric``, which psycopg hands back as a ``Decimal`` -- and
    :func:`_as_paise` refuses a Decimal, so without the cast every target total
    would be refused. The cast is safe because paise fit in a bigint by
    construction and would have overflowed the COLUMN long before the sum.
    """
    money = money_columns(columns)
    dims = dimension_columns(columns)
    out = TableTotals(table=table)

    sums = ", ".join(f'COALESCE(SUM("{c}"), 0)::bigint' for c in money)
    select = f'SELECT COUNT(*)::bigint{", " + sums if money else ""} FROM "{table}"'
    row = query(select, ())[0]
    out.rows = _as_paise(table, "count", row[0])
    out.totals = {c: _as_paise(table, c, row[i + 1]) for i, c in enumerate(money)}

    out.by_dimension = {}
    for dim in dims:
        out.by_dimension[dim] = {}
        if not money:
            continue
        grouped = query(
            f'SELECT "{dim}", {sums} FROM "{table}" GROUP BY "{dim}" ORDER BY "{dim}"', ())
        for grow in grouped:
            key = "\x00NULL" if grow[0] is None else str(grow[0])
            out.by_dimension[dim][key] = {
                c: _as_paise(table, c, grow[i + 1]) for i, c in enumerate(money)}
    return out


def compare(source: TableTotals, target: TableTotals) -> list[str]:
    """Every difference, as a list of human-readable lines. ``[]`` means equal.

    Returns ALL differences rather than the first. A migration report that names
    one discrepancy sends someone to fix it and re-run to find the next; naming
    all of them is one round trip.
    """
    problems: list[str] = []
    if source.table != target.table:
        problems.append(f"comparing different tables: {source.table} vs {target.table}")
    if source.rows != target.rows:
        problems.append(
            f"{source.table}: row count {source.rows} -> {target.rows} "
            f"(difference {target.rows - source.rows:+d})")

    for column in sorted(set(source.totals) | set(target.totals)):
        s = source.totals.get(column)
        t = target.totals.get(column)
        if s is None:
            problems.append(f"{source.table}.{column}: on the target only")
            continue
        if t is None:
            problems.append(f"{source.table}.{column}: on the source only")
            continue
        if s != t:
            problems.append(
                f"{source.table}.{column}: {s} -> {t} paise "
                f"(difference {t - s:+d} paise)")

    for dim in sorted(set(source.by_dimension) | set(target.by_dimension)):
        s_dim = source.by_dimension.get(dim, {})
        t_dim = target.by_dimension.get(dim, {})
        for key in sorted(set(s_dim) | set(t_dim)):
            s_vals = s_dim.get(key, {})
            t_vals = t_dim.get(key, {})
            shown = "NULL" if key == "\x00NULL" else key
            for column in sorted(set(s_vals) | set(t_vals)):
                s = s_vals.get(column, 0)
                t = t_vals.get(column, 0)
                if s != t:
                    problems.append(
                        f"{source.table}.{column} where {dim}={shown}: "
                        f"{s} -> {t} paise (difference {t - s:+d} paise)")
    return problems


def reconcile(source: Mapping[str, TableTotals],
              target: Mapping[str, TableTotals]) -> dict[str, Any]:
    """Reconcile every table. ``ok`` is False if ONE paisa differs anywhere."""
    problems: list[str] = []
    for table in sorted(set(source) | set(target)):
        if table not in target:
            problems.append(f"{table}: present on the source, absent on the target")
            continue
        if table not in source:
            problems.append(f"{table}: present on the target, absent on the source")
            continue
        problems.extend(compare(source[table], target[table]))

    return {
        "ok": not problems,
        "tables_compared": len(set(source) & set(target)),
        "source_rows": sum(t.rows for t in source.values()),
        "target_rows": sum(t.rows for t in target.values()),
        "differences": problems,
        "tolerance": "zero. One paisa of difference fails the migration.",
    }


def assert_reconciled(result: Mapping[str, Any]) -> None:
    if not result.get("ok"):
        raise ReconciliationError(
            "reconciliation failed; the migration is aborted and rolled back:\n  "
            + "\n  ".join(result.get("differences", []))
        )
