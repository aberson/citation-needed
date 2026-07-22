"""Artifact discovery + typed ingestion — the mechanics behind ``cite scan``.

Discovers the 5 v1 artifact types (plan.md §4.1 item 1 + §3.1 artifacts bullet):

- ``rule``      — ``**/.claude/rules/*.md``
- ``skill``     — ``**/.claude/skills/*/SKILL.md`` AND ``**/.claude/commands/*.md``
                  (commands ingest as ``skill``: same shape, same extractor —
                  artifact-type-extensions.md §b)
- ``claude_md`` — every ``CLAUDE.md`` (workspace root + nested project dirs)
- ``plan``      — ONE canonical entry plan per candidate project root, mirroring the
                  descriptor-contract finder: root-first, ``plan.md`` before
                  ``master_plan.md``, then the same names under ``plans/``/``docs/``/
                  ``documentation/``, then a ``*-plan.md``/``*_plan.md`` glob in
                  ``docs``/``documentation`` skipping ``*archive*``/``*brainstorm*``/
                  ``*template*``/``*draft*``
- ``memory``    — ``*.md`` under ``<memory_root>/<project-dir-slug>/memory/``, EXCLUDING
                  the ``MEMORY.md`` index (body memories only). Memory artifacts use the
                  ``memory:<project-dir-slug>/<file>.md`` path scheme; everything else is
                  workspace-relative with forward slashes (plan.md §3.1 amendment 3).

Exclusions: ``.venv/``, ``node_modules/``, ``.git/``, ``docs/archived*``, and any tree whose
registry entry has ``owned = false`` in ``<workspace_root>/.claude/observatory/registry.toml``
(stdlib tomllib; missing registry degrades to "everything coding-root", never crashes).

Pointer artifacts never silently report zero choices (plan.md §4.1): thin-wrapper SKILL.md
bodies ("Read `<path>` and follow it") resolve to the pointed-to file; pointer-only plans
record their linked targets; CLAUDE.md ``@path`` imports are classified. A pointer/import
target that is ITSELF a scanned artifact is recorded as a relationship and SKIPPED (no
double-extraction); an existing non-artifact target is marked for inlining; an unresolvable
target is recorded as ``pointer_unresolved`` and surfaced loudly in scan output.

Degradation contract: the scan COMPLETES on a flaky filesystem. Every directory
enumeration routes through ONE guarded seam — :func:`_scandir_entries` (and the
:func:`_safe_iter_files` / :func:`_safe_listdir` wrappers built on it). ``pathlib``'s
``glob``/``rglob`` and bare ``os.walk`` are BANNED in this module: their internals swallow
per-directory ``OSError`` (``except OSError: pass`` in CPython's wildcard selectors;
``is_dir()``-failure misfiling in ``os.walk``) where NO caller-side try/except can ever
observe it, so entries silently vanish. Through the seam, every failing entry (broken
junction, AV lock, network hiccup, mid-scan delete) is skipped with a loud ``SCAN ERROR``
note in :class:`ScanReport.notes`; one bad entry never zeroes discovery for its siblings
and never crashes the invocation.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection
from typing import cast, get_args

import yaml

from citation_needed import db
from citation_needed.models import (
    DETAILS_MODELS,
    ArtifactDetails,
    ClaudeMdDetails,
    ClaudeMdScope,
    CommandStyle,
    MemoryDetails,
    MemoryKind,
    MemoryScope,
    PlanDetails,
    PlanKind,
    RuleDetails,
    SkillDetails,
)

#: ``--workspace-root`` flag > this env var > default (parent of the citation-needed root).
WORKSPACE_ROOT_ENV = "CITATION_NEEDED_WORKSPACE_ROOT"

#: Pruned wherever they appear — vendored packages ship their own SKILL.md/CLAUDE.md files
#: that must never ingest as workspace choices (corpus-survey.md §1). Membership checks are
#: case-insensitive (Windows filesystems are; a ``.Venv`` is still a venv).
EXCLUDED_DIR_NAMES = frozenset({".venv", "node_modules", ".git"})

#: ``docs/archived*`` exclusion: dirs and files with this prefix inside a ``docs`` dir.
_ARCHIVED_PREFIX = "archived"

#: The per-directory memory index — explicitly excluded; body memories only.
MEMORY_INDEX_NAME = "MEMORY.md"

#: Runtime guard derived from the model's own Literal — one source of truth (models.py).
_MEMORY_KINDS = frozenset(get_args(MemoryKind))

_ENTRY_PLAN_NAMES = ("plan.md", "master_plan.md")
_PLAN_SUBDIRS = ("plans", "docs", "documentation")
_PLAN_SKIP_RE = re.compile(r"archive|brainstorm|template|draft", re.IGNORECASE)

_STEP_RE = re.compile(r"^###\s+Step\s+\w+\s*:", re.MULTILINE)
_PHASE_RE = re.compile(r"^##\s+Phase\b", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
#: CR-tolerant: real workspace files (and Windows ``write_text``) are frequently CRLF.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?(?:\n|\Z)", re.DOTALL)

#: Thin-wrapper pointer line: "Read `<path>.md` ... follow ..." (and close variants).
_POINTER_LINE_RE = re.compile(
    r"\bread\b\s+[`'\"]?(?P<target>[^\s`'\"()]+\.md)[`'\"]?[^\n]*?\bfollow\b",
    re.IGNORECASE,
)
#: A wrapper body is only a pointer when it is thin — a real skill body never is.
_MAX_POINTER_BODY_LINES = 12

#: A plan with no scrapable ``### Step N:``/``## Phase`` units is only POINTER-ONLY when it
#: actually points somewhere: it must contain markdown links AND read like an index — a true
#: thin stub (<= this many non-blank lines, mirroring ``_MAX_POINTER_BODY_LINES`` for skill
#: wrappers), or link-dense (>= 1 link per ``_POINTER_PLAN_LINES_PER_LINK`` non-blank
#: lines). A prose/architecture plan that simply doesn't use the Step-N convention — even a
#: SHORT one that cites a single incidental link — is real content, never a pointer stub
#: (misclassifying it as "nothing here, look elsewhere" is the exact silent-zero failure
#: pointer handling exists to prevent).
_MAX_POINTER_PLAN_CONTENT_LINES = 12
_POINTER_PLAN_LINES_PER_LINK = 8

#: Hard cap on how much of a candidate file :func:`_read` will pull into memory. Every
#: candidate is ``*.md``-filtered, so anything over this is a mis-extensioned blob (a real
#: 20MB "md" peaks ~4x its size in decode+normalize copies) — skipped loudly, never read.
_MAX_READ_BYTES = 8 * 1024 * 1024

#: A directory containing one of these is a project root — the outermost ancestor a
#: relative pointer token may resolve against (see :func:`_pointer_stop_dir`).
_PROJECT_ROOT_MARKERS = ("CLAUDE.md", ".git")

_CODE_FENCE_RE = re.compile(r"^(```|~~~)[^\n]*\n.*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
_CODE_SPAN_RE = re.compile(r"`[^`\n]+`")
#: CLAUDE.md ``@path`` memory-import token. The lookbehind keeps emails (``x@y.com``) out;
#: code fences/spans are stripped before matching so decorators never false-positive.
_IMPORT_RE = re.compile(r"(?:^|(?<=[\s(]))@(?P<target>[\w~.][\w\-./\\]*[\w/])", re.MULTILINE)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


@dataclass(frozen=True)
class RegistryEntry:
    """One ``[[project]]`` row from the observatory registry."""

    slug: str
    path: str  # workspace-relative, forward slashes, no trailing slash
    owned: bool


@dataclass
class DiscoveredArtifact:
    """One typed artifact ready for upsert (``path`` is the two-scheme DB path)."""

    path: str
    artifact_type: str
    project: str
    content_hash: str
    details: ArtifactDetails
    abs_path: Path


@dataclass
class ScanReport:
    """Everything ``cite scan`` needs to upsert + print."""

    workspace_root: Path
    artifacts: list[DiscoveredArtifact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    excluded_dir_count: int = 0
    not_owned_skipped: list[str] = field(default_factory=list)
    memory_index_skipped: int = 0


@dataclass(frozen=True)
class _Candidate:
    abs_path: Path
    rel_posix: str
    artifact_type: str
    command_style: CommandStyle | None = None


# ---------------------------------------------------------------------------
# Roots, registry, project resolution
# ---------------------------------------------------------------------------


def default_workspace_root() -> Path:
    """Env override, else the parent of the citation-needed project root."""
    env = os.environ.get(WORKSPACE_ROOT_ENV)
    if env:
        return Path(env)
    return db.PROJECT_ROOT.parent


def default_memory_root() -> Path:
    """Where Claude Code keeps per-project memory dirs (``~/.claude/projects``)."""
    return Path.home() / ".claude" / "projects"


def workspace_memory_slug(workspace_root: Path) -> str:
    """``C:/Users/x/dev`` -> ``C--Users-x-dev`` (each of ``:\\/`` becomes ``-``).

    Real memory dirs mix drive-letter case (``c--...`` and ``C--...`` both exist), so all
    comparisons against this slug must be case-insensitive.
    """
    return re.sub(r"[:\\/]", "-", str(workspace_root).rstrip("\\/"))


def load_registry(workspace_root: Path, registry_path: Path | None = None) -> list[RegistryEntry]:
    """Parse the observatory registry; a missing file returns an empty list (degradable).

    Raises ``tomllib.TOMLDecodeError`` on malformed TOML and ``OSError`` when the file
    vanishes/locks between the existence check and the read (TOCTOU) — the caller
    (:func:`scan_workspace`) catches both and degrades with a loud note.
    """
    path = registry_path or (workspace_root / ".claude" / "observatory" / "registry.toml")
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: list[RegistryEntry] = []
    raw = data.get("project", [])
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        rel = item.get("path")
        if not isinstance(slug, str) or not isinstance(rel, str) or not slug or not rel:
            continue
        entries.append(
            RegistryEntry(
                slug=slug,
                path=rel.replace("\\", "/").strip("/"),
                owned=bool(item.get("owned", False)),
            )
        )
    return entries


def _registry_match(rel_posix: str, registry: list[RegistryEntry]) -> RegistryEntry | None:
    """Longest registry path-prefix match for a workspace-relative path, or None.

    The comparison is case-insensitive: Windows filesystems are, and a registry entry whose
    casing drifts from the on-disk dir (``Toybox`` vs ``toybox/``) must still match.
    """
    rel_lower = rel_posix.lower()
    best: RegistryEntry | None = None
    best_len = -1
    for entry in registry:
        entry_lower = entry.path.lower()
        if (rel_lower == entry_lower or rel_lower.startswith(entry_lower + "/")) and len(
            entry.path
        ) > best_len:
            best, best_len = entry, len(entry.path)
    return best


def resolve_project(rel_posix: str, registry: list[RegistryEntry]) -> str:
    """Longest registry path-prefix match wins; unmatched in-workspace -> ``coding-root``."""
    entry = _registry_match(rel_posix, registry)
    return entry.slug if entry is not None else "coding-root"


# ---------------------------------------------------------------------------
# Parsing primitives
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict[str, object], str, str | None]:
    """Return ``(frontmatter, body, error)``; malformed YAML is tolerated, never a crash.

    Callers feed text decoded with ``utf-8-sig`` (see :func:`_read`), so no BOM handling
    is needed here.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text, None
    body = text[match.end() :]
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        message = str(exc).splitlines()[0] if str(exc) else ""
        return {}, body, f"{type(exc).__name__}: {message}"[:200]
    if loaded is None:
        return {}, body, None
    if not isinstance(loaded, dict):
        return {}, body, "frontmatter is not a YAML mapping"
    return {str(key): value for key, value in loaded.items()}, body, None


