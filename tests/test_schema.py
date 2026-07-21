"""Schema bootstrap, details_json round-trips, drift guards, CLI integration.

Covers the Step 1 acceptance target: init-db creates all 7 tables + the FTS5 index; a
golden details_json blob per artifact_type round-trips through its pydantic model; the
DETAILS_MODELS registry covers exactly the artifact_type CHECK enum in schema.sql (the
one-source-of-truth drift test); the production CLI entry works end-to-end; and the
citations anti-fabrication CHECK constraints are DB-enforced.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from citation_needed import db, models
from citation_needed.cli import main

SCHEMA_TEXT = db.SCHEMA_PATH.read_text(encoding="utf-8")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "citation.db"
    assert db.init_db(path) is True
    return path


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# (a) init-db creates all 7 tables + the FTS5 table
# ---------------------------------------------------------------------------


def test_init_db_creates_all_tables(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        names = _table_names(conn)
        for table in db.CANONICAL_TABLES:
            assert table in names, f"missing canonical table: {table}"
        assert db.FTS_TABLE in names, "missing FTS5 external-content table"
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        # Load-bearing for concurrent sweep fan-out (plan.md §3.1) — assert, don't assume.
        assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
    finally:
        conn.close()


def test_init_db_is_idempotent(db_path: Path) -> None:
    # Second run against an initialized DB: no-op, no exception, reports False.
    assert db.init_db(db_path) is False


def test_fts_sync_triggers_exist(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        triggers = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert {"citations_fts_ai", "citations_fts_ad", "citations_fts_au"} <= triggers
    finally:
        conn.close()


def _insert_review_scaffold(conn: sqlite3.Connection) -> None:
    """Minimal artifact + review_run rows satisfying FKs, via real column names."""
    conn.execute(
        "INSERT INTO artifacts (path, artifact_type, project, first_seen_at) "
        "VALUES ('.claude/rules/security.md', 'rule', 'coding-root', '2026-07-21T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO review_runs (artifact_id, started_at, artifact_content_hash_at_review, "
        "reviewer_model, tool_schema_version) "
        "VALUES (1, '2026-07-21T00:00:00Z', 'deadbeef', 'claude-sonnet-5', 1)"
    )


def _insert_lost_in_the_middle(conn: sqlite3.Connection) -> None:
    """One canonical external citation row; the ai trigger indexes it into citations_fts."""
    conn.execute(
        "INSERT INTO citations (kind, natural_key, title, url_or_doi, verified_at, "
        "resolution_method, supporting_quote, keywords) "
        "VALUES ('external', 'arxiv:2307.03172', 'Lost in the Middle', "
        "'https://arxiv.org/abs/2307.03172', '2026-07-21T00:00:00Z', 'api_structured', "
        "'performance is highest when relevant information occurs at the beginning', "
        "'long-context position-bias retrieval')"
    )
    conn.commit()


def _fts_hits(conn: sqlite3.Connection, term: str) -> int:
    rows = conn.execute(
        "SELECT rowid FROM citations_fts WHERE citations_fts MATCH ?", (term,)
    ).fetchall()
    return len(rows)


def test_fts_index_syncs_on_insert_and_matches(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        _insert_lost_in_the_middle(conn)
        assert _fts_hits(conn, "middle") == 1
    finally:
        conn.close()


def test_fts_index_syncs_on_update(db_path: Path) -> None:
    """Functional coverage for citations_fts_au: a broken UPDATE trigger body would leave
    stale terms MATCHing (silent BM25 corpus-first corruption), which name-existence and
    INSERT-only tests never see."""
    conn = db.connect(db_path)
    try:
        _insert_lost_in_the_middle(conn)
        assert _fts_hits(conn, "middle") == 1
        conn.execute(
            "UPDATE citations SET title = 'Attention Is All You Need', "
            "supporting_quote = 'the transformer relies entirely on self-attention', "
            "keywords = 'transformer self-attention architecture' "
            "WHERE kind = 'external' AND natural_key = 'arxiv:2307.03172'"
        )
        conn.commit()
        assert _fts_hits(conn, "middle") == 0, "stale pre-update term still MATCHes"
        assert _fts_hits(conn, "transformer") == 1, "post-update term not indexed"
    finally:
        conn.close()


def test_fts_index_syncs_on_delete(db_path: Path) -> None:
    """Functional coverage for citations_fts_ad: a broken DELETE trigger body would keep
    serving deleted corpus rows from the index."""
    conn = db.connect(db_path)
    try:
        _insert_lost_in_the_middle(conn)
        assert _fts_hits(conn, "middle") == 1
        conn.execute(
            "DELETE FROM citations WHERE kind = 'external' AND natural_key = 'arxiv:2307.03172'"
        )
        conn.commit()
        assert _fts_hits(conn, "middle") == 0, "deleted row still MATCHes"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (b) golden details_json blob per artifact_type round-trips through its model
# ---------------------------------------------------------------------------

GOLDEN_DETAILS: dict[str, dict[str, object]] = {
    "memory": {
        "node_type": "memory",
        "memory_kind": "feedback",
        "origin_session_id": "740ce8b1-8ae8-41e5-a58b-7245a7601fee",
        "memory_scope": "global",
        "frontmatter_modified": "2026-07-21T00:00:00Z",
    },
    "skill": {
        "name": "session-wrap",
        "description": "The session-transition front door.",
        "user_invocable": True,
        "has_evals": True,
        "is_pointer": True,
        "pointer_target": ".claude/skills/_shared/session-wrap-core.md",
    },
    "rule": {
        "source_memory_paths": [
            "memory:c--Users-abero-dev/feedback_external_content_prompt_injection.md"
        ],
    },
    "claude_md": {"scope": "project", "project_slug": "citation-needed"},
    "plan": {
        "plan_kind": "root",
        "is_pointer_only": False,
        "step_count": 12,
        "phase_count": 0,
    },
}


@pytest.mark.parametrize("artifact_type", sorted(GOLDEN_DETAILS))
def test_golden_details_json_round_trips(artifact_type: str) -> None:
    model_cls = models.DETAILS_MODELS[artifact_type]
    golden_json = json.dumps(GOLDEN_DETAILS[artifact_type])
    model = model_cls.model_validate_json(golden_json)
    assert json.loads(model.model_dump_json()) == GOLDEN_DETAILS[artifact_type]


@pytest.mark.parametrize("artifact_type", sorted(GOLDEN_DETAILS))
def test_details_models_reject_unknown_fields(artifact_type: str) -> None:
    model_cls = models.DETAILS_MODELS[artifact_type]
    with pytest.raises(ValueError, match="not_a_real_field"):
        model_cls.model_validate({"not_a_real_field": 1})


# ---------------------------------------------------------------------------
# (c) drift test: DETAILS_MODELS registry == the artifact_type CHECK enum in
# schema.sql — parsed from the DDL text, so re-duplication fails here.
# ---------------------------------------------------------------------------


def _schema_artifact_type_enum() -> set[str]:
    match = re.search(
        r"artifact_type\s+TEXT NOT NULL CHECK \(artifact_type IN\s*\(([^)]*)\)",
        SCHEMA_TEXT,
    )
    assert match, "could not locate the artifact_type CHECK enum in schema.sql"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_details_models_registry_matches_schema_enum() -> None:
    schema_enum = _schema_artifact_type_enum()
    assert schema_enum, "parsed an empty artifact_type enum from schema.sql"
    assert set(models.DETAILS_MODELS) == schema_enum


def test_golden_blobs_cover_every_registered_type() -> None:
    assert set(GOLDEN_DETAILS) == set(models.DETAILS_MODELS)


_CREATE_TABLE_RE = re.compile(r"^CREATE TABLE (?:IF NOT EXISTS )?(\w+)", re.MULTILINE)


def test_canonical_tables_matches_schema_create_tables() -> None:
    """Bidirectional drift guard: db.CANONICAL_TABLES == schema.sql's CREATE TABLE set.

    Mirrors test_details_models_registry_matches_schema_enum — set equality, parsed from
    the DDL text, so a table added to either side alone fails here. The regex anchors on
    ``CREATE TABLE``, intentionally skipping ``CREATE VIRTUAL TABLE`` (the FTS5 index);
    its shadow tables are engine-managed and never appear in the DDL.
    """
    schema_tables = set(_CREATE_TABLE_RE.findall(SCHEMA_TEXT))
    assert schema_tables, "parsed zero CREATE TABLE names from schema.sql"
    assert set(db.CANONICAL_TABLES) == schema_tables


# ---------------------------------------------------------------------------
# (d) integration: the production CLI entry point end-to-end
# ---------------------------------------------------------------------------


def test_cli_init_db_and_status_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_db = tmp_path / "data" / "citation.db"

    assert main(["init-db", "--db", str(cli_db)]) == 0
    out = capsys.readouterr().out
    assert "Initialized new database" in out
    assert cli_db.exists()

    # Re-run: idempotent no-op with a clear message, still exit 0.
    assert main(["init-db", "--db", str(cli_db)]) == 0
    assert "already has tables" in capsys.readouterr().out

    assert main(["status", "--db", str(cli_db)]) == 0
    out = capsys.readouterr().out
    assert "Schema version (PRAGMA user_version): 1" in out
    for table in db.CANONICAL_TABLES:
        assert table in out
    assert "0 row(s)" in out
    assert "MISSING" not in out


def test_cli_status_missing_db_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status", "--db", str(tmp_path / "nope.db")]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_cli_migrate_no_pending(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "--db", str(db_path)]) == 0
    assert "no pending migrations" in capsys.readouterr().out


def test_cli_migrate_broken_migration_prints_error(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken migration exits 1 with a clean `error:` line — never a raw traceback."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0002_broken.sql").write_text(
        "CREATE TABLE syntax error here;\n", encoding="utf-8"
    )
    real_migrate = db.migrate
    # Redirect only the migrations dir the CLI's call resolves to; the production
    # migrate() still runs for real against the broken file.
    monkeypatch.setattr(db, "migrate", lambda path: real_migrate(path, migrations_dir=migrations))

    assert main(["migrate", "--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")


# ---------------------------------------------------------------------------
# migrate(): applies 000N_*.sql in order, transactionally, bumping user_version
# ---------------------------------------------------------------------------


def test_migrate_applies_pending_in_order_and_bumps_version(db_path: Path, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0002_add-widget.sql").write_text(
        "CREATE TABLE widget (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )
    (migrations / "0003_add-gadget.sql").write_text(
        "CREATE TABLE gadget (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )

    assert db.migrate(db_path, migrations_dir=migrations) == [2, 3]
    conn = db.connect(db_path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 3
        assert {"widget", "gadget"} <= _table_names(conn)
    finally:
        conn.close()

    # Re-run: nothing pending.
    assert db.migrate(db_path, migrations_dir=migrations) == []


def test_migrate_two_statements_on_one_line_applies(db_path: Path, tmp_path: Path) -> None:
    """conn.execute takes ONE statement at a time; the splitter must peel statements at
    semicolon boundaries within a physical line, not reject the valid migration."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0002_two-on-one-line.sql").write_text(
        "CREATE TABLE a (id INTEGER); CREATE TABLE b (id INTEGER);\n", encoding="utf-8"
    )

    assert db.migrate(db_path, migrations_dir=migrations) == [2]
    conn = db.connect(db_path)
    try:
        assert {"a", "b"} <= _table_names(conn)
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        conn.close()


