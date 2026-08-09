"""Terminal update-selector coverage: handoff only, never skill invocation or writes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from citation_needed import db, update_select
from citation_needed.cli import main


@pytest.fixture()
def selector_db(tmp_path: Path) -> Path:
    path = tmp_path / "citation.db"
    assert db.init_db(path)
    conn = db.connect(path)
    try:
        distill = _artifact(conn, "skills/distill/SKILL.md", "same")
        distill_run = _completed_run(conn, distill, "same")
        _needs_improvement_score(conn, distill, distill_run)

        fresh = _artifact(conn, "skills/fresh/SKILL.md", "same")
        _completed_run(conn, fresh, "same")

        stale = _artifact(conn, "skills/stale/SKILL.md", "new")
        stale_run = _completed_run(conn, stale, "old")
        _needs_improvement_score(conn, stale, stale_run)

        _artifact(conn, "skills/unreviewed/SKILL.md", "current")
        conn.commit()
    finally:
        conn.close()
    return path


def _artifact(conn: sqlite3.Connection, path: str, content_hash: str) -> int:
    cursor = conn.execute(
        "INSERT INTO artifacts (path, artifact_type, project, current_content_hash, first_seen_at) "
        "VALUES (?, 'skill', 'test-project', ?, '2026-08-09T00:00:00Z')",
        (path, content_hash),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _completed_run(conn: sqlite3.Connection, artifact_id: int, content_hash: str) -> int:
    cursor = conn.execute(
        "INSERT INTO review_runs (artifact_id, started_at, finished_at, "
        "artifact_content_hash_at_review, reviewer_model, tool_schema_version) "
        "VALUES (?, '2026-08-09T00:00:00Z', '2026-08-09T01:00:00Z', "
        "?, 'claude-code-2.1.212', 2)",
        (artifact_id, content_hash),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _needs_improvement_score(
    conn: sqlite3.Connection, artifact_id: int, review_run_id: int
) -> None:
    choice = conn.execute(
        "INSERT INTO choices (artifact_id, choice_key, summary, content_hash_at_extraction, "
        "first_extracted_review_run_id, last_confirmed_review_run_id) "
        "VALUES (?, 'weak-choice', 'Weak choice.', 'quote-hash', ?, ?)",
        (artifact_id, review_run_id, review_run_id),
    )
    assert choice.lastrowid is not None
    conn.execute(
        "INSERT INTO scores (review_run_id, choice_id, evidence_backed_share, "
        "interesting_novel_share, unsupported_share, contradicted_share, classification, "
        "composite, composite_band, interpretation_guide_version, literature_searched, "
        "literature_found) VALUES (?, ?, 0.0, 0.0, 1.0, 0.0, 'needs-improvement', "
        "25.0, 'weak', 'v1', 0, 0)",
        (review_run_id, int(choice.lastrowid)),
    )


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in db.CANONICAL_TABLES
    }


def test_candidates_choose_review_or_distill_from_persisted_state(selector_db: Path) -> None:
    conn = db.connect(selector_db)
    try:
        changes_before = conn.total_changes
        candidates = update_select.list_candidates(conn)
        assert conn.total_changes == changes_before
    finally:
        conn.close()

    assert [(candidate.path, candidate.action) for candidate in candidates] == [
        ("skills/distill/SKILL.md", "distill"),
        ("skills/fresh/SKILL.md", "review"),
        ("skills/stale/SKILL.md", "review"),
        ("skills/unreviewed/SKILL.md", "review"),
    ]
    assert candidates[0].command == "/citation-distill skills/distill/SKILL.md"
    assert candidates[1].command == "/citation-review skills/fresh/SKILL.md"
    assert "differs" in candidates[2].reason
    assert "no completed review" in candidates[3].reason


def test_cancel_selects_nothing_and_writes_nothing(selector_db: Path) -> None:
    conn = db.connect(selector_db)
    try:
        candidates = update_select.list_candidates(conn)
        counts_before = _table_counts(conn)
        selected = update_select.choose_candidate(candidates, lambda _prompt: "cancel")
        assert selected is None
        assert _table_counts(conn) == counts_before
    finally:
        conn.close()


def test_cli_direct_selection_prints_exact_handoff_without_writing(
    selector_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = db.connect(selector_db)
    try:
        artifact_id = int(
            conn.execute(
                "SELECT id FROM artifacts WHERE path = 'skills/distill/SKILL.md'"
            ).fetchone()[0]
        )
        counts_before = _table_counts(conn)
    finally:
        conn.close()

    assert main(["update-select", "--artifact-id", str(artifact_id), "--db", str(selector_db)]) == 0

    output = capsys.readouterr().out
    assert output.splitlines()[0] == "/citation-distill skills/distill/SKILL.md"
    assert "Handoff only" in output
    conn = db.connect(selector_db)
    try:
        assert _table_counts(conn) == counts_before
    finally:
        conn.close()


def test_cli_interactive_cancel_writes_nothing(
    selector_db: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(selector_db)
    try:
        counts_before = _table_counts(conn)
    finally:
        conn.close()
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert main(["update-select", "--db", str(selector_db)]) == 0

    assert "Selection cancelled" in capsys.readouterr().out
    conn = db.connect(selector_db)
    try:
        assert _table_counts(conn) == counts_before
    finally:
        conn.close()


def test_unsafe_artifact_path_refuses_a_handoff(selector_db: Path) -> None:
    conn = db.connect(selector_db)
    try:
        unsafe_id = _artifact(conn, "../outside/SKILL.md", "current")
        conn.commit()
        with pytest.raises(update_select.UpdateSelectError, match="safe workspace-relative"):
            update_select.candidate_for_artifact_id(conn, unsafe_id)
    finally:
        conn.close()
