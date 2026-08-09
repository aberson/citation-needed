"""Read-only, versioned overview queries for Citation Needed surfaces.

This module is deliberately separate from :mod:`citation_needed.db`: ``db.py`` owns
connection/bootstrap/migration mechanics, while this module owns domain reads for
the terminal and later observatory artifact. No function here issues DML.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from citation_needed import db, discover
from citation_needed.models import (
    CurrentLocator,
    JustificationArtifact,
    JustificationChoice,
    JustificationCitation,
    JustificationDetail,
    JustificationList,
    JustificationListItem,
    JustificationReview,
    JustificationSearch,
    Overview,
    OverviewArtifactList,
    OverviewArtifactReadiness,
    OverviewCounts,
    OverviewQueue,
    OverviewQueueItem,
    OverviewReviewActivity,
)

RECENT_ACTIVITY_LIMIT = 20
ARTIFACT_LIST_LIMIT = 50
OPEN_QUEUE_LIMIT = 50
MAX_LOCATOR_SOURCE_BYTES = 8 * 1024 * 1024


class JustificationQueryError(RuntimeError):
    """A requested justification cannot be represented from persisted review evidence."""


def overview_for_path(db_path: Path) -> Overview:
    """Return an honest overview for ``db_path`` without creating or changing it.

    A missing database and a file that has not been initialized are both setup states.
    Their table-derived values remain ``None`` rather than being displayed as zero.
    Other SQLite errors are intentionally allowed to reach the CLI, which reports a
    visible error instead of fabricating a usable-looking overview.
    """
    if not db_path.exists():
        return _uninitialized_overview()

    conn = db.connect(db_path)
    try:
        present_tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not set(db.CANONICAL_TABLES).issubset(present_tables):
            return _uninitialized_overview()
        database_schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return query_overview(conn, database_schema_version=database_schema_version)
    finally:
        conn.close()


def query_overview(conn: sqlite3.Connection, *, database_schema_version: int) -> Overview:
    """Read a populated or empty initialized database into the Overview v1 DTO."""
    counts = _counts(conn)
    recent_activity = _recent_activity(conn)
    stale_artifacts = _artifact_readiness_list(conn, "stale")
    unreviewed_artifacts = _artifact_readiness_list(conn, "unreviewed")
    content_unavailable_artifacts = _artifact_readiness_list(conn, "current-content-unavailable")
    open_distill_queue = _open_distill_queue(conn)

    if counts.reviews_in_progress:
        state = "review-in-progress"
    elif counts.active_artifacts == 0:
        state = "initialized-empty"
    elif stale_artifacts.total:
        state = "stale"
    elif unreviewed_artifacts.total or content_unavailable_artifacts.total:
        state = "review-required"
    else:
        state = "ready"

    return Overview(
        state=state,
        database_schema_version=database_schema_version,
        counts=counts,
        recent_activity=recent_activity,
        stale_artifacts=stale_artifacts,
        unreviewed_artifacts=unreviewed_artifacts,
        current_content_unavailable_artifacts=content_unavailable_artifacts,
        open_distill_queue=open_distill_queue,
    )


def _uninitialized_overview() -> Overview:
    """Make unavailable database-derived facts explicit instead of substituting zeroes."""
    return Overview(
        state="uninitialized",
        database_schema_version=None,
        counts=None,
        recent_activity=None,
        stale_artifacts=None,
        unreviewed_artifacts=None,
        current_content_unavailable_artifacts=None,
        open_distill_queue=None,
    )


def _counts(conn: sqlite3.Connection) -> OverviewCounts:
    row = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM artifacts), "
        "(SELECT COUNT(*) FROM artifacts WHERE is_active = 1), "
        "(SELECT COUNT(*) FROM review_runs "
        " WHERE finished_at IS NOT NULL AND status = 'completed'), "
        "(SELECT COUNT(*) FROM review_runs WHERE finished_at IS NULL), "
        "(SELECT COUNT(*) FROM choices), "
        "(SELECT COUNT(*) FROM choices WHERE status = 'active'), "
        "(SELECT COUNT(*) FROM citations), "
        "(SELECT COUNT(*) FROM distill_queue WHERE status = 'open')"
    ).fetchone()
    assert row is not None
    return OverviewCounts(
        artifacts=int(row[0]),
        active_artifacts=int(row[1]),
        completed_reviews=int(row[2]),
        reviews_in_progress=int(row[3]),
        choices=int(row[4]),
        active_choices=int(row[5]),
        citations=int(row[6]),
        open_distill_queue=int(row[7]),
    )


def _recent_activity(conn: sqlite3.Connection) -> list[OverviewReviewActivity]:
    rows = conn.execute(
        "SELECT r.id, r.artifact_id, a.path, a.artifact_type, a.project, r.reviewer_model, "
        "r.started_at, r.finished_at, r.status, r.composite, r.composite_band "
        "FROM review_runs r JOIN artifacts a ON a.id = r.artifact_id "
        "ORDER BY r.started_at DESC, r.id DESC LIMIT ?",
        (RECENT_ACTIVITY_LIMIT,),
    ).fetchall()
    activity: list[OverviewReviewActivity] = []
    for row in rows:
        finished_at = str(row[7]) if row[7] is not None else None
        if finished_at is None:
            state = "in-progress"
        elif str(row[8]) == "aborted":
            state = "aborted"
        else:
            state = "completed"
        activity.append(
            OverviewReviewActivity(
                run_id=int(row[0]),
                artifact_id=int(row[1]),
                artifact_path=str(row[2]),
                artifact_type=str(row[3]),
                project=str(row[4]),
                reviewer_model=str(row[5]),
                started_at=str(row[6]),
                finished_at=finished_at,
                state=state,
                composite=float(row[9]) if row[9] is not None else None,
                composite_band=str(row[10]) if row[10] is not None else None,
            )
        )
    return activity


def _artifact_readiness_list(conn: sqlite3.Connection, state: str) -> OverviewArtifactList:
    """Return a bounded, exact-total list for one non-ready artifact condition."""
    latest_completed = (
        "WITH ranked_completed AS ("
        " SELECT r.id, r.artifact_id, r.finished_at, r.artifact_content_hash_at_review, "
        " ROW_NUMBER() OVER (PARTITION BY r.artifact_id "
        " ORDER BY r.finished_at DESC, r.id DESC) AS sequence "
        " FROM review_runs r WHERE r.finished_at IS NOT NULL AND r.status = 'completed'"
        ") "
    )
    condition = {
        "stale": "latest.id IS NOT NULL AND a.current_content_hash IS NOT NULL "
        "AND a.current_content_hash != latest.artifact_content_hash_at_review",
        "unreviewed": "latest.id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM review_runs pending WHERE pending.artifact_id = a.id "
        "AND pending.finished_at IS NULL)",
        "current-content-unavailable": "latest.id IS NOT NULL AND a.current_content_hash IS NULL",
    }.get(state)
    if condition is None:
        raise ValueError(f"unknown artifact readiness state: {state!r}")

    count_sql = (
        latest_completed + "SELECT COUNT(*) FROM artifacts a "
        "LEFT JOIN ranked_completed latest "
        "ON latest.artifact_id = a.id AND latest.sequence = 1 "
        "WHERE a.is_active = 1 AND " + condition
    )
    count_row = conn.execute(count_sql).fetchone()
    assert count_row is not None
    total = int(count_row[0])
    item_sql = (
        latest_completed
        + "SELECT a.id, a.path, a.artifact_type, a.project, latest.id, latest.finished_at "
        "FROM artifacts a LEFT JOIN ranked_completed latest "
        "ON latest.artifact_id = a.id AND latest.sequence = 1 "
        "WHERE a.is_active = 1 AND " + condition + " ORDER BY a.path ASC, a.id ASC LIMIT ?"
    )
    items = [
        OverviewArtifactReadiness(
            artifact_id=int(row[0]),
            path=str(row[1]),
            artifact_type=str(row[2]),
            project=str(row[3]),
            state=state,
            latest_completed_run_id=int(row[4]) if row[4] is not None else None,
            latest_completed_at=str(row[5]) if row[5] is not None else None,
        )
        for row in conn.execute(item_sql, (ARTIFACT_LIST_LIMIT,))
    ]
    return OverviewArtifactList(total=total, items=items)


def _open_distill_queue(conn: sqlite3.Connection) -> OverviewQueue:
    count_row = conn.execute("SELECT COUNT(*) FROM distill_queue WHERE status = 'open'").fetchone()
    assert count_row is not None
    total = int(count_row[0])
    rows = conn.execute(
        "SELECT q.id, a.path, c.choice_key, q.proposal_kind, q.rank, q.created_at "
        "FROM distill_queue q "
        "JOIN artifacts a ON a.id = q.artifact_id "
        "JOIN choices c ON c.id = q.choice_id "
        "WHERE q.status = 'open' ORDER BY q.rank DESC, q.id ASC LIMIT ?",
        (OPEN_QUEUE_LIMIT,),
    ).fetchall()
    return OverviewQueue(
        total=total,
        items=[
            OverviewQueueItem(
                queue_id=int(row[0]),
                artifact_path=str(row[1]),
                choice_key=str(row[2]),
                proposal_kind=str(row[3]),
                rank=float(row[4]),
                created_at=str(row[5]),
            )
            for row in rows
        ],
    )


# ---------------------------------------------------------------------------
# Justification list/detail
# ---------------------------------------------------------------------------


def list_justifications(
    conn: sqlite3.Connection, artifact_type: str = "skill"
) -> JustificationList:
    """List reviewed artifacts of ``artifact_type`` in deterministic path/id order.

    Each result is backed by its latest completed run and exposes the numeric artifact
    ID accepted by :func:`show_justification`. Inactive artifacts remain visible as
    audit records rather than disappearing from a list that claims to show reviews.
    """
    rows = conn.execute(
        _latest_completed_cte() + "SELECT a.id, a.path, a.artifact_type, a.project, a.is_active, "
        "r.id, r.started_at, r.finished_at, r.reviewer_model, "
        "r.artifact_content_hash_at_review, r.artifact_git_sha_at_review, "
        "r.tool_schema_version, r.composite, r.composite_band, r.interpretation_guide_version, "
        "(SELECT COUNT(*) FROM scores s WHERE s.review_run_id = r.id) "
        "FROM artifacts a JOIN ranked_completed r "
        "ON r.artifact_id = a.id AND r.sequence = 1 "
        "WHERE a.artifact_type = ? ORDER BY a.path ASC, a.id ASC",
        (artifact_type,),
    ).fetchall()
    return JustificationList(
        artifact_type=artifact_type,
        items=[
            JustificationListItem(
                artifact_id=int(row[0]),
                path=str(row[1]),
                artifact_type=str(row[2]),
                project=str(row[3]),
                is_active=bool(int(row[4])),
                latest_review=_review_from_row(row[5:15]),
                choice_count=int(row[15]),
            )
            for row in rows
        ],
    )


def show_justification(
    conn: sqlite3.Connection,
    artifact_id: int,
    *,
    workspace_root: Path,
    memory_root: Path | None = None,
) -> JustificationDetail:
    """Return persisted evidence plus non-guessing current locators for one artifact.

    The database provides all review/citation facts. Current source files are read only
    to relocate a literal stored quote; no source code, skill, or review workflow is
    invoked by this read-side function.
    """
    artifact = conn.execute(
        "SELECT id, path, artifact_type, project, is_active, current_content_hash "
        "FROM artifacts WHERE id = ?",
        (artifact_id,),
    ).fetchone()
    if artifact is None:
        raise JustificationQueryError(f"artifact id {artifact_id} does not exist")
    review_row = conn.execute(
        "SELECT id, started_at, finished_at, reviewer_model, artifact_content_hash_at_review, "
        "artifact_git_sha_at_review, tool_schema_version, composite, composite_band, "
        "interpretation_guide_version "
        "FROM review_runs WHERE artifact_id = ? AND finished_at IS NOT NULL "
        "AND status = 'completed' ORDER BY finished_at DESC, id DESC LIMIT 1",
        (artifact_id,),
    ).fetchone()
    if review_row is None:
        raise JustificationQueryError(f"artifact id {artifact_id} has no completed review run")

    review = _review_from_row(review_row)
    choice_rows = conn.execute(
        "SELECT c.id, c.choice_key, c.summary, c.quote_or_span, c.span_start_line, "
        "c.span_end_line, c.source_path, c.content_hash_at_extraction, c.status, "
        "s.classification, s.composite, s.composite_band, s.evidence_backed_share, "
        "s.interesting_novel_share, s.unsupported_share, s.contradicted_share, s.rationale, "
        "s.literature_searched, s.literature_found, s.search_queries "
        "FROM scores s JOIN choices c ON c.id = s.choice_id "
        "WHERE s.review_run_id = ? ORDER BY c.choice_key ASC, c.id ASC",
        (review.run_id,),
    ).fetchall()
    citations_by_choice = _citations_for_run(conn, review.run_id)
    artifact_path = str(artifact[1])
    artifact_hash = str(artifact[5]) if artifact[5] is not None else None
    choices: list[JustificationChoice] = []
    for row in choice_rows:
        choice_id = int(row[0])
        source_path = str(row[6]) if row[6] is not None else artifact_path
        quote = str(row[3]) if row[3] is not None else None
        start_line = int(row[4]) if row[4] is not None else None
        end_line = int(row[5]) if row[5] is not None else None
        locator = _current_locator(
            source_path=source_path,
            artifact_path=artifact_path,
            quote=quote,
            stored_quote_hash=str(row[7]),
            recorded_span_start_line=start_line,
            recorded_span_end_line=end_line,
            artifact_current_hash=artifact_hash,
            review_artifact_hash=review.artifact_content_hash_at_review,
            workspace_root=workspace_root,
            memory_root=memory_root,
        )
        choices.append(
            JustificationChoice(
                choice_id=choice_id,
                choice_key=str(row[1]),
                summary=str(row[2]),
                quote_or_span=quote,
                status=str(row[8]),
                locator=locator,
                classification=str(row[9]),
                composite=float(row[10]),
                composite_band=str(row[11]),
                evidence_backed_share=float(row[12]),
                interesting_novel_share=float(row[13]),
                unsupported_share=float(row[14]),
                contradicted_share=float(row[15]),
                rationale=str(row[16]) if row[16] is not None else None,
                literature=_search_from_row(row[17:20]),
                citations=citations_by_choice.get(choice_id, []),
            )
        )
    return JustificationDetail(
        artifact=JustificationArtifact(
            artifact_id=int(artifact[0]),
            path=artifact_path,
            artifact_type=str(artifact[2]),
            project=str(artifact[3]),
            is_active=bool(int(artifact[4])),
        ),
        review=review,
        choices=choices,
    )


def _latest_completed_cte() -> str:
    return (
        "WITH ranked_completed AS ("
        " SELECT r.id, r.artifact_id, r.started_at, r.finished_at, r.reviewer_model, "
        " r.artifact_content_hash_at_review, r.artifact_git_sha_at_review, "
        " r.tool_schema_version, r.composite, r.composite_band, r.interpretation_guide_version, "
        " ROW_NUMBER() OVER (PARTITION BY r.artifact_id "
        " ORDER BY r.finished_at DESC, r.id DESC) AS sequence "
        " FROM review_runs r WHERE r.finished_at IS NOT NULL AND r.status = 'completed'"
        ") "
    )


def _review_from_row(row: tuple[Any, ...]) -> JustificationReview:
    """Build the DTO shared by list and detail from the selected review columns."""
    return JustificationReview(
        run_id=int(row[0]),
        started_at=str(row[1]),
        finished_at=str(row[2]),
        reviewer_model=str(row[3]),
        artifact_content_hash_at_review=str(row[4]),
        artifact_git_sha_at_review=str(row[5]) if row[5] is not None else None,
        tool_schema_version=int(row[6]),
        composite=float(row[7]) if row[7] is not None else None,
        composite_band=str(row[8]) if row[8] is not None else None,
        interpretation_guide_version=str(row[9]) if row[9] is not None else None,
    )


def _citations_for_run(
    conn: sqlite3.Connection, review_run_id: int
) -> dict[int, list[JustificationCitation]]:
    """Read citations confirmed by this exact run from verified citation table rows."""
    rows = conn.execute(
        "SELECT cc.choice_id, c.id, c.kind, c.natural_key, c.title, c.authors, c.year, c.venue, "
        "c.url_or_doi, c.workspace_path, c.verified_at, c.resolution_method, c.supporting_quote, "
        "c.keywords, c.source_git_sha, c.source_line_ref, c.notes, cc.relevance_note, "
        "cc.support_direction, cc.first_linked_review_run_id, cc.last_confirmed_review_run_id "
        "FROM choice_citations cc JOIN citations c ON c.id = cc.citation_id "
        "WHERE cc.last_confirmed_review_run_id = ? ORDER BY cc.choice_id ASC, c.id ASC",
        (review_run_id,),
    ).fetchall()
    by_choice: dict[int, list[JustificationCitation]] = {}
    for row in rows:
        choice_id = int(row[0])
        by_choice.setdefault(choice_id, []).append(
            JustificationCitation(
                citation_id=int(row[1]),
                kind=str(row[2]),
                natural_key=str(row[3]),
                title=str(row[4]) if row[4] is not None else None,
                authors=str(row[5]) if row[5] is not None else None,
                year=int(row[6]) if row[6] is not None else None,
                venue=str(row[7]) if row[7] is not None else None,
                url_or_doi=str(row[8]) if row[8] is not None else None,
                workspace_path=str(row[9]) if row[9] is not None else None,
                verified_at=str(row[10]),
                resolution_method=str(row[11]),
                supporting_quote=str(row[12]) if row[12] is not None else None,
                keywords=str(row[13]) if row[13] is not None else None,
                source_git_sha=str(row[14]) if row[14] is not None else None,
                source_line_ref=str(row[15]) if row[15] is not None else None,
                notes=str(row[16]) if row[16] is not None else None,
                relevance_note=str(row[17]),
                support_direction=str(row[18]),
                first_linked_review_run_id=int(row[19]),
                last_confirmed_review_run_id=int(row[20]),
            )
        )
    return by_choice


def _search_from_row(row: tuple[Any, ...]) -> JustificationSearch:
    """Decode recorded search queries without inventing a list for malformed DB text."""
    attempted = bool(int(row[0]))
    found = bool(int(row[1]))
    raw_queries = row[2]
    queries: list[str] | None
    if raw_queries is None:
        queries = None
    else:
        try:
            parsed = json.loads(str(raw_queries))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(query, str) for query in parsed):
            queries = list(parsed)
        else:
            return JustificationSearch(
                attempted=attempted,
                found=found,
                status="invalid-record",
                queries=None,
            )
    if (found and not attempted) or (attempted and not queries):
        status = "invalid-record"
    elif found:
        status = "found"
    elif attempted:
        status = "no-result"
    else:
        status = "not-attempted"
    return JustificationSearch(attempted=attempted, found=found, status=status, queries=queries)


def _current_locator(
    *,
    source_path: str,
    artifact_path: str,
    quote: str | None,
    stored_quote_hash: str,
    recorded_span_start_line: int | None,
    recorded_span_end_line: int | None,
    artifact_current_hash: str | None,
    review_artifact_hash: str,
    workspace_root: Path,
    memory_root: Path | None,
) -> CurrentLocator:
    """Locate a literal quote now; no fuzzy match can produce a guessed line number."""
    common = {
        "source_path": source_path,
        "recorded_span_start_line": recorded_span_start_line,
        "recorded_span_end_line": recorded_span_end_line,
    }
    if quote is None or not quote:
        return CurrentLocator(
            **common,
            status="unavailable",
            method="stored-quote-unavailable",
            current_span_start_line=None,
            current_span_end_line=None,
            detail="stored quote/span is unavailable or empty",
        )
    if hashlib.sha256(quote.encode("utf-8")).hexdigest() != stored_quote_hash:
        return CurrentLocator(
            **common,
            status="unavailable",
            method="stored-quote-hash-mismatch",
            current_span_start_line=None,
            current_span_end_line=None,
            detail="stored content hash does not match the literal stored quote/span",
        )
    if (
        source_path == artifact_path
        and artifact_current_hash is not None
        and artifact_current_hash == review_artifact_hash
        and recorded_span_start_line is not None
        and recorded_span_end_line is not None
    ):
        return CurrentLocator(
            **common,
            status="current",
            method="unchanged-artifact-hash",
            current_span_start_line=recorded_span_start_line,
            current_span_end_line=recorded_span_end_line,
            detail=None,
        )
    text, unavailable_reason = _read_current_source(source_path, workspace_root, memory_root)
    if text is None:
        return CurrentLocator(
            **common,
            status="unavailable",
            method="source-unavailable",
            current_span_start_line=None,
            current_span_end_line=None,
            detail=unavailable_reason,
        )
    matches = _all_exact_matches(text, quote)
    if not matches:
        return CurrentLocator(
            **common,
            status="missing",
            method="quote-not-found",
            current_span_start_line=None,
            current_span_end_line=None,
            detail="the literal stored quote/span is not present in the current source text",
        )
    if len(matches) > 1:
        return CurrentLocator(
            **common,
            status="ambiguous",
            method="multiple-quote-matches",
            current_span_start_line=None,
            current_span_end_line=None,
            detail=(
                f"the literal stored quote/span occurs {len(matches)} times in current source text"
            ),
        )
    start = matches[0]
    end = start + len(quote) - 1
    return CurrentLocator(
        **common,
        status="current",
        method="unique-quote-match",
        current_span_start_line=text.count("\n", 0, start) + 1,
        current_span_end_line=text.count("\n", 0, end) + 1,
        detail=None,
    )


def _all_exact_matches(text: str, quote: str) -> list[int]:
    """Return every overlapping literal occurrence; one and only one is locatable."""
    matches: list[int] = []
    offset = 0
    while True:
        match = text.find(quote, offset)
        if match < 0:
            return matches
        matches.append(match)
        offset = match + 1


def _read_current_source(
    source_path: str, workspace_root: Path, memory_root: Path | None
) -> tuple[str | None, str | None]:
    """Boundedly read a current locator source, confined to workspace/memory roots."""
    resolved, error = _resolve_locator_source(source_path, workspace_root, memory_root)
    if resolved is None:
        return None, error
    try:
        if not resolved.is_file():
            return None, "source is absent or not a regular file"
        if resolved.stat().st_size > MAX_LOCATOR_SOURCE_BYTES:
            return None, f"source exceeds {MAX_LOCATOR_SOURCE_BYTES} byte read limit"
        raw = resolved.read_bytes()
    except OSError as exc:
        return None, f"source cannot be read: {exc}"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "source is not valid UTF-8"
    return text.replace("\r\n", "\n").replace("\r", "\n"), None


def _resolve_locator_source(
    source_path: str, workspace_root: Path, memory_root: Path | None
) -> tuple[Path | None, str | None]:
    """Resolve a DB locator under its allowed root, rejecting absolute/traversal/symlink escapes."""
    if source_path.startswith("memory:"):
        root = memory_root if memory_root is not None else discover.default_memory_root()
        remainder = source_path[len("memory:") :]
        project_slug, separator, relative_file = remainder.partition("/")
        if not project_slug or not separator or not relative_file:
            return None, "memory locator must be memory:<project-dir-slug>/<file>"
        candidate = root / project_slug / "memory" / relative_file
        root_label = "memory root"
    else:
        root = workspace_root
        candidate = root / source_path
        root_label = "workspace root"
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        return None, f"source cannot be resolved: {exc}"
    if not resolved.is_relative_to(resolved_root):
        return None, f"source escapes the {root_label}"
    return resolved, None