def test_migrate_failed_migration_rolls_back(db_path: Path, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0002_broken.sql").write_text(
        "CREATE TABLE will_roll_back (id INTEGER PRIMARY KEY);\nCREATE TABLE syntax error here;\n",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.OperationalError):
        db.migrate(db_path, migrations_dir=migrations)

    conn = db.connect(db_path)
    try:
        # Both the DDL and the version bump rolled back together.
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1
        assert "will_roll_back" not in _table_names(conn)
    finally:
        conn.close()


def test_migrate_missing_db_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        db.migrate(tmp_path / "absent.db")


# ---------------------------------------------------------------------------
# (e) citations CHECK constraints — anti-fabrication is DB-enforced
# ---------------------------------------------------------------------------


def test_external_citation_without_url_or_doi_is_rejected(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO citations (kind, natural_key, verified_at, resolution_method) "
                "VALUES ('external', 'doi:10.0000/fabricated', '2026-07-21T00:00:00Z', "
                "'api_structured')"
            )
    finally:
        conn.close()


def test_duplicate_kind_natural_key_is_rejected(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        insert = (
            "INSERT INTO citations (kind, natural_key, url_or_doi, verified_at, "
            "resolution_method) "
            "VALUES ('external', 'arxiv:2307.03172', 'https://arxiv.org/abs/2307.03172', "
            "'2026-07-21T00:00:00Z', 'api_structured')"
        )
        conn.execute(insert)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert)
    finally:
        conn.close()


