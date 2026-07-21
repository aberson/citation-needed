"""SQLite connection factory, schema bootstrap, and the numbered-migration loop.

``schema.sql`` (project root) is the canonical DDL, executed ONLY against a brand-new DB.
Every change to an existing DB after v0.1 ships as ``migrations/000N_*.sql``, applied in
filename order by :func:`migrate` reading ``PRAGMA user_version``. See plan.md §3.1.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "citation.db"

#: The 7 canonical tables, in creation order (no forward FK references).
CANONICAL_TABLES = (
    "artifacts",
    "review_runs",
    "choices",
    "citations",
    "choice_citations",
    "scores",
    "distill_queue",
)

FTS_TABLE = "citations_fts"

_MIGRATION_NAME_RE = re.compile(r"^(\d+)_[\w-]+\.sql$")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` with the project-wide connection defaults.

    WAL mode + ``busy_timeout=5000`` (a project-wide sweep fans out per-artifact subagents
    whose verify/insert calls can overlap; plan.md §3.1) and ``foreign_keys=ON`` (per-connection
    pragma, so it must be re-asserted here, not only in schema.sql).
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return bool(row[0])


def init_db(path: str | Path, schema_path: str | Path = SCHEMA_PATH) -> bool:
    """Execute ``schema.sql`` against a NEW database at ``path``.

    Idempotent: if the DB already contains tables, this is a no-op returning ``False``
    (the CLI reports it); a fresh initialization returns ``True``. Parent directories
    are created as needed.
    """
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = Path(schema_path).read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        if _has_tables(conn):
            return False
        conn.executescript(schema_sql)
        conn.commit()
        return True
    finally:
        conn.close()


def _iter_statements(sql: str) -> Iterator[str]:
    """Split a migration script into complete SQL statements.

    Uses :func:`sqlite3.complete_statement` (sqlite3_complete), which understands
    ``CREATE TRIGGER ... BEGIN ... END;`` blocks — a naive semicolon split does not.
    Statements are peeled off at semicolon boundaries WITHIN the accumulated buffer,
    so multiple statements sharing one physical line each yield separately
    (``conn.execute`` accepts only one statement at a time). NOT ``executescript``:
    that issues an implicit COMMIT first, breaking per-migration rollback.
    """
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        while True:
            statement, buf = _split_leading_statement(buf)
            if statement is None:
                break
            yield statement
    meaningful = any(
        stripped and not stripped.startswith("--")
        for stripped in (line.strip() for line in buf.splitlines())
    )
    if meaningful:
        raise ValueError(
            "migration script has a trailing incomplete statement (missing ';'?): "
            f"{buf.strip()[:80]!r}"
        )


def _split_leading_statement(buf: str) -> tuple[str | None, str]:
    """Split the shortest complete-statement prefix off ``buf``.

    Scans successive ``;`` boundaries and returns ``(statement, remainder)`` at the
    first prefix :func:`sqlite3.complete_statement` accepts — semicolons inside string
    literals, comments, or trigger BEGIN…END bodies do not complete a statement — or
    ``(None, buf)`` when no prefix is complete yet (caller accumulates more lines).
    """
    offset = 0
    while (semi := buf.find(";", offset)) != -1:
        candidate = buf[: semi + 1]
        if candidate.strip() and sqlite3.complete_statement(candidate):
            return candidate, buf[semi + 1 :]
        offset = semi + 1
    return None, buf


def _pending_migrations(current_version: int, migrations_dir: Path) -> list[tuple[int, Path]]:
    pending: list[tuple[int, Path]] = []
    for file in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME_RE.match(file.name)
        if match is None:
            raise ValueError(
                f"migration filename does not match the 000N_<slug>.sql convention: {file.name}"
            )
        version = int(match.group(1))
        if version > current_version:
            pending.append((version, file))
    return pending


def migrate(path: str | Path, migrations_dir: str | Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every ``migrations/000N_*.sql`` with ``N > PRAGMA user_version``, in order.

    Each migration runs in its own transaction; ``user_version`` is set to the migration's
    ``N`` inside the same transaction, so a failed migration rolls back both the DDL and
    the version bump. Returns the list of applied version numbers (empty if up to date).
    """
    db_path = Path(path)
    if not db_path.exists():
        raise FileNotFoundError(f"database does not exist (run `cite init-db` first): {db_path}")
    conn = connect(db_path)
    applied: list[int] = []
    try:
        conn.isolation_level = None  # explicit transaction control
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for version, file in _pending_migrations(current_version, Path(migrations_dir)):
            script = file.read_text(encoding="utf-8")
            conn.execute("BEGIN")
            try:
                for statement in _iter_statements(script):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version:d}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            applied.append(version)
        return applied
    finally:
        conn.close()