def find_entry_plan(project_root: Path, report: ScanReport | None = None) -> Path | None:
    """Mirror the descriptor-contract §4 canonical-entry-plan finder (ONE plan per root).

    Pass ``report`` (production callers do) so glob-layer enumeration failures surface as
    loud SCAN ERROR notes instead of vanishing (see :func:`_scandir_entries`); probe-level
    failures here raise to the caller's per-root guard.
    """
    for name in _ENTRY_PLAN_NAMES:
        candidate = project_root / name
        if candidate.is_file():
            return candidate
    for sub in _PLAN_SUBDIRS:
        for name in _ENTRY_PLAN_NAMES:
            candidate = project_root / sub / name
            if candidate.is_file():
                return candidate
    for sub in ("docs", "documentation"):
        directory = project_root / sub
        if not directory.is_dir():
            continue
        candidates = sorted(
            {
                path
                for pattern in ("*-plan.md", "*_plan.md")
                for path in _safe_iter_files(directory, pattern, report, "plan glob")
                if not _PLAN_SKIP_RE.search(path.name)
            }
        )
        if candidates:
            return candidates[0]
    return None


def detect_skill_pointer(body: str) -> str | None:
    """Return the raw pointer token of a thin-wrapper body, or None for a real body."""
    content_lines = [
        line for line in (raw.strip() for raw in body.splitlines()) if line and line[0] != "#"
    ]
    if not content_lines or len(content_lines) > _MAX_POINTER_BODY_LINES:
        return None
    match = _POINTER_LINE_RE.search(body)
    return match.group("target") if match else None


