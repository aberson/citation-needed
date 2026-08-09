"""Overview/readiness query coverage for Justification Surfaces Step 11."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import TypedDict

import pytest

from citation_needed import db, read_queries


class _JustificationFixture(TypedDict):
    root: Path
    artifact_id: int
    fast_id: int
    review_run_id: int


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "citation.db"
    assert db.init_db(path)
    return path


def _artifact(conn: sqlite3.Connection, path: str, content_hash: str | None) -> int:
    cursor = conn.execute(
        "INSERT INTO artifacts (path, artifact_type, project, current_content_hash, first_seen_at) "
        "VALUES (?, 'skill', 'coding-root', ?, '2026-08-09T00:00:00Z')",
        (path, content_hash),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _completed_run(
    conn: sqlite3.Connection,
    artifact_id: int,
    reviewed_hash: str,
    *,
    finished_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO review_runs (artifact_id, started_at, finished_at, "
        "artifact_content_hash_at_review, reviewer_model, tool_schema_version, "
        "composite, composite_band) "
        "VALUES (?, '2026-08-09T00:00:00Z', ?, ?, 'claude-code-2.1.212', 2, 75.0, 'adequate')",
        (artifact_id, finished_at, reviewed_hash),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _in_progress_run(conn: sqlite3.Connection, artifact_id: int, reviewed_hash: str) -> int:
    cursor = conn.execute(
        "INSERT INTO review_runs (artifact_id, started_at, artifact_content_hash_at_review, "
        "reviewer_model, tool_schema_version) "
        "VALUES (?, '2026-08-09T05:00:00Z', ?, 'claude-code-2.1.212', 2)",
        (artifact_id, reviewed_hash),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_missing_database_keeps_database_derived_values_unavailable(tmp_path: Path) -> None:
    overview = read_queries.overview_for_path(tmp_path / "does-not-exist.db")

    assert overview.schema_version == 1
    assert overview.state == "uninitialized"
    assert overview.database_schema_version is None
    assert overview.counts is None
    assert overview.recent_activity is None
    assert overview.open_distill_queue is None


def test_initialized_empty_database_reports_real_zeroes(db_path: Path) -> None:
    overview = read_queries.overview_for_path(db_path)

    assert overview.state == "initialized-empty"
    assert overview.database_schema_version == 2
    assert overview.counts is not None
    assert overview.counts.model_dump() == {
        "artifacts": 0,
        "active_artifacts": 0,
        "completed_reviews": 0,
        "reviews_in_progress": 0,
        "choices": 0,
        "active_choices": 0,
        "citations": 0,
        "open_distill_queue": 0,
    }
    assert overview.recent_activity == []
    assert overview.stale_artifacts is not None and overview.stale_artifacts.total == 0
    assert overview.unreviewed_artifacts is not None and overview.unreviewed_artifacts.total == 0
    assert overview.current_content_unavailable_artifacts is not None
    assert overview.current_content_unavailable_artifacts.total == 0
    assert overview.open_distill_queue is not None and overview.open_distill_queue.items == []


def test_ready_database_is_read_only_and_reports_real_activity(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        artifact_id = _artifact(conn, ".claude/skills/example/SKILL.md", "current-hash")
        run_id = _completed_run(
            conn, artifact_id, "current-hash", finished_at="2026-08-09T01:00:00Z"
        )
        conn.commit()
        changes_before = conn.total_changes

        overview = read_queries.query_overview(conn, database_schema_version=2)

        assert conn.total_changes == changes_before
    finally:
        conn.close()

    assert overview.state == "ready"
    assert overview.counts is not None
    assert overview.counts.completed_reviews == 1
    assert overview.counts.reviews_in_progress == 0
    assert [item.run_id for item in overview.recent_activity or []] == [run_id]
    assert overview.recent_activity is not None
    assert overview.recent_activity[0].state == "completed"


def test_populated_overview_identifies_stale_unreviewed_unknown_and_in_progress(
    db_path: Path,
) -> None:
    conn = db.connect(db_path)
    try:
        fresh = _artifact(conn, ".claude/skills/fresh/SKILL.md", "fresh-hash")
        fresh_run = _completed_run(conn, fresh, "fresh-hash", finished_at="2026-08-09T01:00:00Z")
        stale = _artifact(conn, ".claude/skills/stale/SKILL.md", "new-hash")
        stale_run = _completed_run(conn, stale, "old-hash", finished_at="2026-08-09T02:00:00Z")
        _artifact(conn, ".claude/skills/unreviewed/SKILL.md", "seen-hash")
        unavailable = _artifact(conn, ".claude/skills/unavailable/SKILL.md", None)
        _completed_run(conn, unavailable, "old-hash", finished_at="2026-08-09T03:00:00Z")
        pending = _artifact(conn, ".claude/skills/pending/SKILL.md", "pending-hash")
        pending_run = _in_progress_run(conn, pending, "pending-hash")
        choice = conn.execute(
            "INSERT INTO choices (artifact_id, choice_key, summary, content_hash_at_extraction, "
            "first_extracted_review_run_id, last_confirmed_review_run_id) "
            "VALUES (?, 'choice', 'A real choice.', 'choice-hash', ?, ?)",
            (stale, stale_run, stale_run),
        )
        assert choice.lastrowid is not None
        conn.execute(
            "INSERT INTO citations (kind, natural_key, url_or_doi, verified_at, resolution_method) "
            "VALUES ('external', '10.1/example', 'https://doi.org/10.1/example', "
            "'2026-08-09T04:00:00Z', 'api_structured')"
        )
        conn.execute(
            "INSERT INTO distill_queue (choice_id, artifact_id, review_run_id, proposal_kind, "
            "rank, "
            "justification, created_at) VALUES (?, ?, ?, 'trim', 2.25, 'documented absence', "
            "'2026-08-09T04:00:00Z')",
            (int(choice.lastrowid), stale, stale_run),
        )
        conn.commit()

        overview = read_queries.query_overview(conn, database_schema_version=2)
    finally:
        conn.close()

    assert overview.state == "review-in-progress"
    assert overview.counts is not None
    assert overview.counts.artifacts == 5
    assert overview.counts.completed_reviews == 3
    assert overview.counts.reviews_in_progress == 1
    assert overview.counts.choices == 1
    assert overview.counts.citations == 1
    assert overview.counts.open_distill_queue == 1
    assert overview.stale_artifacts is not None
    assert overview.stale_artifacts.total == 1
    assert [item.path for item in overview.stale_artifacts.items] == [
        ".claude/skills/stale/SKILL.md"
    ]
    assert overview.stale_artifacts.items[0].latest_completed_run_id == stale_run
    assert overview.unreviewed_artifacts is not None
    assert [item.path for item in overview.unreviewed_artifacts.items] == [
        ".claude/skills/unreviewed/SKILL.md"
    ]
    assert overview.current_content_unavailable_artifacts is not None
    assert [item.path for item in overview.current_content_unavailable_artifacts.items] == [
        ".claude/skills/unavailable/SKILL.md"
    ]
    assert overview.open_distill_queue is not None
    assert overview.open_distill_queue.total == 1
    assert overview.open_distill_queue.items[0].rank == 2.25
    assert overview.open_distill_queue.items[0].choice_key == "choice"
    assert overview.recent_activity is not None
    assert overview.recent_activity[0].run_id == pending_run
    assert overview.recent_activity[0].state == "in-progress"
    assert fresh_run in [item.run_id for item in overview.recent_activity]


def test_stale_state_outranks_review_required_when_no_review_is_in_progress(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        artifact_id = _artifact(conn, ".claude/skills/stale/SKILL.md", "new-hash")
        _completed_run(conn, artifact_id, "old-hash", finished_at="2026-08-09T01:00:00Z")
        conn.commit()
        overview = read_queries.query_overview(conn, database_schema_version=2)
    finally:
        conn.close()

    assert overview.state == "stale"


# ---------------------------------------------------------------------------
# Step 12: deterministic justification list/detail and current locators
# ---------------------------------------------------------------------------


def _choice(
    conn: sqlite3.Connection,
    *,
    artifact_id: int,
    review_run_id: int,
    choice_key: str,
    quote: str,
    source_path: str | None = None,
    start_line: int = 1,
    end_line: int = 1,
    search_queries: str = '["citation-needed exact evidence"]',
) -> int:
    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    cursor = conn.execute(
        "INSERT INTO choices (artifact_id, choice_key, summary, quote_or_span, "
        "span_start_line, span_end_line, source_path, content_hash_at_extraction, "
        "first_extracted_review_run_id, last_confirmed_review_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id,
            choice_key,
            f"Summary for {choice_key}.",
            quote,
            start_line,
            end_line,
            source_path,
            quote_hash,
            review_run_id,
            review_run_id,
        ),
    )
    assert cursor.lastrowid is not None
    choice_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO scores (review_run_id, choice_id, evidence_backed_share, "
        "interesting_novel_share, unsupported_share, contradicted_share, classification, "
        "composite, composite_band, interpretation_guide_version, rationale, "
        "literature_searched, literature_found, search_queries) "
        "VALUES (?, ?, 1.0, 0.0, 0.0, 0.0, 'well-supported', 100.0, 'strong', "
        "'v1', 'Stored rationale.', 1, 0, ?)",
        (review_run_id, choice_id, search_queries),
    )
    return choice_id


def _justification_fixture(conn: sqlite3.Connection, tmp_path: Path) -> _JustificationFixture:
    root = tmp_path / "workspace"
    source = root / "skills" / "example" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# Example\nUnique evidence line.\nRepeated evidence line.\nRepeated evidence line.\n",
        encoding="utf-8",
    )

    artifact_id = _artifact(conn, "skills/example/SKILL.md", "changed-after-review")
    review_run_id = _completed_run(
        conn,
        artifact_id,
        "reviewed-content-hash",
        finished_at="2026-08-09T01:00:00Z",
    )
    unique_choice = _choice(
        conn,
        artifact_id=artifact_id,
        review_run_id=review_run_id,
        choice_key="unique",
        quote="Unique evidence line.",
        start_line=2,
        end_line=2,
    )
    _choice(
        conn,
        artifact_id=artifact_id,
        review_run_id=review_run_id,
        choice_key="ambiguous",
        quote="Repeated evidence line.",
        start_line=3,
        end_line=3,
    )
    _choice(
        conn,
        artifact_id=artifact_id,
        review_run_id=review_run_id,
        choice_key="missing",
        quote="No longer in the current source.",
        start_line=4,
        end_line=4,
    )
    _choice(
        conn,
        artifact_id=artifact_id,
        review_run_id=review_run_id,
        choice_key="escaped",
        quote="Unique evidence line.",
        source_path="../outside.md",
        start_line=1,
        end_line=1,
    )
    citation = conn.execute(
        "INSERT INTO citations (kind, natural_key, title, url_or_doi, verified_at, "
        "resolution_method, supporting_quote) "
        "VALUES ('external', '10.1/verified', 'Verified source', "
        "'https://doi.org/10.1/verified', '2026-08-09T02:00:00Z', 'api_structured', "
        "'server-verified metadata')"
    )
    assert citation.lastrowid is not None
    conn.execute(
        "INSERT INTO choice_citations (choice_id, citation_id, relevance_note, "
        "support_direction, first_linked_review_run_id, last_confirmed_review_run_id) "
        "VALUES (?, ?, 'Direct verified support.', 'supports', ?, ?)",
        (unique_choice, int(citation.lastrowid), review_run_id, review_run_id),
    )

    fast_id = _artifact(conn, "skills/fast/SKILL.md", "same-content-hash")
    fast_run = _completed_run(
        conn, fast_id, "same-content-hash", finished_at="2026-08-09T02:00:00Z"
    )
    _choice(
        conn,
        artifact_id=fast_id,
        review_run_id=fast_run,
        choice_key="unchanged-hash",
        quote="A stored literal span.",
        start_line=9,
        end_line=10,
    )
    conn.commit()
    return {
        "root": root,
        "artifact_id": artifact_id,
        "fast_id": fast_id,
        "review_run_id": review_run_id,
    }


def test_justification_list_ids_resolve_to_deterministic_details(
    db_path: Path, tmp_path: Path
) -> None:
    conn = db.connect(db_path)
    try:
        fixture = _justification_fixture(conn, tmp_path)
        listing = read_queries.list_justifications(conn, "skill")
        details = [
            read_queries.show_justification(
                conn,
                item.artifact_id,
                workspace_root=fixture["root"],
            )
            for item in listing.items
        ]
    finally:
        conn.close()

    assert listing.schema_version == 1
    assert [item.path for item in listing.items] == [
        "skills/example/SKILL.md",
        "skills/fast/SKILL.md",
    ]
    assert [detail.artifact.artifact_id for detail in details] == [
        item.artifact_id for item in listing.items
    ]
    assert all(
        item.choice_count == len(detail.choices)
        for item, detail in zip(listing.items, details, strict=True)
    )


def test_justification_detail_preserves_verified_evidence_and_honest_locator_states(
    db_path: Path, tmp_path: Path
) -> None:
    conn = db.connect(db_path)
    try:
        fixture = _justification_fixture(conn, tmp_path)
        changes_before = conn.total_changes
        detail = read_queries.show_justification(
            conn,
            fixture["artifact_id"],
            workspace_root=fixture["root"],
        )
        assert conn.total_changes == changes_before
        fast_detail = read_queries.show_justification(
            conn,
            fixture["fast_id"],
            workspace_root=fixture["root"],
        )
    finally:
        conn.close()

    choices = {choice.choice_key: choice for choice in detail.choices}
    unique = choices["unique"]
    assert unique.locator.status == "current"
    assert unique.locator.method == "unique-quote-match"
    assert unique.locator.current_span_start_line == 2
    assert unique.locator.current_span_end_line == 2
    assert unique.literature.status == "no-result"
    assert unique.literature.queries == ["citation-needed exact evidence"]
    assert len(unique.citations) == 1
    citation = unique.citations[0]
    assert citation.citation_id > 0
    assert citation.verified_at == "2026-08-09T02:00:00Z"
    assert citation.resolution_method == "api_structured"
    assert citation.last_confirmed_review_run_id == detail.review.run_id

    ambiguous = choices["ambiguous"].locator
    assert ambiguous.status == "ambiguous"
    assert ambiguous.current_span_start_line is None
    assert ambiguous.current_span_end_line is None

    missing = choices["missing"].locator
    assert missing.status == "missing"
    assert missing.current_span_start_line is None

    escaped = choices["escaped"].locator
    assert escaped.status == "unavailable"
    assert escaped.method == "source-unavailable"
    assert escaped.detail is not None and "escapes" in escaped.detail

    fast_locator = fast_detail.choices[0].locator
    assert fast_locator.status == "current"
    assert fast_locator.method == "unchanged-artifact-hash"
    assert fast_locator.current_span_start_line == 9
    assert fast_locator.current_span_end_line == 10
