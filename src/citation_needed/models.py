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
