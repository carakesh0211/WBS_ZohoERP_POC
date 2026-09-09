"""POC(SQLite) -> PostgreSQL migration tooling.

Five modules, each doing one thing that can be proved on its own:

``schema``      Reads BOTH schemas. The PostgreSQL side is read by a
                depth-aware scanner over ``migrations/pg/*.sql``, not by a
                regex over the ``CREATE TABLE`` body -- a regex cannot cross a
                nested paren, which is exactly the defect found in the money-SQL
                gate this wave.
``export_poc``  Deterministic NDJSON export + ``manifest.json``. Read-only
                against the source, integers stay integers, and the one table
                whose bytes are hashed is exported verbatim.
``reconcile``   Row counts and paisa totals, per table AND per dimension. One
                paisa of difference is a failure.
``audit_chain`` The LEGACY stream: what may be copied, what may not be
                recomputed, and the three ways the obvious import strategy
                makes ``verify_chain`` report a break.
``import_pg``   FK-topological, single transaction, foreign keys ENABLED,
                resumable from a checkpoint, and abort-on-anything.

Every module is importable without psycopg and without a server: the parts that
need PostgreSQL are behind an explicit connection argument, so the arithmetic,
the ordering and the refusals are all testable on a workstation.
"""
