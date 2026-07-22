"""Breakdown renderer — the human-readable review output doc.

Central, gitignored output (plan.md §3.2 / D2): ``breakdowns/<project-slug>/
<artifact-slug>.md`` inside citation-needed's own tree, NEVER written into a target
project (the only shape write-safe for owned and not-owned targets).

Slug rule (plan §3.2): the artifact ``path`` lowercased, the trailing ``.md``
dropped, and every ``/``, ``:``, and space replaced by ``--`` (plus ``\\`` —
defense-in-depth beyond the plan's prose: a backslash is a REAL separator to pathlib
on Windows, so leaving it un-neutralized would be a write-escape sink if a
backslash-laden path ever reached this module); a LEADING ``.`` is also replaced by
``--`` — reproducing the plan's canonical example
(``.claude/rules/subagent-economy.md`` -> ``--claude--rules--subagent-economy``)
and keeping the output file from being dot-hidden on POSIX filesystems.

The slug is deliberately NOT injective (``/``, ``:``, and space all collapse to
``--``), so two ordinary artifact paths can collide (``docs/release notes.md`` vs
``docs/release/notes.md``). :func:`write_breakdown` DETECTS that: the rendered doc's
header records the exact artifact path, so before writing it checks whether the
destination already belongs to a DIFFERENT artifact and, if so, diverts to a
hash-discriminated sibling (``<slug>--<8-hex sha256 of the exact artifact path>.md``)
with a loud collision note in the returned :class:`BreakdownWrite`.
:func:`locate_breakdown` (used by ``cite report``) checks both candidates.

A destination whose header cannot be READ AT ALL — invalid UTF-8 from an interrupted
write or a non-UTF-8 hand-edit of this human-readable doc, or a permission failure —
is treated exactly like a foreign header: ownership is unknown, so the write diverts
(never overwrites a file it cannot identify) and ``cite report`` degrades to the
sibling candidate. Neither path ever lets a ``UnicodeDecodeError`` escape as a raw
traceback (the clean ``error:``/safe-divert contract).

Rendering is DETERMINISTIC: the same :class:`review.CommitResult` always produces the
same bytes — choices render in payload order, removed keys sorted, no wall-clock
reads (every timestamp is a stored DB value). ``cite report`` locates the doc via the
same slug convention.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from citation_needed.review import CommitResult, CommittedChoice

#: One-line rendering of each vote-share column, in schema column order.
_SHARE_LABELS = (
    ("evidence-backed", "evidence_backed_share"),
    ("interesting-novel", "interesting_novel_share"),
    ("unsupported", "unsupported_share"),
    ("contradicted", "contradicted_share"),
)


def artifact_slug(path: str) -> str:
    """Plan §3.2 slug: lowercase, drop trailing ``.md``, ``/``/``\\``/``:``/space ->
    ``--``, leading ``.`` -> ``--`` (canonical: ``.claude/rules/subagent-economy.md``
    -> ``--claude--rules--subagent-economy``). Backslash is neutralized beyond the
    plan's prose — on Windows it is a real path separator (see the module docstring)."""
    slug = path.lower()
    if slug.endswith(".md"):
        slug = slug[: -len(".md")]
    slug = slug.replace("\\", "--").replace("/", "--").replace(":", "--").replace(" ", "--")
    if slug.startswith("."):
        slug = "--" + slug[1:]
    return slug


def breakdown_path(breakdowns_root: Path, project: str, artifact_path: str) -> Path:
    """``<root>/<project-slug>/<artifact-slug>.md`` — the fixed, discoverable path."""
    return breakdowns_root / project / f"{artifact_slug(artifact_path)}.md"


def _discriminated_path(breakdowns_root: Path, project: str, artifact_path: str) -> Path:
    """The collision-diverted sibling: ``<slug>--<8-hex sha256 of the exact path>.md``."""
    suffix = hashlib.sha256(artifact_path.encode("utf-8")).hexdigest()[:8]
    return breakdowns_root / project / f"{artifact_slug(artifact_path)}--{suffix}.md"


_HEADER_PREFIX = "# Citation review — "


@dataclass(frozen=True)
class _UnreadableHeader:
    """Sentinel from :func:`_recorded_artifact_path`: the doc EXISTS but its header
    line could not be read or decoded (invalid UTF-8 from an interrupted write or a
    non-UTF-8 hand-edit, a permission failure, ...). Ownership is UNKNOWN, so both
    callers treat it exactly like a foreign header — divert, never overwrite. (A
    plain ``None`` here would make :func:`write_breakdown` silently overwrite a file
    it cannot identify.)"""

    error: str


