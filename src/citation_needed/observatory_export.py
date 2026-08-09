"""Bounded producer artifacts for Dev Observatory's fixed ``observatory.v1`` reader.

This module is a Citation Needed producer only. It reads the citation database and
current locator sources, writes two versioned JSON envelopes, and never modifies a
review, citation, queue, or source artifact. Dev Observatory owns registry wiring,
labels, and HTML rendering.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citation_needed import db, discover, read_queries
from citation_needed.models import JustificationChoice, JustificationDetail

SCHEMA = "observatory.v1"
DEFAULT_EXPORT_DIR = db.PROJECT_ROOT / "observatory"
OVERVIEW_FILENAME = "citation-overview.v1.json"
JUSTIFICATIONS_FILENAME = "citation-justifications.v1.json"

MAX_EXPORT_BYTES = 512 * 1024
MAX_JUSTIFICATION_ITEMS = 100
MAX_CHOICES_PER_ITEM = 20
MAX_CITATIONS_PER_CHOICE = 10
MAX_LABEL_CHARS = 160
MAX_SUMMARY_CHARS = 2_000
MAX_DETAIL_TEXT_CHARS = 8_000


class ObservatoryExportError(RuntimeError):
    """The exporter cannot produce an honest, bounded contract artifact."""


@dataclass(frozen=True)
class ObservatoryExportResult:
    """The two produced view files and their displayed/total justification counts."""

    overview_path: Path
    justifications_path: Path
    reviewed_skills_total: int
    justifications_exported: int


def export_observatory_artifacts(
    db_path: Path,
    output_dir: Path,
    *,
    workspace_root: Path | None = None,
    memory_root: Path | None = None,
    now: datetime | None = None,
) -> ObservatoryExportResult:
    """Write bounded v1 summary/explorer files without issuing database DML.

    A missing or uninitialized DB is an unavailable producer state, so this function
    refuses rather than creating an empty artifact that could be mistaken for a real
    zero-result scan. An initialized empty DB exports real zeroes and an empty explorer.
    """
    if not db_path.exists():
        raise ObservatoryExportError(f"database does not exist: {db_path.as_posix()}")
    stamp = _utc_stamp(now)
    root = workspace_root if workspace_root is not None else discover.default_workspace_root()
    conn = db.connect(db_path)
    try:
        _require_initialized(conn)
        database_schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        overview = read_queries.query_overview(
            conn, database_schema_version=database_schema_version
        )
        activity_total = _review_activity_total(conn)
        listing = read_queries.list_justifications(conn, "skill")
        details = [
            read_queries.show_justification(
                conn,
                item.artifact_id,
                workspace_root=root,
                memory_root=memory_root,
            )
            for item in listing.items[:MAX_JUSTIFICATION_ITEMS]
        ]
    except (read_queries.JustificationQueryError, sqlite3.Error) as exc:
        raise ObservatoryExportError(f"could not read citation evidence: {exc}") from exc
    finally:
        conn.close()

    overview_payload = _overview_payload(
        overview, activity_total, len(listing.items), len(details), stamp
    )
    justifications_payload = _justifications_payload(details, len(listing.items), stamp)
    overview_path = output_dir / OVERVIEW_FILENAME
    justifications_path = output_dir / JUSTIFICATIONS_FILENAME
    _write_json_atomically(overview_path, overview_payload)
    _write_json_atomically(justifications_path, justifications_payload)
    return ObservatoryExportResult(
        overview_path=overview_path,
        justifications_path=justifications_path,
        reviewed_skills_total=len(listing.items),
        justifications_exported=len(details),
    )


def _require_initialized(conn: sqlite3.Connection) -> None:
    present = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = set(db.CANONICAL_TABLES) - present
    if missing:
        names = ", ".join(sorted(missing))
        raise ObservatoryExportError(f"database is not initialized (missing table(s): {names})")


def _review_activity_total(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()
    assert row is not None
    return int(row[0])


def _utc_stamp(now: datetime | None) -> str:
    value = datetime.now(UTC) if now is None else now
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ObservatoryExportError("generated_at must use an aware UTC timestamp")
    return value.isoformat().replace("+00:00", "Z")


def _overview_payload(
    overview: Any,
    review_activity_total: int,
    reviewed_skills_total: int,
    justifications_exported: int,
    stamp: str,
) -> dict[str, object]:
    counts = overview.counts
    stats: dict[str, object] = {
        "state": overview.state,
        "database_schema_version": overview.database_schema_version,
        "recent_activity_total": review_activity_total,
        "recent_activity_exported": 0,
        "reviewed_skills_total": reviewed_skills_total,
        "justifications_exported": justifications_exported,
    }
    for name in (
        "artifacts",
        "active_artifacts",
        "completed_reviews",
        "reviews_in_progress",
        "choices",
        "active_choices",
        "citations",
        "open_distill_queue",
    ):
        stats[name] = getattr(counts, name) if counts is not None else None
    recent = []
    if overview.recent_activity is not None:
        recent = [
            {
                "id": f"run-{activity.run_id}",
                "label": _display_text(
                    f"{activity.artifact_path} — {activity.state}", MAX_LABEL_CHARS
                ),
                "detail": _display_text(
                    f"Run {activity.run_id}; model {activity.reviewer_model}; "
                    "composite "
                    f"{activity.composite if activity.composite is not None else 'unavailable'}",
                    MAX_DETAIL_TEXT_CHARS,
                ),
            }
            for activity in overview.recent_activity
        ]
    stats["recent_activity_exported"] = len(recent)
    return {"schema": SCHEMA, "generated_at": stamp, "stats": stats, "recent": recent}


def _justifications_payload(
    details: list[JustificationDetail], reviewed_skills_total: int, stamp: str
) -> dict[str, object]:
    items = [_explorer_item(detail) for detail in details]
    # The fixed generic reader intentionally allows only schema/generated_at/items.
    # It sees a bounded item list; its detail carries exact totals so absence of a
    # 101st item cannot be mistaken for a real zero.
    if reviewed_skills_total < len(items):
        raise AssertionError("exported justification count cannot exceed its total")
    return {"schema": SCHEMA, "generated_at": stamp, "items": items}


def _explorer_item(detail: JustificationDetail) -> dict[str, object]:
    displayed_choices = detail.choices[:MAX_CHOICES_PER_ITEM]
    review = detail.review
    summary = _display_text(
        f"Review {review.run_id}: "
        f"{review.composite if review.composite is not None else 'unscored'} "
        f"({review.composite_band or 'unbanded'}); {len(detail.choices)} scored choice(s).",
        MAX_SUMMARY_CHARS,
    )
    return {
        "id": f"artifact-{detail.artifact.artifact_id}",
        "label": _display_text(detail.artifact.path, MAX_LABEL_CHARS),
        "summary": summary,
        "detail": {
            "artifact": detail.artifact.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "choices_total": len(detail.choices),
            "choices_exported": len(displayed_choices),
            "choices": [_choice_detail(choice) for choice in displayed_choices],
        },
    }


def _choice_detail(choice: JustificationChoice) -> dict[str, object]:
    displayed_citations = choice.citations[:MAX_CITATIONS_PER_CHOICE]
    quote = _bounded_text(choice.quote_or_span, MAX_DETAIL_TEXT_CHARS)
    return {
        "choice_id": choice.choice_id,
        "choice_key": _display_text(choice.choice_key, MAX_LABEL_CHARS),
        "summary": _display_text(choice.summary, MAX_SUMMARY_CHARS),
        "quote_or_span": quote,
        "status": choice.status,
        "classification": choice.classification,
        "composite": choice.composite,
        "composite_band": choice.composite_band,
        "vote_shares": {
            "evidence_backed": choice.evidence_backed_share,
            "interesting_novel": choice.interesting_novel_share,
            "unsupported": choice.unsupported_share,
            "contradicted": choice.contradicted_share,
        },
        "rationale": _bounded_text(choice.rationale, MAX_DETAIL_TEXT_CHARS),
        "locator": choice.locator.model_dump(mode="json"),
        "literature": choice.literature.model_dump(mode="json"),
        "citations_total": len(choice.citations),
        "citations_exported": len(displayed_citations),
        "citations": [
            {
                "citation_id": citation.citation_id,
                "kind": citation.kind,
                "natural_key": _bounded_text(citation.natural_key, MAX_DETAIL_TEXT_CHARS),
                "title": _bounded_text(citation.title, MAX_DETAIL_TEXT_CHARS),
                "locator": _bounded_text(
                    citation.url_or_doi or citation.workspace_path, MAX_DETAIL_TEXT_CHARS
                ),
                "verified_at": citation.verified_at,
                "resolution_method": citation.resolution_method,
                "support_direction": citation.support_direction,
                "relevance_note": _bounded_text(citation.relevance_note, MAX_DETAIL_TEXT_CHARS),
                "supporting_quote": _bounded_text(citation.supporting_quote, MAX_DETAIL_TEXT_CHARS),
                "source_line_ref": _bounded_text(citation.source_line_ref, MAX_DETAIL_TEXT_CHARS),
            }
            for citation in displayed_citations
        ],
    }


def _bounded_text(value: str | None, max_chars: int) -> dict[str, object] | None:
    """Preserve absence and mark truncation rather than making a shortened value look whole."""
    if value is None:
        return None
    if len(value) <= max_chars:
        return {"text": value, "truncated": False}
    return {"text": value[:max_chars], "truncated": True}


def _display_text(value: str, max_chars: int) -> str:
    """Keep generic reader label/summary values route-page safe and bounded."""
    normalized = " ".join(value.split())
    if not normalized:
        return "unavailable"
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1] + "…"


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    """Serialize one bounded artifact to a sibling temp file before replacing its target."""
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_EXPORT_BYTES:
        raise ObservatoryExportError(
            f"export artifact {path.name!r} exceeds its {MAX_EXPORT_BYTES}-byte limit"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
        raise ObservatoryExportError(
            f"could not write export artifact {path.as_posix()}: {exc}"
        ) from exc