def test_internal_citation_without_workspace_path_is_rejected(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO citations (kind, natural_key, verified_at, resolution_method) "
                "VALUES ('internal', 'docs/lessons-learned.md', '2026-07-21T00:00:00Z', "
                "'internal-read')"
            )
    finally:
        conn.close()


def test_invalid_resolution_method_is_rejected(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO citations (kind, natural_key, url_or_doi, verified_at, "
                "resolution_method) "
                "VALUES ('external', 'https://example.com', 'https://example.com', "
                "'2026-07-21T00:00:00Z', 'llm_claimed')"
            )
    finally:
        conn.close()


def test_artifacts_path_backslash_is_rejected(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                r"INSERT INTO artifacts (path, artifact_type, project, first_seen_at) "
                r"VALUES ('.claude\rules\security.md', 'rule', 'coding-root', "
                r"'2026-07-21T00:00:00Z')"
            )
    finally:
        conn.close()


def test_scores_share_columns_exist_and_bound(db_path: Path) -> None:
    """Amendment 1: the four *_share vote-share columns are real, REAL, and 0..1-bounded."""
    conn = db.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
        assert {
            "evidence_backed_share",
            "interesting_novel_share",
            "unsupported_share",
            "contradicted_share",
        } <= cols

        _insert_review_scaffold(conn)
        conn.execute(
            "INSERT INTO choices (artifact_id, choice_key, summary, "
            "content_hash_at_extraction, first_extracted_review_run_id, "
            "last_confirmed_review_run_id) "
            "VALUES (1, 'treat-fetched-content-as-data', 'Injection payloads are data', "
            "'cafebabe', 1, 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scores (review_run_id, choice_id, evidence_backed_share, "
                "interesting_novel_share, unsupported_share, contradicted_share, "
                "classification, composite, composite_band, interpretation_guide_version) "
                "VALUES (1, 1, 1.5, 0, 0, 0, 'well-supported', 100.0, 'strong', 'v1')"
            )
    finally:
        conn.close()


def test_choices_source_path_column_exists(db_path: Path) -> None:
    """Amendment 4: choices.source_path (nullable) is a real column."""
    conn = db.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(choices)")}
        assert "source_path" in cols
    finally:
        conn.close()
