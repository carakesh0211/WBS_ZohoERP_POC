# Seed fragments

One file per backend stream, loaded by the lead **after** `seed_demo.sql`.

`migrate_pg.discover()` scans only top-level `*.sql` entries in `migrations/pg/`,
so a subdirectory cannot break migration discovery — which is exactly what
`seed_demo.sql` did when it was placed alongside the migrations in M1-S2.

Numbering matches the migration that creates the tables the fragment fills:
`003_budget.sql`, `004_access.sql`, `005_masters.sql`.

All money is integer paise. All rows are demo data, loadable only through
`app/backend/pg/seed.py`, whose profile and disposable-name guards are not
bypassable by `--force`.
