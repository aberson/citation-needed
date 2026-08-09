"""Pydantic models mirroring ``artifacts.details_json`` per artifact_type.

The DB stores type-specific artifact fields in a single ``details_json`` column
(schema-draft.md §1: hybrid typing — common columns real, per-type fields JSON).
What the DB cannot enforce, these models do: every ``details_json`` write is validated
against the type's model first, and ``tests/test_schema.py`` asserts the
:data:`DETAILS_MODELS` registry covers exactly the ``artifact_type`` CHECK enum in
``schema.sql``, so the two cannot drift.

Field sets follow schema-draft.md §1's per-type table + corpus-survey.md; everything is
optional (discovery may not resolve every field), ``extra="forbid"`` keeps unknown keys
from silently accumulating in the corpus.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: Enum aliases — the ONE definition of each closed value set. ``discover.py`` derives its
#: runtime guards from these via ``typing.get_args`` so the two can never drift
#: (code-quality.md § one source of truth).
MemoryKind = Literal["feedback", "user", "project", "reference"]
MemoryScope = Literal["global", "project"]
CommandStyle = Literal["skill_dir", "commands_dir"]
ClaudeMdScope = Literal["root", "project"]
PlanKind = Literal["root", "master", "feature"]


class _DetailsBase(BaseModel):
    """Shared strictness for every details model."""

    model_config = ConfigDict(extra="forbid")


class MemoryDetails(_DetailsBase):
    """Memory files: YAML frontmatter with a nested ``metadata:`` map (corpus-survey.md §6)."""

    node_type: str | None = None
    memory_kind: MemoryKind | None = None
    origin_session_id: str | None = None
    #: Derived from the memory directory, not a file field: global vs per-project scope.
    memory_scope: MemoryScope | None = None
    frontmatter_modified: str | None = None
    #: Missing/malformed frontmatter is tolerated, never a crash — the error is recorded.
    frontmatter_error: str | None = None


class SkillDetails(_DetailsBase):
    """SKILL.md (and ``.claude/commands/*.md`` ingested as this type): flat frontmatter."""

    name: str | None = None
    description: str | None = None
    user_invocable: bool | None = None
    #: An ``evals/`` sidecar dir is present (reviewed as part of the owning skill).
    has_evals: bool | None = None
    #: Provenance of the same-shaped artifact: ``.claude/skills/*/SKILL.md`` vs
    #: ``.claude/commands/*.md`` (artifact-type-extensions.md §b — same type, same extractor).
    command_style: CommandStyle | None = None
    #: Pointer resolution (corpus-survey.md §1): thin-wrapper bodies of the form
    #: "Read <path> and follow it" resolve to the choice-bearing file.
    is_pointer: bool | None = None
    pointer_target: str | None = None
    #: True when the pointer target is itself a scanned artifact — recorded as a
    #: relationship and SKIPPED at extraction (no double-extraction, plan.md §4.1);
    #: False marks the target for inlining at review time.
    pointer_target_is_artifact: bool | None = None
    #: Pointer body detected but the target file does not exist — surfaced by ``cite scan``
    #: so an unresolved pointer never silently reports zero choices.
    pointer_unresolved: bool | None = None
    #: Which base the pointer target resolved against — ``"file"`` (the pointing file's own
    #: directory), ``"home"``/``"absolute"``/``"workspace"`` for explicit-prefix tokens, or
    #: the workspace-relative posix of the matching ancestor dir (``"."`` = workspace root).
    #: Auditability guard: an ancestor resolution is visible in the row, never silent.
    resolution_base: str | None = None
    #: Missing/malformed frontmatter is tolerated, never a crash — the error is recorded.
    frontmatter_error: str | None = None


class RuleDetails(_DetailsBase):
    """``.claude/rules/*.md``: no frontmatter; the informal ``## Source`` memory list."""

    source_memory_paths: list[str] | None = None


class ClaudeMdDetails(_DetailsBase):
    """CLAUDE.md files: root (auto-loads every session — the most expensive tier) vs project."""

    scope: ClaudeMdScope | None = None
    project_slug: str | None = None
    #: ``@path`` imports that are themselves scanned artifacts — recorded as relationships
    #: and SKIPPED at extraction (their choices belong to their own review; plan.md §4.1).
    artifact_imports: list[str] | None = None
    #: ``@path`` imports to existing non-artifact files — marked for inlining at review time.
    inline_targets: list[str] | None = None
    #: ``@path`` imports whose target file does not exist — surfaced by ``cite scan``.
    pointer_unresolved: list[str] | None = None


class PlanDetails(_DetailsBase):
    """Plan docs: inline-steps vs pointer-only distinction (descriptor-contract.md §4)."""

    plan_kind: PlanKind | None = None
    is_pointer_only: bool | None = None
    step_count: int | None = None
    phase_count: int | None = None
    #: Pointer-only plans: resolved sub-plan links (workspace-relative, forward slashes).
    linked_targets: list[str] | None = None
    #: Plan links whose target file does not exist — surfaced by ``cite scan``.
    pointer_unresolved: list[str] | None = None


#: Public alias for typing a discovered artifact's ``details`` (any per-type model).
ArtifactDetails = _DetailsBase

#: Registry keyed by ``artifacts.artifact_type`` — must cover exactly the CHECK enum in
#: schema.sql (asserted by tests/test_schema.py so re-duplication fails).
DETAILS_MODELS: dict[str, type[_DetailsBase]] = {
    "memory": MemoryDetails,
    "skill": SkillDetails,
    "rule": RuleDetails,
    "claude_md": ClaudeMdDetails,
    "plan": PlanDetails,
}


# ---------------------------------------------------------------------------
# Read-side surface DTOs
# ---------------------------------------------------------------------------

# These models deliberately live beside the write-side details models. The database is
# still the source of truth; this is the versioned, JSON-safe boundary consumed by the
# terminal now and the observatory exporter later (justification-surfaces-plan.md Step 11).

OverviewState = Literal[
    "uninitialized",
    "initialized-empty",
    "review-required",
    "ready",
    "stale",
    "review-in-progress",
]
ReviewActivityState = Literal["in-progress", "completed", "aborted"]
ArtifactReadinessState = Literal["stale", "unreviewed", "current-content-unavailable"]


class _ReadSurfaceBase(BaseModel):
    """Strict, immutable DTO base for versioned read surfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OverviewCounts(_ReadSurfaceBase):
    """Actual counts from an initialized database; never synthesized for an absent DB."""

    artifacts: int
    active_artifacts: int
    completed_reviews: int
    reviews_in_progress: int
    choices: int
    active_choices: int
    citations: int
    open_distill_queue: int


class OverviewReviewActivity(_ReadSurfaceBase):
    """One recent review run, including unfinished work without pretending it completed."""

    run_id: int
    artifact_id: int
    artifact_path: str
    artifact_type: str
    project: str
    reviewer_model: str
    started_at: str
    finished_at: str | None
    state: ReviewActivityState
    composite: float | None
    composite_band: str | None


class OverviewArtifactReadiness(_ReadSurfaceBase):
    """An active artifact whose current review/readiness cannot be called ready."""

    artifact_id: int
    path: str
    artifact_type: str
    project: str
    state: ArtifactReadinessState
    latest_completed_run_id: int | None
    latest_completed_at: str | None


class OverviewArtifactList(_ReadSurfaceBase):
    """A bounded list plus its complete total, so truncation is never mistaken for zero."""

    total: int
    items: list[OverviewArtifactReadiness]


class OverviewQueueItem(_ReadSurfaceBase):
    """One open, ranked distill item in the bounded overview queue."""

    queue_id: int
    artifact_path: str
    choice_key: str
    proposal_kind: str
    rank: float
    created_at: str


class OverviewQueue(_ReadSurfaceBase):
    """Open distill queue, bounded in payload while retaining the full row count."""

    total: int
    items: list[OverviewQueueItem]


class Overview(_ReadSurfaceBase):
    """Version 1 of the read-only ``cite overview --json`` contract."""

    schema_version: Literal[1] = 1
    state: OverviewState
    database_schema_version: int | None
    counts: OverviewCounts | None
    recent_activity: list[OverviewReviewActivity] | None
    stale_artifacts: OverviewArtifactList | None
    unreviewed_artifacts: OverviewArtifactList | None
    current_content_unavailable_artifacts: OverviewArtifactList | None
    open_distill_queue: OverviewQueue | None


# ---------------------------------------------------------------------------
# Skill justification DTOs
# ---------------------------------------------------------------------------

LocatorStatus = Literal["current", "ambiguous", "missing", "unavailable"]
LocatorMethod = Literal[
    "unchanged-artifact-hash",
    "unique-quote-match",
    "multiple-quote-matches",
    "quote-not-found",
    "stored-quote-hash-mismatch",
    "stored-quote-unavailable",
    "source-unavailable",
]
SearchStatus = Literal["not-attempted", "found", "no-result", "invalid-record"]


class JustificationReview(_ReadSurfaceBase):
    """Frozen provenance and score for one completed review run."""

    run_id: int
    started_at: str
    finished_at: str
    reviewer_model: str
    artifact_content_hash_at_review: str
    artifact_git_sha_at_review: str | None
    tool_schema_version: int
    composite: float | None
    composite_band: str | None
    interpretation_guide_version: str | None


class JustificationListItem(_ReadSurfaceBase):
    """One reviewed artifact offered by the deterministic justification list."""

    artifact_id: int
    path: str
    artifact_type: str
    project: str
    is_active: bool
    latest_review: JustificationReview
    choice_count: int


class JustificationList(_ReadSurfaceBase):
    """Version 1 list result; each listed ``artifact_id`` resolves through ``show``."""

    schema_version: Literal[1] = 1
    artifact_type: str
    items: list[JustificationListItem]


class CurrentLocator(_ReadSurfaceBase):
    """A stored choice span plus its honest current-location resolution."""

    source_path: str
    status: LocatorStatus
    method: LocatorMethod
    recorded_span_start_line: int | None
    recorded_span_end_line: int | None
    current_span_start_line: int | None
    current_span_end_line: int | None
    detail: str | None


class JustificationSearch(_ReadSurfaceBase):
    """Persisted literature search record, including auditable documented absence."""

    attempted: bool
    found: bool
    status: SearchStatus
    queries: list[str] | None


class JustificationCitation(_ReadSurfaceBase):
    """One verified citation row linked to a displayed choice."""

    citation_id: int
    kind: str
    natural_key: str
    title: str | None
    authors: str | None
    year: int | None
    venue: str | None
    url_or_doi: str | None
    workspace_path: str | None
    verified_at: str
    resolution_method: str
    supporting_quote: str | None
    keywords: str | None
    source_git_sha: str | None
    source_line_ref: str | None
    notes: str | None
    relevance_note: str
    support_direction: str
    first_linked_review_run_id: int
    last_confirmed_review_run_id: int


class JustificationChoice(_ReadSurfaceBase):
    """A score row and its citation/search/locator evidence for one latest-review choice."""

    choice_id: int
    choice_key: str
    summary: str
    quote_or_span: str | None
    status: str
    locator: CurrentLocator
    classification: str
    composite: float
    composite_band: str
    evidence_backed_share: float
    interesting_novel_share: float
    unsupported_share: float
    contradicted_share: float
    rationale: str | None
    literature: JustificationSearch
    citations: list[JustificationCitation]


class JustificationArtifact(_ReadSurfaceBase):
    """The artifact identity carried by a detail record."""

    artifact_id: int
    path: str
    artifact_type: str
    project: str
    is_active: bool


class JustificationDetail(_ReadSurfaceBase):
    """Version 1 detail result for a reviewed artifact."""

    schema_version: Literal[1] = 1
    artifact: JustificationArtifact
    review: JustificationReview
    choices: list[JustificationChoice]