def extract_imports(text: str) -> list[str]:
    """CLAUDE.md ``@path`` import tokens, in order, code fences/spans stripped, deduped.

    A token must look like a FILE import — an explicit relative/home prefix (``~/``, ``./``,
    ``../``) or a file extension on its final segment — so npm scoped-package names
    documented in prose (``@adobe/leonardo-contrast-colors``) never register as broken
    imports.
    """
    stripped = _CODE_FENCE_RE.sub("", text)
    stripped = _CODE_SPAN_RE.sub("", stripped)
    tokens: list[str] = []
    for match in _IMPORT_RE.finditer(stripped):
        token = match.group("target")
        normalized = token.replace("\\", "/")
        final_segment = normalized.rsplit("/", 1)[-1]
        looks_like_file = (
            normalized.startswith(("~/", "./", "../")) or _EXT_RE.search(final_segment) is not None
        )
        if looks_like_file and token not in tokens:
            tokens.append(token)
    return tokens


def _read(path: Path) -> tuple[str, str] | None:
    """(content-hash, text) — None when unreadable; decode never crashes the scan.

    HASH POLICY: the hash is sha256 over the DECODED text with line endings normalized
    (CRLF/CR -> LF) and any UTF-8 BOM stripped — NOT over the raw bytes. Re-scan
    idempotency (:func:`upsert_artifact`'s ``unchanged`` bucket) must track *logical*
    content: a fresh checkout under a different ``core.autocrlf``, or an editor that
    rewrites line endings, would otherwise flip every file to ``updated`` with zero
    semantic change (the CRLF-flap class windows-shell.md documents).
    """
    try:
        if path.stat().st_size > _MAX_READ_BYTES:
            return None  # mis-extensioned blob; skipped via the callers' loud note
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8-sig", errors="replace")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), text


def _rel_posix(path: Path, workspace_root: Path) -> str | None:
    try:
        return path.resolve().relative_to(workspace_root).as_posix()
    except (OSError, ValueError):
        return None


def _safe_resolve(path: Path) -> Path:
    """``Path.resolve()`` that degrades to the unresolved path instead of raising."""
    try:
        return path.resolve()
    except OSError:
        return path


def _safe_is_dir(path: Path, report: ScanReport, context: str) -> bool:
    """``Path.is_dir()`` that records a SCAN ERROR note instead of raising."""
    try:
        return path.is_dir()
    except OSError as exc:
        report.notes.append(f"SCAN ERROR ({context}): {path.as_posix()}: {exc}")
        return False


def _safe_is_file(path: Path) -> bool:
    """``Path.is_file()`` that degrades to False instead of raising (probe-level guard)."""
    try:
        return path.is_file()
    except OSError:
        return False


def _note_scan_error(report: ScanReport | None, context: str, location: Path, exc: OSError) -> None:
    """Record one loud SCAN ERROR note (no-op only when no report is in play)."""
    if report is not None:
        report.notes.append(f"SCAN ERROR ({context}): {location.as_posix()}: {exc}")