def _recorded_artifact_path(doc: Path) -> str | _UnreadableHeader | None:
    """The exact artifact path a breakdown doc records in its first header line.

    ``None`` when the file is absent or present-but-not-a-rendered-breakdown (both
    mean the canonical name is free to claim); an :class:`_UnreadableHeader` when the
    file EXISTS but its header cannot be read as UTF-8 text (undecodable bytes,
    permission failure — callers divert instead of overwriting an unidentifiable
    file). ``UnicodeDecodeError`` is caught alongside ``OSError`` so a corrupted or
    hand-edited doc can never crash ``cite report`` / ``cite review commit``."""
    try:
        with doc.open(encoding="utf-8") as handle:
            first = handle.readline().rstrip("\r\n")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _UnreadableHeader(error=str(exc))
    if first.startswith(_HEADER_PREFIX):
        return first[len(_HEADER_PREFIX) :]
    return None


def locate_breakdown(breakdowns_root: Path, project: str, artifact_path: str) -> Path:
    """Where this artifact's breakdown actually lives — canonical path first, then the
    hash-discriminated sibling a slug collision diverts to (``cite report`` uses this
    so a collided artifact's report names the file that really holds its review)."""
    canonical = breakdown_path(breakdowns_root, project, artifact_path)
    recorded = _recorded_artifact_path(canonical)
    if recorded is None or recorded == artifact_path:
        return canonical
    # The canonical file belongs to a DIFFERENT artifact — or exists with an
    # unreadable/undecodable header, so it cannot be confirmed as this artifact's —
    # meaning this artifact's doc can only live at its discriminated sibling
    # (missing there = genuinely missing; a re-review regenerates it).
    return _discriminated_path(breakdowns_root, project, artifact_path)


@dataclass(frozen=True)
class BreakdownWrite:
    """What :func:`write_breakdown` did: the written path + a loud collision note
    (``None`` in the common, collision-free case)."""

    path: Path
    collision_note: str | None


def _quote_block(quote: str) -> list[str]:
    return [f"> {line}" if line.strip() else ">" for line in quote.splitlines()]


def _citation_lines(choice: CommittedChoice) -> list[str]:
    if not choice.citations:
        return ["**Citations:** none linked."]
    lines = ["**Citations:**"]
    for citation in choice.citations:
        title = citation.title or "(untitled)"
        locator = (
            citation.source_line_ref
            if citation.kind == "internal" and citation.source_line_ref
            else citation.locator
        )
        lines.append(
            f"- [{citation.kind}] {title} — {locator} "
            f"({citation.support_direction}; {citation.resolution_method})"
        )
        lines.append(f"  - {citation.relevance_note}")
    return lines


def _literature_lines(choice: CommittedChoice) -> list[str]:
    if not choice.literature_searched:
        return ["**Literature search:** not attempted for this choice."]
    if choice.literature_found:
        lines = ["**Literature search:** searched — literature found. Queries tried:"]
    else:
        lines = [
            "**Literature search:** searched — NO literature found "
            "(a recorded, legitimate result, not a failure). Queries tried:"
        ]
    lines.extend(f"- `{query}`" for query in choice.search_queries)
    return lines


def _suggestion_lines(choice: CommittedChoice) -> list[str]:
    if choice.tally.classification != "needs-improvement" and not choice.suggestions:
        return []
    lines = ["**Suggestions:**"]
    if choice.suggestions:
        lines.extend(f"- {suggestion}" for suggestion in choice.suggestions)
    else:
        lines.append(
            "- (none recorded — needs-improvement choices should carry actionable suggestions)"
        )
    return lines


def _choice_block(index: int, choice: CommittedChoice, artifact_path: str) -> list[str]:
    tally = choice.tally
    span_source = choice.source_path or artifact_path
    shares = " · ".join(f"{label} {getattr(tally, attr):.2f}" for label, attr in _SHARE_LABELS)
    lines = [
        f"### {index}. `{choice.choice_key}` — {tally.classification}",
        "",
        f"- **Summary:** {choice.summary}",
        f"- **Category:** {choice.category}",
        f"- **Span:** {span_source}:{choice.span_start_line}-{choice.span_end_line}",
        f"- **Majority label:** {tally.majority_label} (k={len(choice.votes)})",
        f"- **Vote shares:** {shares}",
    ]
    if choice.rationale:
        lines.append(f"- **Rationale:** {choice.rationale}")
    lines.append("")
    lines.extend(_quote_block(choice.quote))
    lines.append("")
    lines.extend(_citation_lines(choice))
    lines.append("")
    lines.extend(_literature_lines(choice))
    suggestions = _suggestion_lines(choice)
    if suggestions:
        lines.append("")
        lines.extend(suggestions)
    return lines


