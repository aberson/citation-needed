"""Distill engine — mechanical proposal generation + the ONE queue rank formula.

Two write paths into ``distill_queue`` (plan.md Step 6 / §4.4), both purely
mechanical — the LLM judgment (drafting proposal text) lives in the skill layer:

- :func:`generate_queue_rows` — for each **needs-improvement** choice of a COMMITTED
  review run, create/refresh one queue row carrying the MECHANICAL default:
  contradicted majority -> ``'rewrite'``, unsupported majority -> ``'trim'``, with a
  justification built from the choice's linked citation ids or its documented
  absence (``scores.literature_searched=1``). A choice with NEITHER rejects the
  whole run loudly — ``distill_queue.justification`` is NOT NULL by design, and a
  justification cannot be fabricated. Well-supported and interesting choices yield
  NO row (a backlog entry for a fine choice is noise, not signal —
  schema-draft.md §8).
- :func:`propose_queue_rows` — the ``cite distill propose`` stdin payload
  (skill-drafted proposals: choice_key, proposal_kind, justification citing
  citation ids or the documented absence, concrete rewrite text where applicable)
  upserts the same rows, REPLACING the mechanical defaults for the same choice.
  Same discipline as ``review commit``: UTF-8 stdin (cli seam), pydantic mirror
  with ``extra="forbid"``, whole-payload reject on ANY invalid entry.

**One row per choice.** Both paths upsert by ``choice_id``: an existing OPEN row is
refreshed in place (no duplicates — the re-propose invariant); a RESOLVED row
(accepted/rejected/applied) is never touched — the recorded operator decision
stands (generate skips it with a note; propose rejects the whole payload).
``status`` stays ``'open'`` through every upsert; only ``resolve_queue_item``
moves it.

**Supersession (queue lifecycle across re-reviews).** OPEN rows are DERIVED state —
an unresolved row carries no operator decision, so it must not outlive the evidence
that produced it. :func:`supersede_stale_open_rows` DELETEs an artifact's open rows
whose choice the newest committed run no longer flags: the choice is no longer
``'active'`` (removed/superseded on re-review), or its newest classification is
anything but ``needs-improvement``. The pass runs inside
``review.commit_review``'s transaction — the ONE seam where the reference run is by
construction the artifact's newest committed state and the purge lands atomically
with the commit that made the rows stale, so there is NO window in which ``cite
queue list`` shows a stale row after a commit. generate/propose deliberately do NOT
run the pass: they accept any committed run id, and purging against a non-latest
run would judge staleness by superseded evidence. RESOLVED rows are immutable audit
and always survive. Belt-and-suspenders: :func:`list_queue` marks any row whose
source run is no longer the artifact's newest committed run (``superseded_run``) so
a not-yet-refreshed row reads as stale, never as current.

**Rank** (plan §4.4): ``(1 - composite/100) * artifact_load_weight``, where
``composite`` is the PER-CHOICE composite stored on the ``scores`` row — each queue
row is one choice, so a contradicted choice (composite 0) outranks an unsupported
one (25) at the same tier. :data:`LOAD_WEIGHTS` is the single source of truth for
the load-weight map (docs/interpretation-guide.md's table is tested against it for
drift — code-quality.md § one source of truth); rank is ALWAYS formula-computed,
never payload-supplied (the skill layer overrides kind + justification only).

**Resolution mapping** (``cite queue resolve``, schema status enum
``open|accepted|rejected|applied``): ``--keep`` -> ``'rejected'`` (proposal
declined; the target text stays), ``--cut``/``--rewrite`` -> ``'accepted'`` (the
proposal proceeds; WHICH edit shape is recorded by the row's ``proposal_kind`` —
run ``cite distill propose`` first if the kind should change). The ``'applied'``
transition is OUT of the CLI's scope: target edits happen outside citation-needed
(plan D1/D11 — the engine never edits a target), so ``applied`` is reserved for
whatever tool performs the edit. ``resolved_by`` comes from ``--by`` or env
``USERNAME``/``USER``; ``resolved_at`` is the pipeline clock, never LLM-supplied.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Constants — single sources of truth (code-quality.md)
# ---------------------------------------------------------------------------

#: The knowledge-placement tier vocabulary (schema.sql distill_queue CHECK enum, v1 —
#: no 'move-to-skill-trigger' in v1 per plan §3.1).
ProposalKind = Literal[
    "move-to-rule",
    "move-to-reference",
    "move-to-memory-pointer",
    "trim",
    "rewrite",
    "delete-superseded",
    "no-action",
]
PROPOSAL_KINDS: tuple[str, ...] = get_args(ProposalKind)

QueueStatus = Literal["open", "accepted", "rejected", "applied"]
QUEUE_STATUSES: tuple[str, ...] = get_args(QueueStatus)

#: Load weights v1 (plan §4.4) — THE single source of truth for the rank formula's
#: weight map; docs/interpretation-guide.md's table is tested against this dict so
#: the two cannot drift. Operationalizes knowledge-placement.md's tier cost
#: ordering: claude_md + rule auto-load every session (most expensive), memory's
#: index is always loaded (body on demand), skill loads on trigger, plan is read at
#: planning/build moments only.
LOAD_WEIGHTS: dict[str, float] = {
    "claude_md": 3.0,
    "rule": 3.0,
    "memory": 1.5,
    "skill": 1.0,
    "plan": 0.75,
}

#: The mechanical default kind per needs-improvement majority label (plan Step 6):
#: verified evidence AGAINST the choice -> rewrite it; a real search that came back
#: empty -> trim it. The skill layer can override via `cite distill propose`.
MECHANICAL_KIND_BY_MAJORITY: dict[str, str] = {
    "contradicted": "rewrite",
    "unsupported": "trim",
}

#: Operator decision -> distill_queue.status (the keep/cut/rewrite mapping — see
#: the module docstring; 'applied' is out of the CLI's scope by design).
STATUS_BY_DECISION: dict[str, str] = {
    "keep": "rejected",
    "cut": "accepted",
    "rewrite": "accepted",
}


class DistillError(RuntimeError):
    """A distill/queue contract violation — the CLI reports it as ``error: ...``."""


def _utc_now() -> str:
    """Pipeline clock (same format as review.py) — never LLM-supplied."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Rank — the ONE implementation of the §4.4 formula