def _scandir_entries(
    directory: Path, report: ScanReport | None, context: str
) -> list[os.DirEntry[str]]:
    """Raw guarded directory listing — the ONE seam every enumeration routes through.

    ``pathlib.Path.glob``/``rglob`` swallow per-directory ``OSError`` INTERNALLY
    (CPython's wildcard selectors do ``except OSError: pass``), so no caller-side
    try/except can ever observe a broken junction, AV lock, or permission failure —
    matching entries just silently vanish from the scan. ``os.walk`` has the sibling
    defect: an entry whose ``is_dir()`` probe fails is silently treated as a file and
    its whole subtree dropped, with the ``onerror`` callback never invoked. This module
    therefore NEVER calls ``glob``/``rglob``/``iterdir``/``os.walk`` directly: every
    directory listing goes through this function, which records a loud SCAN ERROR note
    on failure and returns whatever was read before the failure (sorted by name) — one
    bad directory never aborts the enumeration of its siblings.
    """
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(directory) as handle:
            for entry in handle:
                entries.append(entry)
    except OSError as exc:
        _note_scan_error(report, context, directory, exc)
    entries.sort(key=lambda entry: entry.name)
    return entries


def _safe_iter_files(
    base: Path,
    pattern: str,
    report: ScanReport | None,
    context: str,
    *,
    recursive: bool = False,
) -> Iterator[Path]:
    """Guarded ``glob``/``rglob`` replacement: regular files under ``base`` matching
    ``pattern`` (``fnmatch`` semantics — case-insensitive on Windows, like pathlib's).

    Built on :func:`_scandir_entries` (see there for why pathlib's own ``glob``/``rglob``
    can never provide this): a per-entry failure records a SCAN ERROR note and skips ONLY
    that entry; the enumeration always completes. ``recursive=True`` descends into
    subdirectories (symlinked dirs are not followed), mirroring ``rglob('**')``.
    """
    stack = [base]
    while stack:
        directory = stack.pop()
        subdirs: list[Path] = []
        for entry in _scandir_entries(directory, report, context):
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(Path(entry.path))
                elif entry.is_file() and fnmatch.fnmatch(entry.name, pattern):
                    yield Path(entry.path)
            except OSError as exc:
                _note_scan_error(report, context, Path(entry.path), exc)
        if recursive:
            stack.extend(reversed(subdirs))


def _safe_listdir(directory: Path, report: ScanReport, context: str) -> list[Path]:
    """List a directory through the guarded seam (partial list + loud note on failure)."""
    return [Path(entry.path) for entry in _scandir_entries(directory, report, context)]


def _pointer_stop_dir(
    rel_posix: str,
    base_dir: Path,
    workspace_root: Path,
    registry: list[RegistryEntry],
) -> Path:
    """The artifact's project root — the outermost base :func:`_resolve_target` may try.

    Precedence: the artifact's longest registry path-prefix match; else the nearest
    ancestor of the pointing file's directory holding a project marker
    (:data:`_PROJECT_ROOT_MARKERS`); else the workspace root. Bounding the ancestor walk
    here keeps a dead/typo'd pointer from silently matching an unrelated same-named file
    above the project. Every probe is guarded — a failing probe just walks on.
    """
    entry = _registry_match(rel_posix, registry)
    if entry is not None:
        return workspace_root / entry.path
    ws_key = str(_safe_resolve(workspace_root)).lower()
    probe = base_dir
    while True:
        for marker in _PROJECT_ROOT_MARKERS:
            try:
                if (probe / marker).exists():
                    return probe
            except OSError:
                continue
        if str(_safe_resolve(probe)).lower() == ws_key or probe == probe.parent:
            return workspace_root
        probe = probe.parent


def _resolve_target(
    token: str,
    base_dir: Path,
    workspace_root: Path,
    stop_dir: Path | None = None,
) -> tuple[Path, str | None]:
    """Resolve a pointer/import token; returns ``(path, resolution_base)``.

    ``resolution_base`` names the base the target was found against — ``"file"`` (the
    pointing file's own directory), ``"home"``/``"absolute"``/``"workspace"`` for the
    explicit-prefix forms, or the workspace-relative posix of the ANCESTOR directory that
    matched (``"."`` = the workspace root itself) — and is ``None`` when nothing resolved
    (the caller records the token as unresolved, loudly). Recording the base makes every
    ancestor resolution auditable in the artifact row.

    Resolution policy (a dead/typo'd pointer must never silently false-resolve to an
    unrelated same-named file in a distant ancestor):

    - ``~/`` / absolute / ``/``-leading tokens resolve directly (home, as-is, and the
      workspace root respectively).
    - A bare single-component filename (no ``/``) and any explicitly self-anchored
      ``./`` token resolve ONLY against the pointing file's own directory — never via
      the ancestor walk (a dead ``README.md`` pointer must not match a stranger's
      README at some ancestor).
    - Any other multi-component relative token (including ``../`` forms, which nested
      projects author relative to their own root) tries the file's own directory first,
      then each ancestor, STOPPING at the artifact's project root (``stop_dir`` — see
      :func:`_pointer_stop_dir`; the workspace root is always a hard outer bound), never
      beyond — and an ancestor match is accepted only when the resolved target still
      lies INSIDE the project root (a ``..``-bearing token evaluated at an ancestor
      cannot escape upward). Real-world pointer bodies are frequently written relative
      to a project or "workshop" root rather than the file's own directory (the
      shake_spear thin-wrapper shape: ``Read `skills/x.md``` from
      ``.claude/skills/<name>/SKILL.md``), which the bounded walk preserves.
    """
    normalized = token.replace("\\", "/")
    if normalized.startswith("~/"):
        resolved = _safe_resolve(Path.home() / normalized[2:])
        return resolved, "home" if _safe_is_file(resolved) else None
    if Path(normalized).is_absolute():
        resolved = _safe_resolve(Path(normalized))
        return resolved, "absolute" if _safe_is_file(resolved) else None
    if normalized.startswith("/"):
        resolved = _safe_resolve(workspace_root / normalized.lstrip("/"))
        return resolved, "workspace" if _safe_is_file(resolved) else None
    fallback = _safe_resolve(base_dir / normalized)
    if _safe_is_file(fallback):
        return fallback, "file"
    if "/" not in normalized or normalized.startswith("./"):
        # Bare or explicitly self-anchored: file-relative ONLY, no ancestor walk.
        return fallback, None
    stop_key = str(_safe_resolve(stop_dir if stop_dir is not None else workspace_root)).lower()
    ws_key = str(_safe_resolve(workspace_root)).lower()
    if str(_safe_resolve(base_dir)).lower() not in (stop_key, ws_key):
        for ancestor in base_dir.parents:
            resolved = _safe_resolve(ancestor / normalized)
            # Containment: an ancestor match must stay inside the project root — a
            # `..`-bearing token evaluated at an ancestor must never escape upward.
            if str(resolved).lower().startswith(stop_key + os.sep) and _safe_is_file(resolved):
                return resolved, _rel_posix(ancestor, workspace_root) or ancestor.as_posix()
            ancestor_key = str(_safe_resolve(ancestor)).lower()
            if ancestor_key in (stop_key, ws_key) or ancestor == ancestor.parent:
                break
    return fallback, None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Walk + candidate collection