def render_breakdown(result: CommitResult) -> str:
    """Render one committed review run to markdown (deterministic, stable ordering)."""
    git_sha = (
        f"`{result.artifact_git_sha_at_review}`"
        if result.artifact_git_sha_at_review
        else "(not a git repo)"
    )
    counts = result.classification_counts()
    lines = [
        f"# Citation review — {result.artifact_path}",
        "",
        f"- **Artifact:** {result.artifact_path} ({result.artifact_type}, "
        f"project `{result.project}`)",
        f"- **Review run:** #{result.run_id} — opened {result.started_at}, "
        f"committed {result.finished_at}",
        f"- **Reviewer model:** {result.reviewer_model}",
        f"- **Frozen provenance:** content hash "
        f"`{result.artifact_content_hash_at_review}`; git sha {git_sha}; "
        f"tool schema v{result.tool_schema_version}",
        f"- **Composite:** {result.composite:.1f} / 100 — band "
        f"**{result.composite_band}** (interpretation guide "
        f"{result.interpretation_guide_version})",
        "",
        f"## Choices ({len(result.choices)} scored: "
        f"well-supported {counts['well-supported']}, "
        f"needs-improvement {counts['needs-improvement']}, "
        f"interesting {counts['interesting']}; {len(result.removed_keys)} removed this run)",
        "",
    ]
    for index, choice in enumerate(result.choices, start=1):
        lines.extend(_choice_block(index, choice, result.artifact_path))
        lines.append("")
    if result.removed_keys:
        lines.append("## Removed this run")
        lines.append("")
        lines.extend(
            f"- `{key}` — not re-observed; marked `removed` (its citations remain in the corpus)."
            for key in sorted(result.removed_keys)
        )
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Score semantics (labels, weights, vote shares, composite, bands): "
        f"docs/interpretation-guide.md ({result.interpretation_guide_version})."
    )
    return "\n".join(lines) + "\n"


def write_breakdown(result: CommitResult, breakdowns_root: Path) -> BreakdownWrite:
    """Render + write the breakdown doc; returns the written path + collision note.

    Collision detection (the slug is not injective): when the canonical destination
    already exists AND its recorded header artifact path differs from this result's
    artifact, the write diverts to the hash-discriminated sibling instead of silently
    overwriting the other artifact's doc, and the returned
    :attr:`BreakdownWrite.collision_note` says so loudly. A destination whose header
    cannot be read/decoded at all (corrupt or non-UTF-8 file) diverts the same way —
    ownership unknown means never overwrite — with its own loud note.
    """
    path = breakdown_path(breakdowns_root, result.project, result.artifact_path)
    collision_note: str | None = None
    recorded = _recorded_artifact_path(path)
    if recorded is not None and recorded != result.artifact_path:
        diverted = _discriminated_path(breakdowns_root, result.project, result.artifact_path)
        if isinstance(recorded, _UnreadableHeader):
            collision_note = (
                f"COLLISION: breakdown file '{path.name}' exists but its header could "
                f"not be read as UTF-8 ({recorded.error}) — likely an interrupted write "
                f"or a non-UTF-8 hand-edit; leaving that file untouched and writing this "
                f"review for '{result.artifact_path}' to '{diverted.name}' instead "
                f"(cite report checks both candidates; delete the unreadable file to "
                f"reclaim the canonical name)."
            )
        else:
            collision_note = (
                f"COLLISION: breakdown slug '{path.name}' already belongs to artifact "
                f"'{recorded}'; writing this review for '{result.artifact_path}' to "
                f"'{diverted.name}' instead (cite report checks both candidates)."
            )
        path = diverted
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_breakdown(result), encoding="utf-8", newline="\n")
    return BreakdownWrite(path=path, collision_note=collision_note)


__all__ = [
    "BreakdownWrite",
    "artifact_slug",
    "breakdown_path",
    "locate_breakdown",
    "render_breakdown",
    "write_breakdown",
]
