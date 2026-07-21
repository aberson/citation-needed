# migrations/

Numbered SQL migrations for `data/citation.db`. **From v0.1 onward, migrations own every change
to an existing DB** — `schema.sql` is executed only against brand-new DBs (`cite init-db`) and is
never re-run against a DB that already has tables. The corpus is produced by live search + review
judgment (expensive, not rebuildable-from-seed), so "just rebuild from schema.sql" does not apply
here (plan.md §3.1; docs/research/schema-draft.md §6).

## Numbering convention

- Filename shape: `000N_short-description.sql` — a zero-padded integer prefix (`0002`, `0003`, …)
  followed by an underscore and a kebab slug. The integer `N` is the schema version the migration
  produces.
- `db.migrate()` reads the DB's current `PRAGMA user_version`, globs `migrations/*.sql`, and
  applies every file whose `N` is **greater than** the current `user_version`, in ascending
  filename order. Each migration runs in its own transaction; on success `user_version` is set to
  that migration's `N` (inside the same transaction, so a failed migration rolls back both the DDL
  and the version bump).
- `schema.sql` sets `PRAGMA user_version = 1`, so the first migration is `0002_*.sql`.
- Never renumber or edit an already-shipped migration — append a new one. When a migration lands,
  also update `schema.sql` so a brand-new DB and a migrated DB converge on the same schema.

No migrations exist yet (schema v1 is the `schema.sql` baseline).