# ---------------------------------------------------------------------------


def _classify_walk_file(current: Path, filename: str) -> tuple[str, CommandStyle | None] | None:
    if filename == "CLAUDE.md":
        return "claude_md", None
    if not filename.lower().endswith(".md"):
        return None
    parts = tuple(part.lower() for part in current.parts)
    if parts[-2:] == (".claude", "rules"):
        return "rule", None
    if parts[-2:] == (".claude", "commands"):
        return "skill", "commands_dir"
    if filename == "SKILL.md" and parts[-3:-1] == (".claude", "skills"):
        return "skill", "skill_dir"
    return None


def _collect_walk_candidates(
    workspace_root: Path,
    not_owned: dict[str, str],
    report: ScanReport,
) -> list[_Candidate]:
    # Explicit traversal over the guarded seam — NOT os.walk, whose internals silently
    # misfile an entry (and drop its whole subtree) when its is_dir() probe raises, with
    # the onerror callback never invoked (see _scandir_entries).
    candidates: list[_Candidate] = []
    seen_dirs = {str(workspace_root).lower()}
    stack = [workspace_root]
    while stack:
        current = stack.pop()
        in_docs = current.name.lower() == "docs"
        dir_entries: list[os.DirEntry[str]] = []
        file_names: list[str] = []
        for entry in _scandir_entries(current, report, "walk"):
            try:
                if entry.is_dir():
                    dir_entries.append(entry)
                else:
                    file_names.append(entry.name)
            except OSError as exc:
                # os.walk would silently misfile this entry; surface it and skip it.
                _note_scan_error(report, "walk", Path(entry.path), exc)
        descend: list[Path] = []
        for entry in dir_entries:
            name = entry.name
            if name.lower() in EXCLUDED_DIR_NAMES or (
                in_docs and name.lower().startswith(_ARCHIVED_PREFIX)
            ):
                report.excluded_dir_count += 1
                continue
            child = Path(entry.path)
            try:
                resolved = str(child.resolve()).lower()
                is_symlink = entry.is_symlink()
            except OSError as exc:
                # Broken junction / AV lock: skip THIS dir, keep walking its siblings.
                _note_scan_error(report, "walk", child, exc)
                continue
            slug = not_owned.get(resolved)
            if slug is not None:
                if slug not in report.not_owned_skipped:
                    report.not_owned_skipped.append(slug)
                continue
            if resolved in seen_dirs:
                continue  # junction/symlink alias of an already-walked dir (cycle guard)
            seen_dirs.add(resolved)
            if not is_symlink:  # match os.walk(followlinks=False): never descend a symlink
                descend.append(child)
        stack.extend(reversed(descend))
        for filename in file_names:
            if in_docs and filename.lower().startswith(_ARCHIVED_PREFIX):
                continue
            classified = _classify_walk_file(current, filename)
            if classified is None:
                continue
            artifact_type, style = classified
            abs_path = current / filename
            rel = _rel_posix(abs_path, workspace_root)
            if rel is not None:
                candidates.append(_Candidate(abs_path, rel, artifact_type, style))
            else:
                # Reached through a junction whose real target is outside the workspace.
                report.notes.append(f"outside-workspace target skipped: {abs_path.as_posix()}")
    return candidates


def _collect_plan_candidates(
    workspace_root: Path,
    registry: list[RegistryEntry],
    not_owned: dict[str, str],
    report: ScanReport,
) -> list[Path]:
    """Candidate project roots = workspace root + top-level dirs + owned registry paths.

    Every filesystem probe is guarded PER ENTRY: one bad child (broken junction, AV lock,
    network hiccup) records a SCAN ERROR note and is skipped — it never zeroes plan
    discovery for the siblings collected before or after it.
    """
    roots: list[Path] = [workspace_root]
    for child in sorted(_safe_listdir(workspace_root, report, "plan roots")):
        try:
            if not child.is_dir() or child.name.lower() in EXCLUDED_DIR_NAMES:
                continue
            if str(child.resolve()).lower() in not_owned:
                continue
        except OSError as exc:
            report.notes.append(f"SCAN ERROR (plan roots): {child.as_posix()}: {exc}")
            continue
        roots.append(child)
    for entry in registry:
        if not entry.owned:
            continue
        root = workspace_root / entry.path
        try:
            if root.is_dir():
                roots.append(root)
        except OSError as exc:
            report.notes.append(f"SCAN ERROR (plan roots): {root.as_posix()}: {exc}")
    plans: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            plan = find_entry_plan(root, report)
        except OSError as exc:
            report.notes.append(f"SCAN ERROR (plan discovery): {root.as_posix()}: {exc}")
            continue
        if plan is not None:
            plans.append(plan)
    return plans