# ---------------------------------------------------------------------------


def load_weight(artifact_type: str) -> float:
    """The v1 load weight for an artifact type; unknown types fail loud."""
    try:
        return LOAD_WEIGHTS[artifact_type]
    except KeyError:
        raise DistillError(
            f"no load weight defined for artifact_type {artifact_type!r} — "
            f"expected one of {sorted(LOAD_WEIGHTS)}"
        ) from None


def queue_rank(composite: float, artifact_type: str) -> float:
    """``(1 - composite/100) * artifact_load_weight`` (plan §4.4).

    ``composite`` is the PER-CHOICE composite from the choice's ``scores`` row
    (0..100) — each queue row is one choice, so a contradicted choice (composite 0)
    outranks an unsupported one (25) within the same tier. Higher rank = more
    urgent.
    """
    if not 0.0 <= composite <= 100.0:
        raise DistillError(f"composite must be within 0..100, got {composite!r}")
    return (1.0 - composite / 100.0) * load_weight(artifact_type)


# ---------------------------------------------------------------------------
# Contract models — the `cite distill propose` stdin payload mirror
# ---------------------------------------------------------------------------


class _PayloadBase(BaseModel):
    """``extra="forbid"`` — unknown keys reject the whole payload (same discipline
    as the review-commit contract)."""

    model_config = ConfigDict(extra="forbid")


