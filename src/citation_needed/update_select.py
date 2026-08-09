"""Terminal-only handoff selection for the existing Citation Needed skills.

The selector reads persisted artifact/review state and prints a *command for the
operator to invoke later*. It never imports, invokes, or imitates an LLM skill, and it
never writes a review, queue, or source file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

UpdateAction = Literal["review", "distill"]

DISPLAY_LIMIT = 50


class UpdateSelectError(RuntimeError):
    """A requested update handoff cannot be derived safely from persisted state."""


@dataclass(frozen=True)
class UpdateCandidate:
    """One active skill and the existing skill workflow that may update its evidence."""

    artifact_id: int
    path: str
    action: UpdateAction
    reason: str

    @property
    def command(self) -> str:
        """The exact user-invocable command; formatting is not a shell execution."""
        return f"/citation-{self.action} {self.path}"


def list_candidates(conn: sqlite3.Connection) -> list[UpdateCandidate]:
    """List every active scanned skill in deterministic path/id order without writes."""
    rows = conn.execute(
        "WITH ranked_completed AS ("
        " SELECT r.id, r.artifact_id, r.artifact_content_hash_at_review, "
        " ROW_NUMBER() OVER (PARTITION BY r.artifact_id "
        " ORDER BY r.finished_at DESC, r.id DESC) AS sequence "
        " FROM review_runs r WHERE r.finished_at IS NOT NULL AND r.status = 'completed'"
        ") "
        "SELECT a.id, a.path, a.current_content_hash, r.id, "
        "r.artifact_content_hash_at_review, "
        "EXISTS(SELECT 1 FROM scores s WHERE s.review_run_id = r.id "
        "AND s.classification = 'needs-improvement') "
        "FROM artifacts a LEFT JOIN ranked_completed r "
        "ON r.artifact_id = a.id AND r.sequence = 1 "
        "WHERE a.artifact_type = 'skill' AND a.is_active = 1 "
        "ORDER BY a.path ASC, a.id ASC"
    ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def candidate_for_artifact_id(conn: sqlite3.Connection, artifact_id: int) -> UpdateCandidate:
    """Return one active skill candidate or refuse a non-skill/inactive/unknown ID."""
    rows = conn.execute(
        "WITH ranked_completed AS ("
        " SELECT r.id, r.artifact_id, r.artifact_content_hash_at_review, "
        " ROW_NUMBER() OVER (PARTITION BY r.artifact_id "
        " ORDER BY r.finished_at DESC, r.id DESC) AS sequence "
        " FROM review_runs r WHERE r.finished_at IS NOT NULL AND r.status = 'completed'"
        ") "
        "SELECT a.id, a.path, a.current_content_hash, r.id, "
        "r.artifact_content_hash_at_review, "
        "EXISTS(SELECT 1 FROM scores s WHERE s.review_run_id = r.id "
        "AND s.classification = 'needs-improvement') "
        "FROM artifacts a LEFT JOIN ranked_completed r "
        "ON r.artifact_id = a.id AND r.sequence = 1 "
        "WHERE a.id = ? AND a.artifact_type = 'skill' AND a.is_active = 1",
        (artifact_id,),
    ).fetchall()
    if not rows:
        raise UpdateSelectError(f"artifact id {artifact_id} is not an active scanned skill")
    return _candidate_from_row(rows[0])


def choose_candidate(
    candidates: list[UpdateCandidate], input_fn: Callable[[str], str]
) -> UpdateCandidate | None:
    """Select a displayed one-based index; blank/q/cancel deliberately selects nothing."""
    response = input_fn("Select a skill number, or press Enter to cancel: ").strip()
    if response.lower() in {"", "q", "quit", "cancel"}:
        return None
    try:
        index = int(response)
    except ValueError as exc:
        raise UpdateSelectError("selection must be a displayed positive number or cancel") from exc
    if index < 1 or index > len(candidates):
        raise UpdateSelectError(f"selection must be between 1 and {len(candidates)}, or cancel")
    return candidates[index - 1]


def _candidate_from_row(row: tuple[Any, ...]) -> UpdateCandidate:
    artifact_id = int(row[0])
    path = str(row[1])
    _validate_skill_path(path)
    current_hash = str(row[2]) if row[2] is not None else None
    review_id = int(row[3]) if row[3] is not None else None
    reviewed_hash = str(row[4]) if row[4] is not None else None
    needs_improvement = bool(int(row[5]))
    if review_id is None:
        return UpdateCandidate(
            artifact_id=artifact_id,
            path=path,
            action="review",
            reason="no completed review is recorded",
        )
    if current_hash is None:
        return UpdateCandidate(
            artifact_id=artifact_id,
            path=path,
            action="review",
            reason="current artifact content is unavailable; re-scan and review are required",
        )
    if current_hash != reviewed_hash:
        return UpdateCandidate(
            artifact_id=artifact_id,
            path=path,
            action="review",
            reason="current artifact content differs from the latest completed review",
        )
    if needs_improvement:
        return UpdateCandidate(
            artifact_id=artifact_id,
            path=path,
            action="distill",
            reason="a fresh completed review has a needs-improvement choice",
        )
    return UpdateCandidate(
        artifact_id=artifact_id,
        path=path,
        action="review",
        reason="the current completed review has no needs-improvement choice",
    )


def _validate_skill_path(path: str) -> None:
    """Keep a compromised DB row from becoming a multi-line or traversal handoff command."""
    normalized = path.replace("\\", "/")
    if (
        not path
        or normalized.startswith("/")
        or ":" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
        or any(character in path for character in "\r\n\x00")
    ):
        raise UpdateSelectError(
            f"artifact path is not a safe workspace-relative skill path: {path!r}"
        )