# ---------------------------------------------------------------------------
# Per-type details builders (phase 2 — pointer targets classified against the
# full discovered-path set, so record-vs-inline is decidable). Each builder
# constructs its pydantic model DIRECTLY so mypy checks every field at the
# construction site; upsert_artifact keeps the final validation guard.
# ---------------------------------------------------------------------------


def _skill_details(
    cand: _Candidate,
    text: str,
    workspace_root: Path,
    stop_dir: Path,
    known_paths: set[str],
    report: ScanReport,
) -> SkillDetails:
    frontmatter, body, fm_error = parse_frontmatter(text)
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    invocable = frontmatter.get("user-invocable")
    has_evals: bool | None = None
    if cand.command_style == "skill_dir":
        # Guarded like every other probe: a flaky evals/ stat must not drop the artifact
        # from ingestion while its path stays in known_paths (phantom relationship).
        has_evals = _safe_is_dir(cand.abs_path.parent / "evals", report, "skill evals")
    token = detect_skill_pointer(body)
    pointer_target: str | None = None
    pointer_target_is_artifact: bool | None = None
    pointer_unresolved: bool | None = None
    resolution_base: str | None = None
    if token is not None:
        target, base_label = _resolve_target(token, cand.abs_path.parent, workspace_root, stop_dir)
        if base_label is not None:
            pointer_target = _rel_posix(target, workspace_root) or target.as_posix()
            pointer_target_is_artifact = pointer_target in known_paths
            resolution_base = base_label
            kind = (
                "artifact, recorded+skipped" if pointer_target_is_artifact else ("inline at review")
            )
            report.notes.append(
                f"skill pointer resolved ({kind}, base={base_label}): "
                f"{cand.rel_posix} -> {pointer_target}"
            )
        else:
            pointer_unresolved = True
            report.notes.append(f"UNRESOLVED pointer: {cand.rel_posix} -> {token}")
    if fm_error:
        report.notes.append(f"frontmatter error: {cand.rel_posix} ({fm_error})")
    return SkillDetails(
        name=name if isinstance(name, str) else None,
        description=description if isinstance(description, str) else None,
        user_invocable=invocable if isinstance(invocable, bool) else None,
        has_evals=has_evals,
        command_style=cand.command_style,
        is_pointer=token is not None,
        pointer_target=pointer_target,
        pointer_target_is_artifact=pointer_target_is_artifact,
        pointer_unresolved=pointer_unresolved,
        resolution_base=resolution_base,
        frontmatter_error=fm_error,
    )


def _claude_md_details(
    cand: _Candidate,
    text: str,
    project: str,
    workspace_root: Path,
    stop_dir: Path,
    known_paths: set[str],
    report: ScanReport,
) -> ClaudeMdDetails:
    artifact_imports: list[str] = []
    inline_targets: list[str] = []
    unresolved: list[str] = []
    for token in extract_imports(text):
        target, base_label = _resolve_target(token, cand.abs_path.parent, workspace_root, stop_dir)
        if base_label is not None:
            target_rel = _rel_posix(target, workspace_root) or target.as_posix()
            if target_rel in known_paths:
                artifact_imports.append(target_rel)
                report.notes.append(
                    f"claude_md import (artifact, recorded+skipped, base={base_label}): "
                    f"{cand.rel_posix} -> {target_rel}"
                )
            else:
                inline_targets.append(target_rel)
                report.notes.append(
                    f"claude_md import (inline at review, base={base_label}): "
                    f"{cand.rel_posix} -> {target_rel}"
                )
        else:
            unresolved.append(token)
            report.notes.append(f"UNRESOLVED import: {cand.rel_posix} -> {token}")
    scope: ClaudeMdScope = "root" if cand.rel_posix == "CLAUDE.md" else "project"
    return ClaudeMdDetails(
        scope=scope,
        project_slug=project,
        artifact_imports=_unique(artifact_imports) or None,
        inline_targets=_unique(inline_targets) or None,
        pointer_unresolved=_unique(unresolved) or None,
    )


def _plan_details(
    cand: _Candidate,
    text: str,
    workspace_root: Path,
    stop_dir: Path,
    report: ScanReport,
) -> PlanDetails:
    step_count = len(_STEP_RE.findall(text))
    phase_count = len(_PHASE_RE.findall(text))
    name = cand.abs_path.name.lower()
    plan_kind: PlanKind = (
        "root" if name == "plan.md" else "master" if name == "master_plan.md" else "feature"
    )
    link_tokens = _MD_LINK_RE.findall(text)
    content_line_count = sum(1 for line in text.splitlines() if line.strip())
    pointer_only = (
        step_count == 0
        and phase_count == 0
        and bool(link_tokens)
        and (
            content_line_count <= _MAX_POINTER_PLAN_CONTENT_LINES
            or len(link_tokens) * _POINTER_PLAN_LINES_PER_LINK >= content_line_count
        )
    )
    linked_targets: list[str] | None = None
    pointer_unresolved: list[str] | None = None
    if pointer_only:
        linked: list[str] = []
        unresolved: list[str] = []
        for token in link_tokens:
            target, base_label = _resolve_target(
                token, cand.abs_path.parent, workspace_root, stop_dir
            )
            if base_label is not None:
                linked.append(_rel_posix(target, workspace_root) or target.as_posix())
            else:
                unresolved.append(token)
        linked = _unique(linked)
        unresolved = _unique(unresolved)
        linked_targets = linked or None
        pointer_unresolved = unresolved or None
        report.notes.append(
            f"pointer-only plan: {cand.rel_posix} -> {len(linked)} linked target(s)"
        )
        for token in unresolved:
            report.notes.append(f"UNRESOLVED plan link: {cand.rel_posix} -> {token}")
    return PlanDetails(
        plan_kind=plan_kind,
        is_pointer_only=pointer_only,
        step_count=step_count,
        phase_count=phase_count,
        linked_targets=linked_targets,
        pointer_unresolved=pointer_unresolved,
    )