class ProposalEntry(_PayloadBase):
    """One skill-drafted proposal for one scored choice of the run."""

    choice_key: str
    proposal_kind: ProposalKind
    justification: str  # must cite citation ids or the documented absence
    justifying_citation_ids: list[int] = Field(default_factory=list)
    suggested_rewrite: str | None = None  # concrete replacement text where applicable

    @model_validator(mode="after")
    def _check_shape(self) -> ProposalEntry:
        if not self.choice_key.strip():
            raise ValueError("choice_key must be non-empty")
        if not self.justification.strip():
            raise ValueError(
                "justification must be non-empty — cite citation ids or the "
                "documented absence (distill_queue.justification is NOT NULL)"
            )
        if any(citation_id < 1 for citation_id in self.justifying_citation_ids):
            raise ValueError("justifying_citation_ids must be positive citations.id values")
        if self.suggested_rewrite is not None and not self.suggested_rewrite.strip():
            raise ValueError("suggested_rewrite, when present, must be non-empty")
        if self.proposal_kind == "rewrite" and self.suggested_rewrite is None:
            raise ValueError(
                "a 'rewrite' proposal requires suggested_rewrite — the concrete "
                "replacement text is the proposal (prompts/distill.v1.md contract)"
            )
        return self


class ProposePayload(_PayloadBase):
    """The ``cite distill propose`` stdin payload."""

    proposals: list[ProposalEntry] = Field(min_length=1)
    run_id: int | None = None  # optional when --run is passed; must agree when both

    @model_validator(mode="after")
    def _check_unique_keys(self) -> ProposePayload:
        keys = [entry.choice_key for entry in self.proposals]
        dupes = sorted({key for key in keys if keys.count(key) > 1})
        if dupes:
            raise ValueError(f"duplicate choice_key(s) in payload: {', '.join(dupes)}")
        return self


# ---------------------------------------------------------------------------
# Shared row plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueWrite:
    """One created/refreshed queue row, for the CLI summary."""

    queue_id: int
    choice_id: int
    choice_key: str
    proposal_kind: str
    rank: float
    outcome: str  # 'created' | 'refreshed'


@dataclass(frozen=True)
class GenerateResult:
    """What :func:`generate_queue_rows` did for one committed run."""

    run_id: int
    artifact_id: int
    artifact_path: str
    artifact_type: str
    writes: list[QueueWrite]
    skipped_resolved: list[str]  # choice_keys whose resolved rows were left untouched
    no_row_count: int  # well-supported/interesting choices (no row by design)


@dataclass(frozen=True)
class ProposeResult:
    """What :func:`propose_queue_rows` did for one committed run."""

    run_id: int
    artifact_id: int
    artifact_path: str
    artifact_type: str
    writes: list[QueueWrite]


