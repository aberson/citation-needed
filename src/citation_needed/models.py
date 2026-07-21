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


class _DetailsBase(BaseModel):
    """Shared strictness for every details model."""

    model_config = ConfigDict(extra="forbid")


class MemoryDetails(_DetailsBase):
    """Memory files: YAML frontmatter with a nested ``metadata:`` map (corpus-survey.md §6)."""

    node_type: str | None = None
    memory_kind: Literal["feedback", "user", "project", "reference"] | None = None
    origin_session_id: str | None = None
    #: Derived from the memory directory, not a file field: global vs per-project scope.
    memory_scope: Literal["global", "project"] | None = None
    frontmatter_modified: str | None = None


class SkillDetails(_DetailsBase):
    """SKILL.md (and ``.claude/commands/*.md`` ingested as this type): flat frontmatter."""

    name: str | None = None
    description: str | None = None
    user_invocable: bool | None = None
    #: An ``evals/`` sidecar dir is present (reviewed as part of the owning skill).
    has_evals: bool | None = None
    #: Pointer resolution (corpus-survey.md §1): thin-wrapper bodies of the form
    #: "Read <path> and follow it" resolve to the choice-bearing file.
    is_pointer: bool | None = None
    pointer_target: str | None = None


class RuleDetails(_DetailsBase):
    """``.claude/rules/*.md``: no frontmatter; the informal ``## Source`` memory list."""

    source_memory_paths: list[str] | None = None


class ClaudeMdDetails(_DetailsBase):
    """CLAUDE.md files: root (auto-loads every session — the most expensive tier) vs project."""

    scope: Literal["root", "project"] | None = None
    project_slug: str | None = None


class PlanDetails(_DetailsBase):
    """Plan docs: inline-steps vs pointer-only distinction (descriptor-contract.md §4)."""

    plan_kind: Literal["root", "master", "feature"] | None = None
    is_pointer_only: bool | None = None
    step_count: int | None = None
    phase_count: int | None = None


#: Registry keyed by ``artifacts.artifact_type`` — must cover exactly the CHECK enum in
#: schema.sql (asserted by tests/test_schema.py so re-duplication fails).
DETAILS_MODELS: dict[str, type[_DetailsBase]] = {
    "memory": MemoryDetails,
    "skill": SkillDetails,
    "rule": RuleDetails,
    "claude_md": ClaudeMdDetails,
    "plan": PlanDetails,
}