def _finalize_candidate(
    cand: _Candidate,
    read: tuple[str, str],
    workspace_root: Path,
    registry: list[RegistryEntry],
    known_paths: set[str],
    report: ScanReport,
) -> DiscoveredArtifact:
    content_hash, text = read
    project = resolve_project(cand.rel_posix, registry)
    details: ArtifactDetails
    if cand.artifact_type == "rule":
        # source_memory_paths is a review-time (skill-layer) field, not a discovery field.
        details = RuleDetails()
    else:
        stop_dir = _pointer_stop_dir(cand.rel_posix, cand.abs_path.parent, workspace_root, registry)
        if cand.artifact_type == "skill":
            details = _skill_details(cand, text, workspace_root, stop_dir, known_paths, report)
        elif cand.artifact_type == "claude_md":
            details = _claude_md_details(
                cand, text, project, workspace_root, stop_dir, known_paths, report
            )
        else:
            details = _plan_details(cand, text, workspace_root, stop_dir, report)
    return DiscoveredArtifact(
        path=cand.rel_posix,
        artifact_type=cand.artifact_type,
        project=project,
        content_hash=content_hash,
        details=details,
        abs_path=cand.abs_path,
    )


# ---------------------------------------------------------------------------
# Memory dirs (outside the workspace — `memory:` path scheme)
# ---------------------------------------------------------------------------


def _memory_project_and_scope(
    dir_slug: str,
    ws_slug_lower: str,
    by_norm_slug: dict[str, RegistryEntry],
) -> tuple[str, MemoryScope, bool]:
    """Map one memory project-dir slug to ``(project, memory_scope, skip_not_owned)``."""
    lower = dir_slug.lower()
    if lower == ws_slug_lower:
        return "coding-root", "global", False
    prefix = ws_slug_lower + "-"
    if lower.startswith(prefix):
        remainder = lower[len(prefix) :].replace("_", "-")
        entry = by_norm_slug.get(remainder)
        if entry is not None:
            return entry.slug, "project", not entry.owned
        return "coding-root", "project", False
    return "global", "project", False


def _memory_details(text: str, scope: MemoryScope, path: str, report: ScanReport) -> MemoryDetails:
    frontmatter, _body, fm_error = parse_frontmatter(text)
    metadata_raw = frontmatter.get("metadata")
    metadata: dict[str, object] = (
        {str(key): value for key, value in metadata_raw.items()}
        if isinstance(metadata_raw, dict)
        else {}
    )
    node_type = metadata.get("node_type")
    kind = metadata.get("type")
    origin = metadata.get("originSessionId")
    modified = metadata.get("modified")
    if fm_error:
        report.notes.append(f"frontmatter error: {path} ({fm_error})")
    memory_kind = (
        cast("MemoryKind", kind) if isinstance(kind, str) and kind in _MEMORY_KINDS else None
    )
    return MemoryDetails(
        node_type=node_type if isinstance(node_type, str) else None,
        memory_kind=memory_kind,
        origin_session_id=origin if isinstance(origin, str) else None,
        memory_scope=scope,
        frontmatter_modified=None if modified is None else str(modified),
        frontmatter_error=fm_error,
    )


def _scan_memory(
    memory_root: Path,
    workspace_root: Path,
    registry: list[RegistryEntry],
    report: ScanReport,
    artifacts: dict[str, DiscoveredArtifact],
) -> None:
    if not _safe_is_dir(memory_root, report, "memory root"):
        report.notes.append(
            f"memory root not found: {memory_root.as_posix()} — 0 memory artifacts scanned"
        )
        return
    ws_slug_lower = workspace_memory_slug(workspace_root).lower()
    by_norm_slug = {entry.slug.lower().replace("_", "-"): entry for entry in registry}
    project_dirs = sorted(
        entry
        for entry in _safe_listdir(memory_root, report, "memory root")
        if _safe_is_dir(entry, report, "memory scan")
    )
    for project_dir in project_dirs:
        try:
            _scan_memory_project_dir(project_dir, ws_slug_lower, by_norm_slug, report, artifacts)
        except OSError as exc:
            # One flaky memory dir never aborts the scan of its siblings.
            report.notes.append(f"SCAN ERROR (memory): {project_dir.as_posix()}: {exc}")


