"""``cite`` — argparse CLI entry point.

Purely mechanical verbs (the CLI never calls an LLM). v1 skeleton verbs:

- ``cite init-db``  — execute schema.sql against a brand-new DB (no-op if initialized)
- ``cite migrate``  — apply pending migrations/000N_*.sql in filename order
- ``cite status``   — per-table row counts + schema version; WAL-checkpoints the DB
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from citation_needed import db

_DB_HELP = "Path to the SQLite database (default: data/citation.db under the project root)."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cite",
        description="Citation trail for choices embedded in LLM-facing files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser("init-db", help="Create a new DB from schema.sql.")
    p_init.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_migrate = subparsers.add_parser("migrate", help="Apply pending numbered migrations.")
    p_migrate.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_status = subparsers.add_parser(
        "status", help="Per-table row counts + schema version; WAL-checkpoints the DB."
    )
    p_status.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    return parser


def _cmd_init_db(db_path: Path) -> int:
    created = db.init_db(db_path)
    if created:
        print(f"Initialized new database at {db_path.as_posix()} (schema v1).")
    else:
        print(f"Database at {db_path.as_posix()} already has tables — init-db is a no-op.")
        print("Schema changes to an existing DB ship as migrations (see migrations/README.md).")
    return 0


def _cmd_migrate(db_path: Path) -> int:
    try:
        applied = db.migrate(db_path)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        # FileNotFoundError: no DB yet; ValueError: bad filename / trailing incomplete
        # statement; sqlite3.Error: broken migration SQL. All exit clean, never traceback.
        print(f"error: {exc}")
        return 1
    if applied:
        versions = ", ".join(str(v) for v in applied)
        print(f"Applied {len(applied)} migration(s): {versions}.")
    else:
        print("Database is up to date — no pending migrations.")
    return 0


def _cmd_status(db_path: Path) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        print(f"Database: {db_path.as_posix()}")
        print(f"Schema version (PRAGMA user_version): {version}")
        for table in db.CANONICAL_TABLES:
            try:
                # table names come from the trusted CANONICAL_TABLES constant, never user input
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                print(f"  {table:<16} MISSING")
                continue
            print(f"  {table:<16} {count} row(s)")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``cite`` console script. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "init-db":
        return _cmd_init_db(args.db)
    if args.command == "migrate":
        return _cmd_migrate(args.db)
    if args.command == "status":
        return _cmd_status(args.db)
    raise AssertionError(f"unreachable: unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
