"""``cite`` — argparse CLI entry point.

Purely mechanical verbs (the CLI never calls an LLM). v1 verbs:

- ``cite init-db``  — execute schema.sql against a brand-new DB (no-op if initialized)
- ``cite migrate``  — apply pending migrations/000N_*.sql in filename order
- ``cite status``   — per-table row counts + schema version; WAL-checkpoints the DB
- ``cite scan``     — discover + type LLM-facing artifacts, upsert into ``artifacts``
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from citation_needed import db, discover
from citation_needed.models import DETAILS_MODELS

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

    p_scan = subparsers.add_parser(
        "scan", help="Discover + type LLM-facing artifacts and upsert them into the DB."
    )
    p_scan.add_argument(
        "--project",
        default=None,
        help="Only upsert artifacts resolving to this project slug "
        "(a registry slug, 'coding-root', or 'global').",
    )
    p_scan.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Workspace root to scan (default: CITATION_NEEDED_WORKSPACE_ROOT env var, "
        "else the parent of the citation-needed project root).",
    )
    p_scan.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="Root of the per-project memory dirs (default: ~/.claude/projects). "
        "Mainly for hermetic tests.",
    )
    p_scan.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

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


def _cmd_scan(
    db_path: Path,
    workspace_root: Path | None,
    memory_root: Path | None,
    project_filter: str | None,
) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    root = workspace_root if workspace_root is not None else discover.default_workspace_root()
    if not root.is_dir():
        print(f"error: workspace root does not exist: {root.as_posix()}")
        return 1

    try:
        report = discover.scan_workspace(root, memory_root=memory_root)
    except OSError as exc:
        # scan_workspace degrades per-entry; a root-level filesystem failure still
        # surfaces here as the established clean contract (discover raises, cli catches).
        print(f"error: {exc}")
        return 1
    selected = [
        artifact
        for artifact in report.artifacts
        if project_filter is None or artifact.project == project_filter
    ]

    counts: dict[str, Counter[str]] = {}
    conn = db.connect(db_path)
    try:
        try:
            with conn:  # one transaction for the whole upsert pass
                for artifact in selected:
                    status = discover.upsert_artifact(conn, artifact)
                    counts.setdefault(artifact.artifact_type, Counter())[status] += 1
        except (ValidationError, sqlite3.Error) as exc:
            # ValidationError: a details builder produced a shape its model rejects;
            # sqlite3.Error: constraint/IO failure mid-upsert (the transaction rolls
            # back). Both exit clean, never traceback (db raises -> cli catches).
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()

    print(f"Scanned workspace: {report.workspace_root.as_posix()}")
    if project_filter is not None:
        print(
            f"Project filter: {project_filter} "
            f"({len(selected)} of {len(report.artifacts)} discovered artifact(s))"
        )
    else:
        print(f"Discovered {len(report.artifacts)} artifact(s).")
    print("Per-type counts:")
    for artifact_type in sorted(DETAILS_MODELS):
        type_counts = counts.get(artifact_type, Counter())
        total = sum(type_counts.values())
        print(
            f"  {artifact_type:<10} {total} ({type_counts['new']} new, "
            f"{type_counts['updated']} updated, {type_counts['unchanged']} unchanged)"
        )
    if report.notes:
        print("Pointer/frontmatter notes:")
        for note in report.notes:
            print(f"  {note}")
    print(
        f"Exclusions: {report.excluded_dir_count} excluded dir subtree(s) "
        f"(.venv/node_modules/.git/docs archived); "
        f"not-owned tree(s) skipped: {', '.join(report.not_owned_skipped) or 'none'}; "
        f"{report.memory_index_skipped} memory index file(s) (MEMORY.md) skipped"
    )
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
    if args.command == "scan":
        return _cmd_scan(args.db, args.workspace_root, args.memory_root, args.project)
    raise AssertionError(f"unreachable: unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