def _scan_memory_project_dir(
    project_dir: Path,
    ws_slug_lower: str,
    by_norm_slug: dict[str, RegistryEntry],
    report: ScanReport,
    artifacts: dict[str, DiscoveredArtifact],
) -> None:
    memory_dir = project_dir / "memory"
    if not memory_dir.is_dir():
        return
    dir_slug = project_dir.name
    project, scope, skip_not_owned = _memory_project_and_scope(
        dir_slug, ws_slug_lower, by_norm_slug
    )
    if skip_not_owned:
        if project not in report.not_owned_skipped:
            report.not_owned_skipped.append(project)
        return
    for file in sorted(
        _safe_iter_files(memory_dir, "*.md", report, "memory files", recursive=True)
    ):
        if file.name.lower() == MEMORY_INDEX_NAME.lower():
            report.memory_index_skipped += 1
            continue
        read = _read(file)
        if read is None:
            report.notes.append(f"unreadable/oversized file skipped: {file.as_posix()}")
            continue
        content_hash, text = read
        path = f"memory:{dir_slug}/{file.relative_to(memory_dir).as_posix()}"
        details = _memory_details(text, scope, path, report)
        if path not in artifacts:
            artifacts[path] = DiscoveredArtifact(
                path=path,
                artifact_type="memory",
                project=project,
                content_hash=content_hash,
                details=details,
                abs_path=file,
            )


# ---------------------------------------------------------------------------
# The scan orchestrator + DB upsert
# ---------------------------------------------------------------------------


def scan_workspace(
    workspace_root: Path,
    memory_root: Path | None = None,
    registry_path: Path | None = None,
) -> ScanReport:
    """Discover + type every artifact under ``workspace_root`` (plus memory dirs)."""
    workspace_root = workspace_root.resolve()
    report = ScanReport(workspace_root=workspace_root)

    registry_file = registry_path or workspace_root / ".claude" / "observatory" / "registry.toml"
    try:
        registry = load_registry(workspace_root, registry_path)
    except tomllib.TOMLDecodeError as exc:
        registry = []
        report.notes.append(
            f"registry parse error: {exc} — every in-workspace artifact resolves to coding-root"
        )
    except OSError as exc:
        registry = []
        report.notes.append(
            f"registry unreadable: {exc} — every in-workspace artifact resolves to coding-root"
        )
    else:
        if not registry and not registry_file.is_file():
            report.notes.append(
                f"registry not found: {registry_file.as_posix()} — no owned=false "
                "exclusions; every in-workspace artifact resolves to coding-root"
            )

    not_owned = {
        str((workspace_root / entry.path).resolve()).lower(): entry.slug
        for entry in registry
        if not entry.owned
    }

    candidates = _collect_walk_candidates(workspace_root, not_owned, report)
    existing = {candidate.rel_posix for candidate in candidates}
    for plan_path in _collect_plan_candidates(workspace_root, registry, not_owned, report):
        rel = _rel_posix(plan_path, workspace_root)
        if rel is None:
            report.notes.append(f"outside-workspace target skipped: {plan_path.as_posix()}")
            continue
        if rel in existing:
            continue
        existing.add(rel)
        candidates.append(_Candidate(plan_path, rel, "plan"))

    # Pre-read every candidate so known_paths only contains files that actually ingest —
    # a pointer target whose own read fails must not classify as "recorded as artifact".
    readable: dict[str, tuple[str, str]] = {}
    for candidate in sorted(candidates, key=lambda c: c.rel_posix):
        if candidate.rel_posix in readable:
            continue
        read = _read(candidate.abs_path)
        if read is None:
            report.notes.append(f"unreadable/oversized file skipped: {candidate.rel_posix}")
        else:
            readable[candidate.rel_posix] = read

    known_paths = set(readable)
    artifacts: dict[str, DiscoveredArtifact] = {}
    for candidate in sorted(candidates, key=lambda c: c.rel_posix):
        read = readable.get(candidate.rel_posix)
        if read is None or candidate.rel_posix in artifacts:
            continue
        try:
            artifact = _finalize_candidate(
                candidate, read, workspace_root, registry, known_paths, report
            )
        except OSError as exc:
            # A flaky probe during details-building skips THIS artifact, loudly.
            report.notes.append(f"SCAN ERROR (finalize): {candidate.rel_posix}: {exc}")
            continue
        artifacts[artifact.path] = artifact

    _scan_memory(memory_root or default_memory_root(), workspace_root, registry, report, artifacts)

    report.artifacts = sorted(artifacts.values(), key=lambda a: a.path)
    return report


def upsert_artifact(conn: Connection, artifact: DiscoveredArtifact) -> str:
    """Validated upsert into ``artifacts``; returns ``new`` | ``updated`` | ``unchanged``.

    ``details_json`` is validated through the type's pydantic model (DETAILS_MODELS — the
    single source of truth) as the FINAL guard before any write (builders construct the
    models directly; this re-validation catches a details/type mismatch). Re-scan of an
    unchanged file (same normalized-content hash — see :func:`_read` — same details, same
    type/project) leaves the row untouched.
    """
    model_cls = DETAILS_MODELS[artifact.artifact_type]
    details_json = model_cls.model_validate(artifact.details).model_dump_json(exclude_none=True)
    row = conn.execute(
        "SELECT id, artifact_type, project, current_content_hash, details_json, is_active "
        "FROM artifacts WHERE path = ?",
        (artifact.path,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO artifacts (path, artifact_type, project, is_active, "
            "current_content_hash, first_seen_at, details_json) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (
                artifact.path,
                artifact.artifact_type,
                artifact.project,
                artifact.content_hash,
                _utc_now(),
                details_json,
            ),
        )
        return "new"
    if (
        row[1] == artifact.artifact_type
        and row[2] == artifact.project
        and row[3] == artifact.content_hash
        and row[4] == details_json
        and row[5] == 1
    ):
        return "unchanged"
    conn.execute(
        "UPDATE artifacts SET artifact_type = ?, project = ?, is_active = 1, "
        "current_content_hash = ?, details_json = ? WHERE id = ?",
        (
            artifact.artifact_type,
            artifact.project,
            artifact.content_hash,
            details_json,
            row[0],
        ),
    )
    return "updated"