def _committed_run(conn: sqlite3.Connection, run_id: int) -> tuple[int, str, str, str]:
    """(artifact_id, path, artifact_type, project) — errors unless the run is
    committed (distill runs on committed reviews only; plan Step 6)."""
    row = conn.execute(
        "SELECT r.artifact_id, r.finished_at, r.composite, a.path, a.artifact_type, "
        "a.project FROM review_runs r JOIN artifacts a ON a.id = r.artifact_id "
        "WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise DistillError(f"review run {run_id} does not exist — run `cite review open` first")
    if row[1] is None or row[2] is None:
        raise DistillError(
            f"review run {run_id} is not committed — distill runs on committed "
            "reviews only (run `cite review commit` first)"
        )
    return int(row[0]), str(row[3]), str(row[4]), str(row[5])


@dataclass(frozen=True)
class _ScoredChoice:
    choice_id: int
    choice_key: str
    classification: str
    unsupported_share: float
    contradicted_share: float
    composite: float
    literature_searched: bool
    literature_found: bool
    search_queries: list[str]


def _scored_choices(conn: sqlite3.Connection, run_id: int) -> list[_ScoredChoice]:
    rows = conn.execute(
        "SELECT s.choice_id, c.choice_key, s.classification, s.unsupported_share, "
        "s.contradicted_share, s.composite, s.literature_searched, s.literature_found, "
        "s.search_queries FROM scores s JOIN choices c ON c.id = s.choice_id "
        "WHERE s.review_run_id = ? ORDER BY c.choice_key",
        (run_id,),
    ).fetchall()
    scored: list[_ScoredChoice] = []
    for row in rows:
        choice_key = str(row[1])
        raw_queries = row[8]
        if raw_queries:
            # Fail loud on a corrupted column (calibrate.py's json.loads discipline):
            # review commit always writes json.dumps(list), so anything else is a
            # corrupted scores row, never a value to silently default to [].
            try:
                parsed = json.loads(raw_queries)
            except json.JSONDecodeError as exc:
                raise DistillError(
                    f"scores.search_queries for choice {choice_key!r} in run #{run_id} "
                    f"is not valid JSON — the scores row is corrupted (review commit "
                    "always writes a JSON array)"
                ) from exc
            if not isinstance(parsed, list):
                raise DistillError(
                    f"scores.search_queries for choice {choice_key!r} in run #{run_id} "
                    f"is not a JSON array (got {type(parsed).__name__}) — the scores "
                    "row is corrupted (review commit always writes a JSON array)"
                )
        else:
            parsed = []
        queries = [str(query) for query in parsed]
        scored.append(
            _ScoredChoice(
                choice_id=int(row[0]),
                choice_key=choice_key,
                classification=str(row[2]),
                unsupported_share=float(row[3]),
                contradicted_share=float(row[4]),
                composite=float(row[5]),
                literature_searched=bool(row[6]),
                literature_found=bool(row[7]),
                search_queries=queries,
            )
        )
    return scored


def _linked_citations(conn: sqlite3.Connection, choice_id: int) -> list[tuple[int, str]]:
    """(citation_id, support_direction) pairs linked to the choice, id order."""
    return [
        (int(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT citation_id, support_direction FROM choice_citations "
            "WHERE choice_id = ? ORDER BY citation_id",
            (choice_id,),
        )
    ]


def _existing_queue_row(conn: sqlite3.Connection, choice_id: int) -> tuple[int, str] | None:
    """The choice's queue row as ``(id, status)`` — one row per choice is this
    module's invariant, so at most one exists (ORDER BY is defensive only)."""
    row = conn.execute(
        "SELECT id, status FROM distill_queue WHERE choice_id = ? ORDER BY id LIMIT 1",
        (choice_id,),
    ).fetchone()
    return (int(row[0]), str(row[1])) if row is not None else None


def supersede_stale_open_rows(
    conn: sqlite3.Connection, artifact_id: int, latest_run_id: int
) -> list[str]:
    """DELETE the artifact's OPEN queue rows the newest committed run no longer
    justifies (module docstring § supersession).

    ``latest_run_id`` must be the artifact's newest committed run — in practice the
    run ``review.commit_review`` is committing, which calls this INSIDE its own
    transaction (the caller owns the transaction; nothing here commits). An open
    row survives only when its choice is still ``'active'`` AND scored
    ``needs-improvement`` by ``latest_run_id``; RESOLVED rows
    (accepted/rejected/applied) are immutable audit and are never touched.
    Returns the deleted rows' choice_keys, sorted.
    """
    rows = conn.execute(
        "SELECT q.id, c.choice_key FROM distill_queue q "
        "JOIN choices c ON c.id = q.choice_id "
        "LEFT JOIN scores s ON s.choice_id = q.choice_id AND s.review_run_id = ? "
        "WHERE q.artifact_id = ? AND q.status = 'open' AND (c.status != 'active' "
        "OR s.classification IS NULL OR s.classification != 'needs-improvement')",
        (latest_run_id, artifact_id),
    ).fetchall()
    if not rows:
        return []
    conn.executemany("DELETE FROM distill_queue WHERE id = ?", [(int(row[0]),) for row in rows])
    return sorted(str(row[1]) for row in rows)


def _upsert_open_row(
    conn: sqlite3.Connection,
    *,
    existing_id: int | None,
    choice_id: int,
    artifact_id: int,
    run_id: int,
    proposal_kind: str,
    rank: float,
    justification: str,
    justifying_citation_ids: str | None,
    now: str,
) -> tuple[int, str]:
    """INSERT a fresh open row or refresh the existing open one in place.

    ``status`` is never written on refresh (it is already ``'open'`` — callers
    validated that) and ``created_at`` keeps the row's original creation time.
    Returns ``(queue_id, outcome)``.
    """
    if existing_id is not None:
        conn.execute(
            "UPDATE distill_queue SET proposal_kind = ?, rank = ?, justification = ?, "
            "justifying_citation_ids = ?, review_run_id = ? WHERE id = ?",
            (proposal_kind, rank, justification, justifying_citation_ids, run_id, existing_id),
        )
        return existing_id, "refreshed"
    cursor = conn.execute(
        "INSERT INTO distill_queue (choice_id, artifact_id, review_run_id, proposal_kind, "
        "rank, justification, justifying_citation_ids, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (
            choice_id,
            artifact_id,
            run_id,
            proposal_kind,
            rank,
            justification,
            justifying_citation_ids,
            now,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid), "created"


# ---------------------------------------------------------------------------
# generate_queue_rows — the mechanical default path
# ---------------------------------------------------------------------------


def _majority_negative_label(choice: _ScoredChoice) -> str:
    """Recover the run-committed majority label for a needs-improvement choice.

    ``classification='needs-improvement'`` guarantees the majority label is
    ``unsupported`` or ``contradicted``, and a top-count tie can never reach commit
    (``review.TieError`` rejects it), so comparing the two stored shares recovers
    the majority exactly.
    """
    return "contradicted" if choice.contradicted_share > choice.unsupported_share else "unsupported"


def _mechanical_justification(
    majority: str,
    proposal_kind: str,
    citations: list[tuple[int, str]],
    choice: _ScoredChoice,
) -> tuple[str, str | None]:
    """(justification, justifying_citation_ids JSON) from the choice's real record.

    Callers guarantee ``citations or choice.literature_searched`` — a choice with
    neither was rejected before any row was written.
    """
    if citations:
        ids = [citation_id for citation_id, _ in citations]
        directions = {"supports": 0, "contradicts": 0, "tangential": 0}
        for _, direction in citations:
            if direction in directions:
                directions[direction] += 1
        justification = (
            f"Mechanical default: majority label '{majority}' -> '{proposal_kind}'. "
            f"Evidence on file: citation id(s) {ids} ({directions['supports']} supports / "
            f"{directions['contradicts']} contradicts / {directions['tangential']} tangential "
            "- see choice_citations relevance notes)."
        )
        return justification, json.dumps(ids)
    queries = "; ".join(f'"{query}"' for query in choice.search_queries) or "(none recorded)"
    justification = (
        f"Mechanical default: majority label '{majority}' -> '{proposal_kind}'. "
        f"Documented absence: literature searched, no citations linked "
        f"(literature_found={int(choice.literature_found)}); queries tried: {queries}."
    )
    return justification, None


def generate_queue_rows(conn: sqlite3.Connection, run_id: int) -> GenerateResult:
    """Create/refresh the mechanical-default queue row for each needs-improvement
    choice of a committed run (plan Step 6).

    Well-supported and interesting choices yield NO row. A needs-improvement choice
    with neither linked citations nor a recorded literature search REJECTS the
    whole run loudly BEFORE any write — a justification cannot be fabricated
    (``distill_queue.justification`` is NOT NULL by design). Choices whose queue
    row is already resolved are skipped untouched (the operator decision stands).
    All writes land in one transaction.
    """
    artifact_id, artifact_path, artifact_type, _project = _committed_run(conn, run_id)
    scored = _scored_choices(conn, run_id)
    needs_improvement = [c for c in scored if c.classification == "needs-improvement"]
    no_row_count = len(scored) - len(needs_improvement)

    citations_by_choice = {c.choice_id: _linked_citations(conn, c.choice_id) for c in scored}
    unjustifiable = sorted(
        c.choice_key
        for c in needs_improvement
        if not citations_by_choice[c.choice_id] and not c.literature_searched
    )
    if unjustifiable:
        raise DistillError(
            f"distill generate REJECTED for run #{run_id}: choice(s) "
            f"{', '.join(repr(k) for k in unjustifiable)} have neither linked citations "
            "nor a recorded literature search (scores.literature_searched=0) — a queue "
            "row's justification cannot be fabricated (justification is NOT NULL by "
            "design); re-review with a real search first. No rows were written."
        )

    now = _utc_now()
    writes: list[QueueWrite] = []
    skipped_resolved: list[str] = []
    with conn:  # one transaction — any failure rolls back every write
        for choice in needs_improvement:
            existing = _existing_queue_row(conn, choice.choice_id)
            if existing is not None and existing[1] != "open":
                skipped_resolved.append(choice.choice_key)
                continue
            majority = _majority_negative_label(choice)
            proposal_kind = MECHANICAL_KIND_BY_MAJORITY[majority]
            justification, ids_json = _mechanical_justification(
                majority, proposal_kind, citations_by_choice[choice.choice_id], choice
            )
            queue_id, outcome = _upsert_open_row(
                conn,
                existing_id=existing[0] if existing is not None else None,
                choice_id=choice.choice_id,
                artifact_id=artifact_id,
                run_id=run_id,
                proposal_kind=proposal_kind,
                rank=queue_rank(choice.composite, artifact_type),
                justification=justification,
                justifying_citation_ids=ids_json,
                now=now,
            )
            writes.append(
                QueueWrite(
                    queue_id=queue_id,
                    choice_id=choice.choice_id,
                    choice_key=choice.choice_key,
                    proposal_kind=proposal_kind,
                    rank=queue_rank(choice.composite, artifact_type),
                    outcome=outcome,
                )
            )
    return GenerateResult(
        run_id=run_id,
        artifact_id=artifact_id,
        artifact_path=artifact_path,
        artifact_type=artifact_type,
        writes=writes,
        skipped_resolved=skipped_resolved,
        no_row_count=no_row_count,
    )


# ---------------------------------------------------------------------------
# propose_queue_rows — the skill-drafted override path
# ---------------------------------------------------------------------------


def propose_queue_rows(
    conn: sqlite3.Connection,
    run_id: int,
    payload: ProposePayload | dict[str, Any],
) -> ProposeResult:
    """Upsert skill-drafted proposals over the mechanical defaults (plan Step 6).

    Same discipline as ``review commit``: the WHOLE payload rejects on any invalid
    entry — an unscored choice_key, a citation id absent from the corpus, or a
    choice whose queue row is already resolved (the recorded decision stands) —
    and nothing is written. Any choice SCORED in the run is proposable (the skill
    layer's judgment may exceed the mechanical needs-improvement rule, e.g. a
    ``'no-action'`` record or ``'delete-superseded'`` on a well-supported
    duplicate). Rank stays formula-computed from the choice's scores row — the
    payload overrides kind + justification only. ``status`` stays ``'open'``.
    """
    if isinstance(payload, dict):
        payload = ProposePayload.model_validate(payload)
    if payload.run_id is not None and payload.run_id != run_id:
        raise DistillError(f"payload run_id {payload.run_id} does not match run {run_id}")
    artifact_id, artifact_path, artifact_type, _project = _committed_run(conn, run_id)
    scored_by_key = {choice.choice_key: choice for choice in _scored_choices(conn, run_id)}

    # Validate EVERY entry before any write (whole-payload reject).
    problems: list[str] = []
    existing_by_key: dict[str, tuple[int, str] | None] = {}
    for entry in payload.proposals:
        choice = scored_by_key.get(entry.choice_key)
        if choice is None:
            problems.append(f"choice {entry.choice_key!r} was not scored in run #{run_id}")
            continue
        existing = _existing_queue_row(conn, choice.choice_id)
        existing_by_key[entry.choice_key] = existing
        if existing is not None and existing[1] != "open":
            problems.append(
                f"choice {entry.choice_key!r} already has a resolved queue row "
                f"(status {existing[1]!r}) — the recorded operator decision stands"
            )
        for citation_id in entry.justifying_citation_ids:
            row = conn.execute("SELECT id FROM citations WHERE id = ?", (citation_id,)).fetchone()
            if row is None:
                problems.append(
                    f"choice {entry.choice_key!r} cites citation id {citation_id}, "
                    "which does not exist in the corpus"
                )
    if problems:
        raise DistillError(
            "distill propose REJECTED (whole payload): "
            + "; ".join(problems)
            + ". No rows were written."
        )

    now = _utc_now()
    writes: list[QueueWrite] = []
    with conn:  # one transaction — any failure rolls back every write
        for entry in payload.proposals:
            choice = scored_by_key[entry.choice_key]
            justification = entry.justification.strip()
            if entry.suggested_rewrite is not None:
                justification = (
                    f"{justification}\n\nSuggested rewrite:\n{entry.suggested_rewrite.strip()}"
                )
            ids = sorted(set(entry.justifying_citation_ids))
            existing = existing_by_key[entry.choice_key]
            queue_id, outcome = _upsert_open_row(
                conn,
                existing_id=existing[0] if existing is not None else None,
                choice_id=choice.choice_id,
                artifact_id=artifact_id,
                run_id=run_id,
                proposal_kind=entry.proposal_kind,
                rank=queue_rank(choice.composite, artifact_type),
                justification=justification,
                justifying_citation_ids=json.dumps(ids) if ids else None,
                now=now,
            )
            writes.append(
                QueueWrite(
                    queue_id=queue_id,
                    choice_id=choice.choice_id,
                    choice_key=entry.choice_key,
                    proposal_kind=entry.proposal_kind,
                    rank=queue_rank(choice.composite, artifact_type),
                    outcome=outcome,
                )
            )
    return ProposeResult(
        run_id=run_id,
        artifact_id=artifact_id,
        artifact_path=artifact_path,
        artifact_type=artifact_type,
        writes=writes,
    )


# ---------------------------------------------------------------------------
# Queue triage — list + resolve
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueListRow:
    """One ranked queue row, denormalized for the ``cite queue list`` table."""

    queue_id: int
    artifact_path: str
    artifact_type: str
    project: str
    choice_key: str
    proposal_kind: str
    composite: float  # the per-choice composite from the row's scores record
    composite_band: str
    rank: float
    status: str
    justification: str
    created_at: str
    resolved_at: str | None
    resolved_by: str | None
    review_run_id: int
    #: True when the artifact has a NEWER committed run than the row's source run —
    #: the display-side staleness guard (module docstring § supersession). Open rows
    #: only carry it transiently (commit purges/refreshes); resolved rows keep their
    #: historical source run, so the marker is expected there.
    superseded_run: bool


def list_queue(
    conn: sqlite3.Connection,
    *,
    status: str | None = "open",
    project: str | None = None,
) -> list[QueueListRow]:
    """Ranked queue rows (rank desc, id asc for stable ties). ``status=None``
    lists every status; ``project`` filters on the artifact's registry slug."""
    if status is not None and status not in QUEUE_STATUSES:
        raise DistillError(f"unknown status {status!r} — expected one of {list(QUEUE_STATUSES)}")
    sql = (
        "SELECT q.id, a.path, a.artifact_type, a.project, c.choice_key, q.proposal_kind, "
        "s.composite, s.composite_band, q.rank, q.status, q.justification, q.created_at, "
        "q.resolved_at, q.resolved_by, q.review_run_id, "
        "(SELECT MAX(r.id) FROM review_runs r WHERE r.artifact_id = q.artifact_id "
        "AND r.finished_at IS NOT NULL) "
        "FROM distill_queue q "
        "JOIN choices c ON c.id = q.choice_id "
        "JOIN artifacts a ON a.id = q.artifact_id "
        "JOIN scores s ON s.review_run_id = q.review_run_id AND s.choice_id = q.choice_id"
    )
    conditions: list[str] = []
    params: list[str] = []
    if status is not None:
        conditions.append("q.status = ?")
        params.append(status)
    if project is not None:
        conditions.append("a.project = ?")
        params.append(project)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY q.rank DESC, q.id ASC"
    return [
        QueueListRow(
            queue_id=int(row[0]),
            artifact_path=str(row[1]),
            artifact_type=str(row[2]),
            project=str(row[3]),
            choice_key=str(row[4]),
            proposal_kind=str(row[5]),
            composite=float(row[6]),
            composite_band=str(row[7]),
            rank=float(row[8]),
            status=str(row[9]),
            justification=str(row[10]),
            created_at=str(row[11]),
            resolved_at=str(row[12]) if row[12] is not None else None,
            resolved_by=str(row[13]) if row[13] is not None else None,
            review_run_id=int(row[14]),
            # The row's own run is committed by construction, so a newer committed
            # run exists exactly when the artifact's max committed id is greater.
            superseded_run=row[15] is not None and int(row[15]) != int(row[14]),
        )
        for row in conn.execute(sql, params)
    ]


@dataclass(frozen=True)
class ResolveOutcome:
    """What ``cite queue resolve`` recorded."""

    queue_id: int
    choice_key: str
    artifact_path: str
    decision: str  # keep | cut | rewrite
    status: str  # the mapped distill_queue.status
    resolved_by: str
    resolved_at: str


def default_resolver_name(explicit: str | None = None) -> str:
    """``--by`` wins; else env ``USERNAME`` (Windows) then ``USER`` (POSIX);
    neither -> a loud error (an anonymous resolution is not an audit trail)."""
    if explicit is not None and explicit.strip():
        return explicit.strip()
    for var in ("USERNAME", "USER"):
        value = os.environ.get(var)
        if value is not None and value.strip():
            return value.strip()
    raise DistillError("no resolver identity — pass --by <name> (neither USERNAME nor USER is set)")


def resolve_queue_item(
    conn: sqlite3.Connection,
    queue_id: int,
    decision: str,
    *,
    resolved_by: str,
) -> ResolveOutcome:
    """Record the operator decision on one OPEN queue row.

    Mapping (the module docstring's contract): ``keep`` -> ``'rejected'``,
    ``cut``/``rewrite`` -> ``'accepted'``. A row that is not ``'open'`` refuses
    loudly — the recorded decision stands (re-resolution is not a v1 operation).
    ``resolved_at`` is the pipeline clock.
    """
    status = STATUS_BY_DECISION.get(decision)
    if status is None:
        raise DistillError(
            f"unknown decision {decision!r} — expected one of {sorted(STATUS_BY_DECISION)}"
        )
    if not resolved_by or not resolved_by.strip():
        raise DistillError("resolved_by must be non-empty (the decision must be attributable)")
    row = conn.execute(
        "SELECT q.status, c.choice_key, a.path FROM distill_queue q "
        "JOIN choices c ON c.id = q.choice_id JOIN artifacts a ON a.id = q.artifact_id "
        "WHERE q.id = ?",
        (queue_id,),
    ).fetchone()
    if row is None:
        raise DistillError(f"distill_queue row {queue_id} does not exist — see `cite queue list`")
    if str(row[0]) != "open":
        raise DistillError(
            f"distill_queue row {queue_id} is already resolved (status {row[0]!r}) — "
            "the recorded decision stands; a fresh review + propose creates no "
            "duplicate (one row per choice)"
        )
    resolved_at = _utc_now()
    with conn:
        conn.execute(
            "UPDATE distill_queue SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?",
            (status, resolved_at, resolved_by.strip(), queue_id),
        )
    return ResolveOutcome(
        queue_id=queue_id,
        choice_key=str(row[1]),
        artifact_path=str(row[2]),
        decision=decision,
        status=status,
        resolved_by=resolved_by.strip(),
        resolved_at=resolved_at,
    )


__all__ = [
    "LOAD_WEIGHTS",
    "MECHANICAL_KIND_BY_MAJORITY",
    "PROPOSAL_KINDS",
    "QUEUE_STATUSES",
    "STATUS_BY_DECISION",
    "DistillError",
    "GenerateResult",
    "ProposalEntry",
    "ProposePayload",
    "ProposeResult",
    "QueueListRow",
    "QueueWrite",
    "ResolveOutcome",
    "default_resolver_name",
    "generate_queue_rows",
    "list_queue",
    "load_weight",
    "propose_queue_rows",
    "queue_rank",
    "resolve_queue_item",
    "supersede_stale_open_rows",
]
